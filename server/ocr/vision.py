"""可插拔 OCR：优先 PaddleOCR（中文强），Apple Vision 作为兜底。

选择逻辑：
1. PaddleOCR 已安装 → 用它（中文识别最好）
2. 否则 Apple Vision（pyobjc，英文可用，中文受限）
"""
from __future__ import annotations

from pathlib import Path


class BaseOCR:
    """OCR 抽象。返回 [{text, x, y, w, h}]，坐标归一化 0-1，y 自下而上。"""

    def ocr_image(self, path: Path) -> list[dict]:
        raise NotImplementedError


class VisionOCR(BaseOCR):
    """Apple Vision OCR（pyobjc）。中文受限，作兜底。返回像素坐标（y 自上而下）。"""

    def ocr_image(self, path: Path) -> list[dict]:
        import Vision
        import Quartz

        url = Quartz.CFURLCreateFromFileSystemRepresentation(
            None, bytes(str(path), "utf-8"), len(str(path).encode("utf-8")), False
        )
        src = Quartz.CGImageSourceCreateWithURL(url, None)
        if src is None:
            return []
        cgimage = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
        w = Quartz.CGImageGetWidth(cgimage)
        h = Quartz.CGImageGetHeight(cgimage)
        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cgimage, None)
        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        # 中文支持：显式声明识别语言（默认不含 zh-Hans → 中文识别为乱码）。
        # zh-Hans 简体中文 + zh-Hant 繁体 + en-US 英文，兼容中英混排幻灯片。
        langs = ["zh-Hans", "zh-Hant", "en-US"]
        if hasattr(req, "setRecognitionLanguages_"):
            try:
                req.setRecognitionLanguages_(langs)
            except Exception:  # noqa: BLE001
                pass
        handler.performRequests_error_([req], None)
        out = []
        for obs in req.results() or []:
            box = obs.boundingBox()  # 归一化，y 自下而上
            cand = obs.topCandidates_(1)
            if cand and len(cand) > 0:
                out.append({
                    "text": cand[0].string(),
                    "x": box.origin.x * w, "y": (1 - box.origin.y - box.size.height) * h,
                    "w": box.size.width * w, "h": box.size.height * h,
                })
        return out


class PaddleOCRAdapter(BaseOCR):
    """PaddleOCR（PP-OCRv4/v5），简体中文最强。"""

    def __init__(self) -> None:
        from paddleocr import PaddleOCR

        self.engine = PaddleOCR(
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            text_detection_model_name="PP-OCRv5_mobile_det",
            text_recognition_model_name="PP-OCRv5_mobile_rec",
        )

    def ocr_image(self, path: Path) -> list[dict]:
        # PaddleOCR 3.x predict() 返回 OCRResult；.json 是 dict，含 res.rec_texts
        # 和 res.rec_boxes（[x0,y0,x1,y1] 像素坐标）。
        result = self.engine.predict(str(path))
        out = []
        for page in result:
            res = page.json.get("res", {})
            texts = res.get("rec_texts") or []
            boxes = res.get("rec_boxes") or []
            scores = res.get("rec_scores") or []
            if len(boxes) != len(texts):
                continue
            for i, text in enumerate(texts):
                text = (text or "").strip()
                if not text:
                    continue
                box = boxes[i]
                x0, y0, x1, y1 = box
                out.append({
                    "text": text,
                    "x": float(x0), "y": float(y0),
                    "w": float(x1 - x0), "h": float(y1 - y0),
                    "score": float(scores[i]) if i < len(scores) else None,
                })
        return out


_engine: BaseOCR | None = None


def get_engine() -> BaseOCR:
    global _engine
    if _engine is not None:
        return _engine
    from server.config import settings

    engine = (settings.ocr_engine or "apple").lower()
    if engine == "paddle":
        _engine = PaddleOCRAdapter()
    elif engine == "qwen-vl":
        from server.ocr.qwen_vl import QwenVlOCR

        _engine = QwenVlOCR(settings.asr_api_key)  # 实现 BaseOCR.ocr_image
    else:  # apple / 默认
        _engine = VisionOCR()
    return _engine


def ocr_slide_text(path: Path) -> str:
    """幻灯片整图文字提取。按配置选择引擎：paddle | qwen-vl。

    用于：判断画面是否有重要文字 / 提取幻灯片全文（喂给 DeepSeek 判重要性）。
    """
    from server.config import settings

    engine = (settings.ocr_engine or "paddle").lower()
    if engine == "qwen-vl":
        try:
            from server.ocr.qwen_vl import QwenVlOCR

            ocr = QwenVlOCR(settings.asr_api_key)
            return ocr.ocr_text(Path(path))
        except Exception:  # noqa: BLE001
            pass
    # 默认 / 回退：PaddleOCR
    try:
        return " ".join(r["text"] for r in ocr_image(Path(path)))
    except Exception:  # noqa: BLE001
        return ""


def _normalize(raw: list[dict], w: int, h: int) -> list[dict]:
    """像素坐标归一化到 0-1，y 自顶向下。"""
    out = []
    for r in raw:
        out.append({
            **r,
            "x": r["x"] / w,
            "y": r["y"] / h,
            "w": r["w"] / w,
            "h": r["h"] / h,
        })
    return out


def ocr_image(path: Path) -> list[dict]:
    """整图 OCR，返回 [{text, x, y, w, h}]（归一化，y 自顶向下）。"""
    from PIL import Image

    path = Path(path)
    with Image.open(path) as im:
        w, h = im.size
    return _normalize(get_engine().ocr_image(path), w, h)


def ocr_subtitle_band(path: Path, y0: float, y1: float) -> str:
    """提取视频下部字幕带 [y0,y1]（画面高度比例，自顶向下）的文字。

    先裁剪字幕带区域，再只对该小图 OCR——比整图检测快约 4 倍。
    只保留行中心落在字幕带内的文本，按从左到右拼接。
    """
    from PIL import Image

    path = Path(path)
    with Image.open(path) as im:
        w, h = im.size
        # 裁剪字幕带 [y0, y1]
        crop = im.crop((0, int(h * y0), w, int(h * y1)))
        crop_path = path.with_name(f"{path.stem}_band.png")
        crop.save(crop_path)
    try:
        raw = get_engine().ocr_image(crop_path)
    finally:
        crop_path.unlink(missing_ok=True)

    # 裁剪后都在字幕带内，只需按从左到右拼接
    raw.sort(key=lambda r: r["x"])
    return " ".join(r["text"] for r in raw)
