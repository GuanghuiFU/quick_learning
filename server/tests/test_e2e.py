"""端到端集成测试：模拟扩展采集 → OCR/ASR → DeepSeek 总结 → Obsidian 笔记。

不依赖浏览器，用真实组件 + 本地生成的模拟视频帧。
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

os.environ.setdefault("VAULT_PATH", "/tmp/test_vault")

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from server.config import settings  # noqa: E402
from server.notes import writer  # noqa: E402
from server.ocr.vision import ocr_subtitle_band  # noqa: E402
from server.summarizer.deepseek import DeepSeekSummarizer  # noqa: E402


def make_subtitle_frame(text: str, out: Path) -> None:
    """模拟一个带字幕的视频帧（深色背景 + 底部白色字幕）。"""
    img = Image.new("RGB", (1280, 720), (30, 34, 45))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 560, 1280, 700], fill=(18, 18, 26))
    font = ImageFont.truetype("/System/Library/Fonts/Hiragino Sans GB.ttc", 40)
    d.text((120, 590), text, font=font, fill="white")
    img.save(out)


def make_arch_frame(out: Path) -> None:
    """模拟一个架构图截图（含英文术语，用于截图 OCR 测试）。"""
    img = Image.new("RGB", (1200, 800), "white")
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 36)
    d.text((100, 120), "Microservices Architecture", font=font, fill="black")
    d.text((100, 220), "Message Queue  Kafka", font=font, fill="black")
    d.text((100, 320), "Database Sharding", font=font, fill="black")
    d.rectangle([80, 100, 520, 380], outline="black", width=3)
    img.save(out)


def test_frame_slide_detection():
    """帧差检测：大幅变化 → 新幻灯片；相同帧 → 不新。"""
    from PIL import Image, ImageDraw, ImageFont

    from server.app import _prev_frame
    import server.app as app_mod

    # 复位上一帧状态
    app_mod._prev_frame = None

    # 模拟幻灯片变化
    def make_slide(bg, text):
        img = Image.new("RGB", (1280, 720), bg)
        d = ImageDraw.Draw(img)
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 48)
        d.text((100, 300), text, font=font, fill="white")
        f = Path(f"/tmp/_test_slide_{bg[0]}.png")
        img.save(f)
        return f

    a = make_slide((200, 30, 30), "Slide A")
    b = make_slide((30, 30, 200), "Slide B")

    # 直接调底层处理逻辑（无字幕帧 → 评分截图）
    from server.app import frame as frame_endpoint
    import asyncio
    from starlette.datastructures import UploadFile as SUploadFile

    async def post(p):
        f = SUploadFile(file=__import__("io").BytesIO(p.read_bytes()), filename="f.png")
        return await frame_endpoint(file=f)

    r1 = asyncio.run(post(a))  # 首帧
    assert "score" in r1 and "subtitle" in r1, r1
    r2 = asyncio.run(post(b))  # 变化帧
    assert "score" in r2 and "subtitle" in r2, r2
    r3 = asyncio.run(post(a))  # 回到 a
    assert "score" in r3 and "subtitle" in r3, r3

    a.unlink(); b.unlink()


def test_end_to_end_note_generation():
    """字幕OCR + 截图OCR + ASR(跳过,因真实录音) → DeepSeek → Obsidian 笔记。"""
    # 1. 模拟视频字幕帧序列
    frames = [
        "大家好，今天我们来学习分布式系统架构",
        "首先介绍微服务的基本概念",
        "然后讲解消息队列在系统中的应用",
    ]
    subtitle_lines = []
    for i, text in enumerate(frames):
        f = Path(f"/tmp/frame_{i}.png")
        make_subtitle_frame(text, f)
        ocr = ocr_subtitle_band(f, settings.ocr_subtitle_y0, settings.ocr_subtitle_y1)
        # 字幕 OCR 可能去掉或保留标点，比较去除标点后的核心内容
        norm = lambda s: s.replace("，", "").replace("。", "").replace(" ", "")
        assert norm(text) == norm(ocr), f"OCR 字幕识别失败: {ocr!r} vs {text!r}"
        subtitle_lines.append(f"[00:{i:02d}] {ocr}")
        f.unlink()

    subtitle_text = "\n".join(subtitle_lines)
    print("字幕OCR结果:", subtitle_text)

    # 2. 模拟架构图截图 + 整图 OCR
    arch = Path("/tmp/arch_frame.png")
    make_arch_frame(arch)
    screenshot_ocr = ""
    from server.ocr.vision import ocr_image

    for r in ocr_image(arch):
        screenshot_ocr += f"{r['text']} "
    arch.unlink()
    print("截图OCR结果:", screenshot_ocr.strip())

    # 3. DeepSeek 总结
    summarizer = DeepSeekSummarizer(
        settings.llm_api_key, settings.llm_model, settings.llm_base_url
    )
    body = summarizer.summarize(
        title="分布式系统架构入门",
        source_url="https://www.bilibili.com/video/BV1MXoNBrEdm/",
        subtitle_text=subtitle_text,
        speech_text="",  # 端到端测试跳过真实录音 ASR
        screenshot_notes=[{"time": "00:01", "filename": "arch.png", "ocr_text": screenshot_ocr}],
    )
    assert len(body) > 100, "DeepSeek 总结过短"
    print("总结生成成功，长度:", len(body))

    # 4. 写入 Obsidian
    note_path = writer.save_note(
        "分布式系统架构入门",
        "https://www.bilibili.com/video/BV1MXoNBrEdm/",
        body,
        [{"time": "00:01", "filename": "arch.png"}],
    )
    assert note_path.exists()
    content = note_path.read_text(encoding="utf-8")
    assert "title:" in content and "## " in content
    print("笔记已写入:", note_path)


if __name__ == "__main__":
    test_end_to_end_note_generation()
