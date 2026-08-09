"""对已选关键图再筛一遍（应用信息量过滤），重建 Obsidian 文档。

用法：python -m server.tests.rescreen_slides
从 test/screenshots/飞书AI/guide/selected/ 读取已选图，
用 assess_frame_worthiness 过滤，保留信息量足的，重建速览+笔记。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.config import settings  # noqa: E402
from server.notes import writer  # noqa: E402
from server.summarizer.deepseek import DeepSeekSummarizer  # noqa: E402
from server.tests.test_resume import read_transcript  # noqa: E402


def main() -> None:
    title = "飞书AI"
    video = Path("test/videos/飞书AI.mp4")
    transcript_md = Path("/Users/fuguanghui/Documents/Obsidian Vault/学习笔记/2026-08-08_飞书AI_文字稿.md")
    sel_dir = Path("test/screenshots") / title / "guide" / "selected"
    att_dir = Path(settings.vault_path) / settings.attachments_dir

    if not sel_dir.exists():
        print(f"无已选图目录: {sel_dir}")
        return

    sumz = DeepSeekSummarizer(settings.llm_api_key, settings.llm_model, settings.llm_base_url)

    # 1) 对已选图逐张做信息量精判
    kept = []
    print("=== 再筛选已选图 ===")
    for fp in sorted(sel_dir.glob("guide_*.jpg")):
        ocr_text = vision.ocr_slide_text(fp)
        worthy = sumz.assess_frame_worthiness(ocr_text)
        t = fp.stem.replace("guide_", "").replace("_", ":")
        mark = "✓" if worthy["worthy"] else "✗"
        print(f"{mark} {t} {fp.name}: {worthy['reason'][:40]}")
        if worthy["worthy"]:
            # 确保附件在 vault
            dest = att_dir / fp.name
            if not dest.exists():
                dest.write_bytes(fp.read_bytes())
            kept.append({"time": t, "filename": fp.name, "ocr_text": ocr_text})

    print(f"\n保留 {len(kept)}/{len(list(sel_dir.glob('guide_*.jpg')))} 张")

    # 2) 重建文档（删除旧附件中不再保留的图）
    keep_names = {k["filename"] for k in kept}
    for old in att_dir.glob("guide_*.jpg"):
        if old.name not in keep_names:
            old.unlink(missing_ok=True)
            print(f"  清理附件: {old.name}")

    # 3) 重建速览 + 笔记
    title_t, speech, segments = read_transcript(transcript_md)
    sr = sumz.speedread(title_t, f"local:{video}", speech_text=speech, slides=kept, priority="stt")
    writer.save_speedread(title_t, f"local:{video}", sr, kept)
    print("[速览已重建]")
    note = sumz.summarize(title_t, f"local:{video}", "", speech, kept, priority="stt")
    writer.save_note(title_t, f"local:{video}", note, kept)
    print("[笔记已重建]")


if __name__ == "__main__":
    from server.ocr import vision  # noqa: E402
    main()
