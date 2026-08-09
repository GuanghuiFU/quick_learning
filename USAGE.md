# 视频学习助手 · 详细使用说明

在 Chrome 浏览器看学习视频（B 站、飞书、任意含 `<video>` 的网页），自动把视频内容整理成**带关键截图的图文笔记**存入 Obsidian。

---

## 目录

- [1. 系统架构](#1-系统架构)
- [2. 环境准备](#2-环境准备)
- [3. 配置 `.env`](#3-配置-env)
- [4. 启动本地服务](#4-启动本地服务)
- [5. 使用方式一：Chrome 扩展（实时）](#5-使用方式一chrome-扩展实时)
- [6. 使用方式二：单视频处理](#6-使用方式二单视频处理)
- [7. 使用方式三：批量处理](#7-使用方式三批量处理)
- [8. 关键帧检测原理](#8-关键帧检测原理)
- [9. 输出文件说明](#9-输出文件说明)
- [10. 测试与调试](#10-测试与调试)
- [11. 常见问题](#11-常见问题)

---

## 1. 系统架构

```
┌────────────────────── Chrome 扩展 (MV3) ──────────────────────┐
│  popup        开始/停止学习、状态、字幕/截图计数                │
│  options      OCR/语音开关、优先级、文档类型、自动截图配置       │
│  service-worker  状态机、帧检测循环、分段转写聚合、命令路由       │
│  offscreen    音频捕获 + 回显 + 分段录制转写                    │
│  content-script  页面视频状态监听（含 iframe）                 │
└──────────────────────┬─────────────────────────────────────────┘
                       │ HTTP/WebSocket (localhost:8787)
┌──────────────────────▼─────────────────────────────────────────┐
│  本地 Python 服务 (FastAPI)                                     │
│  OCR      PaddleOCR（字幕带·快） / qwen-vl-ocr（云端）          │
│  STT      千问 qwen-audio ASR（分段 + 时间戳）                  │
│  关键帧   文字指引版检测（guide_capture）                       │
│  总结     DeepSeek（速览 + 笔记 + 重要性判断）                  │
│  输出     Obsidian Markdown 写入                                │
└─────────────────────────────────────────────────────────────────┘
```

**核心数据流**：视频 → ① 音频分段转写（带时间戳）→ ② 字幕 OCR → ③ 文字指引关键帧检测（截图 + OCR）→ ④ DeepSeek 三源融合总结 → ⑤ 写入 Obsidian。

---

## 2. 环境准备

**必需**：
- macOS（使用了 Apple Vision 相关能力，但 OCR 主用 PaddleOCR）
- Python 3.11+
- `ffmpeg`（音频转换、帧采样）：`brew install ffmpeg`

**Python 依赖**：

```bash
cd server
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> `requirements.txt` 已包含 fastapi / uvicorn / pydantic / httpx / pillow / paddlepaddle / paddleocr / pytest。

**Chrome 扩展加载**：
1. 打开 `chrome://extensions`
2. 开启「开发者模式」
3. 点「加载已解压的扩展程序」→ 选择项目 `extension/` 目录

---

## 3. 配置 `.env`

在 `server/` 下创建 `.env`（或复制 `.env.example`），填入你的密钥：

```ini
# ---- DeepSeek LLM（结构化总结 + 画面重要性判断）----
LLM_PROVIDER=deepseek
LLM_MODEL=deepseek-chat
LLM_API_KEY=sk-你的key
LLM_BASE_URL=https://api.deepseek.com

# ---- 千问 audio ASR（语音转写，返回时间戳）----
ASR_PROVIDER=qwen-audio
ASR_MODEL=qwen-audio-3.0-asr-flash
ASR_API_URL=https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation
ASR_API=sk-你的key

# ---- Obsidian 输出 ----
VAULT_PATH=/Users/你的用户名/Documents/Obsidian Vault
NOTES_DIR=学习笔记
ATTACHMENTS_DIR=学习笔记/attachments

# ---- 字幕 OCR 裁剪带（画面高度比例，自顶向下）----
OCR_SUBTITLE_Y0=0.74
OCR_SUBTITLE_Y1=0.98

# ---- OCR 引擎：paddle(本地免费) / qwen-vl(云端效果好) ----
OCR_ENGINE=paddle

# ---- 关键画面评分权重（可选）----
SCORE_W1=0.3
SCORE_W2=0.3
SCORE_W3=0.3
SCORE_W4=0.3
SCORE_THRESHOLD=0.5
```

| 配置 | 说明 |
|------|------|
| `LLM_API_KEY` | DeepSeek，用于总结 + 截图重要性判断（必填） |
| `ASR_API` | 阿里云百炼 DashScope key，用于语音转写（必填，qwen-audio-3.0-asr-flash） |
| `VAULT_PATH` | Obsidian vault 根目录（必填） |
| `OCR_ENGINE` | `paddle`（免费本地）或 `qwen-vl`（云端效果好，走 ASR 的 key） |
| `SCORE_*` | 关键帧评分权重，一般不用改 |

---

## 4. 启动本地服务

```bash
cd server
source .venv/bin/activate
uvicorn server.app:app --host 127.0.0.1 --port 8787
```

验证：浏览器访问 `http://127.0.0.1:8787/health` 应返回 `{"status":"ok"}`。

> 服务必须保持运行，扩展和批处理脚本都依赖它。

---

## 5. 使用方式一：Chrome 扩展（实时）

**适用**：边看视频边自动积累字幕 + 关键截图，视频结束自动出笔记。

1. **确保本地服务已启动**（见第 4 节）。
2. 打开任意含视频的网页（B 站、飞书等）。
3. 点扩展图标 → **「开始学习」**（或按快捷键 `⌘⇧E`）。
4. 正常看视频，后台自动：
   - 每 4.5 分钟把累积音频转写为带时间戳文字（不打断播放）
   - 每 2 秒截一帧做字幕 OCR + 关键帧检测
   - 检测到讲解关键画面自动截图
5. 看到特别重要的画面，按 `⌘⇧S` **手动截图**。
6. 视频播完，自动生成学习文档 → Obsidian 通知提示路径。

**扩展设置**（右键扩展图标 → 选项）：
| 设置 | 说明 |
|------|------|
| 字幕通道（OCR） | 有字幕视频开（B 站），无字幕可关省资源 |
| 语音通道（转写） | 无字幕视频（飞书）开，有字幕可关省 API 成本 |
| 总结优先级 | 字幕为主 / 语音为主 / 两者并重 |
| 文档类型 | 速览 / 完整笔记 / 两者都要 |
| 自动截取关键画面 | 讲 PPT 时开 |

---

## 6. 使用方式二：单视频处理

**适用**：已有视频文件（本地或下载的），离线处理。

```bash
cd server
source .venv/bin/activate

# 方式 A：完整处理（转写 + 字幕 + 关键帧 + 文档）
python -m server.tests.test_resume [视频路径] [文字稿路径]

# 方式 B：已有带时间戳文字稿，只做字幕 + 关键帧 + 文档（推荐复用）
python -m server.tests.test_guide [--seconds N]
```

**`test_resume`**（完整流程）：
- 参数 1：视频路径（默认 `test/videos/飞书AI.mp4`）
- 参数 2：文字稿 md 路径（默认读取 Obsidian 里的文字稿）
- 执行：读文字稿 → 帧采样字幕 OCR → 关键帧检测 → 速览 + 笔记 → 写 Obsidian

**`test_guide`**（文字指引版关键帧，最常用）：
- `--seconds 300`：只处理前 300 秒（快速测试）
- 不带参数：处理全部
- 执行：文字稿时间戳 → 讲解重要性预筛 → 稳定候选帧 → 信息量精判 → 关键图 → 速览

---

## 7. 使用方式三：批量处理（多视频文件夹）

**适用**：一个文件夹里有多个视频，全部自动处理并生成笔记——**自动记重点**。

```bash
cd server
source .venv/bin/activate
python -m server.batch_process /path/to/videos [--priority stt] [--mode both]
```

| 参数 | 说明 |
|------|------|
| `input` | 视频文件、包含视频的文件夹，或视频 URL（配合 `--url`） |
| `--priority` | `ocr` / `stt` / `both`，总结时素材优先级 |
| `--mode` | `note` / `speedread` / `both`，生成文档类型 |
| `--course` | **课程名**：笔记写入 `学习笔记/<course>/` 子文件夹，各课程独立 |
| `--url` | `input` 是视频 URL，先自动下载再处理 |
| `--dl-method` | 下载方式：`auto`(先 yt-dlp 后 opencli 浏览器兜底) / `ytdlp` / `opencli` |
| `--dl-quality` | 下载清晰度（360/480/720/1080） |

支持的格式：mp4 / webm / mkv / mov / m4a / mp3 / wav / flv / avi。无音轨的视频自动跳过转写（只做字幕 + 关键帧）。

**每个视频的处理流程**（与单视频一致）：
1. 分段转写（带时间戳）→ 保存 `_文字稿.md`
2. 帧采样字幕 OCR → 字幕文本
3. **文字指引版关键帧检测**（讲解预筛 → 稳定候选 → 信息量精判）→ 关键图存 vault
4. DeepSeek 生成速览 + 笔记（关键图内嵌）→ 写 Obsidian

**视频下载（两种方式自动切换）**：
- **yt-dlp**（优先）：通用下载，支持多站点
- **opencli 浏览器**（兜底）：通过浏览器桥接复用你已登录的 Chrome 会话，从页面 `__playinfo__` 提取视频流下载。**能下会员/付费内容，几乎不触发反爬**（前提：opencli daemon + 扩展已连接）

**英文视频支持**：自动检测转写语言，英文视频会额外生成：
- `_文字稿.md`（英文原文，带时间戳）
- `_文字稿_双语.md`（**英文 + 中文翻译对照**）
- 速览 + 笔记均为**中文**（术语带英文）

**示例**：
```bash
# 处理 URL，自动下载（yt-dlp优先，失败走opencli浏览器）
python -m server.batch_process "https://www.bilibili.com/video/BV1JTCQBQERg/" --url --course "FastAPI一小时"

# 处理文件夹，指定课程名
python -m server.batch_process /Users/me/videos --course "FastAPI一小时" --mode both

# 强制用 opencli 浏览器下载（会员内容）
python -m server.batch_process "https://..." --url --dl-method opencli --dl-quality 720
```

> **课程子文件夹**：用 `--course` 后，该课程的所有文档（笔记/速览/文字稿）和关键图（attachments）都放在 `学习笔记/<course>/` 下，各课程互不混淆。

> 中间产物（候选帧/关键图/评分）自动落在 `test/screenshots/<标题>/guide/`。

---

## 8. 关键帧检测原理（文字指引版）

核心思路：**用带时间戳的文字稿判断"讲到哪里关键"，只在关键讲解时段寻找画面稳定的帧**。

```
① 讲解重要性预筛：DeepSeek 评估每句文字稿 importance ≥ 0.5 → 关键时段
② 只采样关键时段 → 用相邻帧差判断"画面稳定停留"（连续多帧变化小）
   → 讲师在展示某张图/PPT 时画面才稳定 → 关键候选
③ OCR 内容量过滤：文字过少（空白/水印/台标）→ 排除
④ 与文字稿重复度过滤：OCR 与讲解几乎一样（纯口播无图）→ 排除
⑤ 与已选图去重：避免重复截同画面
⑥ assess_frame_worthiness 精判：画面必须含独立结构信息
   （图表/列表/界面/流程），课程封面/单句结论/提醒 → 排除
⑦ 评分过阈值才保存：
   Score = w1·Svisual + w2·Socr + w3·Stranscript − w4·Sdup_shot − w5·Sdup_transcript
```

**优势**：相比逐帧扫描，**VLM/OCR 调用减少约 80%**（只对预筛后的候选精判），同时过滤掉"封面/单句结论"等低信息量画面。

**中间产物**（便于检查和 debug）：
```
test/screenshots/<标题>/guide/
├── candidates/   采样候选帧（按时间戳）
├── selected/     判定为关键的画面
└── scores.csv    每帧评分明细
```

---

## 9. 输出文件说明

写入 `VAULT_PATH/<NOTES_DIR>/`（若用了 `--course`，则为 `VAULT_PATH/<NOTES_DIR>/<课程名>/`）：

```
学习笔记/
├── 飞书AI/                      ← 课程A子文件夹（--course "飞书AI"）
│   ├── 2026-08-08_标题_速览.md
│   ├── 2026-08-08_标题.md
│   ├── 2026-08-08_标题_文字稿.md
│   └── attachments/              ← 该课程的关键图
│       ├── 标题_guide_00_01.jpg
│       └── ...
├── FastAPI一小时/                ← 课程B子文件夹
│   └── ...
└── 2026-08-08_某视频.md          ← 未指定 course 时直接放根目录
```

| 文件 | 命名 | 内容 |
|------|------|------|
| **速览** | `YYYY-MM-DD_标题_速览.md` | 核心结论 + 分节知识点（**关键图内嵌**）+ 术语表 + 回看时间戳 |
| **完整笔记** | `YYYY-MM-DD_标题.md` | 概述 + 分节要点 + 代码/示例 + 疑问行动项（关键图内嵌） |
| **文字稿** | `YYYY-MM-DD_标题_文字稿.md` | 完整语音转写，**按句带 `[MM:SS]` 时间戳** |

**关键图**：保存到该课程子目录的 `attachments/` 下，命名 `视频名_guide_MM_SS.jpg`，笔记/速览通过 `![[视频名_guide_MM_SS.jpg]]` **内嵌到对应知识点小节**（不单独设截图章节）。图片命名带视频前缀，多视频同时间戳不冲突。

**frontmatter**：所有文档含 `title` / `source` / `date` / `created`（生成时间）/ `tags`。

---

## 10. 测试与调试

```bash
cd server
source .venv/bin/activate

# 单元测试
python -m pytest server/tests/ -q

# 各环节独立测试
python -m server.tests.test_stt          # ASR 转写 + 时间戳
python -m server.tests.test_e2e          # 端到端（OCR→总结→笔记）
python -m server.tests.test_guide --seconds 300  # 文字指引关键帧（前5分钟）
python -m server.tests.rescreen_slides   # 对已选关键图再筛（信息量精判）
python -m server.tests.debug_capture     # 全流程 + 中间产物落盘
```

**调试建议**：
- 关键图不全 → 看 `test/screenshots/<标题>/guide/selected/` 与 `scores.csv`
- 配图质量差 → 调 `SCORE_THRESHOLD`（默认 0.5，调高更严格）
- 想用云端 OCR → `.env` 设 `OCR_ENGINE=qwen-vl`
- 字幕带位置不对 → 调 `OCR_SUBTITLE_Y0/Y1`

**测试数据约定**：测试视频放 `test/videos/`（gitignore，不提交）；中间产物放 `test/screenshots/`。

---

## 11. 常见问题

**Q1: Obsidian 里图片看不到？**
确保 `attachments/` 目录下的图片文件名与笔记里 `![[文件名]]` 一致。关键图由程序自动保存到 attachments，手动复制的图要核对文件名。

**Q2: 为什么有些视频关键图很少/没有？**
关键帧检测按"信息量"过滤——纯口播讲解（画面无图表/列表/界面）或单句结论的视频，符合条件的关键画面本就少。这是**正确行为**，不是 bug。

**Q3: 语音转写很慢？**
转写是分段的（每 4.5 分钟一段调一次 ASR），每段约需数秒到数十秒。长视频整体需要几分钟。关掉语音通道（扩展设置）可完全跳过。

**Q4: DeepSeek / 千问 的 API 费用？**
- DeepSeek：总结 + 关键帧判断，调用次数取决于视频长度（每帧 OCR 判断一次）
- 千问 ASR：分段转写，17 分钟视频约 4 次调用
- 千问 qwen-vl-ocr：仅当 `OCR_ENGINE=qwen-vl` 时才用
- 文字指引版已把关键帧判断调用减少约 80%，费用可控

**Q5: 扩展截图快捷键冲突？**
Chrome 限制命令 ≤ 4 个，Mac 需带 `⇧`。可在 `chrome://extensions/shortcuts` 重新绑定。

**Q6: 视频无音轨（纯画面）？**
`batch_process` 会自动跳过转写，只做字幕 OCR + 关键帧。需要有文字稿才能出文档。

**Q7: 支持哪些网站？**
任意含 `<video>` 元素的网页（B 站、飞书、通用 HTML5 视频站）。跨源 iframe 内的视频，`ended` 自动检测可能失效，但音频捕获和截图仍可用。
