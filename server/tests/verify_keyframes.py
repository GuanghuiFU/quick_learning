"""标注驱动的关键帧选择验证工具。

以 assets/important_pic_label.md 中用户标注的帧为 ground truth，跑选择逻辑中
**确定性**部分（本地过滤 → 去重 → 文本相似去重），输出：
  - 内容覆盖：每个标注"重要"的幻灯片，是否被去重后保留的某帧代表
    （用户标注的是幻灯片，同一张被讲解多帧只保留一张，代表帧可与标注时间略差）
  - 误选：保留了课程封面/片尾credit、卡通插画等无价值帧

LLM 评分（DeepSeek 的 s_ocr/s_transcript）不在此复跑——已有 scores.csv 记录。
本工具定位"结构性"问题：哪些标注帧在去重/本地过滤阶段被错误丢弃。

用法：
    .venv/bin/python -m server.tests.verify_keyframes
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.guide_capture import (  # noqa: E402
    _frame_bands,
    _local_score,
    _text_sim,
    dedup_candidates,
    ocr_slide_text_cached as _ocr_text,
)

# 02 集标注（用户 important_pic_label.md）：time -> 备注
LABELS: dict[int, str] = {
    12: "重要: 示例图",
    54: "重要: 有用信息",
    70: "重要: 示意图",
    89: "重要: 示意图",
    157: "重要: 总结图(三模块合并)",
    188: "次要: 与前面差别不大，随意选一个即可",
    196: "重要: 讲解complier",
    203: "重要: 讲解Spec-Driven Development",
    235: "重要: 聊天机器人",
    247: "重要: Agent",
}
IMPORTANT = {t for t, n in LABELS.items() if n.startswith("重要")}

# 明确"不选/重复"的帧（用户指出不应选）——若被保留视为误选
EXCLUDE: dict[int, str] = {
    13: "重复 t12",
    14: "重复 t12",
    55: "重复 t54",
    158: "重复 t157",
    278: "课程封面/credit",
    279: "课程封面/credit",
    280: "课程封面/credit",
    270: "卡通插画",
    271: "卡通插画",
}

# 候选帧目录：与用户标注同源的集合（seg 编号与 important_pic_label.md 一致）
CAND_DIR = Path("assets/screenshots/Vibe Coding 2026/02-02_为什么要规范驱动开发/guide/candidates")


def load_candidates() -> list[dict]:
    cands = []
    for fp in sorted(CAND_DIR.glob("*.jpg")):
        t = int(fp.stem.split("t")[1].rstrip("s"))
        cands.append({"time": t, "filename": str(fp)})
    return cands


def run() -> None:
    cands = load_candidates()
    print(f"候选帧: {len(cands)} 张 (标注同源集合)")
    deduped = dedup_candidates(cands)
    kept = sorted(deduped, key=lambda c: c["time"])
    print(f"去重+本地过滤后: {len(kept)} 张: {[c['time'] for c in kept]}")

    kept_times = {c["time"] for c in kept}

    # ---- 内容覆盖：标注"重要"的幻灯片是否被某保留帧代表 ----
    print(f"\n=== 内容覆盖（标注重要 {len(IMPORTANT)} 个） ===")
    uncovered = []
    for t in sorted(IMPORTANT):
        note = LABELS[t]
        # 该标注幻灯片的标题（用于在保留帧中找同主题代表）
        label_frames = [c for c in cands if abs(c["time"] - t) <= 2]
        label_title = (_frame_bands(label_frames[0]["filename"])["sub_title"]
                       if label_frames else "")
        # 找标题最接近的保留帧（内容同主题即视为覆盖，时间戳可不同）
        rep = None
        if label_title:
            best, best_sim = None, 0.0
            for c in kept:
                kt = _frame_bands(c["filename"])["sub_title"]
                sim = _text_sim(label_title, kt)
                if sim > best_sim:
                    best, best_sim = c["time"], sim
            if best is not None and best_sim > 0.4:
                rep = best
        if rep is not None:
            print(f"  {t}s({note}) → 保留帧 {rep}s (子标题相似 {best_sim:.2f}) ✓")
        else:
            uncovered.append(t)
            print(f"  {t}s({note}) → 无同主题保留帧 ✗")
    if uncovered:
        print(f"\n⚠ 未覆盖 {len(uncovered)} 个: {uncovered}")
        # 诊断每个未覆盖标注：是被本地过滤还是去重合并
        for t in sorted(uncovered):
            note = LABELS[t]
            near = [c for c in cands if abs(c["time"] - t) <= 2]
            for c in near:
                b = _frame_bands(c["filename"])
                print(f"    {t}s({note}) 候选 {c['time']}s: mid_len={b['mid_len']} "
                      f"title={b['sub_title'][:30]!r}")
    else:
        print("全部内容覆盖 ✓")

    # ---- 误选：保留帧里有没有用户明确说不该选的 ----
    print(f"\n=== 误选检查 ===")
    wrong = [t for t in sorted(kept_times) if t in EXCLUDE]
    if wrong:
        print(f"  ✗ 保留了不应选的帧: {[(t, EXCLUDE[t]) for t in wrong]}")
    else:
        print("  无（无 credit/卡通/重复帧残留）✓")

    # ---- 保留帧明细 ----
    print(f"\n=== 保留帧明细 ===")
    for c in kept:
        t = c["time"]
        b = _frame_bands(c["filename"])
        tag = "重要" if t in IMPORTANT else ("次要" if t in LABELS else "—")
        print(f"  {t:4d}s [{tag}] local={_local_score(c):.2f} sub_title={b['sub_title'][:24]!r} "
              f"mid_len={b['mid_len']}")


if __name__ == "__main__":
    run()
