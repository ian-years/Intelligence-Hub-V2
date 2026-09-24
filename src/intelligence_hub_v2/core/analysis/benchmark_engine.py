"""爆款拆解引擎：把一条口播稿拆成黄金钩子 + 逐句职能 + 起承转合结构（T5.2）。

来源：V1 `launcher/engine/benchmark_engine.py`（223 行，`策略:搬运`）。按 ADR-0018 决定 1
在 `core/analysis/` 适配 —— 它是**纯本地算法**：不接 LLM、不联网、不落库、不写文件。

## 它到底在算什么（用 V1 自己的术语）

1. 拿"口播稿首句 + 标题"去撞 `HOOK_PATTERNS` 那 5 条正则，**先命中者赢**（`break`），
   给它贴一个钩子类型（痛点反常识 / 强承诺教程 / 认知反转断言 / 高价值资源清单 /
   第一人称实战复盘）；全不中兜底成"悬念吸引型"。
2. 稿子按行拆开，每行按**位置**（首行固定 hook、末两行固定 CTA）与 `SENTENCE_ROLES`
   的关键词表贴一个"职能 + 建议画面 + 可复用评级"；都没命中时按进度分三档兜底。
3. 按 `duration_seconds`（缺时长时用 4.2 字/秒的默认语速）给每行推一个 `mm:ss` 时间戳。
4. 产出 `handoff`（二创交接包）：推荐创作模式 SHORT/LONG、推荐模板名、核心矛盾
   （= 钩子承诺）、以及前 3 条被评成"可复用"的句子。

## 规则表逐字保留

`HOOK_PATTERNS` / `SENTENCE_ROLES` / `STRUCTURE_BEATS` / `BORROW_RULES` / `AVOID_RULES`
/ 两个 `visible_evidence` 分支 / 密度文案 —— 全部一字未改。这些关键词与文案是 V1 在真实
爆款稿子上踩出来的产品判断，不是设计出来的：换一个词就等于换一份拆解结论，那是一次
产品决策，得单独说理由。看护见 `tests/unit/analysis/test_benchmark_rules.py`
（"每条规则都要被某个输入命中过" + 条数前置断言）。

## 与 V1 的七处不同（改名的改名、拆假接口的拆假接口，**没有一处改动规则的取值**）

1. 输出键从 camelCase 换成 snake_case（`lineByLine` → `line_by_line` 等）。V1 的驼峰是
   给它那个手写的 `launcher/app.js` 消费的；V2 前端类型由 OpenAPI 生成，没有历史包袱，
   而 snake_case 是 V2 全 API 的既有写法（`docs/specs/`）。
2. `totalWords` → `total_chars`：它算的一直是 `len("".join(lines))`，**是字符数不是词数**。
   留着那个名字就是让下一个读的人以为有个分词器。
3. `video_meta: dict` 那层"两个键名都试一下"（`video_title` 或 `title`）换成显式关键字参数。
   这是 V1 §7.11"三种命名"的形状，V2 只有一个来源（`videos` 表的行），不该继续兼容错名。
4. `_analyze_progression(raw_lines, duration_sec)` 的 **`duration_sec` 从来没被读过** ——
   四段 beats 的 `timeRange` 是硬编码的 `00:00 - 00:15 / 02:00 - 结束`。那个参数在 V1 里
   是装饰：它让"结构推进"看起来跟时长有关，其实只跟字数（密度那一行）有关。参数删掉，
   beats 常量留下（它是规则表），并且在这里写明：**beats 的时间轴与这条视频无关**。
5. `_generate_dos_and_donts(title, hook, raw_lines)` 的**三个参数一个都没用** —— 它返回的
   四条 borrow / 三条 avoid 对每条视频都一模一样。V2 把它降级成两个模块常量，不再伪装成
   "按这条视频生成的建议"。文案一字未改。
6. `duration_seconds` 不再 `int()` 截断：V2 的 `videos.duration_seconds` 列本来就是 Float，
   截断只会让 59.7 秒变成 59 秒。规则 `duration < 90 → SHORT` 的判定结果不受影响。
7. V1 在 `launcher_server.py` 里把结果写成 `benchmark-analysis-<id>.json` **磁盘缓存**
   （命中就直接返回旧文件）。V2 不落这张缓存：拆解是纯函数，输入是库里当前的行，
   而缓存会让"重跑转写之后"读到旧稿子的拆解结果，而响应里没有任何东西告诉调用方它是旧的。

## 2026-09-24 用真 schema 临时库跑出来的三条 V1 行为（照搬，不修；记账在这里）

1. **标题会盖过首句**：钩子匹配是"表序先命中者赢 × (首句 or 标题)"，所以一条以"千万别…"
   开头的稿子，只要标题里有"如何"，就会被定成第 2 条模式"强承诺教程"而不是第 3 条
   "认知反转断言"。那条 B 站样本（`如何把口播稿拆成可复用的结构` + 首句"千万别再用…"）实测如此。
2. **末两行恒为 CTA**：`idx >= total - 2` 排在关键词匹配之前，所以 7 行的稿子里第 6 行
   哪怕写的是"这里要注意一个避坑点"（bonus 的关键词）也会被贴成 CTA。行越少，这个影子越长。
3. **`key_takeaways` 结构性地只收 ▲ 那一档**：判据是子串 `"可复用"`，而 ★ 那几行写的是
   "极高复用" —— 三个连续字不在里面。于是 hook / solution / proof / cta 的句子永远进不了交接包。
   三条都有用例钉住（`tests/unit/analysis/test_benchmark_engine.py`）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from pydantic import BaseModel, Field

__all__ = [
    "AVOID_RULES",
    "BENCHMARK_SPEC_VERSION",
    "BORROW_RULES",
    "HOOK_PATTERNS",
    "PROGRESS_BANDS",
    "SENTENCE_ROLES",
    "STRUCTURE_BEAT_TEXT",
    "BenchmarkAnalysis",
    "CreationHandoff",
    "DosAndDonts",
    "HookBreakdown",
    "HookPattern",
    "LineBreakdown",
    "SentenceRole",
    "StructureBeat",
    "StructureProgression",
    "analyze_benchmark",
    "classify_sentence",
    "split_transcript_lines",
]

BENCHMARK_SPEC_VERSION: Final = "viral-video-benchmark/v1"
"""拆解规范的版本号，V1 原样。它出现在响应里是为了让下游能分辨"这是哪一版规则算的"。"""

DEFAULT_TITLE: Final = "未命名作品"
DEFAULT_CREATOR: Final = "未知创作者"
DEFAULT_PLATFORM: Final = "短视频"
"""三个兜底标签（V1 原值）。它们出现在 `lineByLine` 之外的标题位，不是规则命中结果。"""

DEFAULT_SPEECH_RATE: Final = 4.2
"""缺时长时假定 4.2 字/秒（V1 原值）。中文口播的正常语速，不是测出来的。"""

MIN_TIMELINE_SECONDS: Final = 30
"""有时长时，语速按 `字数 / max(时长, 30)` 算（V1 原值）：10 秒的稿子不会被算成闪电语速。"""

PROMISE_MIN_LINE_CHARS: Final = 6
"""首句超过 6 个字才把"核心承诺"取为首句本身，否则用标题套话（V1 原值）。"""

PUNCH_SHORT_LINE_CHARS: Final = 35
PUNCH_SCORE_SHORT: Final = 92
PUNCH_SCORE_LONG: Final = 85
PUNCH_SCORE_EMPTY: Final = 60
"""`punch_score` 的全部取值：首句 ≤35 字给 92，更长给 85，没有稿子给 60（V1 原值）。

**它是规则常数，不是测量** —— 没有任何打分模型，只有"首句长不长"这一个观察。
"""

CTA_TAIL_LINES: Final = 2
CTA_TAIL_RATIO: Final = 0.9
"""末段判为 CTA 的两个条件：倒数第 `CTA_TAIL_LINES` 行之内，或进度比 ≥ `CTA_TAIL_RATIO`。"""

DENSITY_HIGH_CHARS: Final = 800
"""信息密度的分界（非空白外的总字符数，V1 原值）。"""

SHORT_MAX_SECONDS: Final = 90
LONG_FALLBACK_CHARS: Final = 500
"""`handoff.recommended_mode` 的两条判据（V1 原值）。"""

TAKEAWAY_LIMIT: Final = 3
"""交接包里最多带几条要点（V1 原值）。"""

REUSABLE_MARK: Final = "可复用"
"""`reusable` 文案里的那个标记 —— `key_takeaways` 靠它筛句子（V1 原判据）。"""


@dataclass(frozen=True)
class HookPattern:
    """一条钩子模式。`pattern` 是**原样搬来的正则**，改它等于改产品判断。"""

    pattern: str
    hook_type: str
    description: str


@dataclass(frozen=True)
class SentenceRole:
    """一个逐句职能。`role_key` 是 V1 元组第一个元素（它在 V1 里被丢掉，只留文案）。"""

    role_key: str
    keywords: tuple[str, ...]
    job: str
    visual: str
    reusable: str


HOOK_PATTERNS: Final[tuple[HookPattern, ...]] = (
    HookPattern(
        r"(你(目前|是否|是不是|有没有|还在)|大家(有没有|都在)|很多人(不知道|以为))",
        "痛点反常识",
        "以第二人称直接唤醒目标受众现实卡点，迅速收拢注意力。",
    ),
    HookPattern(
        r"(如何|怎么|怎样|带你|手把手|教你|一分钟|分钟搞定|全流程)",
        "强承诺教程",
        "结果与交付物先行，明确给出观众看完能获得的确定性回报。",
    ),
    HookPattern(
        r"(别再|不要再|千万别|真正意义上|颠覆|颠覆了|取代|死掉)",
        "认知反转断言",
        "通过高冲突性行业断言激发好奇与认知失调，引发停留。",
    ),
    HookPattern(
        r"(开源|免费|工具|插件|网站|神器|代码|一键)",
        "高价值资源清单",
        "抛出稀缺或高效实用工具，直接激发收藏与转发冲动。",
    ),
    HookPattern(
        r"(我做|我在|一年|复盘|实测|深度体验)",
        "第一人称实战复盘",
        "以真实战绩与第一视角建立信任锚点，增加内容说服力。",
    ),
)
"""黄金钩子的 5 条模式（V1 原表，含正则与文案，一字未改）。**顺序有意义**：先命中者赢。"""

FALLBACK_HOOK_TYPE: Final = "悬念吸引型"
FALLBACK_HOOK_DESCRIPTION: Final = "开篇通过抛出核心思考引发观众好奇心与停留意愿。"
"""5 条都不中时的兜底（V1 原值）。"""

SENTENCE_ROLES: Final[tuple[SentenceRole, ...]] = (
    SentenceRole(
        "hook",
        ("你", "有没有", "为什么", "怎么", "如何", "今天", "到底", "最近", "颠覆", "其实"),
        "开篇建立痛点与高价值承诺",
        "真人出镜＋动效 (大字断言)",
        "★ 极高复用：短视频前3秒黄金开场，建议保留框架换用你自己的业务主题",
    ),
    SentenceRole(
        "problem",
        ("痛点", "问题", "很多", "难", "困扰", "麻烦", "慢", "效率低", "成本", "卡住"),
        "指出行业/受众常见卡点与误区",
        "真人出镜＋动效 (左右对照/痛点卡)",
        "▲ 结构可复用：精准戳中用户焦虑，建议替换为同类目标人群的真实场景",
    ),
    SentenceRole(
        "concept",
        ("其实", "本质", "核心", "定义", "逻辑", "原理", "所谓", "真正", "区别"),
        "拆解底层逻辑与认知升级",
        "真人出镜＋动效 (概念标签墙/教学图解)",
        "▲ 观点可复用：提炼底层认知，可用你的降维比喻重新阐释",
    ),
    SentenceRole(
        "solution",
        ("方法", "步骤", "第一", "第二", "第三", "首先", "其次", "接着", "方案", "技巧"),
        "给出系统化解决方案与操作路径",
        "真实录屏 (操作步骤演示)",
        "★ 极高复用：清晰的递进清单化交付，观众极易收藏",
    ),
    SentenceRole(
        "proof",
        ("演示", "看这里", "打开", "输入", "点击", "生成", "效果", "测试", "实操", "跑一下"),
        "展示真实操作与确定性结果证明",
        "真实录屏 (输入→动作→结果链)",
        "★ 极高复用：真实可复现的屏幕演示，杜绝假界面",
    ),
    SentenceRole(
        "bonus",
        ("注意", "避坑", "秘诀", "彩蛋", "关键点", "窍门", "隐藏功能", "顺便"),
        "抛出隐藏技巧或高阶避坑点",
        "全屏AI视频 (空间隐喻/氛围换气)",
        "▲ 节奏重置：在视频中后段提供额外信息奖励，提升完播率",
    ),
    SentenceRole(
        "cta",
        ("总结", "收藏", "关注", "下期", "评论区", "领取", "源码", "试试", "建议", "一起来"),
        "升维收束总结与行动转化指引",
        "真人出镜＋动效 (认知收束/行动卡)",
        "★ 极高复用：双层转化收拢（收藏+评论区互动）",
    ),
)
"""逐句职能表（V1 原表）。

两点 V1 的形状要说清：

- **`keywords` 只对中间 5 档生效**（problem / concept / solution / proof / bonus）。
  首行恒为 `hook`、末两行恒为 `cta`，由位置决定；`hook` 与 `cta` 的关键词表在 V1 里
  从来没被读过（`SENTENCE_ROLES[1:6]` 这个切片就是它），V2 保留表本身、并在这里写明。
- 匹配顺序 = 表顺序，先命中者赢。
"""

TUTORIAL_TITLE_WORDS: Final = ("如何", "怎么", "插件", "学习", "教程", "方法")
SCREEN_EVIDENCE_WORDS: Final = ("插件", "演示", "代码", "网站", "ppt", "html", "tool")
REUSABLE_TEMPLATE_NAME: Final = "教程型收藏闭环"
JUDGMENT_TEMPLATE_NAME: Final = "判断先于界面"
"""`handoff.recommended_template` 的两条判据与两个模板名（V1 原值）。"""

DENSITY_HIGH_LABEL: Final = "极高密度信息流（每8-10秒提供一个新认知/操作成果）"
DENSITY_STANDARD_LABEL: Final = "标准紧凑叙事（节奏平稳，适合教程理解）"

REWARD_CADENCE: Final = "采用【认知刺激 → 录屏实操 → 隐藏彩蛋】三层节奏交替，防止视觉疲劳"
PROOF_MOMENT: Final = "在进入实操前先亮出成品效果，缩短观众建立信任的等待时间"


@dataclass(frozen=True)
class ProgressBand:
    """没命中任何关键词时，按**进度位置**兜底的三档（V1 原值）。"""

    upper_ratio: float
    job: str
    visual: str
    reusable: str


PROGRESS_BANDS: Final[tuple[ProgressBand, ...]] = (
    ProgressBand(
        0.25,
        "铺垫问题背景与行业现状",
        "真人出镜＋动效 (说明图/痛点卡)",
        "▲ 场景可复用：引入观众共鸣点",
    ),
    ProgressBand(
        0.7,
        "核心实操拆解与论点递进",
        "真实录屏 (操作演示 / 局部特写)",
        "★ 极高复用：核心价值交付段落",
    ),
    ProgressBand(
        1.1,
        "进阶技巧提示与逻辑收束",
        "真人出镜＋动效 (认知收束金句)",
        "★ 极高复用：强化结论与专业度",
    ),
)
"""进度兜底三档。第三档的 `1.1` 是"其余全部"的意思（V1 的 `else` 分支）。"""


class HookBreakdown(BaseModel):
    """开篇黄金钩子的剖析。"""

    sentence: str = Field(description="稿子的第一行（钩子本体）。")
    type: str = Field(description="命中的钩子模式名；5 条都不中时是『悬念吸引型』。")
    description: str
    promise: str = Field(description="核心承诺：首句超过 6 个字时就是首句本身（去掉句末标点）。")
    visible_evidence: str = Field(description="开场建议配的视觉素材（按标题关键词二选一）。")
    punch_score: int = Field(
        description="92（首句 ≤35 字）或 85（更长），空稿兜底 60。规则常数，不是测量。"
    )


class LineBreakdown(BaseModel):
    """一行口播的职能拆解。"""

    index: int
    timestamp: str = Field(description="推算的 mm:ss 起点，按语速累计，不是稿子里的时间戳。")
    sentence: str
    role_key: str | None = Field(
        default=None,
        description="职能表里的键（hook/problem/…）；走进度兜底时没有键，为 null。",
    )
    role: str = Field(description="职能说明文案，V1 的 `role` 字段（它给的就是这句文案）。")
    visual: str
    reusable: str


class StructureBeat(BaseModel):
    phase: str
    time_range: str = Field(description="V1 的硬编码时间轴 —— 与这条视频的实际时长无关。")
    core_job: str


class StructureProgression(BaseModel):
    beats: list[StructureBeat]
    density: str
    reward_cadence: str
    proof_moment: str


class DosAndDonts(BaseModel):
    borrow: list[str] = Field(description="可借鉴清单（规范常量，每条视频相同）。")
    avoid: list[str] = Field(description="避坑清单（规范常量，每条视频相同）。")


class CreationHandoff(BaseModel):
    """交给下游二创的交接包。"""

    source_title: str
    creator: str
    platform: str
    recommended_mode: str = Field(description="SHORT / LONG。")
    recommended_template: str
    core_contradiction: str = Field(description="= hook.promise。")
    key_takeaways: list[str] = Field(description="前 3 条被评成『可复用』的原句。")


class BenchmarkAnalysis(BaseModel):
    """一条口播稿的完整拆解结果（纯函数产物，V2 不落库）。"""

    version: str
    title: str
    creator: str
    platform: str
    duration_seconds: float
    total_chars: int = Field(description="所有有效行的字符总数（不含换行）。V1 误名为 totalWords。")
    hook: HookBreakdown
    line_by_line: list[LineBreakdown]
    structure_progression: StructureProgression
    dos_and_donts: DosAndDonts
    handoff: CreationHandoff


_COMMENT_PREFIX = "#"
"""V1 会丢掉以 `#` 开头的行 —— `reference.md` 那类带元信息头的稿子直接喂进来时用的。"""


def split_transcript_lines(transcript_text: str) -> list[str]:
    """按行拆开：丢空行与 `#` 注释行，其余**逐字保留**（不按标点再切）。"""
    return [
        stripped
        for raw in transcript_text.splitlines()
        if (stripped := raw.strip()) and not stripped.startswith(_COMMENT_PREFIX)
    ]


def match_hook(*, title: str, first_line: str) -> HookPattern | None:
    """5 条钩子模式里第一条命中的（首句优先于标题，先命中者赢）。都没中返回 None。"""
    for item in HOOK_PATTERNS:
        if re.search(item.pattern, first_line) or re.search(item.pattern, title):
            return item
    return None


def classify_sentence(*, line: str, index: int, total: int) -> tuple[str | None, str, str, str]:
    """一行口播 → `(role_key, 职能文案, 建议画面, 可复用评级)`。

    判据顺序与 V1 一致：首行 → 末段 → 中间 5 档关键词 → 按进度兜底。
    `role_key` 在兜底分支是 `None`（V1 把键丢了，所以那三档从来没有键）。
    """
    ratio = index / max(1, total - 1)

    if index == 0:
        head = SENTENCE_ROLES[0]
        return head.role_key, head.job, head.visual, head.reusable

    if index >= total - CTA_TAIL_LINES or ratio >= CTA_TAIL_RATIO:
        tail = SENTENCE_ROLES[6]
        return tail.role_key, tail.job, tail.visual, tail.reusable

    for candidate in SENTENCE_ROLES[1:6]:
        if any(keyword in line for keyword in candidate.keywords):
            return candidate.role_key, candidate.job, candidate.visual, candidate.reusable

    # 前两档按进度比例判，第三档就是 V1 的那个 `else`：剩下的全部（它的 upper_ratio 不参与判定）。
    *bands, catch_all = PROGRESS_BANDS
    for band in bands:
        if ratio < band.upper_ratio:
            return None, band.job, band.visual, band.reusable
    return None, catch_all.job, catch_all.visual, catch_all.reusable


def analyze_benchmark(
    *,
    title: str,
    transcript_text: str,
    creator: str = "",
    platform: str = "",
    duration_seconds: float | None = None,
) -> BenchmarkAnalysis:
    """拆解一条口播稿。**纯函数**：同样的输入必然给出同样的输出，不读库、不写文件。"""
    clean_title = title.strip() or DEFAULT_TITLE
    clean_creator = creator.strip() or DEFAULT_CREATOR
    clean_platform = platform.strip() or DEFAULT_PLATFORM
    duration = float(duration_seconds or 0)

    lines = split_transcript_lines(transcript_text)
    if not lines:
        return _empty_result(title=clean_title, creator=clean_creator, platform=clean_platform)

    first_line = lines[0]
    total_chars = len("".join(lines))
    hook = _analyze_hook(title=clean_title, first_line=first_line)
    line_by_line = _line_by_line(lines, duration_seconds=duration)
    progression = _analyze_progression(total_chars=total_chars)

    mode = (
        "SHORT"
        if (duration and duration < SHORT_MAX_SECONDS) or total_chars < LONG_FALLBACK_CHARS
        else "LONG"
    )
    template = (
        REUSABLE_TEMPLATE_NAME
        if any(w in clean_title for w in TUTORIAL_TITLE_WORDS)
        else JUDGMENT_TEMPLATE_NAME
    )

    return BenchmarkAnalysis(
        version=BENCHMARK_SPEC_VERSION,
        title=clean_title,
        creator=clean_creator,
        platform=clean_platform,
        duration_seconds=duration,
        total_chars=total_chars,
        hook=hook,
        line_by_line=line_by_line,
        structure_progression=progression,
        dos_and_donts=DosAndDonts(borrow=list(BORROW_RULES), avoid=list(AVOID_RULES)),
        handoff=CreationHandoff(
            source_title=clean_title,
            creator=clean_creator,
            platform=clean_platform,
            recommended_mode=mode,
            recommended_template=template,
            core_contradiction=hook.promise,
            key_takeaways=[
                line.sentence for line in line_by_line if REUSABLE_MARK in line.reusable
            ][:TAKEAWAY_LIMIT],
        ),
    )


def _analyze_hook(*, title: str, first_line: str) -> HookBreakdown:
    """开篇钩子剖析（V1 的两条二选一规则原样）。"""
    matched = match_hook(title=title, first_line=first_line)
    hook_type = matched.hook_type if matched else FALLBACK_HOOK_TYPE
    description = matched.description if matched else FALLBACK_HOOK_DESCRIPTION

    promise = f"通过《{title}》向观众拆解高效方法与实战路径"
    if len(first_line) > PROMISE_MIN_LINE_CHARS:
        promise = first_line.rstrip("，。？！,.?!")

    visible_evidence = "真人近景半身出镜 + 核心大字断言贴片 (前1.5秒完成视觉微变化)"
    if any(word in title.lower() for word in SCREEN_EVIDENCE_WORDS):
        visible_evidence = "真实产品/插件界面高清录屏 + 最终成品效果特写 (0.0s 结果先亮)"

    return HookBreakdown(
        sentence=first_line,
        type=hook_type,
        description=description,
        promise=promise,
        visible_evidence=visible_evidence,
        punch_score=(
            PUNCH_SCORE_SHORT if len(first_line) <= PUNCH_SHORT_LINE_CHARS else PUNCH_SCORE_LONG
        ),
    )


def _line_by_line(lines: list[str], *, duration_seconds: float) -> list[LineBreakdown]:
    """逐句拆解 + 按语速推时间戳。"""
    total_chars = sum(len(line) for line in lines)
    base_speed = (
        (total_chars / max(duration_seconds, MIN_TIMELINE_SECONDS))
        if duration_seconds
        else DEFAULT_SPEECH_RATE
    )

    results: list[LineBreakdown] = []
    elapsed_sec = 0.0
    for idx, line in enumerate(lines):
        role_key, job, visual, reusable = classify_sentence(line=line, index=idx, total=len(lines))
        results.append(
            LineBreakdown(
                index=idx + 1,
                timestamp=f"{int(elapsed_sec // 60):02d}:{int(elapsed_sec % 60):02d}",
                sentence=line,
                role_key=role_key,
                role=job,
                visual=visual,
                reusable=reusable,
            )
        )
        elapsed_sec += max(1.5, len(line) / base_speed)
    return results


def _analyze_progression(*, total_chars: int) -> StructureProgression:
    """起承转合。**四段 beats 是规范常量**，只有 `density` 真的看了输入（字符数）。"""
    density = DENSITY_HIGH_LABEL if total_chars > DENSITY_HIGH_CHARS else DENSITY_STANDARD_LABEL
    return StructureProgression(
        beats=[
            StructureBeat(phase=beat.phase, time_range=beat.time_range, core_job=beat.core_job)
            for beat in STRUCTURE_BEAT_TEXT
        ],
        density=density,
        reward_cadence=REWARD_CADENCE,
        proof_moment=PROOF_MOMENT,
    )


def _empty_result(*, title: str, creator: str, platform: str) -> BenchmarkAnalysis:
    """稿子存在但一行正文都没有（空文件、只有 `#` 注释）。

    注意与 API 层的分工：**没有口播稿是 404**（路由判），走到这里说明"有稿、稿子是空的"。
    """
    return BenchmarkAnalysis(
        version=BENCHMARK_SPEC_VERSION,
        title=title,
        creator=creator,
        platform=platform,
        duration_seconds=0,
        total_chars=0,
        hook=HookBreakdown(
            sentence=title,
            type="基础标题",
            description="暂无转写文本",
            promise=title,
            visible_evidence="—",
            punch_score=PUNCH_SCORE_EMPTY,
        ),
        line_by_line=[],
        structure_progression=StructureProgression(
            beats=[], density="—", reward_cadence="—", proof_moment="—"
        ),
        dos_and_donts=DosAndDonts(borrow=[], avoid=[]),
        handoff=CreationHandoff(
            source_title=title,
            creator=creator,
            platform=platform,
            recommended_mode="SHORT",
            recommended_template=REUSABLE_TEMPLATE_NAME,
            core_contradiction="",
            key_takeaways=[],
        ),
    )


@dataclass(frozen=True)
class _BeatText:
    phase: str
    time_range: str
    core_job: str


STRUCTURE_BEAT_TEXT: Final[tuple[_BeatText, ...]] = (
    _BeatText(
        "起 · 痛点唤醒",
        "00:00 - 00:15",
        "前3秒抛出黄金钩子，明确指出观众痛点与预期收益",
    ),
    _BeatText(
        "承 · 认知反转",
        "00:15 - 00:45",
        "打破传统低效做法，给出“判断先于界面”的高效新认知",
    ),
    _BeatText(
        "转 · 证明链条",
        "00:45 - 02:00",
        "真实录屏演示核心功能与实操步骤，建立确定性证据",
    ),
    _BeatText(
        "合 · 升维收束",
        "02:00 - 结束",
        "一句话升维回收痛点（不是X而是Y），引导观众收藏实践",
    ),
)
"""起承转合四段（V1 原文，含那个硬编码时间轴）。"""

BORROW_RULES: Final[tuple[str, ...]] = (
    "🎯 【开篇直奔主题】：前3秒坚决不做冗长的自我介绍，第一句话即给出核心判断或结果承诺；",
    "🪜 【递进逻辑骨架】：遵循「痛点唤醒 → 认知反转 → 真实证明 → 升维收束」的标准短视频爆款路径；",
    "💎 【一处必截图资产】：设置清晰的清单、步骤图或参数配置卡片，提供明确的收藏价值锚点；",
    "🎥 【三通道视觉切换】：真人观点用大字动效板，操作用真实录屏，抽象隐喻适度穿插全屏AI镜头。",
)
AVOID_RULES: Final[tuple[str, ...]] = (
    "🚫 【切勿硬套人设】：不要复制原作者的独家口癖、个人履历、面部特征或社区身份；",
    "🚫 【切勿空口承诺】：涉及工具或代码演示时，必须有真实操作结果，杜绝仅靠口播假吹；",
    "🚫 【切勿贪多嚼不烂】：单条短视频聚焦解决 1 个最核心痛点，多层复杂体系应拆为合集。",
)
"""可借鉴 / 避坑两份清单（V1 原文）。

V1 的 `_generate_dos_and_donts(title, hook, raw_lines)` 收下三个参数却一个都不用，
所以这两份清单**对每条视频都相同** —— 这里就干脆写成常量，不再伪装成"按这条视频算的"。
"""
