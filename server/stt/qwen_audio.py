"""千问 qwen-audio-3.0-asr-flash 语音转写实现。

链路：上传音频到 DashScope -> 获取临时 URL -> 传给 ASR 的 input_audio.data。
（直接 base64 内联会因参数过长失败；file_id 字段模型未支持。）
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from .base import STTProvider

FILE_UPLOAD_URL = "https://dashscope.aliyuncs.com/api/v1/files"


class QwenAudioSTT(STTProvider):
    def __init__(self, api_key: str, model: str, api_url: str) -> None:
        self.api_key = api_key
        self.model = model
        self.api_url = api_url

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    async def _upload_get_url(self, audio_path: Path, client: httpx.AsyncClient) -> str:
        """上传音频文件，返回临时可访问 URL。"""
        with audio_path.open("rb") as f:
            files = {"file": (audio_path.name, f, "audio/wav")}
            resp = await client.post(FILE_UPLOAD_URL, headers=self._headers(), files=files)
        resp.raise_for_status()
        data = resp.json().get("data", {})
        uploaded = (data.get("uploaded_files") or [{}])[0]
        file_id = uploaded.get("file_id")
        if not file_id:
            raise RuntimeError(f"上传失败: {resp.text[:300]}")

        # 单文件 GET 拿临时 URL；429 限流时短暂重试
        for attempt in range(3):
            info = await client.get(f"{FILE_UPLOAD_URL}/{file_id}", headers=self._headers())
            if info.status_code != 429:
                break
            await asyncio.sleep(3 * (attempt + 1))
        info.raise_for_status()
        url = info.json()["data"].get("url")
        if not url:
            raise RuntimeError(f"无法获取文件 URL: {info.text[:300]}")
        return url

    async def transcribe(self, audio_path: Path) -> str:
        """转写音频，返回全文文本。"""
        segments = await self.transcribe_segments(audio_path)
        return "\n".join(s["text"] for s in segments)

    async def transcribe_segments(self, audio_path: Path) -> list[dict]:
        """转写音频，返回带时间戳的分段列表。

        每段: {"text": str, "begin_ms": int, "end_ms": int, "words": [...带词级时间戳]}
        用于文字稿按句分段，以及定位关键画面。
        """
        if not audio_path.exists():
            raise FileNotFoundError(f"音频不存在: {audio_path}")

        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=30.0)) as client:
            audio_url = await self._upload_get_url(audio_path, client)

            payload = {
                "model": self.model,
                "input": {
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "input_audio", "input_audio": {"data": audio_url}}
                            ],
                        }
                    ]
                },
                "parameters": {"format": "wav", "sample_rate": "16000"},
            }
            headers = {**self._headers(), "Content-Type": "application/json", "X-DashScope-SSE": "disable"}
            resp = await client.post(self.api_url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

        # qwen-audio-3.0-asr-flash 返回：
        # output.output.sentence = 单句 {begin_time, end_time, text, words[]}
        # words[] 逐词带 begin_time/end_time/punctuation
        inner = data.get("output", {}).get("output", {})
        sentence = inner.get("sentence")
        if not sentence or not sentence.get("text"):
            raise RuntimeError(f"ASR 返回结构异常: {json.dumps(data, ensure_ascii=False)[:500]}")

        words = sentence.get("words") or []
        if not words:
            # 无词级时间戳 → 整段作为一个分段
            return [{
                "text": sentence["text"],
                "begin_ms": sentence.get("begin_time", 0),
                "end_ms": sentence.get("end_time", 0),
                "words": [],
            }]

        # 用词级时间戳聚合成分句：遇到句末标点（中英文）切分
        sentences = []
        cur_words = []
        _SENT_END = ("。", "！", "？", "…", ".", "!", "?", "...")
        for w in words:
            cur_words.append(w)
            if w.get("punctuation") in _SENT_END:
                text = "".join(x.get("text", "") for x in cur_words)
                begin = cur_words[0].get("begin_time", 0)
                end = cur_words[-1].get("end_time", begin)
                sentences.append({
                    "text": text,
                    "begin_ms": begin,
                    "end_ms": end,
                    "words": cur_words,
                })
                cur_words = []
        # 尾部未闭合的句子
        if cur_words:
            text = "".join(x.get("text", "") for x in cur_words)
            begin = cur_words[0].get("begin_time", 0)
            end = cur_words[-1].get("end_time", begin)
            sentences.append({"text": text, "begin_ms": begin, "end_ms": end, "words": cur_words})
        if not sentences:
            sentences = [{
                "text": sentence["text"],
                "begin_ms": sentence.get("begin_time", 0),
                "end_ms": sentence.get("end_time", 0),
                "words": words,
            }]
        return sentences
