"""DeepSeek 结构化总结：融合 字幕OCR + 语音转写 + 截图OCR 三源。"""
from __future__ import annotations

import httpx


def _build_prompt(
    title: str,
    source_url: str,
    subtitle_text: str,
    speech_text: str,
    screenshot_notes: list[dict],
    priority: str = "ocr",
) -> str:
    screenshots = ""
    if screenshot_notes:
        parts = []
        for s in screenshot_notes:
            parts.append(f"- [{s.get('time', '')}] {s.get('filename', '')}: {s.get('ocr_text', '')}")
        screenshots = "截图及其 OCR 文字：\n" + "\n".join(parts)

    # 优先级说明
    prio_note = {
        "ocr": "以字幕(OCR)文本为主干，语音转写补充。",
        "stt": "以语音转写为主干，字幕(OCR)补充。",
        "both": "字幕与语音转写同等重要，互相补充、合并去重。",
    }.get(priority, "以字幕(OCR)文本为主干，语音转写补充。")

    return f"""你是学习笔记助手。请根据以下来自视频课程的素材，生成一份结构化的 Markdown 学习笔记。

视频标题：{title}
来源：{source_url}

=== 字幕文本（OCR 自视频画面） ===
{subtitle_text[:12000]}

=== 语音转写文本 ===
{speech_text[:12000]}

=== {screenshots} ===

要求：
1. 输出 Markdown，包含：**概述**、**核心要点**（分小节，用 ## 标题）、**代码/示例**（如有）、**时间戳索引**（若素材带时间点）、**疑问/行动项**。
2. 素材优先级：{prio_note} 哪边信息更完整可靠就以哪边为主。
3. **若上面给出了截图/幻灯片清单，把对应的图片 `![[图片文件名.png]]` 内嵌到最相关的小节里**，不要单独设"截图"或"附件"章节；只能引用清单里实际存在的文件名。
4. 去掉口语化冗余、重复，保持信息密度。
5. 只输出 Markdown 正文，不要额外说明。
"""


class DeepSeekSummarizer:
    def __init__(self, api_key: str, model: str, base_url: str) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")

    def _chat(self, prompt: str, system: str = "你是专业的结构化笔记助手，擅长把视频内容整理为高质量学习笔记。") -> str:
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.3,
        }
        # 超时 + 重试（避免 API 请求挂起导致处理停滞）
        import time as _time

        last_err = None
        for attempt in range(3):
            try:
                resp = httpx.post(url, json=payload, headers=headers, timeout=90)
                if resp.status_code == 429:
                    _time.sleep(5 * (attempt + 1))
                    continue
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"].strip()
            except Exception as e:  # noqa: BLE001
                last_err = e
                if attempt < 2:
                    _time.sleep(3 * (attempt + 1))
                else:
                    raise RuntimeError(f"DeepSeek 调用失败(3次): {last_err}") from last_err
        raise RuntimeError(f"DeepSeek 调用失败: {last_err}")

    def speedread(
        self,
        title: str,
        source_url: str,
        subtitle_text: str = "",
        speech_text: str = "",
        slides: list[dict] | None = None,
        priority: str = "ocr",
    ) -> str:
        """生成图文并茂的学习速览：核心结论 + 关键术语 + 幻灯片时间线。"""
        slides_txt = ""
        embed_rule = ""
        if slides:
            parts = []
            for s in slides:
                parts.append(f"- [{s.get('time', '')}] 幻灯片 {s.get('filename', '')}：{s.get('ocr_text', '')}")
            slides_txt = "演讲幻灯片（自动截图，含OCR文字）：\n" + "\n".join(parts)
            embed_rule = (
                "2. **关键知识点**：分小节（## 标题），每节给出要点，"
                "**把实际存在的幻灯片内联嵌入**，Obsidian 引用格式为 `![[图片文件名.png]]`。"
                "**注意：只能引用上面列出的幻灯片文件名，绝不能凭空编造不存在的图片！**"
            )
        else:
            embed_rule = "2. **关键知识点**：分小节（## 标题），每节给出要点。本视频没有自动截图，不要引用任何图片。"

        prio_note = {
            "ocr": "以字幕为主干。",
            "stt": "以语音为主干。",
            "both": "字幕与语音合并、去重。",
        }.get(priority, "以字幕为主干。")

        prompt = f"""你是学习速览助手。请根据视频课程的素材，生成一份**图文并茂的学习速览** Markdown 文档，用于快速复习。

视频标题：{title}
来源：{source_url}

=== 字幕文本（OCR） ===
{subtitle_text[:12000]}

=== 语音转写 ===
{speech_text[:12000]}

=== {slides_txt} ===

要求：
1. 顶部给 **2-3 句核心结论**（这节课最重要的收获）。
{embed_rule}
3. **术语表**：列出课程中出现的专业术语及一句话解释（表格）。
4. **值得回看的时间戳**：列出重点时刻（若有）。
5. 素材优先级：{prio_note} 信息更完整的一边为主。
6. **不要单独设置"截图"或"附件"章节**——所有图片直接内嵌在对应的知识点小节里。
7. 简洁、信息密度高，适合复习扫读。只输出 Markdown 正文。
"""
        return self._chat(prompt, system="你是学习速览助手，擅长把视频整理成图文并茂的复习速览。")

    def assess_slide(self, title: str, ocr_text: str) -> dict:
        """判断一帧画面（OCR 全文）是否值得保存为学习截图。

        返回 {"important": bool, "score": int, "reason": str}
        重要：含关键概念、架构图说明、公式、图表标题、新术语定义等。
        不重要：纯过渡画面、人物特写、无信息量的装饰、重复内容。
        """
        prompt = f"""你是视频学习截图筛选助手。判断下面这一帧视频画面（已 OCR 出文字）是否值得保存为学习笔记截图。

视频标题：{title}

=== 画面 OCR 文字 ===
{ocr_text[:2000]}

判断标准：
- **值得保存**（important=true）：含关键概念/定义、架构图说明、公式、重要图表标题、新术语、方法论要点、结论性文字。这些是学习重点。
- **不值得**（important=false）：过渡画面、无文字信息、装饰性内容、人物讲话特写、与内容无关的文字、或只有一句话寒暄。

请返回严格 JSON：{{"important": true/false, "score": 0-100, "reason": "一句话理由"}}，不要多余文字。"""

        import json as _json

        try:
            resp = self._chat(prompt, system="你是严格的截图筛选器，只返回 JSON。")
            # 容忍 markdown 代码块包裹
            resp = resp.strip()
            if resp.startswith("```"):
                resp = resp.split("```")[1].lstrip("json").strip()
            start, end = resp.find("{"), resp.rfind("}")
            data = _json.loads(resp[start : end + 1])
            return {
                "important": bool(data.get("important", False)),
                "score": int(data.get("score", 0)),
                "reason": str(data.get("reason", "")),
            }
        except Exception as e:  # noqa: BLE001
            # 解析失败 → 保守返回不重要（避免误存）
            return {"important": False, "score": 0, "reason": f"判断失败: {e}"}

    def score_ocr_importance(self, ocr_text: str) -> float:
        """评估画面 OCR 文字的重要程度，返回 0~1 分（Socr）。"""
        if not ocr_text.strip():
            return 0.0
        prompt = f"""评估下面这段从视频画面 OCR 出的文字对"学习重点"的重要程度。

评分标准：
- 有实质教学内容（标题/要点列表/概念定义/操作说明/架构说明/公式/术语/结论）→ 0.7~1.0
- 标题卡片、章节标题、讲师口播时展示的要点 → 0.6~0.9
- 仅水印/台标/播放器UI/无意义数字 → 0.2 以下
- 完全无关/空 → 0

=== OCR 文字 ===
{ocr_text[:1500]}

只返回一个 0~1 的数字（如 0.8），不要任何其他文字。"""
        try:
            resp = self._chat(prompt, system="你只返回 0~1 的数字。").strip()
            import re

            m = re.search(r"0?\.\d+|1\.0?|[01]", resp)
            return max(0.0, min(1.0, float(m.group(0)))) if m else 0.0
        except Exception:  # noqa: BLE001
            return 0.0

    def score_transcript_importance(self, transcript: str) -> float:
        """评估当前讲解内容的重要程度，返回 0~1 分（Stranscript）。"""
        if not transcript.strip():
            return 0.0
        prompt = f"""评估下面这段视频讲解文字对"学习重点"的重要程度。
讲解关键概念/方法论/结论/过渡句/寒暄 → 前两者高分，后两者低分。

=== 讲解文字 ===
{transcript[:1500]}

只返回一个 0~1 的数字（如 0.7），不要任何其他文字。"""
        try:
            resp = self._chat(prompt, system="你只返回 0~1 的数字。").strip()
            import re

            m = re.search(r"0?\.\d+|1\.0?|[01]", resp)
            return max(0.0, min(1.0, float(m.group(0)))) if m else 0.0
        except Exception:  # noqa: BLE001
            return 0.0

    @staticmethod
    def text_similarity(a: str, b: str) -> float:
        """两段文字的重合度 0~1（Sduplicate）。用字符级 Jaccard。"""
        if not a or not b:
            return 0.0
        sa, sb = set(a[:200]), set(b[:200])
        if not sa or not sb:
            return 0.0
        return len(sa & sb) / len(sa | sb)

    def assess_frame_worthiness(self, ocr_text: str) -> dict:
        """判断一帧画面（OCR 文字）是否值得作为关键截图（信息量精判）。

        值得：画面包含独立的图表/列表/界面/结构信息，是讲解的"展示物"。
        包括：教学大纲/课程目标页、架构图、功能列表、操作界面、表格、流程图、步骤清单。
        不值得：纯标题封面（只有一个大标题）、单句结论/提醒（信息已含在讲解中）、过渡画面。
        返回 {"worthy": bool, "reason": str}
        """
        if not ocr_text.strip():
            return {"worthy": False, "reason": "无文字"}
        prompt = f"""这是视频某帧画面OCR出的文字。判断它是否值得作为学习笔记的"关键截图"。

判断标准：
- **值得**（worthy=true）：画面包含独立的结构化信息，即使没有讲解也能看懂内容。包括：
  - 教学大纲/课程目标页（含工具、步骤、要求、学习内容列表）
  - 架构图、功能列表、操作界面、表格、流程图、步骤清单
  - 具体的配置/命令/代码展示
- **不值得**（worthy=false）：
  - 纯标题封面（只有一个大标题，无结构内容）
  - 单句结论/提醒（信息已含在讲解中，无额外结构）
  - 纯人物特写、过渡画面、水印、导航栏/菜单栏截图

=== OCR 文字 ===
{ocr_text[:1500]}

只返回 JSON：{{"worthy": true/false, "reason": "一句话原因"}}，不要多余文字。"""

        import json as _json

        try:
            resp = self._chat(prompt, system="你是严格的截图筛选器，只返回 JSON。").strip()
            if resp.startswith("```"):
                resp = resp.split("```")[1].lstrip("json").strip()
            start, end = resp.find("{"), resp.rfind("}")
            data = _json.loads(resp[start : end + 1])
            return {"worthy": bool(data.get("worthy", False)), "reason": str(data.get("reason", ""))}
        except Exception as e:  # noqa: BLE001
            return {"worthy": False, "reason": f"判断失败: {e}"}

    def summarize(
        self,
        title: str,
        source_url: str,
        subtitle_text: str,
        speech_text: str,
        screenshot_notes: list[dict] | None = None,
        priority: str = "ocr",
    ) -> str:
        prompt = _build_prompt(title, source_url, subtitle_text, speech_text, screenshot_notes or [], priority)
        return self._chat(prompt)

    def translate(self, text: str) -> str:
        """把英文文字稿翻译成中文。"""
        if not text.strip():
            return ""
        prompt = f"""把下面的英文视频讲解翻译成简体中文，要求：
1. 保留原有的时间戳标记 `[MM:SS]`（如有）。
2. 逐句翻译，专业术语在中文后加括号保留英文（如 路由(Router)）。
3. 翻译通顺自然，适合学习。

=== 英文原文 ===
{text[:15000]}

只输出翻译后的中文。"""
        return self._chat(prompt, system="你是专业的英文技术视频翻译，翻译准确通顺。")

    def summarize_chinese(
        self,
        title: str,
        source_url: str,
        speech_text: str,
        translated_text: str,
        screenshot_notes: list[dict] | None = None,
    ) -> str:
        """针对英文视频，基于英文原文+中文翻译，生成中文学习笔记。"""
        shots = "".join(
            f"- [{s.get('time', '')}] {s.get('filename', '')}: {s.get('ocr_text', '')}\n"
            for s in (screenshot_notes or [])
        )
        prompt = f"""请根据下面的英文视频讲解（含中文翻译），生成一份**中文**的结构化学习笔记。

视频标题：{title}
来源：{source_url}

=== 英文讲解原文 ===
{speech_text[:12000]}

=== 中文翻译 ===
{translated_text[:12000]}

=== 截图信息 ===
{shots}

要求：
1. 用中文输出，包含：**概述**、**核心要点**（分小节，用 ## 标题）、**代码/示例**（如有，保留英文原样）、**疑问/行动项**。
2. 若有截图，把对应图片 `![[文件名]]` 内嵌到最相关的小节。
3. 内容准确完整，术语用中文+括号英文。
4. 只输出 Markdown 正文。"""
        return self._chat(prompt, system="你是专业的中文技术学习笔记助手。")

    def course_summary(
        self,
        course_title: str,
        source_url: str,
        episodes: list[dict],
    ) -> str:
        """生成课程整体脉络总结。

        episodes: [{title, theme, key_points, url}] 每集的核心信息。
        输出骨架式总结：学习要点 + 关键解释 + Markdown 流程图。
        """
        ep_lines = []
        for e in episodes:
            ep_lines.append(f"### {e['title']}")
            ep_lines.append(f"- 主题：{e.get('theme', '')}")
            ep_lines.append(f"- 要点：{e.get('key_points', '')}")
            ep_lines.append("")
        eps_txt = "\n".join(ep_lines)

        prompt = f"""你是课程脉络梳理专家。请为整个课程生成一份**大脉络骨架总结**文档，帮助学习者建立整体认知。

课程标题：{course_title}
来源：{source_url}

以下是各集的要点（来自每集的学习文档）：

{eps_txt[:15000]}

要求：
1. **先给出整体学习路径图**：用 Markdown 流程图或分步列表，展示课程的主干脉络（先学什么、再学什么、为什么这个顺序）。
2. **骨架式学习要点**：列出 3-6 个核心学习要点，每个要点配 1-2 句关键解释（不是复述，而是提炼本质）。
3. **知识点关系**：说明各知识点之间的关联（如：路由是 request 的基础、ORM 依赖前面的模型定义等）。
4. **补充与拔高**：可以补充视频之外、但对该主题重要的内容（如最佳实践、常见坑、进阶方向），用"补充"标记。
5. **整体展望**：学完本课程后应该掌握什么、下一步可以学什么。

风格：简洁骨架式，信息密度高，适合整体复习。可用 Markdown 流程图（mermaid 或 ```flow```）。只输出 Markdown 正文。
"""
        return self._chat(prompt, system="你是课程脉络梳理专家，擅长把多节课程提炼成一张整体学习地图。")
