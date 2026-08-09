"""处理 6 套课程：逐套下载 + 多进程并行生成学习笔记。

用法：python -m server.tests.process_courses [课程名] [--workers N]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# 6 套课程配置：course名(目录) + 播放列表URL
COURSES = [
    {"name": "LLM_FineTuning", "url": "https://www.bilibili.com/video/BV1DRqbBZEBY/", "course_title": "大模型微调"},
    {"name": "RLHF_Book", "url": "https://www.bilibili.com/video/BV1rpLj6xEzU/", "course_title": "人类反馈强化学习"},
    {"name": "AI_Agent_Memory", "url": "https://www.bilibili.com/video/BV1m6wzzLEn3/", "course_title": "AI Agent 记忆"},
    {"name": "Pydantic_LLM", "url": "https://www.bilibili.com/video/BV1PkbBzgEHX/", "course_title": "Pydantic for LLM"},
    {"name": "AI_Code_Review", "url": "https://www.bilibili.com/video/BV1XT3t6hEEH/", "course_title": "AI 代码审查"},
    {"name": "Spec_Driven_Dev", "url": "https://www.bilibili.com/video/BV1fWdBB1EDk/", "course_title": "Spec-Driven 开发"},
]


def _process_episode_worker(video_path: str, course_title: str) -> str:
    """worker 进程：处理单集（转写+字幕+关键帧+文档）。"""
    import asyncio

    from server.batch_process import process_one
    from server.config import settings
    from server.summarizer.deepseek import DeepSeekSummarizer
    from server.stt.qwen_audio import QwenAudioSTT

    summarizer = DeepSeekSummarizer(settings.llm_api_key, settings.llm_model, settings.llm_base_url)
    stt = QwenAudioSTT(settings.asr_api_key, settings.asr_model, settings.asr_api_url)
    asyncio.run(process_one(summarizer, stt, Path(video_path), "both", "both", course=course_title))
    return f"完成: {Path(video_path).name}"


def download_course(course: dict) -> list[Path]:
    """下载一套课程全部集。返回视频路径列表（断点续传：已有 NN-主题 则跳过下载）。"""
    import re

    from server.downloader import download_opencli, download_playlist_ytdlp

    name = course["name"]
    url = course["url"]
    dl_dir = Path("assets/videos") / name
    dl_dir.mkdir(parents=True, exist_ok=True)

    # 已有 NN-主题 命名的视频（之前处理过）→ 跳过下载
    existing_named = sorted(dl_dir.glob("*-*.mp4"))
    if existing_named:
        print(f"[已有 {len(existing_named)} 集已下载，跳过下载]")
        vids = existing_named
    else:
        print(f"[下载] {course['course_title']} ({name})")
        vids = download_playlist_ytdlp(url, dl_dir, quality="360")

        # 补齐缺失集（opencli 浏览器）
        if vids:
            nums = []
            for v in vids:
                m = re.search(r"p(\d+)", v.stem)
                if m:
                    nums.append(int(m.group(1)))
            max_n = max(nums) if nums else 0
            existing = set(nums)
            missing = [i for i in range(1, max_n + 1) if i not in existing]
            if missing:
                print(f"[补齐缺失集] {missing}（opencli 浏览器）")
                for n in missing:
                    p = download_opencli(f"{url}?p={n}", dl_dir, quality="340")
                    if p:
                        print(f"  ✓ 补齐 p{n}")

        # 重命名为 "NN-主题"（提取每集副标题）
        vids = sorted(dl_dir.glob("*.mp4"))
        _rename_with_theme(vids, url)

    vids = sorted(dl_dir.glob("*.mp4"))
    print(f"[待处理] {len(vids)} 集")
    return vids


def _rename_with_theme(vids: list[Path], playlist_url: str) -> None:
    """把 NN_pNN.mp4 重命名为 NN-主题.mp4（从播放列表元数据提取副标题）。"""
    import subprocess

    from server.downloader import _find_ytdlp

    yt = _find_ytdlp()
    try:
        # 非 flat 获取每集标题（B站 flat 返回 NA）
        out = subprocess.run(
            [yt, "--skip-download", "--print", "%(playlist_index)s|%(title)s", playlist_url],
            capture_output=True, text=True, timeout=300,
        )
        if out.returncode != 0:
            print("  [获取标题失败，保留原命名]")
            return
        titles = {}
        for line in out.stdout.strip().splitlines():
            if "|" in line:
                idx, t = line.split("|", 1)
                titles[idx.strip()] = t.strip()
    except Exception:  # noqa: BLE001
        print("  [获取标题失败，保留原命名]")
        return

    import re as _re

    for v in vids:
        m = _re.search(r"(\d+)_p\d+", v.stem)
        if not m:
            continue
        idx = m.group(1)
        t = titles.get(idx)
        if not t:
            continue
        # 提取 pNN 之后的主题（如 "p01 1.大语言模型微调之道1——介绍" → "1-大语言模型微调之道1-介绍"）
        pm = _re.search(r"[pP]\d+\s+(.*)$", t)
        theme = pm.group(1) if pm else t
        # 清理：去掉序号、多余符号
        theme = _re.sub(r"^\d+[\.、]\s*", "", theme)
        theme = _re.sub(r"[^\w一-鿿-]+", "-", theme).strip("-")[:50]
        if not theme:
            continue
        new_name = f"{idx}-{theme}{v.suffix}"
        new_path = v.with_name(new_name)
        if not new_path.exists():
            v.rename(new_path)
            print(f"  [重命名] {v.name} → {new_name}")


def gen_course_summary(course: dict, workers: int = 1) -> None:
    """课程整体总结（必须在全部集处理完后调用）。"""
    from server.config import settings as st
    from server.summarizer.deepseek import DeepSeekSummarizer
    from server.tests.gen_course_summary import _extract_episodes

    course_title = course["course_title"]
    course_dir = Path(st.vault_path) / st.notes_dir / course_title
    episodes = _extract_episodes(course_dir, course_title)
    if not episodes:
        print(f"[课程总结] {course_title} 无速览文档，跳过")
        return
    print(f"[课程总结] {course_title}：基于 {len(episodes)} 集速览")
    sumz = DeepSeekSummarizer(st.llm_api_key, st.llm_model, st.llm_base_url)
    url = course.get("url", "")
    body = sumz.course_summary(course_title, url, episodes)
    from server.notes import writer

    writer.save_course_summary(course_title, url, body, course=course_title)
    print(f"[课程整体总结已生成] {course_title}")


def process_all_courses(courses: list[dict], workers: int = 6) -> None:
    """跨课程并行处理所有课程。

    1) 先下载全部课程（纯网络，无 API）
    2) 收集所有集任务，全局进程池并行处理（跨课程并行，API 全忙）
    3) 每套课程全部集完成后，生成该课程整体总结
    """
    # 1) 下载全部课程
    courses_videos: dict[str, list[Path]] = {}
    for course in courses:
        print(f"\n{'='*60}\n[课程] {course['course_title']} ({course['name']})\n{'='*60}")
        vids = download_course(course)
        if not vids:
            print(f"[警告] {course['name']} 无视频")
        courses_videos[course["name"]] = vids

    # 2) 收集所有集的（course, video）任务
    all_tasks: list[tuple[dict, Path]] = []
    for course in courses:
        for v in courses_videos.get(course["name"], []):
            all_tasks.append((course, v))
    print(f"\n[全部任务] {len(all_tasks)} 集，{workers} 个 worker 跨课程并行")

    # 3) 全局进程池并行处理（跨课程并行）
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(_process_episode_worker, str(v), course["course_title"]): (course, v)
            for course, v in all_tasks
        }
        for fut in futures:
            course, v = futures[fut]
            try:
                result = fut.result()
                print(f"  [ok] [{course['course_title']}] {Path(v).name[:40]} → {result[:15]}")
            except Exception as e:  # noqa: BLE001
                print(f"  [失败] [{course['course_title']}] {Path(v).name[:40]}: {str(e)[:100]}")

    # 4) 每套课程全部集完成后，生成整体总结
    print("\n[生成各课程整体总结]")
    for course in courses:
        if courses_videos.get(course["name"]):
            try:
                gen_course_summary(course, workers)
            except Exception as e:  # noqa: BLE001
                print(f"  !! 课程总结异常 {course['course_title']}: {e}")


def main() -> None:
    ap = argparse.ArgumentParser(description="处理多套课程（跨课程并行）")
    ap.add_argument("course", nargs="?", default=None, help="只处理某课程名（如 LLM_FineTuning）")
    ap.add_argument("--workers", type=int, default=6, help="并行 worker 数（默认6）")
    args = ap.parse_args()

    # 选择要处理的课程
    targets = [c for c in COURSES if not args.course or c["name"] == args.course]
    if not targets:
        print(f"未找到课程 {args.course}")
        return
    process_all_courses(targets, args.workers)
    print("\n全部课程处理完成")


if __name__ == "__main__":
    main()
