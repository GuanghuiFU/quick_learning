"""跳过转写，复用已有时间戳文字稿，处理视频剩余步骤（字幕OCR + 关键画面截图 + 文档）。

改进：按文字稿时间戳在每句讲解处采样帧 → 主体区变化检测 → 评分 → 存 assets/screenshots。
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.config import settings  # noqa: E402
from server import capture  # noqa: E402
from server.notes import writer  # noqa: E402
from server.ocr import vision  # noqa: E402
from server.summarizer.deepseek import DeepSeekSummarizer  # noqa: E402


def read_transcript(path: Path) -> tuple[str, str, list[dict]]:
    """读取文字稿 md，返回 (title, speech_text, segments[{time_sec, text}])。"""
    content = path.read_text(encoding="utf-8")
    title = ""
    for line in content.splitlines():
        if line.startswith("title:"):
            title = line.split(":", 1)[1].strip().strip('"')
            break
    segments = []
    for l in content.splitlines():
        m = re.match(r"\*\*\[(\d+):(\d+)\]\*\*\s*(.*)", l) or re.match(r"\[(\d+):(\d+)\]\s*(.*)", l)
        if m:
            t = int(m.group(1)) * 60 + int(m.group(2))
            text = m.group(3).strip()
            if text:
                segments.append({"time_sec": t, "text": text})
    speech = "\n".join(f"[{s['time_sec']//60:02d}:{s['time_sec']%60:02d}] {s['text']}" for s in segments)
    return title or path.stem, speech, segments


def extract_subtitles(video: Path, sample_secs: list[int]) -> str:
    """在指定时间点采样帧，做字幕带 OCR，返回带时间戳字幕文本。"""
    import shutil

    out = Path("/tmp/vla_sub")
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    sub_lines = []
    last = ""
    for t in sample_secs:
        fp = out / f"t{t}.jpg"
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(t), "-i", str(video), "-frames:v", "1", str(fp)],
            capture_output=True,
        )
        if not fp.exists():
            continue
        text = vision.ocr_subtitle_band(fp, settings.ocr_subtitle_y0, settings.ocr_subtitle_y1).strip()
        if text and text != last:
            sub_lines.append(f"[{t//60:02d}:{t%60:02d}] {text}")
            last = text
        fp.unlink(missing_ok=True)
    shutil.rmtree(out, ignore_errors=True)
    return "\n".join(sub_lines)


def main() -> None:
    if len(sys.argv) > 1:
        video = Path(sys.argv[1])
    else:
        video = Path(__file__).resolve().parents[2] / "test" / "videos" / "飞书AI.mp4"
    if len(sys.argv) > 2:
        transcript_md = Path(sys.argv[2])
    else:
        transcript_md = Path("/Users/fuguanghui/Documents/Obsidian Vault/学习笔记/2026-08-08_飞书AI_文字稿.md")
    if not video.exists():
        print(f"视频不存在: {video}"); sys.exit(1)
    if not transcript_md.exists():
        print(f"文字稿不存在: {transcript_md}"); sys.exit(1)

    title, speech, segments = read_transcript(transcript_md)
    print(f"标题: {title}, 文字稿 {len(segments)} 句")

    # 1) 字幕 OCR：在每句开始处采样（时间戳对齐讲解）
    sample_secs = [s["time_sec"] for s in segments]
    subtitle = extract_subtitles(video, sample_secs)
    print(f"字幕 {len([l for l in subtitle.splitlines() if l.strip()])} 条")

    # 2) 关键画面：按时间戳采样 + 评分
    sumz = DeepSeekSummarizer(settings.llm_api_key, settings.llm_model, settings.llm_base_url)

    # 在讲解密集处（每句时间戳附近）采样候选帧，直接落盘到 debug/candidates
    out = Path(__file__).resolve().parents[2] / "test" / "screenshots" / title / "debug"
    cand_dir, sel_dir = out / "candidates", out / "selected"
    for d in (cand_dir, sel_dir):
        d.mkdir(parents=True, exist_ok=True)

    cands = []
    for i, seg in enumerate(segments):
        t = seg["time_sec"]
        fp = cand_dir / f"{i:03d}_t{t}s.jpg"
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(t), "-i", str(video), "-frames:v", "1", str(fp)],
            capture_output=True,
        )
        if fp.exists():
            cands.append({"time": t, "filename": fp})
    print(f"采样帧 {len(cands)} 张 (已存 {cand_dir})")

    # 3) 评分并保存关键图
    import shutil

    rows, kept, selected = [], [], []
    for i, c in enumerate(cands):
        ocr_text = vision.ocr_slide_text(c["filename"])
        s_ocr = sumz.score_ocr_importance(ocr_text) if ocr_text.strip() else 0.0
        s_visual = 0.6  # 已按讲解时间采样
        s_transcript = sumz.score_transcript_importance(c.get("transcript", "")) if c.get("transcript") else 0.5
        s_dup = max((sumz.text_similarity(ocr_text, t) for t in selected), default=0.0)
        score = (
            settings.score_w1 * s_visual
            + settings.score_w2 * s_ocr
            + settings.score_w3 * s_transcript
            - settings.score_w4 * s_dup
        )
        rows.append({"t": c["time"], "ocr": ocr_text, "s_ocr": round(s_ocr, 2),
                     "s_dup": round(s_dup, 2), "score": round(score, 3),
                     "chosen": score >= settings.score_threshold})
        if score >= settings.score_threshold:
            data = c["filename"].read_bytes()
            name = f"shot_{c['time']//60:02d}_{c['time']%60:02d}.png"
            # 存 vault + selected 副本
            path = writer.save_attachment(data, name)
            shutil.copy(c["filename"], sel_dir / name)
            kept.append({"time": f"{c['time']//60:02d}:{c['time']%60:02d}",
                         "filename": path.name, "ocr_text": ocr_text, "score": round(score, 2)})
            selected.append(ocr_text)
            print(f"  [关键图] {kept[-1]['time']} {name} score={score:.2f} ocr={ocr_text[:30]}")
        if i % 30 == 0:
            print(f"  进度 {i}/{len(cands)} 选中 {len(kept)}")

    # 写评分 CSV + OCR 结果
    import csv

    with open(out / "scores.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["t", "ocr", "s_ocr", "s_dup", "score", "chosen"])
        w.writeheader()
        w.writerows(rows)
    with open(out / "ocr_results.txt", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(f"t={r['t']}s chosen={r['chosen']} score={r['score']} Socr={r['s_ocr']} Sdup={r['s_dup']}\n  OCR: {r['ocr'][:120]}\n")
    print(f"关键图 {len(kept)}/{len(cands)} 张 (中间产物: {out})")

    # 4) 生成文档
    url = f"local:{video}"
    sr = sumz.speedread(title, url, subtitle_text=subtitle, speech_text=speech, slides=kept, priority="stt")
    writer.save_speedread(title, url, sr, kept)
    print("[速览已生成]")
    note = sumz.summarize(title, url, subtitle, speech, kept, priority="stt")
    writer.save_note(title, url, note, kept)
    print("[笔记已生成]")
    print("全部完成")


if __name__ == "__main__":
    main()
