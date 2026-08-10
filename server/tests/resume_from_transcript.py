"""从已有文字稿继续处理（跳过 ASR 转写），完成字幕链 + 关键帧 + 笔记。

用于长视频：首次转写已完成并保存文字稿后，若后续阶段中断（如关键帧 OCR 慢），
可复用文字稿 + OCR 磁盘缓存重跑，不必重新 ASR 转写。

用法：
    .venv/bin/python -m server.tests.resume_from_transcript \
        <视频路径> <文字稿路径> <课程名>
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.config import settings  # noqa: E402
from server.notes import writer  # noqa: E402
from server.stt.qwen_audio import QwenAudioSTT  # noqa: E402
from server.subtitle import resolve_subtitle  # noqa: E402
from server.summarizer.deepseek import DeepSeekSummarizer  # noqa: E402
from server.tests.test_resume import read_transcript  # noqa: E402
from server import task_db  # noqa: E402


def main() -> None:
    if len(sys.argv) < 4:
        print("用法: resume_from_transcript <视频> <文字稿.md> <课程名>")
        sys.exit(1)
    video = Path(sys.argv[1])
    transcript_md = Path(sys.argv[2])
    course = sys.argv[3]

    title, speech, segments = read_transcript(transcript_md)
    print(f"复用文字稿: {title} ({len(segments)} 句)")

    summarizer = DeepSeekSummarizer(settings.llm_api_key, settings.llm_model, settings.llm_base_url)
    stt = QwenAudioSTT(settings.asr_api_key, settings.asr_model, settings.asr_api_url)

    # 字幕链（B站 AI 字幕 → 校验 → ASR 兜底）
    bili_url = f"https://www.bilibili.com/video/{video.stem}" if video.stem.startswith("BV") else f"local:{video}"
    sub_result = resolve_subtitle(
        source_url=bili_url, video=video, speech=speech,
        config=settings, deepseek=summarizer, stt=stt, db=task_db,
    )
    subtitle = sub_result["text"]
    print(f"[字幕] {len([l for l in subtitle.splitlines() if l.strip()])} 条 (来源: {sub_result['source']})")

    # 关键帧（复用 OCR 磁盘缓存）
    from server.guide_capture import guide_capture

    guide_segs = [{"time_sec": s["time_sec"], "text": s["text"]} for s in segments]
    slides = guide_capture(summarizer, video, guide_segs, title, course=course)
    print(f"[关键画面] {len(slides)} 张")

    # 速览 + 完整笔记
    sr = summarizer.speedread(title, f"local:{video}", subtitle, speech, slides, priority="stt")
    writer.save_speedread(title, f"local:{video}", sr, slides, course=course)
    print("[速览已保存]")

    note = summarizer.summarize(title, f"local:{video}", subtitle, speech, slides, priority="stt")
    writer.save_note(title, f"local:{video}", note, slides, course=course)
    print("[完整笔记已保存]")


if __name__ == "__main__":
    main()
