"""字幕策略：可插拔来源链 + 双维度质量校验 + 自适应决策。

来源链（SUB_SOURCES，可扩展）：
  bili_cc  → B站手工CC字幕（yt-dlp，免费，优先）
  bili_ai  → B站AI字幕（长视频 + 双维度校验才用）
  yt_ai    → YouTube AI字幕（预留，可扩展）
  asr      → ASR 语音转写兜底
  ocr      → OCR 字幕（可选降级）

并发安全：字幕抓取用 yt-dlp（读 Chrome 登录态，每个 worker 独立并行）；
          opencli 只在视频下载兜底时用（此处加锁串行）。
"""
from __future__ import annotations

import fcntl
import re
import subprocess
import tempfile
from pathlib import Path

# opencli 跨进程锁（单 Chrome 单 daemon，需串行访问）
_OPENCLI_LOCK_PATH = "/tmp/video_learning_opencli.lock"


def _opencli_locked(fn, *args, **kwargs):
    """串行化 opencli 操作（防止多 worker 抢同一 Chrome）。"""
    with open(_OPENCLI_LOCK_PATH, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            return fn(*args, **kwargs)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


# ---------- B 站字幕下载（yt-dlp，读 Chrome 登录态，可并行） ----------

def _find_ytdlp() -> str:
    import shutil

    p = shutil.which("yt-dlp")
    if p:
        return p
    project = Path(__file__).resolve().parents[1]
    cand = project / ".venv" / "bin" / "yt-dlp"
    return str(cand) if cand.exists() else "yt-dlp"


def _download_subs(url: str, lang: str) -> str:
    """用 yt-dlp 读 Chrome 登录态，下载指定语言字幕，返回 SRT 文本。失败返回空串。"""
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "sub"
        cmd = [
            _find_ytdlp(), "--skip-download",
            "--cookies-from-browser", "chrome",
            "--write-subs", "--sub-langs", lang, "--sub-format", "srt",
            "-o", str(out), url,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return ""
        # 找下载的字幕文件
        subs = list(Path(td).glob("*.srt"))
        if not subs:
            return ""
        return subs[0].read_text(encoding="utf-8", errors="ignore")


def fetch_bilibili_subtitle_cc(url: str) -> str:
    """获取 B 站手工 CC 字幕（非 AI）。手工 CC lan 为 zh-Hans/zh-CN。"""
    # 先试中文 CC，再试英文 CC
    for lang in ["zh-Hans", "zh-CN", "en"]:
        srt = _download_subs(url, lang)
        if srt:
            return srt
    return ""


def fetch_bilibili_subtitle_ai(url: str) -> str:
    """获取 B 站 AI 字幕（ai-zh）。"""
    return _download_subs(url, "ai-zh")


# ---------- 字幕文本处理 ----------

def srt_to_text(srt: str) -> str:
    """SRT → 带时间戳文本：'[MM:SS] content'。"""
    lines = []
    for block in srt.strip().split("\n\n"):
        parts = block.splitlines()
        if len(parts) < 3:
            continue
        # 时间行：00:00:01,420 --> 00:00:04,140
        m = re.match(r"(\d+):(\d+):(\d+)", parts[1])
        if not m:
            continue
        h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
        ts = f"{(h*60+mi):02d}:{s:02d}"
        content = "".join(parts[2:]).strip()
        lines.append(f"[{ts}] {content}")
    return "\n".join(lines)


# ---------- 双维度质量校验 ----------

def verify_subtitle_quality(source: str, url: str, ai_sub: str, stt, video: Path,
                            deepseek, db, sample_sec: int = 30, threshold: float = 0.85) -> bool:
    """双维度校验 AI 字幕质量，结果逐句入库。返回 True(可用)/False(回退ASR)。

    维度1 一致性：AI字幕片段 vs ASR 转写（ASR 也可能错，所以还需维度2）
    维度2 合理性：DeepSeek 独立判断字幕内容通顺（不看 ASR）
    threshold: 两个维度都需达到的合格线（默认 0.85，严格；可配置）
    """
    import subprocess as sp

    # 抽样前 30 秒音频
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "sample.wav"
        sp.run(["ffmpeg", "-y", "-ss", "0", "-t", str(sample_sec), "-i", str(video),
                "-ar", "16000", "-ac", "1", str(wav)], capture_output=True, timeout=60)

        # 维度1：ASR 转写抽样
        import asyncio
        import threading

        # verify_subtitle_quality 是同步函数，但可能被 async 主流程（process_one）调用，
        # 也可能被独立脚本调用。stt.transcribe 是 async：
        # - 若已在 running loop 里，asyncio.run() 会抛 "cannot be called from a running
        #   event loop"，run_until_complete() 也会报 loop already running → 主协程卡死。
        # - 稳妥方案：在独立线程里用 asyncio.run()（新线程自带新 loop），两种情况都安全。
        box: dict = {}

        def _run() -> None:
            try:
                box["text"] = asyncio.run(stt.transcribe(wav))
            except Exception as e:  # noqa: BLE001
                box["error"] = str(e)

        th = threading.Thread(target=_run, daemon=True)
        th.start()
        th.join(timeout=330)
        if "error" in box:
            print(f"  [字幕校验 ASR 采样失败] {box['error']}")
            asr_text = ""
        else:
            asr_text = box.get("text", "")

    # AI 字幕前 30 秒片段（取前几句）
    ai_lines = [l for l in ai_sub.splitlines() if l.strip()]
    ai_sample = "\n".join(ai_lines[:10])

    # 维度1 一致性：DeepSeek 语义判断 ASR vs 字幕是否表达同一内容
    #（比字符匹配鲁棒：能处理字幕语言≠语音语言，如英文讲解+中文字幕）
    c = deepseek.evaluate_subtitle_consistency(ai_sample, asr_text)

    # 维度2 合理性：DeepSeek 判断字幕内容通顺合理
    r = deepseek.evaluate_subtitle(ai_sample)

    # 逐句入库（以句子为单位统计）
    accepted = c >= threshold and r >= threshold
    for sentence in ai_lines[:10]:
        db.record_subtitle_quality(source, url, sentence, c, r, accepted)
    return accepted


def _text_similarity(a: str, b: str) -> float:
    """文本相似度（0-1）。用字符 bigram 重叠，比字符 Jaccard 更鲁棒。

    AI 字幕 vs ASR 转写：标点/换行不同，但内容相近时 bigram 重叠高。
    """
    if not a or not b:
        return 0.0
    a_clean = re.sub(r"[\[\]\d:：\s，。！？,.!?]+", "", a)[:200]
    b_clean = re.sub(r"[\[\]\d:：\s，。！？,.!?]+", "", b)[:200]
    if not a_clean or not b_clean:
        return 0.0
    # 字符 bigram 集合
    def bigrams(s: str) -> set:
        return {s[i:i+2] for i in range(len(s) - 1)}

    ga, gb = bigrams(a_clean), bigrams(b_clean)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


# ---------- 自适应决策 ----------

def subtitle_should_use_ai(source: str, db, threshold: float = 0.95) -> bool:
    """该来源 AI 字幕是否可信任（合格率 > threshold 大规模采用，只抽查）。"""
    return db.subtitle_should_trust(source, threshold)


# ---------- 来源链解析 ----------

def resolve_subtitle(source_url: str, video: Path, speech: str, config, deepseek,
                     stt, db) -> dict:
    """智能决策字幕来源，返回 {text, source, verified}。

    优先级：
      1) B站视频 → 先试手工 CC 字幕（免费，最高质量）
      2) 长视频(>threshold) → 试 AI 字幕：
           - 该来源历史合格率 >95% → 直接采用（trust）
           - 否则 → 双维度校验，结果入库；通过用 AI，失败回退 ASR
      3) 短视频或无字幕 → ASR 兜底（speech）
    """
    from server import task_db
    from server.ocr import vision

    db = db or task_db
    is_bili = "bilibili.com" in (source_url or "")
    is_youtube = "youtube.com" in (source_url or "")

    # 强制模式
    if config.subtitle_source == "asr":
        return {"text": speech, "source": "asr", "verified": False}
    if config.subtitle_source == "ocr":
        # OCR 字幕（旧行为保留）
        try:
            subtitle, _ = _extract_ocr_subtitle(video, config)
            return {"text": subtitle, "source": "ocr", "verified": False}
        except Exception:
            return {"text": speech, "source": "asr", "verified": False}
    if config.subtitle_source == "off":
        return {"text": "", "source": "off", "verified": False}

    # auto 模式
    # 1) B站/YouTube 手工 CC 字幕（yt-dlp，可并行）
    if is_bili:
        cc = fetch_bilibili_subtitle_cc(source_url)
        if cc:
            return {"text": srt_to_text(cc), "source": "bili_cc", "verified": True}
    if is_youtube:
        from server.subtitle import fetch_youtube_subtitle_ai  # 预留扩展
        cc = fetch_youtube_subtitle_ai(source_url) if hasattr(globals(), "fetch_youtube_subtitle_ai") else ""
        if cc:
            return {"text": srt_to_text(cc), "source": "yt_cc", "verified": True}

    # 2) 长视频 → AI 字幕（双维度校验）
    video_len = _video_duration(video)
    if video_len > config.subtitle_ai_min_sec:
        ai_source = "bili_ai" if is_bili else ("yt_ai" if is_youtube else None)
        if ai_source:
            ai_srt = fetch_bilibili_subtitle_ai(source_url) if is_bili else ""
            if ai_srt:
                ai_text = srt_to_text(ai_srt)
                # 历史合格率高 → 直接采用（trust）
                if subtitle_should_use_ai(ai_source, db, config.subtitle_trust_threshold):
                    return {"text": ai_text, "source": ai_source, "verified": True}
                # 否则双维度校验
                if config.subtitle_verify:
                    ok = verify_subtitle_quality(ai_source, source_url, ai_text, stt, video,
                                                 deepseek, db,
                                                 threshold=config.subtitle_verify_threshold)
                    if ok:
                        return {"text": ai_text, "source": ai_source, "verified": True}
                    print("  [AI字幕校验未通过，回退ASR]")

    # 3) ASR 兜底
    return {"text": speech, "source": "asr", "verified": False}


def _video_duration(video: Path) -> float:
    """视频时长（秒）。"""
    import subprocess as sp

    r = sp.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "csv=p=0", str(video)], capture_output=True, text=True, timeout=30)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def _extract_ocr_subtitle(video: Path, config):
    """OCR 字幕（旧行为，供 SUBTITLE_SOURCE=ocr 用）。"""
    from server.batch_process import extract_subtitles_and_slides

    return extract_subtitles_and_slides(video)


# ---------- opencli 锁（供视频下载兜底使用） ----------

def opencli_locked_open(url: str) -> str:
    """带锁的 opencli 打开页面（视频下载兜底用）。"""
    return _opencli_locked(subprocess.run,
                           ["opencli", "browser", "default", "open", url],
                           capture_output=True, text=True, timeout=60)
