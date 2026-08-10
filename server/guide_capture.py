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

import hashlib
import json
import os

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

# ---- OCR 磁盘缓存 -----------------------------------------------------------
# PaddleOCR 单帧 ~3-5s，候选帧几十上百张，每次重跑全量 OCR 很慢。
# 以图片内容哈希为键，落盘缓存，规则迭代时秒级复用。
_OCR_CACHE_PATH = os.environ.get(
    "KEYFRAME_OCR_CACHE", "/tmp/keyframe_ocr_cache.json")
_ocr_cache: dict[str, str] = {}
if os.path.exists(_OCR_CACHE_PATH):
    try:
        with open(_OCR_CACHE_PATH, encoding="utf-8") as _f:
            _ocr_cache = json.load(_f)
    except Exception:  # noqa: BLE001
        _ocr_cache = {}


def _flush_ocr_cache() -> None:
    try:
        with open(_OCR_CACHE_PATH, "w", encoding="utf-8") as _f:
            json.dump(_ocr_cache, _f, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass


def ocr_slide_text_cached(fp) -> str:
    """OCR 帧内文字，带内容哈希磁盘缓存（避免重复跑慢速 OCR）。

    与 _ocr_regions_cached 共享同一份推理：文本 = 区域结果的 text 拼接
    （vision.ocr_slide_text 本来就是这么实现的）。未命中文本键时，先查区域键，
    命中则派生，避免同一帧被 OCR 两次。
    """
    fp = str(fp)
    h = hashlib.md5(Path(fp).read_bytes()).hexdigest()
    hit = _ocr_cache.get(h)
    if hit is not None:
        return hit
    regions = _ocr_regions_cached(fp)          # 复用区域缓存（含一次推理）
    text = " ".join(r["text"] for r in regions).strip()
    _ocr_cache[h] = text
    return text


def _ocr_regions_cached(fp) -> list[dict]:
    """OCR 帧内带位置的文字区域，同样走磁盘缓存（键前缀 R:）。

    返回 [{text, x, y, w, h}]（归一化，y 自顶向下），供标题区/正文区分析用。
    """
    fp = str(fp)
    h = hashlib.md5(Path(fp).read_bytes()).hexdigest()
    hit = _ocr_cache.get("R:" + h)
    if hit is not None:
        return hit
    regions = vision.ocr_image(fp)
    _ocr_cache["R:" + h] = regions
    _flush_ocr_cache()
    return regions


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
# 同帧判定的子标题相似阈值：同一张幻灯片（仅字幕变）子标题必然一致。
# 实测：41 vs 56（同一张 "Likely results" 错误图）子标题 sim=0.44（OCR 噪声），
# 而不同幻灯片 196 vs 203=0.17、12 vs 41=0.31。0.4 可合并前者、隔离后者。
SAME_FRAME_TITLE_SIM = 0.4
# 关键画面最小 OCR 内容量（低于此值视为无信息画面，直接过滤）
MIN_KEYFRAME_OCR = 50
# 画面复杂度下限（边缘方差，低于=纯色/无内容）
MIN_EDGE_VAR = 100
# 正文区内容量下限：mid_len 低于此视为"过渡/动画帧"（无实质正文）
# 实测 03 集 t18~33（层级图动画过渡）mid_len=13；正常内容帧 ≥24
MIN_MID_LEN = 15


def _frame_ocr_len(fp) -> int:
    """本地 OCR 提取文字量（PaddleOCR，快，无 API）。"""
    try:
        return len(ocr_slide_text_cached(fp).strip())
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


def _text_quality(ocr_text: str) -> float:
    """OCR 文本质量（0-1）：惩罚乱码/无意义符号。

    乱码特征：符号占比高、字母数字随机拼接（如 'Wi6I110A Sptedrn tevipeei'）。
    正常文本：中文或英文单词占主导。
    """
    if not ocr_text:
        return 0.0
    # 有效字符（汉字+字母）
    cn = sum(1 for c in ocr_text if '一' <= c <= '鿿')
    letters = sum(1 for c in ocr_text if c.isalpha())
    symbols = sum(1 for c in ocr_text if not c.isalnum() and not c.isspace())
    total = max(len(ocr_text), 1)
    # 符号占比高 = 乱码
    if symbols / total > 0.2:
        return 0.1
    # 有效字母+汉字占比
    return min(1.0, (cn + letters) / total)


def _local_score(c: dict) -> float:
    """本地确定性评分：OCR 内容量 + 画面复杂度 + 文本质量（不依赖 AI 判断）。

    用于去重保留策略和候选排序，可解释、稳定。
    """
    ocr_len = c.get("ocr_len")
    ocr_text = c.get("ocr_text", "")
    if ocr_len is None:
        ocr_text = _ocr_text(c["filename"])
        c["ocr_text"] = ocr_text
        ocr_len = len(ocr_text.strip())
        c["ocr_len"] = ocr_len
    edge, color = c.get("complexity", (None, None))
    if edge is None:
        edge, color = _frame_complexity(c["filename"])
        c["complexity"] = (edge, color)
    quality = _text_quality(ocr_text)
    # 内容量 + 复杂度 + 文本质量（惩罚乱码）
    return ocr_len * 0.5 * quality + edge * 0.0005 + color * 0.1


def _ocr_text(fp) -> str:
    """本地 OCR 提取文字（PaddleOCR，快，无 API）。"""
    try:
        return ocr_slide_text_cached(fp).strip()
    except Exception:  # noqa: BLE001
        return ""


def _text_sim(a: str, b: str) -> float:
    """文本相似度（字符 bigram Jaccard），用于判断是否同一主题。"""
    import re as _re

    def clean(s):
        return _re.sub(r"[^\w一-鿿]+", "", s)[:200]

    ca, cb = clean(a), clean(b)
    if not ca or not cb:
        return 0.0

    def bigrams(s):
        return {s[i:i+2] for i in range(len(s) - 1)}

    ga, gb = bigrams(ca), bigrams(cb)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


# 标题区：画面顶部（不含水印角标），同一张幻灯片此处像素/文字一致
TITLE_BAND_Y = 0.28
# 中间正文区：实际内容所在
MID_BAND_Y0, MID_BAND_Y1 = 0.30, 0.85


def _frame_bands(fp) -> dict:
    """提取帧的标题区文字 / 正文区文字与区域数（带区域缓存）。

    返回 {"title": str, "sub_title": str, "title_regions": int, "mid": str,
          "mid_regions": int, "mid_len": int, "regions": int}。
    """
    regions = _ocr_regions_cached(fp)
    title = [r for r in regions if r["y"] < TITLE_BAND_Y]
    # 子标题带：排除顶部水印角标行（JETBRAINS/DeepLearning.AI 的 y≈0.01，OCR 易把
    # "AI" 识别成 "41" 等噪声），用 [0.05,0.30) 作为"真正的幻灯片标题"
    sub_title = [r for r in regions if 0.05 <= r["y"] < TITLE_BAND_Y]
    mid = [r for r in regions if MID_BAND_Y0 <= r["y"] <= MID_BAND_Y1]
    return {
        "title": "".join(r["text"] for r in title),
        "sub_title": "".join(r["text"] for r in sub_title),
        "title_regions": len(title),
        "mid": "".join(r["text"] for r in mid),
        "mid_regions": len(mid),
        "mid_len": len("".join(r["text"] for r in mid)),
        "regions": len(regions),
    }


# 课程片头/片尾 credit 帧特征词：这些词几乎只出现在课程封面/结尾的水印横幅上
# （"XX课程研究院 + 讲师名 + 斯坦福"），正常教学内容不会出现。
CREDIT_KEYWORDS = ("研究院", "吴思达", "吴恩达", "斯坦福")
# credit 帧画面复杂度上限：实测 02 集封面/片尾帧 edge_var 174~290，正常内容帧 ≥343
CREDIT_MAX_EDGE = 320
# 视频标题封面/章节标题页特征：**正文区(mid)** 含品牌 Logo（JETBRAINS/DeepLearning.AI）。
# 正常内容帧的正文区从不含 Logo（Logo 只在顶部角标），标题封面页会把课程名/合作方
# Logo 放在画面中央。实测 03 集 t0/t1（视频标题页）、02 集 t278~280（片尾credit）命中。
LOGO_MID_WORDS = ("JETBRAINS", "DeepLearning", "Deeplearning", "deeplearning")


def _is_credit_frame(fp) -> bool:
    """识别课程封面/片尾credit帧、视频标题封面页：只有课程名/Logo，无实际教学正文。

    两种情况：
    - 含课程水印专属词（研究院/讲师/斯坦福）且画面复杂度低（封面/片尾页）
    - **正文区含品牌 Logo**（标题封面页把 Logo 放画面中央，正常内容页不会）
    实测 02 集 t278~280、03 集 t0/t1 命中；正常内容帧不受影响。
    """
    text = _ocr_text(fp)
    if not text:
        return False
    # 情况1：课程水印 + 低复杂度
    if any(k in text for k in CREDIT_KEYWORDS):
        edge, _ = _frame_complexity(fp)
        if edge < CREDIT_MAX_EDGE:
            return True
    # 情况2：正文区含品牌 Logo（标题封面页特征）
    mid = _frame_bands(fp)["mid"]
    if any(k in mid for k in LOGO_MID_WORDS):
        return True
    return False


def _is_cartoon_frame(fp) -> bool:
    """识别卡通/插画帧：正文区几乎没有文字（mid_len 极低），画面复杂度却达标。

    实测 02 集 t269~271（讲师卡通插画，"Your pair programmer"）mid_len≈1-4。
    正常内容帧（t12）mid_len 也有 25，但正文区有实际内容；卡通帧正文区为空。
    """
    bands = _frame_bands(fp)
    return bands["mid_len"] <= 5


def _is_same_frame(a: dict, b: dict) -> bool:
    """判断两帧是否为同一张幻灯片（主体几乎相同，仅字幕/细微变化）。

    要求**同时**满足：
    - 主体区（排除字幕带）像素 diff < DEDUP_DIFF
    - 标题区文字相似（同一张幻灯片的标题必然一致）

    仅 diff 不够——实测 t196(编译器) vs t203(SDD) 主体 diff 仅 0.017
    （版式相近、都有 spec.md/code.cpp 流程），但子标题一个是 "Compiler" 一个是
    "Spec-Driven Development"，是不同的幻灯片，必须保留两张。

    标题用 **子标题带 [0.05,0.30)**（排除顶部水印角标行，避免 "DeepLearning.AI"
    的 OCR 噪声如 41→"41"）。同一张幻灯片（如 41 vs 56）子标题一致。
    """
    try:
        img_a = Image.open(a["filename"]).convert("RGB").resize((160, 90))
        img_b = Image.open(b["filename"]).convert("RGB").resize((160, 90))
        diff = _body_region_diff(img_b, np.asarray(img_a, dtype=np.int16))
    except Exception:  # noqa: BLE001
        return False
    if diff is None or diff >= DEDUP_DIFF:
        return False
    ta = _frame_bands(a["filename"])["sub_title"]
    tb = _frame_bands(b["filename"])["sub_title"]
    if not ta or not tb:
        return True  # 无标题可比较 → 以像素为准
    return _text_sim(ta, tb) > SAME_FRAME_TITLE_SIM


def _is_same_topic(a: dict, b: dict) -> bool:
    """判断两帧是否为同一知识主题（讲解同一张图/概念，如总结图与分图）。

    要求**同时**满足：
    - 子标题区相似（>0.4）：同一主题的幻灯片标题必然一致
    - 正文区相似（>0.4）：正文内容同主题

    双条件本身保守：02 集编译器(196) vs SDD(203) 的 sub/mid 相似仅 0.12~0.17，
    0.4 阈值下仍分开 ✓；分图 vs 总结图 0.78~0.97 仍合并 ✓。降到 0.4 可让
    网页/代码浏览型视频（画面变化频繁但主题延续）合并更充分，减少冗余帧。

    只用正文相似会误合并**共享概念词**的不同幻灯片——03 集 t112(Feature process)
    与 t122(Project evolution) 正文都含 "Feature phase"（mid_sim=0.65~0.69），
    但子标题不同（0.19~0.22），用户明确两者都重要，必须分开。
    """
    fa, fb = _frame_bands(a["filename"]), _frame_bands(b["filename"])
    if not fa["sub_title"] or not fb["sub_title"] or not fa["mid"] or not fb["mid"]:
        return False
    return (_text_sim(fa["sub_title"], fb["sub_title"]) > 0.4
            and _text_sim(fa["mid"], fb["mid"]) > 0.4)


def _is_junk_frame(c: dict) -> bool:
    """无学习价值的画面：课程封面/片尾 credit 帧、卡通插画帧。

    这些帧 OCR/复杂度 可能达标（所以本地过滤挡不住），但内容为空壳。
    返回 True 表示应排除。
    """
    if _is_credit_frame(c["filename"]):
        c["reject"] = "本地规则:课程封面/片尾credit帧"
        return True
    if _is_cartoon_frame(c["filename"]):
        c["reject"] = "本地规则:卡通/无正文帧"
        return True
    return False


def dedup_candidates(cands: list[dict]) -> list[dict]:
    """候选帧去重（滑动窗口，高效）：同一张幻灯片被讲解多帧 → 只保留一张。

    策略：每帧只与**之前保留的最近 N 帧**比较（N=窗口），而非全局 O(n²)。
    - 同一张幻灯片（主体 diff 小 **且** 标题区一致）→ 合并，保留"内容更丰富"帧
    - 同一知识主题（正文区相似，如总结图 vs 分图）→ 合并，保留"内容更丰富"帧
      （总结图通常正文区文字更多，见 02 集 t157 三模块总结图）

    依据：_body_region_diff 对重复帧 ≈0.0；标题区一致区分"同一张"；
    正文区相似区分"同一主题"。内容更丰富的帧 = 正文区文字更多（信息全，如总结图）。
    """
    if not cands:
        return cands
    WINDOW = 8  # 滑动窗口：只和最近 8 帧比较
    cands = sorted(cands, key=lambda c: c["time"])
    kept = []
    for c in cands:
        # 本地过滤：无信息画面直接排除
        if _frame_ocr_len(c["filename"]) < MIN_KEYFRAME_OCR:
            c["reject"] = "本地规则:内容量不足"
            continue
        edge, _ = _frame_complexity(c["filename"])
        if edge < MIN_EDGE_VAR:
            c["reject"] = "本地规则:纯色/无内容"
            continue
        # 课程封面/片尾 credit 帧、卡通插画帧 → 排除
        if _is_junk_frame(c):
            continue
        # 正文区过于稀疏（mid_len < MIN_MID_LEN）→ 过渡/动画帧，无实质内容
        # 实测 03 集 t18~33（"Feature level" 层级图动画过渡帧）mid_len=13，
        # 用户标注这类"作用不大"；02 集正常内容帧 mid_len ≥ 24。
        if _frame_bands(c["filename"])["mid_len"] < MIN_MID_LEN:
            c["reject"] = "本地规则:正文区内容稀疏"
            continue
        cur_ocr = _ocr_text(c["filename"])
        # 只和窗口内最近的帧比较
        dup_of = None
        dup_kind = None
        for prev in kept[-WINDOW:]:
            if _is_same_frame(c, prev):
                dup_of, dup_kind = prev, "同一张"
                break
            if _is_same_topic(c, prev):
                dup_of, dup_kind = prev, "同一主题"
                break
        if dup_of is None:
            c["ocr_text"] = cur_ocr
            kept.append(c)
        else:
            # 同一画面/主题：保留"正文内容更丰富"的帧（信息更全，如总结图）
            c_mid = _frame_bands(c["filename"])["mid_len"]
            p_mid = _frame_bands(dup_of["filename"])["mid_len"]
            if c_mid > p_mid:
                kept[kept.index(dup_of)] = c
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
        ocr_text = ocr_slide_text_cached(c["filename"])
        entry["ocr_text"] = ocr_text[:100]
        # 内容量过滤：空白/水印排除
        if len(ocr_text.strip()) < MIN_OCR_CHARS:
            c["reject"] = "内容过少"
            entry["decision"] = "排除:内容过少"
            score_log.append(entry)
            continue
        # 与文字稿重复度：OCR 内容与讲解文字高度重合 → 纯口播无图，不是关键图。
        # 但幻灯片标题/要点经常与讲解措辞重合（如总结图的 "Control code with small
        # changes to spec" 正是口播句），直接用字符 Jaccard 会误杀真实幻灯片
        # （实测 t157 总结图 sim=0.53、t254 Agent 图 sim=0.57 被误排）。
        # 因此仅在"画面本身几乎无结构化内容"（正文区文字极少）时才用它：
        # 无正文的纯口播/过渡画面 + 与讲解高度重复 → 排除；有正文的幻灯片 → 保留。
        if _frame_bands(c["filename"])["mid_len"] <= 10:
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
