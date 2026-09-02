"""验证本地规则去重：02 集标注帧是否被保留。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.guide_capture import dedup_candidates  # noqa: E402

cand_dir = Path("backups_关键帧_20260809/02集_candidates原始/guide/candidates")
cands = [
    {"time": int(fp.stem.split("t")[1].rstrip("s")), "filename": str(fp)}
    for fp in sorted(cand_dir.glob("*.jpg"))
]
print(f"原始候选: {len(cands)}")
deduped = dedup_candidates(cands)
times = sorted(c["time"] for c in deduped)
print(f"去重+本地过滤后: {len(deduped)} 帧: {times}")

labeled = [12, 54, 70, 89, 157, 188, 196, 203, 235, 247]
found = [t for t in labeled if t in set(times)]
print(f"标注帧保留: {len(found)}/10 → {found}")
print(f"标注帧缺失: {[t for t in labeled if t not in set(times)]}")
