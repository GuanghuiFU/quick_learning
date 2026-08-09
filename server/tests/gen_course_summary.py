"""生成课程整体脉络总结。

从课程的 速览版/ 文档读取每集核心信息 → DeepSeek 生成整体总结 → 写入课程根目录。
用法：python -m server.tests.gen_course_summary <course名> [--course-url URL]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.config import settings  # noqa: E402
from server.notes import writer  # noqa: E402
from server.summarizer.deepseek import DeepSeekSummarizer  # noqa: E402

# 已知系列信息（标题/URL）可在此补充；未知时从文档推断
COURSE_URLS = {
    "FastAPI一小时": "https://www.bilibili.com/video/BV1JTCQBQERg/",
}


def _extract_episodes(course_dir: Path, course: str) -> list[dict]:
    """从速览版文档提取每集 {title, theme, key_points, url}。"""
    sp_dir = course_dir / "速览版"
    episodes = []
    if not sp_dir.exists():
        print(f"无速览版目录: {sp_dir}")
        return episodes
    for md in sorted(sp_dir.glob("*.md")):
        content = md.read_text(encoding="utf-8")
        # 提取 title
        title = ""
        for line in content.splitlines():
            if line.startswith("title:"):
                title = line.split(":", 1)[1].strip().strip('"')
                break
        # 提取 source url
        url = ""
        for line in content.splitlines():
            if line.startswith("source:"):
                url = line.split(":", 1)[1].strip().strip('"')
                break
        # 提取核心结论（速览开头）——取"核心结论"小节内容
        key_points = ""
        if "核心结论" in content:
            seg = content.split("核心结论", 1)[1]
            # 取前几行非空内容
            key_points = "\n".join(
                l.strip() for l in seg.splitlines()[:8] if l.strip() and not l.startswith("##")
            )[:500]
        episodes.append({"title": title or md.stem, "theme": title or md.stem,
                         "key_points": key_points, "url": url})
    return episodes


def main() -> None:
    course = sys.argv[1] if len(sys.argv) > 1 else "FastAPI一小时"
    course_dir = Path(settings.vault_path) / settings.notes_dir / course
    url = COURSE_URLS.get(course, f"local:{course}")

    episodes = _extract_episodes(course_dir, course)
    print(f"读取到 {len(episodes)} 集的速览信息")

    if not episodes:
        print("未找到任何集，检查课程名或速览版目录")
        return

    sumz = DeepSeekSummarizer(settings.llm_api_key, settings.llm_model, settings.llm_base_url)
    body = sumz.course_summary(course, url, episodes)
    path = writer.save_course_summary(course, url, body, course=course)
    print(f"[课程整体总结已生成] {path}")


if __name__ == "__main__":
    main()
