"""Obsidian 笔记写入：生成 Markdown + 附件落盘 + frontmatter + 图片引用格式检查。"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from server.config import settings

# 子文件夹命名（课程路径下）
DIR_ATTACHMENTS = "attachments"
DIR_SPEEDREAD = "速览版"
DIR_NOTE = "完整版"
DIR_TRANSCRIPT = "文字稿"


def _safe_filename(title: str) -> str:
    """去掉标题里不安全的文件名字符。"""
    return re.sub(r'[\\/:*?"<>|\s]+', "_", title.strip())[:80] or "untitled"


def _course_dir_name(course: str) -> str:
    """课程目录名：保留空格（macOS 允许），仅去首尾空白，去除不安全字符但不转空格。"""
    # 保留空格，仅去除真正危险的字符（/ : * ? " < > |）
    return re.sub(r'[\\/:*?"<>|]+', "_", course.strip())


def _course_root(course: str | None = None) -> Path:
    """课程根目录：有 course 则 `学习笔记/<course>/`，否则 `学习笔记/`。"""
    root = settings.notes_root
    if course:
        root = root / _course_dir_name(course)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _sub_dir(course: str | None, sub: str) -> Path:
    """课程下某个子文件夹（attachments/速览版/完整版/文字稿）。"""
    root = _course_root(course)
    d = root / sub
    d.mkdir(parents=True, exist_ok=True)
    return d


def _attachments_dir(course: str | None = None) -> Path:
    return _sub_dir(course, DIR_ATTACHMENTS)


def _speedread_dir(course: str | None = None) -> Path:
    return _sub_dir(course, DIR_SPEEDREAD)


def _note_dir(course: str | None = None) -> Path:
    return _sub_dir(course, DIR_NOTE)


def _transcript_dir(course: str | None = None) -> Path:
    return _sub_dir(course, DIR_TRANSCRIPT)


def save_attachment(data: bytes, filename: str, course: str | None = None) -> Path:
    """保存截图/图片附件到 vault 附件目录，返回落盘路径。"""
    root = _attachments_dir(course)
    # 防止路径穿越
    safe = Path(filename).name
    dest = root / safe
    dest.write_bytes(data)
    return dest


# ---------- 图片引用格式检查 ----------

# Obsidian 图片引用：![[文件名.ext]]（半角感叹号，无多余反引号）
_IMG_REF_RE = re.compile(r"[!！]\[\[([^\]]+)\]\]")


def fix_image_refs(markdown: str) -> str:
    """修正 Markdown 里的图片引用格式问题。

    1. 中文感叹号 ！→ ！
    2. 去掉 `![[...]]` 内部的多余反引号（`05_x_guide.jpg` → 05_x_guide.jpg）
    3. 去掉文件名首尾空白
    """
    def _fix(m: re.Match) -> str:
        name = m.group(1)
        # 去掉反引号
        name = name.replace("`", "")
        name = name.strip()
        return f"![[{name}]]"

    return _IMG_REF_RE.sub(_fix, markdown)


def validate_image_refs(markdown: str) -> list[str]:
    """检查图片引用是否有格式问题，返回有问题的引用列表（空=全正常）。"""
    problems = []
    for m in _IMG_REF_RE.finditer(markdown):
        raw = m.group(0)
        name = m.group(1)
        if raw.startswith("！"):
            problems.append(f"{raw} → 中文感叹号")
        elif "`" in name:
            problems.append(f"{raw} → 含多余反引号")
        elif name != name.strip():
            problems.append(f"{raw} → 首尾空白")
    return problems


def build_note(
    title: str,
    source_url: str,
    body: str,
    screenshots: list[dict] | None = None,
    *,
    note_date: date | None = None,
) -> str:
    """组装 Obsidian Markdown 笔记（含 frontmatter + 生成时间 + 来源 URL）。"""
    import datetime

    d = note_date or date.today()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "---",
        f'title: "{title}"',
        f'source: "{source_url}"',
        f'date: "{d.isoformat()}"',
        f'created: "{now}"',
        "tags: [学习, 视频]",
        "---",
        "",
        f"# {title}",
        "",
        f"> 生成时间：{now}",
        "",
    ]
    if source_url:
        lines.append(f"> 📚 学习资料：{source_url}")
        lines.append("")
    lines.append("## 内容")
    lines.append("")
    lines.append(fix_image_refs(body.strip()))
    return "\n".join(lines)


def save_note(title: str, source_url: str, body: str, screenshots: list[dict] | None = None,
              course: str | None = None) -> Path:
    """写入完整笔记到 vault。返回笔记文件路径。"""
    root = _note_dir(course)
    filename = f"{_safe_filename(title)}.md"
    path = root / filename
    path.write_text(build_note(title, source_url, body, screenshots), encoding="utf-8")
    return path


def save_speedread(title: str, source_url: str, body: str, slides: list[dict] | None = None,
                   course: str | None = None) -> Path:
    """写入速览到 vault。文件名加 _速览 后缀。"""
    root = _speedread_dir(course)
    filename = f"{_safe_filename(title)}_速览.md"
    path = root / filename
    path.write_text(build_note(title, source_url, body, slides), encoding="utf-8")
    return path


def save_transcript(
    title: str,
    source_url: str,
    speech_text: str,
    segments: list[dict] | None = None,
    note_date: date | None = None,
    course: str | None = None,
) -> Path:
    """写入完整文字稿到 vault。文件名加 _文字稿 后缀。

    segments 可选：带 begin_ms/end_ms 的分句列表，用于按时间戳分段 + 定位关键画面。
    """
    import datetime

    root = _transcript_dir(course)
    d = note_date or date.today()
    filename = f"{_safe_filename(title)}_文字稿.md"
    path = root / filename

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "---",
        f'title: "{title}"',
        f'source: "{source_url}"',
        f'date: "{d.isoformat()}"',
        f'created: "{now}"',
        "tags: [学习, 视频, 文字稿]",
        "---",
        "",
        f"# {title} — 完整文字稿",
        "",
        f"> 生成时间：{now}",
        "",
    ]
    if segments:
        for s in segments:
            ms = s.get("begin_ms", 0)
            m, sec = ms // 60000, (ms // 1000) % 60
            lines.append(f"**[{m:02d}:{sec:02d}]** {s.get('text', '')}")
            lines.append("")
    else:
        lines.append(speech_text.strip() or "（本视频无音频转写）")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def save_transcript_bilingual(
    title: str,
    source_url: str,
    speech_text: str,
    translated_text: str,
    segments: list[dict] | None = None,
    note_date: date | None = None,
    course: str | None = None,
) -> Path:
    """写入中英双语文字稿：英文原文 + 中文翻译对照。文件名 _文字稿_双语。

    translated_text: 中文翻译全文（与 speech_text 逐段对应）。
    """
    import datetime

    root = _transcript_dir(course)
    d = note_date or date.today()
    filename = f"{_safe_filename(title)}_文字稿_双语.md"
    path = root / filename

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 中英对照：按时间戳段对齐
    lines = [
        "---",
        f'title: "{title}"',
        f'source: "{source_url}"',
        f'date: "{d.isoformat()}"',
        f'created: "{now}"',
        "tags: [学习, 视频, 文字稿, 双语]",
        "---",
        "",
        f"# {title} — 双语文字稿",
        "",
        f"> 生成时间：{now}",
        "",
        "> 英文原文 / 中文翻译对照",
        "",
    ]
    # 按段对齐：英文 segments 和中文翻译分段
    en_parts = speech_text.strip().split("\n") if speech_text else []
    zh_parts = translated_text.strip().split("\n") if translated_text else []
    max_len = max(len(en_parts), len(zh_parts))
    for i in range(max_len):
        en = en_parts[i] if i < len(en_parts) else ""
        zh = zh_parts[i] if i < len(zh_parts) else ""
        # 提取时间戳（如果有），并去掉原文里的时间戳避免重复
        ts = ""
        import re as _re

        m = _re.match(r"\[(\d+):(\d+)\]", en) or _re.match(r"\[(\d+):(\d+)\]", zh)
        if m:
            ts = f"**[{m.group(1)}:{m.group(2)}]**"
        # 去掉开头的时间戳标记（正文不再重复）
        en_clean = _re.sub(r"^\[?\d+:\d+\]?\s*", "", en).strip()
        zh_clean = _re.sub(r"^\[?\d+:\d+\]?\s*", "", zh).strip()
        if en_clean:
            lines.append(f"{ts} {en_clean}" if ts else en_clean)
        if zh_clean:
            lines.append(f"- 中文：{zh_clean}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def save_course_summary(title: str, source_url: str, body: str, course: str | None = None) -> Path:
    """写入课程整体总结到 vault。放课程根目录，文件名 _整体总结。"""
    import datetime

    root = _course_root(course)
    d = date.today()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    filename = f"{_safe_filename(title)}_整体总结.md"
    path = root / filename
    lines = [
        "---",
        f'title: "{title} 整体总结"',
        f'source: "{source_url}"',
        f'date: "{d.isoformat()}"',
        f'created: "{now}"',
        "tags: [学习, 课程总结]",
        "---",
        "",
        f"# {title} — 整体脉络总结",
        "",
        f"> 生成时间：{now}",
        "",
        "## 内容",
        "",
        fix_image_refs(body.strip()),
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
