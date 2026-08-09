"""诊断脚本：完整截图流程，所有中间产物保存到 test/screenshots/<title>/debug/。

保存内容：
- 每帧截图 candidates/
- 每帧 OCR 结果 ocr_results.txt
- 每帧评分与是否选中 scores.csv
- 选中的关键图 selected/
"""
from __future__ import annotations

import csv
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.config import settings  # noqa: E402
from server.notes import writer  # noqa: E402
from server.ocr import vision  # noqa: E402
from server.summarizer.deepseek import DeepSeekSummarizer  # noqa: E402
from server.tests.test_resume import read_transcript  # noqa: E402


def main() -> None:
    video = Path("test/videos/飞书AI.mp4")
    transcript_md = Path("/Users/fuguanghui/Documents/Obsidian Vault/学习笔记/2026-08-08_飞书AI_文字稿.md")
    title, speech, segments = read_transcript(transcript_md)

    out = Path("test/screenshots") / title / "debug"
    cand_dir = out / "candidates"
    sel_dir = out / "selected"
    for d in (cand_dir, sel_dir):
        d.mkdir(parents=True, exist_ok=True)

    sumz = DeepSeekSummarizer(settings.llm_api_key, settings.llm_model, settings.llm_base_url)

    rows = []
    selected = []
    kept = []
    with tempfile.TemporaryDirectory() as td:
        for i, seg in enumerate(segments):
            t = seg["time_sec"]
            fp = Path(td) / f"{i:03d}.jpg"
            subprocess.run(
                ["ffmpeg", "-y", "-ss", str(t), "-i", str(video), "-frames:v", "1", str(fp)],
                capture_output=True,
            )
            if not fp.exists():
                continue
            # 保存候选帧
            cand_fp = cand_dir / f"{i:03d}_t{t}s.jpg"
            import shutil

            shutil.copy(fp, cand_fp)

            ocr_text = vision.ocr_slide_text(fp)
            s_ocr = sumz.score_ocr_importance(ocr_text) if ocr_text.strip() else 0.0
            s_visual = 0.6
            s_transcript = 0.5
            s_dup = max((sumz.text_similarity(ocr_text, s) for s in selected), default=0.0)
            score = (
                settings.score_w1 * s_visual
                + settings.score_w2 * s_ocr
                + settings.score_w3 * s_transcript
                - settings.score_w4 * s_dup
            )
            chosen = score >= settings.score_threshold
            rows.append({
                "t": t, "ocr": ocr_text, "s_ocr": round(s_ocr, 2),
                "s_dup": round(s_dup, 2), "score": round(score, 3),
                "chosen": chosen,
            })
            if chosen:
                # 存 vault + selected 目录
                name = f"shot_{t//60:02d}_{t%60:02d}.png"
                path = writer.save_attachment(fp.read_bytes(), name)
                shutil.copy(fp, sel_dir / name)
                kept.append({"time": f"{t//60:02d}:{t%60:02d}", "filename": path.name, "ocr_text": ocr_text})
                selected.append(ocr_text)
            if i % 30 == 0:
                print(f"  进度 {i}/{len(segments)} 选中 {len(kept)}")

    # 写 CSV
    with open(out / "scores.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["t", "ocr", "s_ocr", "s_dup", "score", "chosen"])
        w.writeheader()
        w.writerows(rows)

    # 写 OCR 结果汇总
    with open(out / "ocr_results.txt", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(f"t={r['t']}s chosen={r['chosen']} score={r['score']} Socr={r['s_ocr']} Sdup={r['s_dup']}\n  OCR: {r['ocr'][:120]}\n")

    print(f"\n=== 结果 ===")
    print(f"选中 {len(kept)}/{len(rows)} 张")
    for k in kept:
        print(f"  {k['time']} {k['filename']} ocr={k['ocr_text'][:40]!r}")
    print(f"中间产物已保存: {out}")


if __name__ == "__main__":
    main()
