"""脚本生成引擎：模板式二创口播脚本（T5.3）。

来源：V1 `launcher/engine/draft_engine.py`（324 行，`策略:搬运`）。按 ADR-0018 决定 1 在
`core/analysis/` 适配。**不接 LLM、不联网、不落库、不写文件。**

## 它到底在算什么（这里要说得很直白，因为它很容易被读成"生成"）

它**不生成**任何东西。它是四张写死的分镜表（`BEAT_TABLES`）加两张写死的贴图卡表，
按 `template_key` 选一张，然后把**三样东西填进占位符**：

- `{topic}` ← 从 `topic`（或来源作品标题）里去掉"如何/怎样/为什么"剩下的那串字，
- `{pain}` ← 三条"标题关键词 → 痛点文案"规则（`TOPIC_RULES`）里第一条命中的，都没有就用默认句，
- `{solution}` ← 同上，与 pain 成对。

所以"一份 7 句的导演级口播脚本"= 表里那 7 句 + 至多 3 处填空。**其余句子与这条视频、
与那份口播稿都没有关系** —— 它们是规范文案，一字不改地给每一个主题。

V1 的 `_compose_beats(..., raw_text=...)` 与 `_extract_keywords(title, raw_text)` 都收了
`raw_text`（口播稿正文）**却一个字都没读过**。也就是说：V1 那个"依据当前爆款生成"的按钮，
口播稿对产出的唯一影响是"标题里有没有 ai/内容/视频这几个词"。V2 不假装这件事 ——
参数直接删掉，调用方（路由）因此只取 `videos.title` 一个字段，不进口播稿。

## 规则表逐字保留

`TEMPLATES`（4 张模板的 name/desc/film_job）、`TOPIC_RULES`（3 条 + 两个默认句）、
`BEAT_TABLES`（三张分镜表的全部 channel / visualCue / speech）、`CARD_TABLES`。
唯一的形式差别：f-string 的字面量拆成 `{topic}` / `{pain}` / {solution} 占位符，
这样"输出里每一句都来自规则表"这条不变量才能被测出来
（见 `tests/unit/analysis/test_draft_engine.py`）。

## 与 V1 的四处不同

1. `source_context: dict` → 两个显式参数（`source_title`）。V1 里那个字典有
   `title` / `transcript` / `summary` / `creator` / `platform` 五个键，其中三个从来没人读。
2. `custom_instructions` 参数**在 V1 里收下后从未使用**。V2 的引擎不带这个参数，
   路由层的处理是"非空就 422 如实拒绝"，而不是继续假装生效（AGENTS.md §1.3）。
3. `template_key == "tool_demo"` 在 V1 的 `_compose_beats` 里**没有分支**：它落进
   `else`（教程型收藏闭环）却仍把自己报成"工具实战演示"。V2 保留这个行为（那是既成事实，
   动它等于动产品），但在响应里加一个 `beats_template_key` 字段说明**实际用的是哪张表**，
   把降级摆在明面上。缺的那张分镜表要么将来补，要么把这个键摘掉 —— 但不要靠"看不出来"糊。
4. `mode`（SHORT/LONG）在 V1 里同样**不影响分镜**（`_compose_beats` 没读过它），
   只是回显。V2 保留回显（它同时是 `benchmark_engine.handoff.recommended_mode` 的落点），
   但在这里写明：**想要更短的本子要换 `short_fast` 模板，不是换 mode**。

`estimated_duration_seconds` 是 `总口播字数 / 4.0` 取整（V1 原式）：一个换算常数，
不是朗读测量。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from pydantic import BaseModel, Field

__all__ = [
    "BEAT_TABLES",
    "CARD_TABLES",
    "DRAFT_SPEC_VERSION",
    "SPEECH_CHARS_PER_SECOND",
    "TEMPLATES",
    "TOPIC_RULES",
    "DraftMode",
    "DraftScript",
    "ScriptBeat",
    "ScriptCard",
    "TemplateKey",
    "TopicRule",
    "generate_draft_script",
    "template_keys",
]

DRAFT_SPEC_VERSION: Final = "draft-ai-work-video/v1"
"""脚本规范的版本号，V1 原样。"""

DEFAULT_TITLE: Final = "未命名二创作品"
"""标题兜底（V1 原值）。走 HTTP 时通常到不了这里 —— 路由要求 `topic`/`video_id` 至少给一个。"""

SPEECH_CHARS_PER_SECOND: Final = 4.0
"""估算时长用的语速换算（V1 原值，`int(总字数 / 4.0)`）。"""

TOPIC_STRIP_WORDS: Final = ("如何", "怎样", "为什么")
"""从标题里剥掉的疑问前缀（V1 原表）：留下来的那串才当主题用。"""

DraftMode = Literal["SHORT", "LONG"]
"""`mode` 只是回执，不改变分镜数量 —— 见模块 docstring 第 4 条。"""

TemplateKey = Literal["tutorial_save_loop", "judgment_first", "tool_demo", "short_fast"]
"""四个模板键。V1 用 `dict.get(k, 默认)` 静默兜底，V2 的路由用 `Literal` 直接 422。"""

FALLBACK_BEATS_KEY: Final = "tutorial_save_loop"
"""`tool_demo` 实际用的那张分镜表（V1 的 `else` 分支）。"""


@dataclass(frozen=True)
class TemplateMeta:
    """一张模板的对外说明。"""

    name: str
    desc: str
    film_job: str


@dataclass(frozen=True)
class BeatTemplate:
    """一行分镜：通道 + 画面指示 + 口播模板（带 `{topic}`/`{pain}`/`{solution}` 占位符）。"""

    channel: str
    visual_cue: str
    speech: str


@dataclass(frozen=True)
class CardTemplate:
    """一张贴图卡：标题模板 + 类型 + 正文（同样是模板）。"""

    title: str
    type: str
    content: str


@dataclass(frozen=True)
class TopicRule:
    """一条"标题关键词 → 痛点/解法"规则。

    `lowered_triggers` 在 `title.lower()` 里找（V1 的 `"ai" in title.lower()`），
    `exact_triggers` 在原标题里找（V1 的 `"组织" in title`）—— 两种判法混在同一个
    `if` 里是 V1 原样，分开写是因为把它们并成一列会让人以为 "AI" 也算大小写敏感。
    """

    lowered_triggers: tuple[str, ...]
    exact_triggers: tuple[str, ...]
    pain: str
    solution: str


TEMPLATES: Final[dict[str, TemplateMeta]] = {
    "tutorial_save_loop": TemplateMeta(
        name="教程型收藏闭环",
        desc="强承诺 → 降维比喻 → 清单提炼 → 关键实操 → 隐藏避坑 → 升维追更",
        film_job="手把手入门 / 资源清单 / 教程型收藏",
    ),
    "judgment_first": TemplateMeta(
        name="判断先于界面",
        desc="认知断言 → 痛点对照 → 真实录屏证明 → 降维模型 → 行动回收",
        film_job="认知口播 / 成品秀 / 改习惯",
    ),
    "tool_demo": TemplateMeta(
        name="工具实战演示",
        desc="结果先亮 → 输入动作结果链 → 隐藏技巧彩蛋 → 资料包领取",
        film_job="工具测评 / 深度案例拆解",
    ),
    "short_fast": TemplateMeta(
        name="极速短视频 (35-65s)",
        desc="前3秒黄金钩子 → 1个核心矛盾 → 1套极速解法 → 1张保存卡片",
        film_job="单点技巧 / 极速认知提炼",
    ),
}
"""四张模板的说明（V1 原表）。注意 `tool_demo` 只有说明、没有分镜表（见模块 docstring）。"""

DEFAULT_PAIN: Final = "很多人面对这个场景还在手动盲测，反复踩坑效率极低"
DEFAULT_SOLUTION: Final = "掌握这套核心底层逻辑与工具链，一个人就能顶一个团队"

TOPIC_RULES: Final[tuple[TopicRule, ...]] = (
    TopicRule(
        ("ai", "native"),
        ("组织",),
        "传统团队把 AI 当打工外挂，结果组织臃肿、交付依然缓慢",
        "真正的 AI-native 是从第一天就用自闭环代码和工作流重构生产方式",
    ),
    TopicRule(
        ("twitter",),
        ("涨粉", "内容"),
        "天天日更几小时却毫无互动，找不到爆款的底层机制",
        "用内容杠杆和高信息密度清单，精准抓住目标受众的核心注意力",
    ),
    TopicRule(
        ("youtube",),
        ("视频", "插件"),
        "长视频动辄半小时根本看不完，抓不住重点浪费大量时间",
        "用自动化插件一键提取精准逐字稿与核心要点，快速吸取精华",
    ),
)
"""三条主题规则（V1 原表，**顺序有意义**：第一条命中者赢）。"""

BEAT_TABLES: Final[dict[str, tuple[BeatTemplate, ...]]] = {
    "judgment_first": (
        BeatTemplate(
            "真人出镜＋动效",
            "【画面：真人近景出镜，背景微暗，正上方弹出高对比度大字断言卡「判断先于界面」】",
            "别再到处找教程了！关于{topic}，绝大多数人都把顺序完全搞反了。",
        ),
        BeatTemplate(
            "真人出镜＋动效",
            "【画面：画面左侧展示常见错误路径打叉，右侧切入高价值对比卡】",
            "{pain}。如果你不先想清楚底层交付物，换再多工具也是白搭。",
        ),
        BeatTemplate(
            "真实录屏",
            "【画面：屏幕切入真实工作界面，鼠标精准点击运行核心工作流，展示秒级输出】",
            "你看这套实操逻辑：{solution}，输入明确指令后直接跑出最终结果。",
        ),
        BeatTemplate(
            "真实录屏",
            "【画面：镜头特写局部核心配置参数与关键细节，停留2秒】",
            "这里有个关键细节很多人会漏掉：把核心规则固定在系统提示词里，避免反复微调。",
        ),
        BeatTemplate(
            "真人出镜＋动效",
            "【画面：全屏展示贴图 A「核心方法对照清单」，停留2秒提示截图保存】",
            "把这张核心对照表截图存好。记住：省的不是几分钟操作时间，而是你反复试错的决策成本。",
        ),
        BeatTemplate(
            "真人出镜＋动效",
            "【画面：切回真人半身，右下角弹出关注与评论区指引卡】",
            "完整配置清单和提示词我整理好了，评论区回复对应关键词，直接发你！",
        ),
    ),
    "short_fast": (
        BeatTemplate(
            "真人出镜＋动效",
            "【画面：0.0s 极速开场，真人手持设备或直视镜头，上方大字「30秒搞定」】",
            "搞定{topic}，千万不要再去死记复杂的步骤！",
        ),
        BeatTemplate(
            "真实录屏",
            "【画面：极速切换到电脑录屏，光标一键点击执行，直接展示成品效果】",
            "直接用这套三步法：打开配置，填入核心提示词，点击一键生成。",
        ),
        BeatTemplate(
            "全屏AI视频",
            "【画面：全屏科幻数据流穿梭氛围换气镜头，无人出镜，节奏轻快】",
            "以前要折腾一整天的事情，现在只要一分钟就能搞定。",
        ),
        BeatTemplate(
            "真人出镜＋动效",
            "【画面：弹出贴图 A 全屏高清卡片，提示长按收藏】",
            "这套口诀先点赞收藏起来，下期带你实操进阶玩法！",
        ),
    ),
    "tutorial_save_loop": (
        BeatTemplate(
            "真人出镜＋动效",
            "【画面：真人出镜，眼神坚定，开篇右上角展示成品高分徽章，前1.5秒微变焦】",
            "今年你可以不学任何花哨的概念，但{topic}这套逻辑，你一定要彻底搞懂。",
        ),
        BeatTemplate(
            "真人出镜＋动效",
            "【画面：左侧展示痛点场景插画，右侧打出醒目红字警示】",
            "{pain}。今天我用最接地气的方式，帮你一次性理清。",
        ),
        BeatTemplate(
            "真人出镜＋动效",
            "【画面：切入信息板「核心三步法全景图」，建立框架预期】",
            "整个流程其实就分三步：第一步定标准，第二步建流程，第三步做自动化交付。",
        ),
        BeatTemplate(
            "真实录屏",
            "【画面：屏幕切入实操演示，逐项录制操作过程，重点步骤放大高亮】",
            "首先看第一步实操：{solution}。这一步做扎实，后面就能全自动运转。",
        ),
        BeatTemplate(
            "真实录屏",
            "【画面：录屏展示关键避坑设置，鼠标在关键勾选项打圈提示】",
            "这里有个 90% 的人都踩过的坑：参数千万别选默认值，调到这个档位才是最佳效果。",
        ),
        BeatTemplate(
            "真人出镜＋动效",
            "【画面：全屏展示贴图 A「实操避坑与完整清单」，全屏停留 2 秒建议截图】",
            "这张完整的操作清单建议大家直接截图保存，做的时候随时对着看。",
        ),
        BeatTemplate(
            "真人出镜＋动效",
            "【画面：切回真人近景，收尾升维口播，右下角弹出互动提示】",
            "工具永远只是放大器，真正的壁垒是你的业务思考。关注我，持续为你拆解实战方法！",
        ),
    ),
}
"""三张分镜表（V1 原文）。`tool_demo` 故意不在这里 —— 它落 `FALLBACK_BEATS_KEY`。"""

CARD_TABLES: Final[dict[str, CardTemplate]] = {
    "judgment_first": CardTemplate(
        title="《{topic} · 核心认知与避坑对照表》",
        type="对照清单卡",
        content=(
            "【低效误区】把 AI 当纯外挂，手动搬运重复流程\n"
            "【高效解法】自闭环工作流，输入即出成品\n"
            "【关键细节】系统级规则沉淀，杜绝每次从零调试"
        ),
    ),
    "short_fast": CardTemplate(
        title="{topic} 极速极简实操卡",
        type="极简操作卡",
        content="步骤一：拉取基础模板\n步骤二：注入核心业务规则\n步骤三：一键导出并验收",
    ),
    "tutorial_save_loop": CardTemplate(
        title="《{topic} · 完整实操闭环清单》",
        type="收藏级清单包",
        content=(
            "1. 核心准备：明确交付目标与标准样本\n"
            "2. 关键配置：自定义规则注入与边界限制\n"
            "3. 提效秘诀：避开默认参数误区，启用批处理\n"
            "4. 验收要点：严格对齐业务真实结果"
        ),
    ),
}
CARD_ID: Final = "贴图 A"
"""每张表都只有一张卡，V1 给的 id 恒为「贴图 A」。"""

BEAT_IDS: Final = ("S01", "S02", "S03", "S04", "S05", "S06", "S07")
"""分镜编号（V1 手写在每一行上，最多 7 行）。"""


class ScriptBeat(BaseModel):
    id: str
    channel: str = Field(description="三通道之一：真人出镜＋动效 / 真实录屏 / 全屏AI视频。")
    visual_cue: str = Field(description="内联画面指示，V1 的 `visualCue`（【画面：…】）。")
    speech: str = Field(description="这一句的口播原文 —— 它一定来自分镜表，不来自模型。")


class ScriptCard(BaseModel):
    id: str
    title: str
    type: str
    content: str


class DraftScript(BaseModel):
    """一份二创脚本。字段是 V1 的形状（键名换成 snake_case）。"""

    version: str
    title: str
    mode: str
    template_key: str
    template_name: str
    film_job: str
    beats_template_key: str = Field(
        description=(
            "**实际用了哪张分镜表**。`tool_demo` 没有自己的表，它的产出与 "
            "`tutorial_save_loop` 一字不差 —— 这个字段就是让那次降级看得见。"
        ),
    )
    total_beats: int
    estimated_duration_seconds: int = Field(description="总口播字数 / 4.0 取整，是换算不是测量。")
    beats: list[ScriptBeat]
    cards: list[ScriptCard]
    teleprompter_text: str = Field(description="纯提词器正文：`Sxx｜口播`，句间空行。")
    script_markdown: str = Field(description="完整的 script.md（V1 落盘的就是这一段）。")


def template_keys() -> tuple[str, ...]:
    """对外可请求的模板键（顺序即 `TEMPLATES` 的声明顺序）。"""
    return tuple(TEMPLATES)


def extract_fill_words(*, title: str) -> dict[str, str]:
    """标题 → `{topic}` / `{pain}` / `{solution}` 三个填空值（V1 的规则原样）。"""
    topic = title
    for word in TOPIC_STRIP_WORDS:
        topic = topic.replace(word, "")
    pain = DEFAULT_PAIN
    solution = DEFAULT_SOLUTION
    lowered = title.lower()
    for rule in TOPIC_RULES:
        if any(trigger in lowered for trigger in rule.lowered_triggers) or any(
            trigger in title for trigger in rule.exact_triggers
        ):
            pain = rule.pain
            solution = rule.solution
            break
    return {"topic": topic.strip(), "pain": pain, "solution": solution}


def generate_draft_script(
    *,
    topic: str = "",
    template_key: str = FALLBACK_BEATS_KEY,
    mode: DraftMode = "SHORT",
    source_title: str = "",
) -> DraftScript:
    """按模板拼一份二创脚本。**纯函数**：不读库、不进口播稿、不写文件。

    `topic` 空时退到 `source_title`（V1 的 `topic or source["title"]`），再空退到
    `DEFAULT_TITLE`。`template_key` 非法直接 `ValueError` —— V1 的 `.get(默认)` 会让
    "我要工具实战演示"变成"你给我教程型收藏闭环"，而且响应里看不出来。
    """
    if template_key not in TEMPLATES:
        msg = f"未知模板键：{template_key}（可选：{'、'.join(template_keys())}）"
        raise ValueError(msg)

    title = topic.strip() or source_title.strip() or DEFAULT_TITLE
    words = extract_fill_words(title=title)
    beats_key = template_key if template_key in BEAT_TABLES else FALLBACK_BEATS_KEY
    meta = TEMPLATES[template_key]

    beats = [
        ScriptBeat(
            id=BEAT_IDS[index],
            channel=beat.channel,
            visual_cue=beat.visual_cue,
            speech=beat.speech.format(**words),
        )
        for index, beat in enumerate(BEAT_TABLES[beats_key])
    ]
    card = CARD_TABLES[beats_key]
    cards = [
        ScriptCard(
            id=CARD_ID,
            title=card.title.format(**words),
            type=card.type,
            content=card.content.format(**words),
        )
    ]
    spoken_chars = sum(len(beat.speech) for beat in beats)
    return DraftScript(
        version=DRAFT_SPEC_VERSION,
        title=title,
        mode=mode,
        template_key=template_key,
        template_name=meta.name,
        film_job=meta.film_job,
        beats_template_key=beats_key,
        total_beats=len(beats),
        estimated_duration_seconds=int(spoken_chars / SPEECH_CHARS_PER_SECOND),
        beats=beats,
        cards=cards,
        teleprompter_text="\n\n".join(f"{beat.id}｜{beat.speech}" for beat in beats),
        script_markdown=_render_script_markdown(
            title=title,
            mode=mode,
            template_name=meta.name,
            film_job=meta.film_job,
            beats=beats,
            cards=cards,
        ),
    )


def _render_script_markdown(
    *,
    title: str,
    mode: str,
    template_name: str,
    film_job: str,
    beats: list[ScriptBeat],
    cards: list[ScriptCard],
) -> str:
    """拼 `script.md`（V1 的排版一字未改，包括那些 emoji 小标题）。"""
    spoken_chars = sum(len(beat.speech) for beat in beats)
    md_lines = [
        f"# 二创导演口播脚本：{title}",
        "",
        "> 遵循 `draft-ai-work-video` 导演级标准交付：Sxx 逐句口播 + 三通道视觉分镜 + 贴图卡片包。",
        "",
        "## 📋 拍摄决策头 (Decision Header)",
        f"- **作品定位**：{film_job}",
        f"- **创作模式**：{mode} (预计时长约 {int(spoken_chars / SPEECH_CHARS_PER_SECOND)} 秒)",
        f"- **采用模板**：{template_name}",
        "- **视觉通道**：真人出镜＋动效 (信任/断言) ｜ 真实录屏 (实操证明) "
        "｜ 全屏AI视频 (空间换气)",
        "",
        "---",
        "",
        "## 🎬 逐句分镜与口播脚本 (Director Spoken Units)",
        "",
    ]
    for beat in beats:
        md_lines.extend(
            [
                f"### {beat.id} · 【{beat.channel}】",
                beat.visual_cue,
                f"> **口播**：{beat.speech}",
                "",
            ]
        )
    md_lines.extend(["---", "", "## 🖼️ 观众高转化截图贴图包 (On-screen Cards)", ""])
    for card in cards:
        md_lines.extend(
            [
                f"### {card.id}：{card.title} ({card.type})",
                "```text",
                card.content,
                "```",
                "",
            ]
        )
    return "\n".join(md_lines)
