"""批量处理视频文件夹：自动转写 + 字幕OCR + 智能截图 + 生成笔记 → Obsidian。

用法：
    python -m server.batch_process /path/to/videos [--format webm] [--priority stt] [--mode both]

前置：需要 ffmpeg（帧采样+转音频）、yt-dlp 可选（仅当输入是 URL）。
"""
from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.config import settings  # noqa: E402
from server.guide_capture import guide_capture  # noqa: E402
from server.notes import writer  # noqa: E402
from server.ocr import vision  # noqa: E402
from server.stt.qwen_audio import QwenAudioSTT  # noqa: E402
from server.summarizer.deepseek import DeepSeekSummarizer  # noqa: E402

VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".m4a", ".mp3", ".wav", ".flv", ".avi"}
CHUNK_SEC = 270  # 4.5 分钟一段（千问 ASR 单次限 5 分钟）

def fmt_time(sec: float) -> str:
    return f"{int(sec) // 60:02d}:{int(sec) % 60:02d}"


def find_videos(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(
        p for p in path.rglob("*") if p.suffix.lower() in VIDEO_EXTS
    )


async def transcribe_video(stt: QwenAudioSTT, video: Path) -> tuple[str, list[dict]]:
    """把视频音频切段转写，返回 (带时间戳全文, 全部分句列表[带begin_ms])。"""
    # 先检测是否有音频流
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(video)],
        capture_output=True, text=True,
    )
    if not probe.stdout.strip():
        print("  [无音轨，跳过转写]")
        return "", []

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        wav = td / "full.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(video), "-ar", "16000", "-ac", "1", str(wav)],
            check=True, capture_output=True,
        )
        import math

        dur = math.ceil(float(
            subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(wav)],
                capture_output=True, text=True,
            ).stdout.strip() or 0
        ))
        segs = []
        all_segments = []  # 所有分句（带全局时间戳）
        for start in range(0, dur, CHUNK_SEC):
            chunk = td / f"chunk_{start}.wav"
            subprocess.run(
                ["ffmpeg", "-y", "-ss", str(start), "-t", str(CHUNK_SEC), "-i", str(wav), str(chunk)],
                check=True, capture_output=True,
            )
            try:
                segs_text = await stt.transcribe_segments(chunk)
                for s in segs_text:
                    if s["text"].strip():
                        # 全局时间戳 = 段起始 + 段内偏移
                        s["begin_ms"] += start * 1000
                        s["end_ms"] += start * 1000
                        all_segments.append(s)
                if segs_text:
                    chunk_text = "\n".join(s["text"] for s in segs_text)
                    segs.append((start, chunk_text))
            except Exception as e:  # noqa: BLE001
                print(f"  [转写段 {fmt_time(start)} 失败] {e}")
        full = "\n".join(f"[{fmt_time(s)}] {t}" for s, t in segs)
        return full, all_segments


def extract_subtitles_and_slides(video: Path) -> tuple[str, list[dict]]:
    """采样视频帧，OCR 字幕带。返回 (字幕文本, 空列表)。"""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        frames = td / "frames"
        frames.mkdir()
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(video), "-vf", "fps=0.25", str(frames / "f_%05d.jpg")],
            check=True, capture_output=True,
        )
        frame_paths = sorted(frames.glob("*.jpg"))

        subtitles = []
        last_sub = ""
        total = len(frame_paths)
        for idx, fp in enumerate(frame_paths):
            if idx % 50 == 0:
                print(f"  [OCR 进度 {idx}/{total}]")
            t = idx * 4  # 帧索引 → 秒（fps=0.25）
            sub = vision.ocr_subtitle_band(fp, settings.ocr_subtitle_y0, settings.ocr_subtitle_y1).strip()
            if sub and sub != last_sub:
                subtitles.append({"time": fmt_time(t), "text": sub})
                last_sub = sub
        return (
            "\n".join(f"[{s['time']}] {s['text']}" for s in subtitles),
            [],
        )


def _segments_for_guide(segments: list[dict]) -> list[dict]:
    """把 transcribe 返回的分句（begin_ms）转换为 guide_capture 需要的格式（time_sec）。"""
    out = []
    for s in segments:
        out.append({
            "time_sec": round(s.get("begin_ms", 0) / 1000),
            "text": s.get("text", ""),
        })
    # 按时间排序去重
    out.sort(key=lambda x: x["time_sec"])
    seen = set()
    deduped = []
    for s in out:
        if s["time_sec"] not in seen:
            seen.add(s["time_sec"])
            deduped.append(s)
    return deduped


def _is_english(text: str) -> bool:
    """粗略判断转写是否为英文（英文/非汉字比例）。"""
    if not text.strip():
        return False
    cn = sum(1 for c in text if '一' <= c <= '鿿')
    total = sum(1 for c in text if c.isalpha())
    if total == 0:
        return False
    return cn / total < 0.2  # 汉字占比 < 20% 视为英文


async def process_one(summarizer, stt, video: Path, priority: str, mode: str, course: str | None = None) -> None:
    from server import task_db

    title = video.stem
    print(f"\n=== 处理: {video.name} ({video.stat().st_size / 1e6:.1f} MB) ===")
    course_key = course or "default"

    # 登记到状态数据库
    task_db.register_episode(course_key, str(video))

    # 断点续传：数据库记录 或 完整版文件存在 → 跳过（兼容无 DB 历史数据）
    if mode in ("note", "both"):
        db_done = task_db.is_episode_done(course_key, str(video), "notes")
        file_done = (writer._note_dir(course) / f"{writer._safe_filename(title)}.md").exists()
        if db_done or file_done:
            print(f"  [跳过] {title} 已处理过（{'数据库' if db_done else '完整版文件存在'}）")
            return

    # 1) 语音转写（返回全文 + 带时间戳分句）
    speech, segments = await transcribe_video(stt, video)
    print(f"[转写完成] {len(speech)} 字, {len(segments)} 句")

    # 1b) 语言检测：英文则生成双语文字稿 + 中文总结
    is_en = _is_english(speech)
    translated = ""
    if is_en:
        print("[检测到英文视频，生成双语文字稿]")
        # 英文原文（带时间戳分段）
        en_speech = "\n".join(
            f"[{s['begin_ms']//60000:02d}:{(s['begin_ms']//1000)%60:02d}] {s['text']}"
            for s in segments if s.get("text", "").strip()
        )
        translated = summarizer.translate(en_speech)
        writer.save_transcript(title, f"local:{video}", en_speech, segments=segments, course=course)
        writer.save_transcript_bilingual(
            title, f"local:{video}", en_speech, translated, segments=segments, course=course
        )
        task_db.mark_stage(course_key, str(video), "transcript")
        print(f"[双语文字稿已保存] {title}_文字稿.md + _文字稿_双语.md")
    else:
        writer.save_transcript(title, f"local:{video}", speech, segments=segments, course=course)
        print(f"[文字稿已保存] {title}_文字稿.md")

    # 2) 字幕解析（智能来源链）：B站CC → B站AI(长视频+校验) → ASR → OCR(可选)
    from server.subtitle import resolve_subtitle
    from server import task_db as _db

    source_url = f"local:{video}" if not (video.name and video.name.startswith(("http", "BV"))) else ""
    # 实际 URL 来源：batch_process 主要处理本地视频，B 站 URL 需在下载阶段保留
    # 这里用视频名近似判断（若是 B 站下载的，文件名含 BV 号）
    bili_url = f"https://www.bilibili.com/video/{video.stem.split('_')[0]}" if video.stem.startswith("BV") else ""

    sub_result = resolve_subtitle(
        source_url=bili_url or source_url,
        video=video,
        speech=speech,
        config=settings,
        deepseek=summarizer,
        stt=stt,
        db=_db,
    )
    subtitle = sub_result["text"]
    print(f"[字幕] {len([l for l in subtitle.splitlines() if l.strip()])} 条 (来源: {sub_result['source']})")

    # 3) 文字指引版关键帧检测（讲解预筛 → 稳定候选 → 信息量精判）
    if segments:
        guide_segs = _segments_for_guide(segments)
        slides = guide_capture(summarizer, video, guide_segs, title, course=course)
        print(f"[关键画面] {len(slides)} 张")
    else:
        # 无音轨/无转写 → 无文字指引，跳过关键帧
        slides = []
        print("[关键画面] 无文字稿，跳过")

    # 4) 生成文档（英文视频用中文总结）
    if is_en:
        # 英文视频：速览用中文，完整笔记用中文
        if mode in ("speedread", "both"):
            body = summarizer.speedread(title, f"local:{video}", subtitle, translated, slides, priority)
            writer.save_speedread(title, f"local:{video}", body, slides, course=course)
            print("[速览已生成（中文）]")
        if mode in ("note", "both"):
            body = summarizer.summarize_chinese(title, f"local:{video}", speech, translated, slides)
            writer.save_note(title, f"local:{video}", body, slides, course=course)
            print("[笔记已生成（中文）]")
        task_db.mark_stage(course_key, str(video), "notes")
        return

    if mode in ("speedread", "both"):
        body = summarizer.speedread(title, f"local:{video}", subtitle, speech, slides, priority)
        writer.save_speedread(title, f"local:{video}", body, slides, course=course)
        print("[速览已生成]")
    if mode in ("note", "both"):
        body = summarizer.summarize(title, f"local:{video}", subtitle, speech, slides, priority)
        writer.save_note(title, f"local:{video}", body, slides, course=course)
        print("[笔记已生成]")
    task_db.mark_stage(course_key, str(video), "notes")


async def main() -> None:
    ap = argparse.ArgumentParser(description="批量处理视频文件夹 → Obsidian 笔记")
    ap.add_argument("input", help="视频文件、包含视频的文件夹，或视频 URL（配合 --url）")
    ap.add_argument("--priority", default="both", choices=["ocr", "stt", "both"])
    ap.add_argument("--mode", default="both", choices=["note", "speedread", "both"])
    ap.add_argument("--course", default=None, help="课程名：笔记写入 学习笔记/<course>/ 子文件夹")
    ap.add_argument("--url", action="store_true", help="input 是视频 URL，先下载再处理")
    ap.add_argument("--dl-method", default=settings.download_method,
                    choices=["auto", "ytdlp", "opencli"],
                    help="下载方式：auto(先yt-dlp后opencli浏览器) | ytdlp | opencli")
    ap.add_argument("--dl-quality", default=settings.download_quality,
                    help="下载清晰度（如 360/480/720/1080）")
    args = ap.parse_args()

    # 如果 input 是 URL 或指定了 --url，先下载
    videos = []
    if args.url or str(args.input).startswith(("http://", "https://")):
        from server.downloader import download_video

        dl_dir = Path("assets/videos") / "downloads"
        print(f"[下载] {args.input} (方式: {args.dl_method}, 清晰度: {args.dl_quality})")
        p = download_video(str(args.input), dl_dir, quality=args.dl_quality, method=args.dl_method)
        if not p:
            print("下载失败，请检查 URL 或换下载方式")
            return
        videos = [p]
    else:
        videos = find_videos(Path(args.input))
        if not videos:
            print("未找到视频文件")
            return

    print(f"找到 {len(videos)} 个视频")

    summarizer = DeepSeekSummarizer(settings.llm_api_key, settings.llm_model, settings.llm_base_url)
    stt = QwenAudioSTT(settings.asr_api_key, settings.asr_model, settings.asr_api_url)

    for v in videos:
        try:
            await process_one(summarizer, stt, v, args.priority, args.mode, course=args.course)
        except Exception as e:  # noqa: BLE001
            import traceback

            print(f"  !! 处理失败: {e}")
            traceback.print_exc()

    print("\n全部处理完成，笔记已写入 Obsidian")


if __name__ == "__main__":
    asyncio.run(main())
