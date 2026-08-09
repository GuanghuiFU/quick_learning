"""server 核心模块单元测试。"""
from __future__ import annotations

import os

os.environ.setdefault("VAULT_PATH", "/tmp/test_vault")

import pytest  # noqa: E402
from pathlib import Path  # noqa: E402

from server.config import settings  # noqa: E402
from server.notes.writer import build_note, save_note  # noqa: E402
from server.ocr.vision import get_engine  # noqa: E402


def test_config_loads_env():
    assert settings.llm_api_key, "LLM_API_KEY 应从 .env 加载"
    assert settings.asr_api_key, "ASR_API 应从 .env 加载"
    assert settings.asr_model == "qwen-audio-3.0-asr-flash"


def test_notes_writer_builds_markdown():
    note = build_note(
        "测试标题",
        "https://example.com",
        "## 要点\n- a\n\n![[shot.png]]",  # 图片由 LLM 内嵌到正文
        [{"filename": "shot.png", "time": "01:23"}],
    )
    assert "title: \"测试标题\"" in note
    assert "![[shot.png]]" in note  # 正文内嵌
    assert "## 要点" in note
    assert "## 📷 截图" not in note  # 无独立截图章节


def test_notes_writer_saves_file():
    os.environ["VAULT_PATH"] = "/tmp/test_vault"
    p = save_note("笔记", "https://x.com", "内容")
    assert p.exists()
    assert p.suffix == ".md"


def test_ocr_engine_available():
    eng = get_engine()
    assert eng is not None
