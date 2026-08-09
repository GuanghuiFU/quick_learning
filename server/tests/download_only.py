"""仅下载余下课程的视频（不处理），可与处理并行。

用法：python -m server.tests.download_only [课程名...]（默认余下 3 套）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# 待下载的课程（前 2 套已完成，AI Agent 记忆已下载）
REMAINING = [
    {"name": "Pydantic_LLM", "url": "https://www.bilibili.com/video/BV1PkbBzgEHX/", "course_title": "Pydantic for LLM"},
    {"name": "AI_Code_Review", "url": "https://www.bilibili.com/video/BV1XT3t6hEEH/", "course_title": "AI 代码审查"},
    {"name": "Spec_Driven_Dev", "url": "https://www.bilibili.com/video/BV1fWdBB1EDk/", "course_title": "Spec-Driven 开发"},
]


def main() -> None:
    from server.tests.process_courses import download_course

    # 可选参数指定只下载某套
    names = sys.argv[1:] or None
    for course in REMAINING:
        if names and course["name"] not in names:
            continue
        try:
            vids = download_course(course)
            print(f"[下载完成] {course['course_title']}: {len(vids)} 集")
        except Exception as e:  # noqa: BLE001
            import traceback

            print(f"  !! 下载异常: {e}")
            traceback.print_exc()
    print("全部下载完成")


if __name__ == "__main__":
    main()
