"""关键帧去重/过滤规则的单元测试（用真实临时小图，避免依赖大视频/API）。

针对 guide_capture.dedup_candidates 及其判别函数（_is_same_frame / _is_same_topic）
做确定性验证，回归保护：同一张幻灯片合并且保留内容更丰富帧、不同标题分开、
同主题合并到总结图。

用法：
    .venv/bin/python -m pytest server/tests/test_keyframe_dedup.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import server.guide_capture as gc  # noqa: E402


@pytest.fixture
def setup(monkeypatch, tmp_path):
    """创建真实临时小图，patch 掉昂贵像素 diff（读图本身保留）。"""
    monkeypatch.setattr(gc, "_frame_ocr_len", lambda fp: 200)      # 内容量充足
    monkeypatch.setattr(gc, "_frame_complexity", lambda fp: (800.0, 40.0))  # 复杂度达标
    monkeypatch.setattr(gc, "_body_region_diff", lambda img, prev: 0.001)   # 主体几乎相同
    monkeypatch.setattr(gc, "_is_junk_frame", lambda c: False)
    return tmp_path


def _mk(tmp_path, time: int, title: str, mid: str) -> dict:
    """构造候选帧：写一张 1x1 临时图，并注入标题/正文元数据。"""
    fp = tmp_path / f"{time:04d}.jpg"
    Image.new("RGB", (1, 1), "white").save(fp)
    # 用 attribute 记录标题/正文（_frame_bands 在测试里改用这些）
    d = {"time": time, "filename": str(fp), "_title": title, "_mid": mid}
    gc._bands_meta = getattr(gc, "_bands_meta", {})
    gc._bands_meta[str(fp)] = {"sub_title": title, "mid": mid, "mid_len": len(mid)}
    return d


@pytest.fixture
def fake_bands(monkeypatch):
    """替换 _frame_bands：从 _bands_meta 读，不跑真实 OCR。"""

    def _fake(fp: str) -> dict:
        meta = getattr(gc, "_bands_meta", {})
        return meta.get(str(fp), {"sub_title": "", "mid": "", "mid_len": 0, "regions": 0})

    monkeypatch.setattr(gc, "_frame_bands", _fake)


def test_dedup_merges_same_slide_keeps_richer(setup, fake_bands) -> None:
    """同一张幻灯片（同标题）多帧 → 只留内容更丰富的一帧。"""
    cands = [
        _mk(setup, 10, "vibe coding", "create button"),
        _mk(setup, 11, "vibe coding", "create button"),                     # 与10重复
        _mk(setup, 12, "vibe coding", "create button more detail"),         # 更丰富
    ]
    kept = gc.dedup_candidates(cands)
    assert [c["time"] for c in kept] == [12]  # 保留内容量最高的帧


def test_dedup_keeps_distinct_titles(setup, fake_bands) -> None:
    """不同标题（不同幻灯片，如 编译器 vs SDD 流程）→ 各自保留。"""
    cands = [
        _mk(setup, 100, "Compiler", "machine code binary"),
        _mk(setup, 101, "Spec-Driven Development", "spec.md code.cpp"),
    ]
    kept = gc.dedup_candidates(cands)
    assert len(kept) == 2  # 两张都要


def test_dedup_same_topic_merges_summary(setup, fake_bands) -> None:
    """同一知识主题（分图 vs 总结图）→ 合并，保留正文更丰富（总结图）。"""
    cands = [
        _mk(setup, 130, "Benefits of spec", "control code eliminate context decay"),
        _mk(setup, 160, "Benefits of spec",
            "control code eliminate context decay improve intent fidelity"),
    ]
    kept = gc.dedup_candidates(cands)
    assert [c["time"] for c in kept] == [160]  # 总结图（内容更全）胜出


def test_dedup_keeps_distinct_adjacent_slides(setup, fake_bands) -> None:
    """相邻但不同的幻灯片（版式相近、正文不同）→ 保留两张。"""
    cands = [
        _mk(setup, 196, "Compiler", "machine code binary 010101"),
        _mk(setup, 197, "Compiler", "machine code binary 010101"),          # 同帧重复
        _mk(setup, 203, "Spec-Driven Development", "spec.md code.cpp"),     # 不同幻灯片
    ]
    kept = gc.dedup_candidates(cands)
    times = sorted(c["time"] for c in kept)
    assert 196 in times and 203 in times  # 编译器 + SDD 都要
    assert len(times) == 2                # 197 与 196 合并


def test_same_topic_requires_same_subtitle(setup, fake_bands) -> None:
    """同主题判定必须同时满足子标题相似——仅正文共享概念词不算同主题。

    03 集 t112(Feature process) vs t122(Project evolution) 正文都含 "Feature phase"
    （mid 相似），但子标题不同，用户明确两者都重要，必须分开。
    """
    cands = [
        _mk(setup, 112, "Feature process", "feature phase 1 feature phase 2 specification"),
        _mk(setup, 113, "Feature process", "feature phase 1 feature phase 2 specification"),
        _mk(setup, 122, "Project evolution", "feature phase 1 feature phase 2 replacement"),
    ]
    kept = gc.dedup_candidates(cands)
    times = sorted(c["time"] for c in kept)
    assert 112 in times and 122 in times  # Feature process 和 Project evolution 都保留
    assert len(times) == 2                # 113 与 112 合并


def test_title_cover_frame_rejected(setup, fake_bands, monkeypatch) -> None:
    """视频标题封面页（正文区含品牌 Logo）→ 排除。

    03 集 t0（"Spec-Driven Development ... Workflow overview" + JETBRAINS/DeepLearning
    Logo 在画面中央）是章节标题页，不是学习内容。
    """
    from PIL import Image

    fp = setup / "0000.jpg"
    Image.new("RGB", (2, 2), "white").save(fp)
    c = {"time": 0, "filename": str(fp)}
    meta = getattr(gc, "_bands_meta", {})
    meta[str(fp)] = {
        "sub_title": "Spec-Driven Development",
        "mid": "with Coding Agents Workflow overview JETBRAINS DeepLearning.AI",
        "mid_len": 52,
    }
    gc._bands_meta = meta
    monkeypatch.setattr(gc, "_ocr_text", lambda fp: meta[str(fp)]["mid"])  # 模拟全文 OCR
    # 此帧应被 _is_credit_frame 识别（正文区含 Logo）
    assert gc._is_credit_frame(str(fp)) is True
