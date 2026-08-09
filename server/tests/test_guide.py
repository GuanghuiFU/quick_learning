"""测试文字指引版关键帧检测（前 N 秒）。

用法：python -m server.tests.test_guide [--seconds 300]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.config import settings  # noqa: E402
from server.guide_capture import guide_capture, score_transcript_lines  # noqa: E402
from server.notes import writer  # noqa: E402
from server.summarizer.deepseek import DeepSeekSummarizer  # noqa: E402
from server.tests.test_resume import read_transcript  # noqa: E402


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=0, help="只处理前 N 秒；0=全部")
    args = ap.parse_args()
    seconds = args.seconds
    video = Path("test/videos/飞书AI.mp4")
    transcript_md = Path("/Users/fuguanghui/Documents/Obsidian Vault/学习笔记/2026-08-08_飞书AI_文字稿.md")
    title, speech, segments = read_transcript(transcript_md)

    if seconds:
        segments = [s for s in segments if s["time_sec"] <= seconds]
        print(f"前 {seconds}s: {len(segments)} 句")
    else:
        print(f"全部 {len(segments)} 句")

    sumz = DeepSeekSummarizer(settings.llm_api_key, settings.llm_model, settings.llm_base_url)
    kept = guide_capture(sumz, video, segments, title)

    print(f"\n=== 关键图 {len(kept)} 张 ===")
    for k in kept:
        print(f"  {k['time']} {k['filename']} score={k['score']} ocr={k['ocr_text'][:40]!r}")

    # 生成速览 + 完整笔记
    suffix = f"_前{seconds}s" if seconds else ""
    sr = sumz.speedread(title, f"local:{video}",
                        speech_text=speech, slides=kept, priority="stt")
    writer.save_speedread(f"{title}{suffix}", f"local:{video}", sr, kept)
    print(f"[速览已生成: {title}{suffix}]")
    if not seconds:
        note = sumz.summarize(title, f"local:{video}", "", speech, kept, priority="stt")
        writer.save_note(title, f"local:{video}", note, kept)
        print("[完整笔记已生成]")


if __name__ == "__main__":
    main()
