"""应用配置。从 .env 读取，密钥不硬编码。"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # .env 里是 LLM_* / ASR_* 全大写下划线键，映射到字段
        case_sensitive=False,
    )

    # ---- DeepSeek LLM（结构化总结） ----
    llm_provider: str = "deepseek"
    llm_model: str = "deepseek-chat"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"

    # ---- 千问 audio ASR（语音转写） ----
    asr_provider: str = "qwen-audio"
    asr_model: str = "qwen-audio-3.0-asr-flash"
    asr_api_url: str = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
    asr_api_key: str = Field(default="", validation_alias="ASR_API")

    # ---- Obsidian 笔记输出 ----
    vault_path: str = ""
    notes_dir: str = "学习笔记"                   # vault 内笔记子目录
    attachments_dir: str = "学习笔记/attachments"

    # ---- 字幕 OCR 裁剪带（视频画面高度比例，自顶向下） ----
    ocr_subtitle_y0: float = 0.74
    ocr_subtitle_y1: float = 0.98

    # ---- 视频下载配置 ----
    download_quality: str = "720"    # 下载清晰度 360/480/720/1080，.env: DOWNLOAD_QUALITY
    download_method: str = "auto"    # 下载方式 auto/ytdlp/opencli，.env: DOWNLOAD_METHOD

    # ---- 截图中间产物目录（assets/screenshots，供检查） ----
    screenshots_dir: str = "assets/screenshots"

    # ---- OCR 引擎：paddle (本地免费) | qwen-vl (云端，效果好) ----
    ocr_engine: str = "paddle"   # .env: OCR_ENGINE

    # ---- 关键截图评分权重（Score = w1*Svisual + w2*Socr + w3*Stranscript - w4*Sduplicate） ----
    score_w1: float = 0.3       # 视觉变化
    score_w2: float = 0.3       # OCR 内容重要性
    score_w3: float = 0.3       # 讲解内容重要性
    score_w4: float = 0.3       # 与已选画面重复度
    score_threshold: float = 0.5  # 超过才保存

    @property
    def notes_root(self) -> Path:
        return Path(self.vault_path) / self.notes_dir

    @property
    def attachments_root(self) -> Path:
        return Path(self.vault_path) / self.attachments_dir


settings = Settings()
