# 视频学习助手 · 详细使用说明

在 Chrome 浏览器看学习视频（B 站、飞书、任意含 `<video>` 的网页），自动把视频内容整理成**带关键截图的图文笔记**存入 Obsidian。

---

## 目录

- [1. 系统架构](#1-系统架构)
- [2. 环境准备](#2-环境准备)
- [3. 配置 `.env`](#3-配置-env)
- [4. 启动本地服务](#4-启动本地服务)
- [5. 使用方式一：Chrome 扩展（实时）](#5-使用方式一chrome-扩展实时)
- [6. 使用方式二：单视频 / URL 处理](#6-使用方式二单视频--url-处理)
- [7. 使用方式三：批量与多课程并行](#7-使用方式三批量与多课程并行)
- [8. 视频下载](#8-视频下载)
- [9. 关键帧检测原理](#9-关键帧检测原理)
- [10. 断点续传与可靠性](#10-断点续传与可靠性)
- [11. 输出文件说明](#11-输出文件说明)
- [12. 测试与调试](#12-测试与调试)
- [13. 常见问题](#13-常见问题)

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
                       │ HTTP (localhost:8787)
┌──────────────────────▼─────────────────────────────────────────┐
│  本地 Python 服务 (FastAPI)                                     │
│  下载     yt-dlp 优先 → opencli 浏览器兜底                      │
│  OCR      PaddleOCR（字幕带·快） / qwen-vl-ocr（云端）          │
│  STT      千问 qwen-audio ASR（分段 + 时间戳）                  │
│  关键帧   文字指引版检测 + Qwen3.7-Flash 视觉精判               │
│  总结     DeepSeek（速览 + 笔记 + 课程总结 + 中英翻译）          │
│  状态     SQLite 断点续传（task_db）                            │
│  输出     Obsidian Markdown 写入（课程子文件夹）                │
└─────────────────────────────────────────────────────────────────┘
```

**核心数据流**：视频 → ① 音频分段转写（带时间戳）→ ② 字幕 OCR → ③ 文字指引关键帧检测 → ④ DeepSeek 总结（英文自动翻译中文）→ ⑤ 写入 Obsidian。

---

## 2. 环境准备

**必需**：
- macOS（PaddleOCR 支持；其他平台需适配）
- Python 3.11+
- `ffmpeg`（音频转换、帧采样）：`brew install ffmpeg`
- （可选）`yt-dlp` 和 `opencli` 用于视频下载

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
```

| 配置 | 说明 |
|------|------|
| `LLM_API_KEY` | DeepSeek，用于总结 + 翻译 + 课程总结（必填） |
| `ASR_API` | 阿里云百炼 DashScope key，用于语音转写 + 视觉判断（必填） |
| `VAULT_PATH` | Obsidian vault 根目录（必填） |
| `OCR_ENGINE` | `paddle`（免费本地）或 `qwen-vl`（云端效果好） |
| `SCORE_*` | 关键帧评分权重（可选，一般不用改） |

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

## 6. 使用方式二：单视频 / URL 处理

**适用**：已有视频文件，或直接给视频 URL。

```bash
cd server
source .venv/bin/activate

# 处理本地视频（完整流程：转写 + 字幕 + 关键帧 + 中英文档）
python -m server.batch_process /path/to/video.mp4 --course "课程名" --mode both

# 处理视频 URL（自动下载：yt-dlp 优先，opencli 浏览器兜底）
python -m server.batch_process "https://www.bilibili.com/video/BVxxx" --url --course "课程名"

# 复用已有文字稿，只做字幕 + 关键帧 + 文档（省转写 API）
python -m server.tests.test_guide --seconds 300
```

**`test_guide`**（文字指引版关键帧，最常用）：
- `--seconds 300`：只处理前 300 秒（快速测试）
- 不带参数：处理全部
- 执行：文字稿时间戳 → 讲解重要性预筛 → 稳定候选帧 → 视觉精判 → 关键图 → 文档

**断点续传**：已处理过的集（有完整版笔记或数据库记录）自动跳过，不会重复转写/总结。

---

## 7. 使用方式三：批量与多课程并行

### 批量处理文件夹

```bash
python -m server.batch_process /path/to/videos --course "课程名" --mode both
```

| 参数 | 说明 |
|------|------|
| `input` | 视频文件、文件夹，或 URL（配合 `--url`） |
| `--course` | 课程名：写入 `学习笔记/<course>/` 子文件夹 |
| `--mode` | `note` / `speedread` / `both` |
| `--url` | input 是 URL，先下载再处理 |
| `--dl-method` | `auto`(先 yt-dlp 后 opencli) / `ytdlp` / `opencli` |
| `--dl-quality` | 下载清晰度（360/480/720/1080） |

### 多套课程跨课程并行

```bash
python -m server.tests.process_courses --workers 6
```

- 自动处理 `COURSES` 里配置的所有课程
- **跨课程并行**：多套课程同时处理（API 最大化利用）
- 每套课程全部集完成后，自动生成**课程整体总结**
- 断点续传：已完成的集跳过，绝不重做

### 每套课程的处理流程

1. 分段转写（带时间戳）→ 保存 `_文字稿.md`
2. 帧采样字幕 OCR → 字幕文本
3. **文字指引版关键帧检测** → 关键图存 vault
4. DeepSeek 生成速览 + 笔记（英文自动中文）→ 写 Obsidian
5. 全部集完成后 → 课程整体总结

---

## 8. 视频下载

支持两种下载方式，**yt-dlp 优先，失败自动用 opencli 浏览器兜底**，且**按配置选择清晰度、自动降级**。

### 清晰度配置

在 `.env` 配置（或命令行 `--dl-quality` / `--dl-method` 覆盖）：

```ini
# .env
DOWNLOAD_QUALITY=720   # 360 / 480 / 720 / 1080
DOWNLOAD_METHOD=auto   # auto / ytdlp / opencli
```

- **高清下载**：B 站 format ID 按清晰度动态选择（360→30016, 480→30032, 720→30064, 1080→30080）。**登录 B 站会员后，yt-dlp（带浏览器 cookie）或 opencli（复用 Chrome 登录态）都能下 720p/1080p**。
- **自动降级**：目标清晰度不可用（非会员/源站限制）时，自动降到 720 → 480 → 360，保证任何环境都能下载成功。

### yt-dlp（默认）
- 通用下载，支持多站点
- 优先选 H264 编码（兼容 ffmpeg 4.2 无 AV1 解码器）
- 按 `DOWNLOAD_QUALITY` 选清晰度，失败逐级降级

### opencli 浏览器（兜底）
- 通过浏览器桥接复用你已登录的 Chrome 会话
- 打开视频页 → `eval` 提取 `__playinfo__` → 下载视频+音频分片 → ffmpeg 合并
- **能下会员/付费内容**（复用你的登录态），几乎不触发反爬
- 前置：`opencli` daemon 运行 + Chrome 扩展已连接（`opencli doctor` 检查）

```bash
# 强制用 opencli 浏览器下载（会员内容）
python -m server.batch_process "https://..." --url --dl-method opencli --dl-quality 720
```

---

## 9. 关键帧检测原理（文字指引版）

核心思路：**用带时间戳的文字稿判断"讲到哪里关键"，只在关键讲解时段寻找画面稳定的帧**。

```
① 讲解重要性预筛：DeepSeek 评估每句文字稿 importance ≥ 0.5 → 关键时段
② 只采样关键时段 → 用相邻帧差判断"画面稳定停留"（连续多帧变化小）
   → 讲师在展示某张图/PPT 时画面才稳定 → 关键候选
③ OCR 内容量过滤：文字过少（空白/水印/台标）→ 排除
④ 与文字稿重复度过滤：OCR 与讲解几乎一样（纯口播无图）→ 排除
⑤ 与已选图去重：避免重复截同画面
⑥ Qwen3.7-Flash 视觉精判：直接看图，判断是否有独立结构信息
   （教学大纲/架构图/功能列表/界面 → 值得；文件管理器/终端/单句结论 → 排除）
⑦ 评分过阈值才保存：
   Score = w1·Svisual + w2·Socr + w3·Stranscript − w4·Sdup
```

**优势**：相比逐帧扫描，**VLM 调用减少约 80%**，且视觉模型能识别文件管理器/终端等无价值画面。

**中间产物**（便于溯源）：
```
test/screenshots/<课程>/<视频>/guide/
├── candidates/   采样候选帧（按时间戳）
├── selected/     判定为关键的画面
└── scores.csv    每帧评分明细（含视觉判断结论、排除原因）
```

---

## 10. 断点续传与可靠性

### SQLite 状态管理
`task_db.py` 用 SQLite 记录每集处理状态：
- 登记每集（课程 + 视频路径）
- 标记阶段完成（转写/字幕/关键帧/笔记）
- 查询是否已完成

### 断点续传
`batch_process.process_one` 处理前检查：
- **数据库记录**该集已完成 → 跳过
- **完整版笔记文件存在** → 跳过（兼容无数据库的历史数据）

**效果**：中途失败/中断后重跑，已完成的集全部跳过，只处理未完成的。

### 超时保护（防卡死）
| 环节 | 保护 |
|------|------|
| DeepSeek API | 90s 超时 + 3 次重试 |
| Qwen3.7-Flash 视觉 API | 90s 超时 + 3 次重试 |
| PaddleOCR 字幕 OCR | 10 分钟超时，卡死跳过字幕（不影响关键帧和笔记） |

---

## 11. 输出文件说明

写入 `VAULT_PATH/<NOTES_DIR>/<课程名>/`，四个子文件夹：

```
学习笔记/
└── 大模型微调/                  ← 课程子文件夹
    ├── 完整版/                  ← 中文完整笔记（含关键图内嵌）
    │   └── 01-大语言模型微调之道1-介绍.md
    ├── 速览版/                  ← 中文速览（结论 + 要点 + 术语表）
    │   └── 01-大语言模型微调之道1-介绍_速览.md
    ├── 文字稿/                  ← 语音转写全文（带时间戳）
    │   ├── 01-..._文字稿.md         （英文视频另有 _文字稿_双语.md）
    │   └── 01-..._文字稿_双语.md
    ├── attachments/            ← 该课程的关键图
    │   └── 01-..._guide_01_28.jpg
    └── 2026-08-09_大模型微调_整体总结.md  ← 课程整体脉络总结
```

| 文件 | 命名 | 内容 |
|------|------|------|
| **速览** | `NN-主题_速览.md` | 核心结论 + 分节知识点（**关键图内嵌**）+ 术语表 + 回看时间戳 |
| **完整笔记** | `NN-主题.md` | 概述 + 分节要点 + 代码/示例 + 疑问行动项（关键图内嵌） |
| **文字稿** | `NN-主题_文字稿.md` | 完整语音转写，**按句带 `[MM:SS]` 时间戳** |
| **双语文字稿** | `NN-主题_文字稿_双语.md` | 英文原文 + 中文翻译对照（英文视频） |
| **课程总结** | `日期_课程_整体总结.md` | 学习路径图 + 骨架式要点 + 知识点关系 + 补充拔高 |

**关键图**：命名 `视频名_guide_MM_SS.jpg`，笔记/速览通过 `![[文件名]]` **内嵌到对应知识点小节**（不单独设截图章节）。图片命名带视频前缀，多视频不冲突。

**frontmatter**：所有文档含 `title` / `source` / `date` / `created`（生成时间）/ `tags`。完整版额外有「📚 学习资料」URL 溯源。

**文件名**：`NN-主题`（无日期前缀，日期在 frontmatter 里）。视频名可从每集自带 title 提取，过长则从文字稿摘要。

---

## 12. 测试与调试

```bash
cd server
source .venv/bin/activate

# 单元测试
python -m pytest server/tests/ -q

# 各环节独立测试
python -m server.tests.test_stt          # ASR 转写 + 时间戳
python -m server.tests.test_e2e          # 端到端（OCR→总结→笔记）
python -m server.tests.test_guide --seconds 300  # 文字指引关键帧（前5分钟）
python -m server.tests.test_resume       # 复用已有文字稿，处理视频剩余步骤
python -m server.tests.process_courses --workers 3  # 跨课程并行处理
```

**调试建议**：
- 关键图不全 → 看 `test/screenshots/<课程>/<视频>/guide/scores.csv` 的排除原因
- 配图质量差 → 调 `SCORE_THRESHOLD`（默认 0.5）
- 想用云端 OCR → `.env` 设 `OCR_ENGINE=qwen-vl`
- 字幕带位置不对 → 调 `OCR_SUBTITLE_Y0/Y1`

**测试数据约定**：测试视频放 `test/videos/`（gitignore，不提交）；中间产物放 `test/screenshots/`。

---

## 13. 常见问题

**Q1: Obsidian 里图片看不到？**
确保 `attachments/` 目录下的图片文件名与笔记里 `![[文件名]]` 一致。关键图由程序自动保存到课程子文件夹的 attachments，检查引用是否匹配。

**Q2: 为什么有些视频关键图很少/没有？**
关键帧检测按"信息量"过滤——纯口播讲解（画面无图表/列表/界面）或单句结论的视频，符合条件的关键画面本就少。这是**正确行为**，不是 bug。

**Q3: 语音转写很慢？**
转写是分段的（每 4.5 分钟一段调一次 ASR），每段需数秒到数十秒。长视频整体需要几分钟。关掉语音通道可完全跳过。

**Q4: 处理中断了怎么办？**
直接重跑即可——**断点续传会自动跳过已完成的集**，只处理未完成的。SQLite 记录 + 完整版文件双重判断，不会重复消耗 API。

**Q5: 下载视频失败？**
- 自动重试：`--dl-method auto` 会先试 yt-dlp，失败用 opencli 浏览器
- 会员/付费内容：`--dl-method opencli`（复用你已登录的 Chrome）
- 需要 `opencli doctor` 确认浏览器桥接已连接

**Q6: 支持哪些网站？**
任意含 `<video>` 元素的网页。下载支持 yt-dlp 覆盖的站点（B 站、YouTube 等），opencli 兜底能下登录内容。跨源 iframe 内视频 `ended` 检测可能失效，但音频捕获和截图仍可用。

**Q7: PaddleOCR 卡住了？**
偶发的线程池死锁，已加 10 分钟超时自动跳过字幕（不影响关键帧和笔记）。重跑时断点续传会继续。
