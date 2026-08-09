"""ASR 转写本地验证脚本：python -m server.tests.test_stt"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.config import settings  # noqa: E402
from server.stt.qwen_audio import QwenAudioSTT  # noqa: E402


async def main() -> None:
    audio = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/test_asr.wav")
    stt = QwenAudioSTT(
        api_key=settings.asr_api_key,
        model=settings.asr_model,
        api_url=settings.asr_api_url,
    )
    text = await stt.transcribe(audio)
    print("ASR RESULT:", text)


if __name__ == "__main__":
    asyncio.run(main())
