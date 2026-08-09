"""视频下载器：先试 yt-dlp，失败则用 opencli 浏览器方式（模拟浏览器/猫抓路线）。

opencli 方式：通过浏览器桥接打开视频页 → eval 提取 __playinfo__ → 下载视频+音频分片 → ffmpeg 合并。
优势：复用已登录的浏览器会话，能下会员/付费内容，几乎不触发反爬。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path


def _find_ytdlp() -> str:
    """找 yt-dlp：先 PATH，再项目 venv。"""
    p = shutil.which("yt-dlp")
    if p:
        return p
    # 项目 venv 路径
    project = Path(__file__).resolve().parents[1]
    for cand in [project / ".venv" / "bin" / "yt-dlp"]:
        if cand.exists():
            return str(cand)
    raise RuntimeError("未找到 yt-dlp，请安装或指定路径")


def download_ytdlp(url: str, out_dir: Path, quality: str = "360") -> Path | None:
    """用 yt-dlp 下载单个视频。成功返回视频路径，失败返回 None。"""
    yt = _find_ytdlp()
    out_dir.mkdir(parents=True, exist_ok=True)
    # 优先 H264(avc1) 编码（ffmpeg 4.2 无 AV1 解码），再退化到任意
    cmd = [
        yt, "-f", f"bv*[height<={quality}][vcodec^=avc1]+ba/bv*[height<={quality}]+ba/b",
        "--merge-output-format", "mp4",
        "-o", str(out_dir / "%(title)s.%(ext)s"),
        "--no-playlist",
        url,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        print(f"  [yt-dlp 失败] {r.stderr.strip()[-200:]}")
        return None
    # 找下载的文件
    mp4s = list(out_dir.glob("*.mp4"))
    if not mp4s:
        return None
    return max(mp4s, key=lambda p: p.stat().st_mtime)


def download_playlist_ytdlp(url: str, out_dir: Path, quality: str = "360") -> list[Path]:
    """用 yt-dlp 下载整个播放列表。多格式策略依次尝试，返回下载的视频列表。"""
    yt = _find_ytdlp()
    out_dir.mkdir(parents=True, exist_ok=True)
    # 依次尝试的格式选择（B 站不同视频格式 ID 可能不同）
    format_specs = [
        "30016+30216",  # B站 360p H264 + 音频（通用，最稳）
        f"bv*[height<={quality}][vcodec^=avc1]+ba/bv*[height<={quality}]+ba/b",  # H264 通用
        "30032+30232",  # B站 480p H264 + 音频
        "b",            # 默认最佳
    ]
    # 输出名：先简单 NN_pNN（避免含课程全名导致过长/重复），后续由 _rename_with_theme 生成清晰主题名
    out_tpl = str(out_dir / "%(playlist_index)02d_p%(playlist_index)02d.%(ext)s")
    for fmt in format_specs:
        cmd = [
            yt, "-f", fmt,
            "--merge-output-format", "mp4",
            "-o", out_tpl,
            url,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        vids = sorted(out_dir.glob("*.mp4"))
        if vids:
            print(f"  [格式 {fmt}] 下载成功 {len(vids)} 集")
            return vids
        print(f"  [格式 {fmt}] 失败: {r.stderr.strip()[-120:]}")
    return []


def _opencli_eval(js: str) -> str:
    """通过 opencli 在浏览器页面执行 JS，返回结果文本。"""
    r = subprocess.run(
        ["opencli", "browser", "default", "eval", js],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        raise RuntimeError(f"opencli eval 失败: {r.stderr.strip()[-200:]}")
    return r.stdout.strip()


def download_opencli(url: str, out_dir: Path, quality: str = "340") -> Path | None:
    """用 opencli 浏览器方式下载。提取 __playinfo__ → 下载分片 → 合并。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        # 1) 打开视频页
        r = subprocess.run(
            ["opencli", "browser", "default", "open", url],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            print(f"  [opencli open 失败] {r.stderr.strip()[-200:]}")
            return None
        import time

        time.sleep(5)  # 等页面加载

        # 2) 提取 __playinfo__ 里指定分辨率的视频 + 音频 URL
        js = f"""(() => {{
          const p = window.__playinfo__;
          if (!p) return "NO_PLAYINFO";
          const dash = (p.data || {{}}).dash || {{}};
          const vids = dash.video || [];
          const auds = dash.audio || [];
          if (!vids.length) return "NO_VIDEO_STREAM";
          // 选不超过目标高度的最高分辨率，否则取最低
          let vid = vids.filter(v => (v.height || 0) <= {quality})
                        .sort((a,b) => (b.height||0)-(a.height||0))[0] || vids[vids.length-1];
          const aud = auds[0];
          return JSON.stringify({{
            videoUrl: vid.baseUrl,
            audioUrl: aud ? aud.baseUrl : null,
            videoHeight: vid.height
          }});
        }})()"""
        info_text = _opencli_eval(js)
        if "NO_PLAYINFO" in info_text or "NO_VIDEO_STREAM" in info_text:
            print(f"  [opencli 无播放信息] {info_text}")
            return None
        info = json.loads(info_text)
        video_url, audio_url = info["videoUrl"], info.get("audioUrl")

        # 3) 下载分片（需 Referer + UA）
        headers = [
            "-H", "Referer: https://www.bilibili.com/",
            "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        ]
        title = _extract_title(url)
        v_file = out_dir / f"{title}_video.m4s"
        a_file = out_dir / f"{title}_audio.m4s"
        for fn, u in [(v_file, video_url), (a_file, audio_url)]:
            if not u:
                continue
            subprocess.run(
                ["curl", "-sL", "-o", str(fn), "--max-time", "300"] + headers + [u],
                capture_output=True, timeout=360,
            )
        # 4) 合并为 mp4
        out_mp4 = out_dir / f"{title}.mp4"
        if v_file.exists() and a_file.exists() and v_file.stat().st_size > 1000:
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(v_file), "-i", str(a_file), "-c", "copy", str(out_mp4)],
                capture_output=True, timeout=300,
            )
            v_file.unlink(missing_ok=True)
            a_file.unlink(missing_ok=True)
            if out_mp4.exists():
                return out_mp4
        elif v_file.exists() and v_file.stat().st_size > 1000 and not a_file.exists():
            # 无音频，直接用视频
            v_file.rename(out_mp4)
            return out_mp4
    except Exception as e:  # noqa: BLE001
        print(f"  [opencli 下载异常] {e}")
    return None


def _extract_title(url: str) -> str:
    """从 URL 提取标题（BV号兜底）。"""
    m = re.search(r"video/(BV\w+)", url)
    if m:
        return m.group(1)
    m = re.search(r"/([^/]+)/?$", url)
    return (m.group(1) if m else "video")[:60]


def download_video(url: str, out_dir: Path, quality: str = "360",
                   method: str = "auto") -> Path | None:
    """下载视频。method: auto(先yt-dlp后opencli) | ytdlp | opencli。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    if method in ("auto", "ytdlp"):
        print(f"  [尝试 yt-dlp 下载] {url}")
        p = download_ytdlp(url, out_dir, quality)
        if p:
            return p
        if method == "ytdlp":
            return None
    if method in ("auto", "opencli"):
        print(f"  [yt-dlp 失败，尝试 opencli 浏览器下载] {url}")
        p = download_opencli(url, out_dir, quality)
        if p:
            return p
    return None
