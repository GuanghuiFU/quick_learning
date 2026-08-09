"""真实视频端到端集成测试：模拟扩展采集真实 B 站视频 → OCR/ASR → DeepSeek → Obsidian。

前置：/tmp/vla_chunk_{0..3}.wav 音频分段，/tmp/vla_frames/*.jpg 画面帧
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.config import settings  # noqa: E402
from server.notes import writer  # noqa: E402
from server.ocr import vision  # noqa: E402
from server.stt.qwen_audio import QwenAudioSTT  # noqa: E402
from server.summarizer.deepseek import DeepSeekSummarizer  # noqa: E402

TITLE = "用AI的方式，重新打开飞书！"
URL = "https://www.bilibili.com/video/BV1MXoNBrEdm/"


def fmt_time(sec: float) -> str:
    m = int(sec) // 60
    s = int(sec) % 60
    return f"{m:02d}:{s:02d}"


async def main() -> None:
    # 1) 分段 ASR 转写（模拟扩展每 4.5 分钟转一段）
    stt = QwenAudioSTT(settings.asr_api_key, settings.asr_model, settings.asr_api_url)
    segs = []
    for i in range(4):
        path = Path(f"/tmp/vla_chunk_{i}.wav")
        if not path.exists():
            print(f"跳过缺失音频: {path}")
            continue
        text = await stt.transcribe(path)
        start = i * 270
        print(f"[ASR chunk {i} @{start}s] {len(text)} 字")
        segs.append({"start": start, "text": text})
    speech_text = "\n".join(f"[{fmt_time(s['start'])}] {s['text']}" for s in segs)
    with open("/tmp/vla_speech.txt", "w") as f:
        f.write(speech_text)
    print("语音转写已保存 /tmp/vla_speech.txt")

    # 2) 帧 OCR（字幕带 + 幻灯片检测）
    subtitles = []
    slides = []
    last_sub = ""
    prev_thumb = None
    for frame in sorted(Path("/tmp/vla_frames").glob("*.jpg")):
        t = float(frame.stem.split("_")[1])
        # 字幕带 OCR
        sub = vision.ocr_subtitle_band(frame, settings.ocr_subtitle_y0, settings.ocr_subtitle_y1).strip()
        if sub and sub != last_sub:
            subtitles.append({"time": fmt_time(t), "text": sub})
            last_sub = sub
        # 幻灯片检测（简化帧差）
        try:
            from PIL import Image
            img = Image.open(frame).convert("RGB").resize((160, 90))
            import numpy as np
            arr = np.asarray(img, dtype=np.int16)
            if prev_thumb is not None:
                diff = float(np.mean(np.abs(arr - prev_thumb)) / 255.0)
            else:
                diff = None
            prev_thumb = arr
            is_new = (diff is None or diff > 0.12) and not sub
            if is_new:
                slides.append({"time": fmt_time(t), "filename": frame.name, "ocr_text": ""})
        except Exception as e:
            print("frame skip:", e)
    subtitle_text = "\n".join(f"[{s['time']}] {s['text']}" for s in subtitles)
    with open("/tmp/vla_subtitles.txt", "w") as f:
        f.write(subtitle_text)
    print(f"字幕 {len(subtitles)} 条, 幻灯片 {len(slides)} 张")
    print("字幕已保存 /tmp/vla_subtitles.txt")

    # 3) 生成速览 + 笔记
    summarizer = DeepSeekSummarizer(settings.llm_api_key, settings.llm_model, settings.llm_base_url)
    # 速览
    sr_body = summarizer.speedread(
        TITLE, URL,
        subtitle_text=subtitle_text,
        speech_text=speech_text,
        slides=slides,
        priority="stt",  # 飞书视频无字幕，语音为主
    )
    sr_path = writer.save_speedread(TITLE, URL, sr_body, slides)
    print("速览已写入:", sr_path)
    # 完整笔记
    note_body = summarizer.summarize(TITLE, URL, subtitle_text, speech_text, slides, priority="stt")
    note_path = writer.save_note(TITLE, URL, note_body, slides)
    print("笔记已写入:", note_path)


if __name__ == "__main__":
    asyncio.run(main())
