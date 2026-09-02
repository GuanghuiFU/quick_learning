"""处理某个 B 站合集下的一组视频：并发下载 + 多进程并行生成图文笔记。

与 process_courses（播放列表 URL、按 P 号补齐）的区别：这里接受**显式 BV 清单**，
适合"合集里只要这几集"或"合集 URL 探测不到分P"的场景。

用法：
    .venv/bin/python -u -m server.tests.process_series --course "AI+企业" --workers 4 \
        https://www.bilibili.com/video/BV1xxx BV1yyy ...

笔记落到 学习笔记/<course>/{文字稿,速览版,完整版,attachments}/，与单视频流程一致。
断点续传：已有完整版笔记的集会跳过（process_one 内置）。
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# 让进度实时可见：python 重定向到文件时默认块缓冲，会把"在跑"看起来像"卡死"
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")


def normalize_bv(arg: str) -> str:
    """从 'BV1xx'、完整 URL、或带参数的 URL 里取出 BV 号。"""
    m = BV_RE.search(arg)
    if not m:
        raise ValueError(f"不是 BV 号或 B 站 URL: {arg}")
    return m.group(1)


def _worker(args: tuple) -> tuple[str, str]:
    """子进程：处理单集（转写+字幕+关键帧+速览+完整版）。"""
    import asyncio
    import traceback

    from server.batch_process import process_one
    from server.config import settings
    from server.notes import writer
    from server.stt.qwen_audio import QwenAudioSTT
    from server.summarizer.deepseek import DeepSeekSummarizer

    video_path, course = args
    v = Path(video_path)
    try:
        summarizer = DeepSeekSummarizer(
            settings.llm_api_key, settings.llm_model, settings.llm_base_url)
        stt = QwenAudioSTT(settings.asr_api_key, settings.asr_model, settings.asr_api_url)
        asyncio.run(process_one(summarizer, stt, v, "both", "both", course=course))
        note = writer._note_dir(course) / f"{writer._safe_filename(v.stem)}.md"
        return (v.name, "OK" if note.exists() else "无完整版笔记产出")
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return (v.name, f"失败: {str(e)[:120]}")


def main() -> None:
    ap = argparse.ArgumentParser(description="合集多视频并行处理")
    ap.add_argument("urls", nargs="+", help="BV 号或 B 站视频 URL")
    ap.add_argument("--course", required=True, help="课程名（Obsidian 子目录）")
    ap.add_argument("--workers", type=int, default=4, help="并行处理进程数（默认4）")
    ap.add_argument("--quality", default=None, help="下载清晰度，默认取 .env")
    args = ap.parse_args()

    from server.config import settings
    from server.downloader import download_video
    from server.notes import writer

    bvs = [normalize_bv(u) for u in args.urls]
    dupes = {b for b in bvs if bvs.count(b) > 1}
    if dupes:
        print(f"[警告] 清单里有重复 BV，已去重: {sorted(dupes)}")
    seen, uniq = set(), []
    for b in bvs:
        if b not in seen:
            seen.add(b)
            uniq.append(b)
    bvs = uniq
    print(f"[清单] {len(bvs)} 集: {' '.join(bvs)}")

    # 1) 下载（yt-dlp 自带"已下载即跳过"，重跑不重复拉流）
    dl_dir = Path("assets/videos") / re.sub(r'[\\/:*?"<>|]+', "_", args.course)
    dl_dir.mkdir(parents=True, exist_ok=True)
    videos: list[Path] = []
    failed_dl: list[str] = []
    for bv in bvs:
        url = f"https://www.bilibili.com/video/{bv}"
        print(f"\n[下载] {bv}")
        try:
            p = download_video(url, dl_dir, quality=args.quality or settings.download_quality,
                               method="ytdlp")
            if p is None:  # yt-dlp 全清晰度降级后仍失败（不抛异常，返回 None）
                raise RuntimeError("yt-dlp 各清晰度均失败")
            videos.append(p)
            print(f"  ✓ {p.name}")
        except Exception as e:  # noqa: BLE001
            failed_dl.append(bv)
            print(f"  ✗ 下载失败: {str(e)[:150]}")
            # 兜底：目录里已有同名 BV 的文件就用它
            guess = [f for f in dl_dir.glob("*.mp4") if bv in f.name]
            if guess:
                videos.append(guess[0])
                print(f"  ↺ 改用已有文件 {guess[0].name}")

    print(f"\n[下载完成] 成功 {len(videos)} / 失败 {len(failed_dl)}")

    # 2) 多进程并行处理
    if not videos:
        print("没有可处理的视频")
        return
    print(f"[处理] {len(videos)} 集 × {args.workers} 并行\n")
    results = []
    with cf.ProcessPoolExecutor(max_workers=args.workers) as pool:
        for name, status in pool.map(_worker, [(str(v), args.course) for v in videos]):
            results.append((name, status))
            print(f"[{'✓' if status == 'OK' else '✗'}] {status}  {name[:50]}", flush=True)

    # 3) 汇总
    ok = [n for n, s in results if s == "OK"]
    print(f"\n{'=' * 60}\n全部完成：{len(ok)}/{len(videos)} 集已产出完整版笔记")
    for n, s in results:
        if s != "OK":
            print(f"  ⚠ {s}  {n[:60]}")
    print(f"输出目录: {writer._note_dir(args.course)}")
    if failed_dl:
        print(f"下载失败（未处理）: {' '.join(failed_dl)}")


if __name__ == "__main__":
    main()
