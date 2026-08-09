"""Qwen3.7-Flash 视觉判断：直接看图片判断是否值得作为学习关键截图。

对比纯文本 OCR 判断（DeepSeek），视觉模型能看到真实画面（布局/图表/界面），
能准确区分"架构图/教学大纲/功能列表"（重要）和"文件管理器/终端/水印"（不重要）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import httpx

UPLOAD_URL = "https://dashscope.aliyuncs.com/api/v1/files"
GEN_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
MODEL = "qwen3.7-flash"

# 判断 prompt：明确"值得"与"不值得"的类别
_ASSESS_PROMPT = """这张图片是视频中的一帧。判断它是否值得作为学习笔记的"关键截图"。

值得（worthy=true）：画面包含独立的结构化学习内容，即使没有讲解也能看懂。包括：
- 架构图、流程图、数据流图
- 教学大纲/课程目标页（含工具、步骤、要求）
- 功能列表、对比表格、操作界面（含具体配置/命令/代码展示）
- 图表、数据可视化

不值得（worthy=false）：
- 文件管理器/资源管理器、终端命令行输入中、导航栏/菜单栏截图
- 纯人物/讲师特写、过渡画面、水印/台标
- 内容被遮挡或几乎空白

只返回 JSON：{"worthy": true/false, "reason": "一句话原因"}，不要多余文字。"""


class QwenVision:
    def __init__(self, api_key: str, model: str = MODEL) -> None:
        self.api_key = api_key
        self.model = model

    def _headers(self, with_content: bool = False) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self.api_key}"}
        if with_content:
            h["Content-Type"] = "application/json"
            h["X-DashScope-SSE"] = "disable"
        return h

    def _upload_get_url(self, image_path: Path) -> str:
        with image_path.open("rb") as f:
            files = {"file": (image_path.name, f, "image/jpeg")}
            resp = httpx.post(UPLOAD_URL, headers=self._headers(), files=files, timeout=60)
        resp.raise_for_status()
        data = resp.json().get("data", {})
        uploaded = (data.get("uploaded_files") or [{}])[0]
        file_id = uploaded.get("file_id")
        if not file_id:
            raise RuntimeError(f"上传失败: {resp.text[:300]}")
        info = httpx.get(f"{UPLOAD_URL}/{file_id}", headers=self._headers(), timeout=30)
        info.raise_for_status()
        return info.json()["data"]["url"]

    def assess(self, image_path: Path | str) -> dict:
        """视觉判断图片是否值得作为关键截图。返回 {"worthy": bool, "reason": str}。"""
        path = Path(image_path)
        if not path.exists():
            return {"worthy": False, "reason": f"图片不存在: {path}"}
        url = self._upload_get_url(path)
        payload = {
            "model": self.model,
            "input": {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "image", "image": url},
                            {"type": "text", "text": _ASSESS_PROMPT},
                        ],
                    }
                ]
            },
        }
        # 超时 + 重试（避免 API 偶发卡死/限流导致处理停滞）
        last_err = None
        for attempt in range(3):
            try:
                resp = httpx.post(
                    GEN_URL, json=payload, headers=self._headers(with_content=True),
                    timeout=90,
                )
                if resp.status_code == 429:
                    import time

                    time.sleep(5 * (attempt + 1))
                    continue
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                if attempt < 2:
                    import time

                    time.sleep(3 * (attempt + 1))
                else:
                    return {"worthy": False, "reason": f"视觉API调用失败: {str(e)[:60]}"}
        else:
            return {"worthy": False, "reason": f"视觉API失败: {last_err}"}
        try:
            text = data["output"]["choices"][0]["message"]["content"][0]["text"]
        except (KeyError, IndexError, TypeError):
            return {"worthy": False, "reason": "响应解析失败"}
        # 提取 JSON
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return {"worthy": False, "reason": f"非 JSON 响应: {text[:80]}"}
        try:
            d = json.loads(m.group(0))
            return {"worthy": bool(d.get("worthy", False)), "reason": str(d.get("reason", ""))}
        except Exception:  # noqa: BLE001
            return {"worthy": False, "reason": f"JSON 解析失败: {text[:80]}"}

    def assess_anomaly(self, image_path: Path | str) -> dict:
        """仅检测画面是否异常（花屏/全黑/严重遮挡/完全空白），不判断内容是否重要。

        返回 {"anomaly": bool, "reason": str}。默认不拒绝（permissive），
        重要与否由本地规则（OCR内容量+复杂度）决定，避免视觉API误拒有效画面。
        """
        path = Path(image_path)
        if not path.exists():
            return {"anomaly": False, "reason": f"图片不存在: {path}"}
        url = self._upload_get_url(path)
        prompt = """这张图片是视频中的一帧。只判断画面是否有**技术异常**：
- 花屏/马赛克/撕裂、全黑、全白、严重遮挡、摄像头被挡住 → anomaly=true
- 画面正常（即使是纯讲解/普通画面）→ anomaly=false

不要判断内容是否重要。只返回 JSON：{"anomaly": true/false, "reason": "原因"}"""
        payload = {
            "model": self.model,
            "input": {
                "messages": [
                    {"role": "user", "content": [
                        {"type": "image", "image": url},
                        {"type": "text", "text": prompt},
                    ]}
                ]
            },
        }
        try:
            resp = httpx.post(GEN_URL, json=payload, headers=self._headers(with_content=True), timeout=90)
            resp.raise_for_status()
            data = resp.json()
            text = data["output"]["choices"][0]["message"]["content"][0]["text"]
        except Exception as e:  # noqa: BLE001
            return {"anomaly": False, "reason": f"视觉检查失败: {str(e)[:40]}"}
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return {"anomaly": False, "reason": f"非 JSON: {text[:60]}"}
        try:
            d = json.loads(m.group(0))
            return {"anomaly": bool(d.get("anomaly", False)), "reason": str(d.get("reason", ""))}
        except Exception:  # noqa: BLE001
            return {"anomaly": False, "reason": f"JSON 解析失败: {text[:60]}"}
