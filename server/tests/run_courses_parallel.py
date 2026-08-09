"""每套课程一个独立进程并行处理（4套同时跑，各内部顺序处理）。

用法：python -m server.tests.run_courses_parallel [--workers 4]
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# 余下 4 套课程（前 2 套已完成）
COURSES = [
    {"name": "AI_Agent_Memory", "course_title": "AI Agent 记忆", "url": "https://www.bilibili.com/video/BV1m6wzzLEn3/"},
    {"name": "Pydantic_LLM", "course_title": "Pydantic for LLM", "url": "https://www.bilibili.com/video/BV1PkbBzgEHX/"},
    {"name": "AI_Code_Review", "course_title": "AI 代码审查", "url": "https://www.bilibili.com/video/BV1XT3t6hEEH/"},
    {"name": "Spec_Driven_Dev", "course_title": "Spec-Driven 开发", "url": "https://www.bilibili.com/video/BV1fWdBB1EDk/"},
]


def run_course_worker(course: dict, log_file: Path | None = None) -> None:
    """一个课程的处理 worker：内部顺序处理各集。"""
    import asyncio
    from pathlib import Path as P

    from server.batch_process import process_one
    from server.config import settings
    from server.summarizer.deepseek import DeepSeekSummarizer
    from server.stt.qwen_audio import QwenAudioSTT

    name = course["name"]
    course_title = course["course_title"]
    dl_dir = P("assets/videos") / name

    # 下载（若未下载）
    vids = sorted(dl_dir.glob("*.mp4"))
    if not vids:
        from server.tests.process_courses import download_course

        vids = download_course(course)

    print(f"[{course_title}] 共 {len(vids)} 集")
    summarizer = DeepSeekSummarizer(settings.llm_api_key, settings.llm_model, settings.llm_base_url)
    stt = QwenAudioSTT(settings.asr_api_key, settings.asr_model, settings.asr_api_url)

    for v in vids:
        try:
            asyncio.run(process_one(summarizer, stt, v, "both", "both", course=course_title))
            print(f"[{course_title}] 完成: {v.name[:40]}")
        except Exception as e:  # noqa: BLE001
            print(f"[{course_title}] 失败 {v.name[:30]}: {str(e)[:120]}")

    # 课程整体总结
    try:
        from server.tests.process_courses import gen_course_summary

        gen_course_summary(course)
        print(f"[{course_title}] 整体总结已生成")
    except Exception as e:  # noqa: BLE001
        print(f"[{course_title}] 总结失败: {e}")


def main() -> None:
    log_dir = Path("/tmp/course_logs")
    log_dir.mkdir(exist_ok=True)

    procs = []
    for course in COURSES:
        log_file = log_dir / f"{course['name']}.log"
        print(f"[启动] {course['course_title']} → {log_file}")
        # 每个课程一个独立子进程
        proc = subprocess.Popen(
            [sys.executable, "-u", "-c", """
import sys
sys.path.insert(0, '.')
from server.tests.run_courses_parallel import run_course_worker, COURSES
course = next(c for c in COURSES if c['name'] == %r)
run_course_worker(course)
""" % course["name"]],
            stdout=open(log_file, "w"),
            stderr=subprocess.STDOUT,
        )
        procs.append(proc)

    print(f"[并行] 4 套课程各自独立进程运行中")
    for p in procs:
        p.wait()
    print("全部课程处理完成")


if __name__ == "__main__":
    main()
