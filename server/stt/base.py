"""STT / ASR 抽象层。当前实现：千问 qwen-audio-3.0-asr-flash。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class STTProvider(ABC):
    """语音转文字提供者。"""

    @abstractmethod
    async def transcribe(self, audio_path: Path) -> str:
        """将音频文件转写为文本。"""
