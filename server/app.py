"""FastAPI 应用入口：OCR / ASR / 截图 / 帧差检测 / 总结 / 笔记 端点。"""
from __future__ import annotations

import asyncio
import subprocess
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from server.config import settings
from server.notes import writer
from server.ocr import vision as vision_ocr
from server.stt.qwen_audio import QwenAudioSTT
from server.summarizer.deepseek import DeepSeekSummarizer

app = FastAPI(title="Video Learning Assistant")


class SummarizeRequest(BaseModel):
    title: str
    source_url: str
    subtitle_text: str = ""
    speech_text: str = ""
    mode: str = "note"        # note | speedread
    priority: str = "ocr"     # ocr | stt | both
    slides: list[dict] = []   # [{filename, time, ocr_text}]


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/ocr/full")
async def ocr_full(file: UploadFile = File(...)):
    """整图 OCR：识别图片中的全部文字。"""
    data = await file.read()
    tmp = Path("/tmp/vla_ocr_full.png")
    tmp.write_bytes(data)
    try:
        return {"results": vision_ocr.ocr_image(tmp)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/ocr/subtitle")
async def ocr_subtitle(file: UploadFile = File(...)):
    """字幕带 OCR：裁剪图片底部字幕区域并识别。"""
    data = await file.read()
    tmp = Path("/tmp/vla_ocr_subtitle.png")
    tmp.write_bytes(data)
    try:
        text = vision_ocr.ocr_subtitle_band(
            tmp, settings.ocr_subtitle_y0, settings.ocr_subtitle_y1
        )
        return {"text": text}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e)) from e


# 帧差检测：记住上一帧缩略图 + 已保存的关键画面文字
_prev_frame = None
_selected_slides_texts: list[str] = []
_summarizer_cache = None


def _get_summarizer() -> DeepSeekSummarizer:
    global _summarizer_cache
    if _summarizer_cache is None:
        _summarizer_cache = DeepSeekSummarizer(
            settings.llm_api_key, settings.llm_model, settings.llm_base_url
        )
    return _summarizer_cache


@app.post("/api/frame")
async def frame(file: UploadFile = File(...), transcript: str = Form("")):
    """帧检测：字幕OCR + 关键画面评分截图。

    Score = w1*Svisual + w2*Socr + w3*Stranscript - w4*Sduplicate
    transcript: 当前视频时间附近的转写（SW 传入），用于讲解重要度信号。
    """
    global _prev_frame
    data = await file.read()
    try:
        import io

        from PIL import Image

        img = Image.open(io.BytesIO(data)).convert("RGB")
        thumb = img.resize((160, 90))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"图片解析失败: {e}") from e

    # 计算与上一帧的差异（平均像素差）
    diff_ratio = None
    if _prev_frame is not None:
        import numpy as np

        a = np.asarray(thumb, dtype=np.int16)
        b = np.asarray(_prev_frame, dtype=np.int16)
        diff_ratio = float(np.mean(np.abs(a - b)) / 255.0)
    _prev_frame = thumb

    # ① 字幕带 OCR
    tmp = Path("/tmp/vla_frame.png")
    tmp.write_bytes(data)
    subtitle = vision_ocr.ocr_subtitle_band(
        tmp, settings.ocr_subtitle_y0, settings.ocr_subtitle_y1
    )
    tmp.unlink(missing_ok=True)

    # ② 关键截图：按评分公式 Score = w1*Svisual + w2*Socr + w3*Stranscript - w4*Sduplicate
    #    四信号：视觉变化 / OCR重要度 / 讲解重要度 / 与已选画面重复度
    result = {"subtitle": subtitle, "diff": diff_ratio}
    sumz = _get_summarizer()

    # 视觉变化信号 Svisual：首帧1.0，否则用帧差比例归一化
    s_visual = 1.0 if diff_ratio is None else min(1.0, diff_ratio * 5)

    # 文字信号：OCR 全文（按配置引擎）
    tmp2 = Path("/tmp/vla_slide.png")
    tmp2.write_bytes(data)
    try:
        full_text = vision_ocr.ocr_slide_text(tmp2)
    finally:
        tmp2.unlink(missing_ok=True)

    # 讲解信号 Stranscript：当前视频时间附近的转写（SW 通过 form 传入 transcript）
    transcript_s = transcript if isinstance(transcript, str) else ""

    # 早退：画面基本无变化 且 无文字 且 无讲解 → 不是重点
    if (s_visual < 0.3 and not full_text.strip() and not transcript_s.strip()):
        result["score"] = 0.0
        result["capture"] = False
        result["reason"] = "画面无变化、无文字、无讲解"
        return result

    s_ocr = sumz.score_ocr_importance(full_text) if full_text.strip() else 0.0
    # transcript 可能是 Form 对象（测试直接调函数时）——归一化为字符串
    transcript_s = transcript if isinstance(transcript, str) else ""
    s_transcript = sumz.score_transcript_importance(transcript_s) if transcript_s.strip() else 0.0
    # 与已选画面的重复度 Sduplicate
    s_dup = 0.0
    if _selected_slides_texts:
        s_dup = max(
            sumz.text_similarity(full_text, t) for t in _selected_slides_texts
        ) if full_text.strip() else 0.0

    score = (
        settings.score_w1 * s_visual
        + settings.score_w2 * s_ocr
        + settings.score_w3 * s_transcript
        - settings.score_w4 * s_dup
    )
    result["score"] = round(score, 3)
    result["signals"] = {"visual": round(s_visual, 3), "ocr": round(s_ocr, 3),
                         "transcript": round(s_transcript, 3), "duplicate": round(s_dup, 3)}

    if score >= settings.score_threshold:
        path = writer.save_attachment(data, f"slide_{uuid.uuid4().hex[:8]}.png")
        _selected_slides_texts.append(full_text)
        if len(_selected_slides_texts) > 20:
            _selected_slides_texts.pop(0)
        result.update({
            "capture": True,
            "path": str(path),
            "name": path.name,
            "ocr_text": full_text,
            "reason": f"评分 {score:.2f} ≥ 阈值 {settings.score_threshold}",
        })
    else:
        result["capture"] = False
        result["reason"] = f"评分 {score:.2f} < 阈值 {settings.score_threshold}"
    return result


@app.post("/api/screenshot")
async def screenshot(file: UploadFile = File(...)):
    """保存截图附件，返回 Obsidian 相对路径。"""
    data = await file.read()
    path = writer.save_attachment(data, file.filename or "screenshot.png")
    return {"path": str(path), "name": path.name}


@app.post("/api/transcribe")
async def transcribe(file: UploadFile = File(...), start_offset: float = Form(0)):
    """上传音频片段，转成 16kHz WAV 后调用千问 ASR 转写为文本。

    start_offset: 该片段相对视频开头的起始秒数（用于带时间戳）。
    """
    data = await file.read()
    suffix = Path(file.filename or "audio.webm").suffix.lstrip(".") or "webm"
    tmp = Path(f"/tmp/vla_asr_{uuid.uuid4().hex}.{suffix}")
    tmp.write_bytes(data)
    wav = Path(f"/tmp/vla_asr_{uuid.uuid4().hex}.wav")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(tmp), "-ar", "16000", "-ac", "1", str(wav)],
            check=True,
            capture_output=True,
        )
        stt = QwenAudioSTT(
            api_key=settings.asr_api_key,
            model=settings.asr_model,
            api_url=settings.asr_api_url,
        )
        text = await stt.transcribe(wav)
        return {"text": text, "start_offset": start_offset}
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=400, detail=f"音频转换失败: {e.stderr.decode()[:300]}") from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e)) from e
    finally:
        tmp.unlink(missing_ok=True)
        wav.unlink(missing_ok=True)


@app.post("/api/notes/generate")
async def generate_note(req: SummarizeRequest):
    """用 DeepSeek 生成笔记/速览并写入 Obsidian。"""
    summarizer = DeepSeekSummarizer(
        settings.llm_api_key, settings.llm_model, settings.llm_base_url
    )
    if req.mode == "speedread":
        body = summarizer.speedread(
            req.title, req.source_url,
            subtitle_text=req.subtitle_text,
            speech_text=req.speech_text,
            slides=req.slides,
            priority=req.priority,
        )
        path = writer.save_speedread(req.title, req.source_url, body, req.slides)
    else:
        body = summarizer.summarize(
            req.title, req.source_url,
            req.subtitle_text, req.speech_text,
            priority=req.priority,
        )
        path = writer.save_note(req.title, req.source_url, body, req.slides)
    return {"path": str(path)}


if __name__ == "__main__":
    uvicorn.run("server.app:app", host="127.0.0.1", port=8787, reload=False)
