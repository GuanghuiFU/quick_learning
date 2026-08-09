"""SQLite 任务状态管理：记录每集视频的处理状态，支持可靠断点续传与去重。

表 episodes:
  id, course, video_path(唯一), video_name,
  transcript_done, subtitle_done, keyframes_done, notes_done,
  transcript_path, note_path, created_at, updated_at
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[1] / "task_state.db"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course TEXT NOT NULL,
            video_path TEXT UNIQUE NOT NULL,
            video_name TEXT,
            transcript_done INTEGER DEFAULT 0,
            subtitle_done INTEGER DEFAULT 0,
            keyframes_done INTEGER DEFAULT 0,
            notes_done INTEGER DEFAULT 0,
            transcript_path TEXT,
            note_path TEXT,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    conn.commit()
    return conn


def get_episode(course: str, video_path: str) -> dict | None:
    conn = _conn()
    row = conn.execute(
        "SELECT * FROM episodes WHERE course=? AND video_path=?",
        (course, video_path),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def register_episode(course: str, video_path: str) -> None:
    """登记一集（若不存在）。"""
    conn = _conn()
    conn.execute(
        "INSERT OR IGNORE INTO episodes (course, video_path, video_name) VALUES (?, ?, ?)",
        (course, video_path, Path(video_path).name),
    )
    conn.commit()
    conn.close()


def mark_stage(course: str, video_path: str, stage: str, path: str = "") -> None:
    """标记某阶段完成。stage: transcript|subtitle|keyframes|notes。"""
    col = f"{stage}_done"
    conn = _conn()
    # 更新阶段标志 + 对应路径
    conn.execute(
        f"UPDATE episodes SET {col}=1, updated_at=datetime('now','localtime') "
        f"WHERE course=? AND video_path=?",
        (course, video_path),
    )
    if path:
        path_col = {"transcript": "transcript_path", "note": "note_path"}.get(stage)
        if path_col:
            conn.execute(
                f"UPDATE episodes SET {path_col}=? WHERE course=? AND video_path=?",
                (path, course, video_path),
            )
    conn.commit()
    conn.close()


def is_episode_done(course: str, video_path: str, stage: str) -> bool:
    """检查某阶段是否完成。"""
    ep = get_episode(course, video_path)
    if not ep:
        return False
    col = f"{stage}_done"
    return bool(ep.get(col, 0))


def pending_episodes(course: str, stage: str) -> list[dict]:
    """列出某课程某阶段未完成的集。"""
    conn = _conn()
    col = f"{stage}_done"
    rows = conn.execute(
        "SELECT * FROM episodes WHERE course=? AND ({} = 0 OR {} IS NULL)".format(col, col),
        (course,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def all_episodes(course: str | None = None) -> list[dict]:
    conn = _conn()
    if course:
        rows = conn.execute("SELECT * FROM episodes WHERE course=?", (course,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM episodes").fetchall()
    conn.close()
    return [dict(r) for r in rows]
