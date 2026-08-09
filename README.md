# 视频学习助手

在 Chrome 浏览器看学习视频时（B 站、飞书、任意含 `<video>` 的网页），自动把视频内容变成结构化笔记存进 Obsidian。

> 📖 **详细使用说明见 [USAGE.md](USAGE.md)**（含环境搭建、配置、三种使用方式、关键帧原理、调试指南）。

## 功能

- **🎯 字幕自动提取**：后台定时截取当前页面画面，用 **PaddleOCR** 识别底部字幕带，去重后累积成带时间戳的字幕文本（不依赖字幕 API，读画面即可）。
- **📷 快捷键截图记重点**：按 `⌘⇧S` 手动截取当前画面（架构图、公式等）。
- **🎙 分段实时语音转写**：用 `tabCapture` 捕获标签页音频（离屏回放，不影响你听视频），**播放中每 4.5 分钟后台转写一段**（千问 ASR 单次限 5 分钟，不打断播放），视频结束即可快速拿到全文。
- **⏱ ASR 时间戳**：转写返回**逐句时间戳**（begin_ms/end_ms），文字稿按句分段标注 `[MM:SS]`，便于回看视频定位关键画面。
- **🌐 英文视频双语支持**：自动检测转写语言，英文视频生成**英文原文 + 中文翻译双语文字稿**，速览和笔记均为**中文**（术语中英对照）。
- **🧠 图文并茂速览**：视频结束后，自动生成**学习速览**——核心结论 + 关键知识点（内嵌真实幻灯片）+ 术语表 + 值得回看的时间戳；也可生成完整笔记。
- **🔍 文字指引版关键帧检测**：用带时间戳文字稿做**讲解重要性预筛**（DeepSeek 评估每句是否讲关键概念），只在关键讲解时段采样，用**画面稳定停留**判定候选，再用 **OCR 内容量过滤** + **与文字稿重复度过滤** + **与已选图去重**，最后 **Qwen3.7-Flash 视觉模型精判**（直接看图判断是否值得截图）。相比全帧扫描，**VLM 调用减少约 80%**，且过滤掉文件管理器/终端等无价值画面。
  - 公式：`Score = w1·Svisual + w2·Socr + w3·Stranscript − w4·Sdup`
  - 中间产物（候选帧/选中图/评分CSV）保存在 `assets/screenshots/<课程>/<视频>/guide/`，便于溯源
- **🧠 OCR 引擎可配置**：`OCR_ENGINE=paddle`（本地免费）或 `qwen-vl`（云端效果好），`.env` 配置。
- **📜 完整文字稿**：每个视频的完整语音转写全文单独保存为 `_文字稿.md`（带时间戳分句 + 生成时间），英文视频额外生成 `_文字稿_双语.md`。
- **📚 课程子文件夹 + 整体总结**：用 `--course` 按课程建独立文件夹（速览版/完整版/文字稿/attachments 四个子目录），全部集处理完后自动生成**课程整体脉络总结**（学习路径图 + 骨架式要点 + 知识点关系 + 补充拔高）。
- **🗂 批量处理（多视频自动记重点）**：给一个视频文件夹或 URL，自动逐个处理（分段转写 → 字幕 OCR → 文字指引版关键帧检测 → 速览+笔记+文字稿），全部写入 Obsidian。支持**跨课程并行**（`process_courses.py`，多套课程同时处理）。
- **🔗 视频自动下载**：`download_video` 支持 **yt-dlp 优先、失败用 opencli 浏览器兜底**（复用你已登录的 Chrome 会话，能下会员内容、不触发反爬）。从 URL 提取 `__playinfo__` 下载分片 + ffmpeg 合并。
- **💾 SQLite 状态管理**：`task_db.py` 记录每集处理状态（转写/字幕/关键帧/笔记），**可靠断点续传**——已有完整版笔记或数据库记录完成的集自动跳过，绝不重复消耗 API。
- **🛡 超时保护**：DeepSeek / 视觉 API 加 90s 超时 + 重试，PaddleOCR 字幕 OCR 加 10 分钟超时——偶发卡死自动恢复，不阻塞整批处理。
- **🔧 通道可配置**：OCR 字幕 / 语音转写可分别开关，可设总结优先级，文档类型可选。
- **📁 自动存入 Obsidian**：文档按课程写入 `学习笔记/<课程>/<完整版|速览版|文字稿|attachments>/`，文件名 `NN-主题.md`（无日期前缀，日期在 frontmatter）。

> 支持任意含视频的网页，不限于 B 站。扩展监听当前标签页，检测到 `<video>` 元素即可工作（含 iframe 内嵌视频）。

## 架构

```
Chrome 扩展 (MV3)  ── 采集 ──▶  本地 Python 服务 (FastAPI)
  · tabCapture 录音              · OCR（PaddleOCR + qwen-vl）
  · captureVisibleTab 截图/字幕  · ASR（千问 qwen-audio）
  · content script 监听视频结束  · 关键帧（Qwen3.7-Flash 视觉判断）
  · 快捷键命令                   · 总结（DeepSeek，中文）
                                 · 下载（yt-dlp + opencli 兜底）
                                 · 状态管理（SQLite 断点续传）
                                 · 写 Obsidian
```

扩展不持有任何 API key、不写任意路径；所有密钥在服务端 `.env`。

## 快速开始

### 1. 配置 `.env`

```bash
cd server
cp .env.example .env   # 然后填入你的 key
```

需要：
- `LLM_API_KEY` — DeepSeek（总结 + 关键帧判断用）
- `ASR_API` — 阿里云百炼 DashScope key（语音转写 + 视觉判断用）
- `VAULT_PATH` — 你的 Obsidian vault 路径

### 2. 启动本地服务

```bash
cd server
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn server.app:app --host 127.0.0.1 --port 8787
```

需要 `ffmpeg`（音频转 16kHz WAV）：`brew install ffmpeg`

### 3. 加载 Chrome 扩展

1. 打开 `chrome://extensions`
2. 开启「开发者模式」
3. 「加载已解压的扩展程序」→ 选择 `extension/` 目录
4. 在扩展快捷键设置里确认：`⌘⇧E` 开始/停止学习、`⌘⇧S` 截图

### 4. 使用

1. 打开任意含视频的网页（B 站、飞书、通用 HTML5 视频站）
2. 点扩展图标 → 「开始学习」（或按 `⌘⇧E`）
3. 正常看视频，后台自动积累字幕、转写音频、检测关键画面
4. 看到重点画面按 `⌘⇧S` 手动截图
5. 视频播完，自动生成学习文档 → Obsidian 通知提示路径

### 5. 离线处理（视频/URL → 笔记）

```bash
# 处理本地视频文件夹（可指定课程）
python -m server.batch_process /path/to/videos --course "课程名" --mode both

# 处理 URL（自动下载：yt-dlp 优先，opencli 浏览器兜底，按配置选清晰度）
python -m server.batch_process "https://..." --url --course "课程名"

# 多套课程跨课程并行处理
python -m server.tests.process_courses --workers 6
```

**下载清晰度配置**（`.env`）：
```ini
DOWNLOAD_QUALITY=720   # 360 / 480 / 720 / 1080（B站会员可下高清）
DOWNLOAD_METHOD=auto   # auto / ytdlp / opencli
```
目标清晰度不可用时**自动降级**（720→480→360），任何环境都能下载。

### 6. 配置（扩展 options 页）

| 配置 | 说明 |
|------|------|
| 字幕通道（OCR） | 有字幕视频开（B 站/课程），无字幕视频可关省资源 |
| 语音通道（转写） | 无字幕视频（飞书/会议）开，有字幕可关省 API 成本 |
| 总结优先级 | 字幕为主 / 语音为主 / 两者并重 |
| 文档类型 | 速览 / 完整笔记 / 两者都要 |
| 自动截取幻灯片 | 讲 PPT 时开，画面大幅变化自动截图 |

## 测试

```bash
cd server
./.venv/bin/python -m pytest server/tests/          # 单元测试
./.venv/bin/python -m server.tests.test_stt          # ASR 真实调用测试
./.venv/bin/python -m server.tests.test_e2e          # 端到端（OCR→总结→笔记）
./.venv/bin/python -m server.tests.test_guide        # 文字指引关键帧（可 --seconds N 快速测）
./.venv/bin/python -m server.tests.test_resume       # 复用已有文字稿，处理视频剩余步骤
```

**测试数据约定**：测试用的视频放在 `assets/videos/`（已 gitignore，不提交）；中间数据放 `assets/` 目录。测试视频不要删除，可复用。

## 已知限制

- **DRM 内容**：Widevine/HDCP 保护的视频（Netflix 等）音频捕获为静音；B 站、飞书一般不受影响。截图（captureVisibleTab）不受 DRM 限制，始终可截画面。
- **字幕带位置**：默认裁剪画面底部 26%（`OCR_SUBTITLE_Y0/Y1`），不同播放器布局可调整。
- **分段转写成本**：播放中每 4.5 分钟调用一次千问 ASR（20 分钟视频约 5 次调用）；关掉语音通道可省。
- **PaddleOCR 偶发死锁**：极少数情况下字幕 OCR 线程池卡死，已加 10 分钟超时自动跳过字幕（不影响关键帧和笔记）。
- **跨域 iframe 视频**：跨源 iframe 内的视频，扩展 content script 无法注入（受同源限制），`ended` 检测可能失效；但音频捕获与截图仍可用。
- **语音转写为兜底**：多数视频有字幕，字幕 OCR 优先；仅当字幕缺失时语音为主。

## 项目结构

```
extension/          Chrome 扩展
  manifest.json     MV3 配置、权限、快捷键
  service-worker.js 后台编排：状态机、帧检测循环、分段转写聚合、命令路由
  offscreen.js      离屏音频捕获+回显+分段录制转写
  content-script.js 页面视频状态监听（含 iframe）
  popup.js/options.js
server/             本地服务
  app.py            FastAPI 端点（OCR/ASR/帧差检测/总结）
  config.py         .env 配置
  batch_process.py  单视频/批量处理（含断点续传）
  process_courses.py 多套课程跨课程并行处理
  downloader.py     yt-dlp 优先 + opencli 浏览器兜底下载
  guide_capture.py  文字指引版关键帧检测
  task_db.py        SQLite 状态管理（断点续传）
  ocr/
    vision.py       PaddleOCR（字幕带）+ 引擎选择
    qwen_vl.py      qwen-vl-ocr 云端 OCR
    qwen_vision.py  Qwen3.7-Flash 视觉判断（关键帧精判）
  stt/
    base.py         STT 抽象
    qwen_audio.py   千问 ASR（分段 + 时间戳）
  summarizer/
    deepseek.py     DeepSeek 笔记 + 速览 + 课程总结 + 翻译
  notes/
    writer.py       Obsidian 写入（四文件夹 + 图片格式检查）
  tests/            测试与工具脚本
```
