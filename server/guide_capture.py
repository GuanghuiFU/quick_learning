"""文字指引版关键帧检测。

核心思路（基于用户设计）：
1. 用带时间戳的文字稿做"讲解重要性"预筛：DeepSeek 评估每句是否关键。
2. 只在"关键讲解时段"内采样帧，用帧差稳定性判断画面是否长时间停留。
3. 内容量过滤：OCR 文字过少（空白/水印）→ 排除。
4. 重复度过滤：OCR 与文字稿高度重复（口播无图）→ 排除；与已选图重复 → 排除。
5. 最终才选择性调用 VLM 精判（控制成本，不是所有帧都调）。

评分公式扩展：
Score = w1*Svisual + w2*Socr + w3*Stranscript - w4*Sdup_shot - w5*Sdup_transcript
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image

from server.config import settings
from server.notes import writer
from server.ocr import vision

# 稳定停留判定：连续 N 帧变化 < 阈值 视为画面稳定
STABLE_FRAMES = 3
STABLE_DIFF = 0.05
# 内容量下限：OCR 有效字符数（低于此视为空白/水印）
MIN_OCR_CHARS = 8
# 视觉判断引擎缓存
_vision = None


def _get_vision():
    """Qwen3.7-Flash 视觉判断（直接看图，比文本判断准）。"""
    global _vision
    if _vision is None:
        from server.ocr.qwen_vision import QwenVision

        _vision = QwenVision(settings.asr_api_key)
    return _vision


def _body_region_diff(img: Image.Image, prev: np.ndarray) -> float | None:
    """主体区（排除字幕带）像素差比例。"""
    if prev is None:
        return None
    arr = np.asarray(img, dtype=np.int16)
    body_h = int(img.height * (settings.ocr_subtitle_y0))
    a, b = arr[:body_h], prev[:body_h]
    if a.shape != b.shape or a.size == 0:
        return 0.0
    return float(np.mean(np.abs(a - b)) / 255.0)


def score_transcript_lines(sumz, segments: list[dict]) -> list[dict]:
    """为每句文字稿评分讲解重要性（Stranscript），返回带重要度的分句。"""
    out = []
    for seg in segments:
        s = sumz.score_transcript_importance(seg["text"]) if seg["text"].strip() else 0.0
        out.append({**seg, "importance": s})
    return out


def sample_candidates(video: Path, segments: list[dict], out_dir: Path, fps: float = 1.0) -> list[dict]:
    """按文字稿时间戳采样，找出"画面稳定停留"的候选帧。

    思路：对每个讲解分句时刻，采样其前后窗口的帧，若连续多帧变化小 → 画面稳定，
    该帧可作为关键候选。候选帧落盘到 out_dir。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True)

    cands = []
    for i, seg in enumerate(segments):
        t = seg["time_sec"]
        # 采样该时刻附近 3 帧（t-1, t, t+1）判断稳定性
        window = [t - 1, t, t + 1]
        frame_paths = []
        for wt in window:
            if wt < 0:
                continue
            fp = out_dir / f"seg{i:04d}_t{wt}s.jpg"
            subprocess.run(
                ["ffmpeg", "-y", "-ss", str(wt), "-i", str(video), "-frames:v", "1", str(fp)],
                capture_output=True,
            )
            if fp.exists():
                frame_paths.append(fp)
        if len(frame_paths) < 2:
            continue
        # 计算相邻帧差
        prev_thumb = None
        stable_count = 0
        diffs = []
        for fp in frame_paths:
            img = Image.open(fp).convert("RGB")
            thumb = img.resize((160, 90))
            diff = _body_region_diff(thumb, prev_thumb)
            prev_thumb = np.asarray(thumb, dtype=np.int16)
            if diff is not None:
                diffs.append(diff)
        # 画面稳定 = 相邻帧差都小
        if diffs and all(d < STABLE_DIFF for d in diffs):
            cands.append({
                "time": t,
                "filename": frame_paths[1] if len(frame_paths) > 1 else frame_paths[0],
                "importance": seg.get("importance", 0.0),  # 关联讲解重要度
            })
        else:
            # 不稳定则删掉临时帧
            for fp in frame_paths:
                fp.unlink(missing_ok=True)
    return cands


# 去重阈值：画面主体差异低于此值视为同一张幻灯片（只有字幕变化）
DEDUP_DIFF = 0.05
# 关键画面最小 OCR 内容量（低于此值视为无信息画面，直接过滤）
MIN_KEYFRAME_OCR = 50
# 画面复杂度下限（边缘方差，低于=纯色/无内容）
MIN_EDGE_VAR = 100


def _frame_ocr_len(fp) -> int:
    """本地 OCR 提取文字量（PaddleOCR，快，无 API）。"""
    try:
        return len(vision.ocr_slide_text(fp).strip())
    except Exception:  # noqa: BLE001
        return 0


def _frame_complexity(fp) -> tuple[float, float]:
    """本地计算画面复杂度：(边缘方差, 颜色方差)。纯色/背景 → 低。"""
    try:
        gray = np.asarray(Image.open(fp).convert("L"), dtype=np.float32)
        grad = np.gradient(gray)
        edge_var = float(np.var(grad[0]) + np.var(grad[1]))
        rgb = np.asarray(Image.open(fp).convert("RGB"), dtype=np.float32)
        color_var = float(rgb.std(axis=(0, 1)).mean())
        return edge_var, color_var
    except Exception:  # noqa: BLE001
        return 0.0, 0.0


def _local_score(c: dict) -> float:
    """本地确定性评分：OCR 内容量 + 画面复杂度（不依赖 AI 判断）。

    用于去重保留策略和候选排序，可解释、稳定。
    """
    ocr_len = c.get("ocr_len")
    if ocr_len is None:
        ocr_len = _frame_ocr_len(c["filename"])
        c["ocr_len"] = ocr_len
    edge, color = c.get("complexity", (None, None))
    if edge is None:
        edge, color = _frame_complexity(c["filename"])
        c["complexity"] = (edge, color)
    # 内容量 + 复杂度：信息越丰富分越高
    return ocr_len * 0.5 + edge * 0.0005 + color * 0.1


def dedup_candidates(cands: list[dict]) -> list[dict]:
    """候选帧去重：同一张幻灯片被讲解多帧（画面主体相同，仅字幕变化）→ 只保留一张。

    保留策略：**本地评分**（OCR 内容量 + 画面复杂度）最高的帧，而非 DeepSeek importance。
    依据：_body_region_diff（排除字幕带）对重复帧返回 ≈0.0，对不同画面返回 0.3+。
    """
    if not cands:
        return cands
    # 按时间排序
    cands = sorted(cands, key=lambda c: c["time"])
    kept = []
    for c in cands:
        # 本地过滤：无信息画面（纯色/无文字）直接排除
        if _frame_ocr_len(c["filename"]) < MIN_KEYFRAME_OCR:
            c["reject"] = "本地规则:内容量不足"
            continue
        edge, _ = _frame_complexity(c["filename"])
        if edge < MIN_EDGE_VAR:
            c["reject"] = "本地规则:纯色/无内容"
            continue
        if not kept:
            kept.append(c)
            continue
        # 与最近保留的候选帧比较画面主体差异
        prev = kept[-1]
        try:
            img_cur = Image.open(c["filename"]).convert("RGB").resize((160, 90))
            img_prev = Image.open(prev["filename"]).convert("RGB").resize((160, 90))
            diff = _body_region_diff(img_cur, np.asarray(img_prev, dtype=np.int16))
        except Exception:  # noqa: BLE001
            diff = None
        if diff is None or diff >= DEDUP_DIFF:
            # 画面主体不同 → 是新幻灯片，保留
            kept.append(c)
        else:
            # 同一张幻灯片：保留本地评分更高的（内容更丰富的）
            if _local_score(c) > _local_score(prev):
                kept[-1] = c
    return kept


def guide_capture(sumz, video: Path, segments: list[dict], title: str, course: str | None = None) -> list[dict]:
    """完整文字指引关键帧检测，返回选中关键图 [{time, filename, ocr_text, score}]。

    course: 课程名，关键图附件写入 学习笔记/<course>/attachments/（默认用 title）。
    """
    out_root = Path("assets/screenshots") / (course or title) / title / "guide"
    cand_dir = out_root / "candidates"
    sel_dir = out_root / "selected"
    for d in (cand_dir, sel_dir):
        d.mkdir(parents=True, exist_ok=True)

    # 1) 讲解重要性预筛
    segs_scored = score_transcript_lines(sumz, segments)
    # 关键讲解时段：importance >= 0.5
    key_segs = [s for s in segs_scored if s["importance"] >= 0.5]
    print(f"讲解重要分句 {len(key_segs)}/{len(segments)} (importance>=0.5)")

    # 2) 只在关键时段采样稳定候选帧
    cands = sample_candidates(video, key_segs, cand_dir)
    print(f"稳定候选帧 {len(cands)} 张")

    # 2b) 候选帧去重：同一张幻灯片被讲解多帧（只有字幕变）→ 只保留一张
    cands = dedup_candidates(cands)
    print(f"去重后候选帧 {len(cands)} 张")

    # 3) 评分：内容量 + 重复度过滤 + VLM 精判
    kept, selected_texts = [], []
    score_log = []  # 评分明细（用于溯源）
    for c in cands:
        entry = {
            "time": f"{c['time']//60:02d}:{c['time']%60:02d}",
            "filename": Path(c["filename"]).name,
            "ocr_text": "",
            "s_ocr": "", "s_transcript": "", "s_dup": "", "score": "",
            "visual_verdict": "", "decision": "候选",
        }
        ocr_text = vision.ocr_slide_text(c["filename"])
        entry["ocr_text"] = ocr_text[:100]
        # 内容量过滤：空白/水印排除
        if len(ocr_text.strip()) < MIN_OCR_CHARS:
            c["reject"] = "内容过少"
            entry["decision"] = "排除:内容过少"
            score_log.append(entry)
            continue
        # 与文字稿重复度：OCR 内容与讲解文字高度重合 → 纯口播无图，不是关键图
        # 找到该候选对应的分句文字稿
        cur_transcript = next(
            (s["text"] for s in segments if abs(s["time_sec"] - c["time"]) <= 2), ""
        )
        s_dup_transcript = sumz.text_similarity(ocr_text, cur_transcript) if cur_transcript else 0.0
        if s_dup_transcript > 0.5:
            c["reject"] = f"与文字稿重复 {s_dup_transcript:.2f}"
            entry["decision"] = "排除:与文字稿重复"
            score_log.append(entry)
            continue
        s_ocr = sumz.score_ocr_importance(ocr_text)
        entry["s_ocr"] = round(s_ocr, 2)
        s_dup = max((sumz.text_similarity(ocr_text, t) for t in selected_texts), default=0.0)
        entry["s_dup"] = round(s_dup, 2)
        s_visual = 0.8  # 稳定停留画面给高分
        entry["s_visual"] = s_visual
        s_transcript = c.get("importance", 0.5)  # 该帧对应分句的讲解重要度
        entry["s_transcript"] = round(s_transcript, 2)
        score = (
            settings.score_w1 * s_visual
            + settings.score_w2 * s_ocr
            + settings.score_w3 * s_transcript
            - settings.score_w4 * s_dup
        )
        c["score"] = round(score, 2)
        entry["score"] = c["score"]
        # 视觉检查：仅用于检测"画面异常"（花屏/全黑/严重遮挡），不判断内容是否重要
        # 重要与否由本地规则（OCR内容量+复杂度+评分）决定——避免视觉API误拒有效画面
        try:
            worthy = _get_vision().assess_anomaly(c["filename"])
        except Exception as e:  # noqa: BLE001
            worthy = {"anomaly": False, "reason": f"视觉检查失败: {str(e)[:40]}"}
        entry["visual_verdict"] = worthy["reason"][:60]
        if worthy.get("anomaly"):
            c["reject"] = f"画面异常: {worthy['reason']}"
            entry["decision"] = "排除:画面异常"
            score_log.append(entry)
            continue
        # 本地规则决定是否保留：评分达标即可（不依赖视觉API判断重要性）
        if score >= settings.score_threshold:
            # 文件名加视频标识前缀，避免不同视频同时间戳覆盖
            import re as _re

            safe_title = _re.sub(r'[^0-9a-zA-Z一-鿿]+', '_', title)[:30]
            name = f"{safe_title}_guide_{c['time']//60:02d}_{c['time']%60:02d}.jpg"
            data = c["filename"].read_bytes()
            path = writer.save_attachment(data, name, course=course or title)
            shutil.copy(c["filename"], sel_dir / name)
            kept.append({"time": f"{c['time']//60:02d}:{c['time']%60:02d}",
                         "filename": path.name, "ocr_text": ocr_text, "score": round(score, 2)})
            selected_texts.append(ocr_text)
            entry["decision"] = f"选中"
            score_log.append(entry)
            print(f"  [关键图] {kept[-1]['time']} {name} score={score:.2f} ocr={ocr_text[:30]}")

    # 写评分明细 CSV（便于溯源）
    import csv as _csv

    try:
        with open(out_root / "scores.csv", "w", newline="", encoding="utf-8") as f:
            fields = ["time", "filename", "ocr_text", "s_visual", "s_ocr", "s_transcript",
                      "s_dup", "score", "visual_verdict", "decision"]
            w = _csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(score_log)
    except Exception as e:  # noqa: BLE001
        print(f"  [评分明细写入失败] {e}")
    return kept
