"""关键画面截图：主体内容区变化检测 + 评分 + 中间产物保留。

改进点（相对旧逻辑）：
1. 帧差比较排除字幕带 → 字幕变化不干扰主体变化检测。
2. 候选放宽：主体区视觉变化 > 阈值即候选，不管有无字幕。
3. 评分不再"无文字直接跳过"：无文字但视觉/讲解重要也给分。
4. 中间产物（候选帧/关键图）保留在 test/screenshots，不删除。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
from PIL import Image

from server.config import settings
from server.notes import writer
from server.ocr import vision

# 主体内容区（排除字幕带）：y 在 [0, y0) 的区域
_BODY_Y0 = 0.0  # 主体从顶部开始


def _body_region_diff(img: Image.Image, prev: np.ndarray) -> float | None:
    """计算两帧主体内容区（排除字幕带）的像素差比例。"""
    if prev is None:
        return None
    arr = np.asarray(img, dtype=np.int16)
    # 只取主体区（顶部到字幕带上沿）
    body_h = int(img.height * (settings.ocr_subtitle_y0 - _BODY_Y0))
    a = arr[:body_h]
    b = prev[:body_h]
    if a.shape != b.shape or a.size == 0:
        return 0.0
    return float(np.mean(np.abs(a - b)) / 255.0)


def extract_candidates(video: Path, out_dir: Path, fps: float = 0.25) -> list[dict]:
    """帧采样，检测主体区变化，返回候选帧 [{time, filename}]。保留全部候选。"""
    import subprocess
    import tempfile

    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True)

    with tempfile.TemporaryDirectory() as td:
        frames = Path(td) / "frames"
        frames.mkdir()
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(video), "-vf", f"fps={fps}", str(frames / "f_%05d.jpg")],
            check=True, capture_output=True,
        )
        paths = sorted(frames.glob("*.jpg"))
        cands = []
        prev_thumb = None
        for idx, fp in enumerate(paths):
            t = idx * int(1 / fps)
            img = Image.open(fp).convert("RGB")
            thumb = img.resize((160, 90))
            diff = _body_region_diff(thumb, prev_thumb)
            prev_thumb = np.asarray(thumb, dtype=np.int16)
            if diff is None or diff > 0.05:
                # 保留候选帧到 out_dir
                dest = out_dir / f"cand_{idx:05d}_t{t}s.jpg"
                shutil.copy(fp, dest)
                cands.append({"time": t, "filename": dest})
        return cands


def score_candidates(sumz, cands: list[dict], transcript: str = "") -> list[dict]:
    """对候选评分（Score = w1*Svisual + w2*Socr + w3*Stranscript - w4*Sduplicate），返回选中的。"""
    kept, selected = [], []
    for c in cands:
        ocr_text = vision.ocr_slide_text(c["filename"])
        s_ocr = sumz.score_ocr_importance(ocr_text) if ocr_text.strip() else 0.0
        s_visual = 0.6  # 已通过主体区变化检测
        s_transcript = sumz.score_transcript_importance(transcript) if transcript.strip() else 0.0
        s_dup = max((sumz.text_similarity(ocr_text, t) for t in selected), default=0.0)
        score = (
            settings.score_w1 * s_visual
            + settings.score_w2 * s_ocr
            + settings.score_w3 * s_transcript
            - settings.score_w4 * s_dup
        )
        c["score"] = score
        if score >= settings.score_threshold:
            name = f"slide_{Path(c['filename']).stem}.jpg"
            data = Path(c["filename"]).read_bytes()
            path = writer.save_attachment(data, name)
            kept.append({
                "time": fmt_time(c["time"]),
                "filename": path.name,
                "ocr_text": ocr_text,
                "score": score,
            })
            selected.append(ocr_text)
    return kept


def fmt_time(sec: float) -> str:
    return f"{int(sec) // 60:02d}:{int(sec) % 60:02d}"
