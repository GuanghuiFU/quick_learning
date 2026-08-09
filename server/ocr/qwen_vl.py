"""qwen-vl-ocr：DashScope 云端 OCR（千问视觉模型）。

用于整图文字提取 / 判断画面是否有重要文字。
- 无文字画面返回 "0"
- 有文字画面返回识别全文
"""
from __future__ import annotations

from pathlib import Path

import httpx

UPLOAD_URL = "https://dashscope.aliyuncs.com/api/v1/files"
GEN_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
MODEL = "qwen-vl-ocr"


class QwenVlOCR:
    def __init__(self, api_key: str, model: str = MODEL) -> None:
        self.api_key = api_key
        self.model = model

    def _headers(self, with_content: bool = False) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self.api_key}"}
        if with_content:
            h["Content-Type"] = "application/json"
            h["X-DashScope-SSE"] = "disable"
        return h

    def _upload_get_url(self, path: Path) -> str:
        with path.open("rb") as f:
            files = {"file": (path.name, f, "image/png")}
            resp = httpx.post(UPLOAD_URL, headers=self._headers(), files=files, timeout=120)
        resp.raise_for_status()
        data = resp.json().get("data", {})
        file_id = (data.get("uploaded_files") or [{}])[0].get("file_id")
        if not file_id:
            raise RuntimeError(f"上传失败: {resp.text[:300]}")
        info = httpx.get(f"{UPLOAD_URL}/{file_id}", headers=self._headers(), timeout=60)
        info.raise_for_status()
        return info.json()["data"]["url"]

    def ocr_text(self, image_path: Path | str) -> str:
        """识别整图文字。无文字返回空串。"""
        path = Path(image_path)
        url = self._upload_get_url(path)
        payload = {
            "model": self.model,
            "input": {
                "messages": [
                    {"role": "user", "content": [{"type": "image", "image": url}]}
                ]
            },
        }
        resp = httpx.post(GEN_URL, json=payload, headers=self._headers(with_content=True), timeout=180)
        resp.raise_for_status()
        data = resp.json()
        try:
            content = data["output"]["choices"][0]["message"]["content"][0]
        except (KeyError, IndexError, TypeError):
            return ""
        text = content.get("ocr_result", {}).get("processed_text", "") or content.get("text", "")
        # qwen-vl-ocr 无文字时返回 "0"
        if text.strip() in ("0", ""):
            return ""
        return text.strip()

    def has_text(self, image_path: Path | str) -> bool:
        """画面是否有文字（用于幻灯片筛选，替代 DeepSeek 判断前置过滤）。"""
        return bool(self.ocr_text(image_path))
