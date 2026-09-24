"""脚本生成引擎：**每一句都要能在规则表里找到出处**。

V1 这个引擎不是抽取式、更不是生成式 —— 它是"四张写死的分镜表 + 三处填空"
（见 `core/analysis/draft_engine.py` 模块 docstring）。所以"不许幻觉"这条不变量在这里
只能写成它的真实形状：

> 输出里的每一句口播，把 `{topic}` / `{pain}` / `{solution}` 三个填空值还原成占位符之后，
> 必须**逐字等于** `BEAT_TABLES` / `CARD_TABLES` 里的某一行模板。

换句话说：引擎唯一被允许"发明"的内容，是调用方自己给的那个主题串。
哪天有人往表外塞一句话（哪怕是"看起来更顺"的过渡语），这条用例会立刻红。
"""

from __future__ import annotations

import pytest

from intelligence_hub_v2.core.analysis.draft_engine import (
    BEAT_TABLES,
    CARD_TABLES,
    DEFAULT_TITLE,
    SPEECH_CHARS_PER_SECOND,
    TEMPLATES,
    TOPIC_RULES,
    DraftScript,
    extract_fill_words,
    generate_draft_script,
    template_keys,
)

ALL_SPEECH_TEMPLATES = {beat.speech for table in BEAT_TABLES.values() for beat in table}
ALL_CUE_TEMPLATES = {beat.visual_cue for table in BEAT_TABLES.values() for beat in table}
ALL_CARD_TITLES = {card.title for card in CARD_TABLES.values()}
ALL_CARD_CONTENTS = {card.content for card in CARD_TABLES.values()}

# 四个主题：一个什么都不触发（走两个默认句），三个各触发一条规则。
TOPICS: tuple[str, ...] = (
    "量子堆肥",
    "AI-native 组织架构",
    "涨粉机制",
    "视频合集插件",
)

# 一个主题都不在的标题：只用来触发规则，不出现在输出里（`{topic}` 会先被剥掉疑问词）。
RULE_TRIGGERS: tuple[tuple[str, str], ...] = (
    ("ai", "传统团队把 AI 当打工外挂，结果组织臃肿、交付依然缓慢"),
    ("native", "传统团队把 AI 当打工外挂，结果组织臃肿、交付依然缓慢"),
    ("组织", "传统团队把 AI 当打工外挂，结果组织臃肿、交付依然缓慢"),
    ("twitter", "天天日更几小时却毫无互动，找不到爆款的底层机制"),
    ("涨粉", "天天日更几小时却毫无互动，找不到爆款的底层机制"),
    ("内容", "天天日更几小时却毫无互动，找不到爆款的底层机制"),
    ("youtube", "长视频动辄半小时根本看不完，抓不住重点浪费大量时间"),
    ("视频", "长视频动辄半小时根本看不完，抓不住重点浪费大量时间"),
    ("插件", "长视频动辄半小时根本看不完，抓不住重点浪费大量时间"),
)


def test_the_template_tables_are_all_present() -> None:
    """前置断言：四张模板、三张分镜表、三条规则、两个默认句都在。

    `tool_demo` **故意**没有分镜表（V1 的缺口，见模块 docstring 第 3 条）。
    """
    assert len(TEMPLATES) == 4
    assert set(template_keys()) == {
        "tutorial_save_loop",
        "judgment_first",
        "tool_demo",
        "short_fast",
    }
    assert set(BEAT_TABLES) == {"tutorial_save_loop", "judgment_first", "short_fast"}
    assert set(CARD_TABLES) == set(BEAT_TABLES)
    assert len(TOPIC_RULES) == 3
    assert ALL_SPEECH_TEMPLATES, "分镜表被清空了，下面所有用例都会空转"
    assert len(ALL_SPEECH_TEMPLATES) == sum(len(t) for t in BEAT_TABLES.values()), "有两句撞车了"


@pytest.mark.parametrize("template_key", template_keys())
@pytest.mark.parametrize("topic", TOPICS)
def test_every_line_comes_from_a_rule_or_from_the_callers_topic(
    template_key: str, topic: str
) -> None:
    """反幻觉闸：还原填空值之后，每一句都必须逐字命中规则表。"""
    script = generate_draft_script(topic=topic, template_key=template_key)
    words = _fill_words(topic)
    assert script.beats, "没有分镜，下面的循环在空转"
    for beat in script.beats:
        assert _unfill(beat.speech, words) in ALL_SPEECH_TEMPLATES
        assert beat.visual_cue in ALL_CUE_TEMPLATES
    for card in script.cards:
        assert _unfill(card.title, words) in ALL_CARD_TITLES
        assert _unfill(card.content, words) in ALL_CARD_CONTENTS


def _fill_words(topic: str) -> dict[str, str]:
    """把引擎该填的三个值自己算一遍（与 `extract_fill_words` 同判据，用于反向替换）。"""
    return extract_fill_words(title=topic)


def _unfill(text: str, words: dict[str, str]) -> str:
    """把三个填空值换回占位符 —— 长串先换，避免主题恰好是某条文案的子串。"""
    out = text
    for key in ("pain", "solution", "topic"):
        value = words[key]
        if value:
            out = out.replace(value, f"{{{key}}}")
    return out


@pytest.mark.parametrize("template_key", template_keys())
def test_beat_count_and_ids_come_from_the_selected_table(template_key: str) -> None:
    script = generate_draft_script(topic="量子堆肥", template_key=template_key)
    table = BEAT_TABLES[script.beats_template_key]
    assert script.total_beats == len(script.beats) == len(table)
    assert [beat.id for beat in script.beats] == [
        f"S{index:02d}" for index in range(1, len(table) + 1)
    ]
    assert len(script.cards) == 1


def test_tool_demo_reports_which_table_it_actually_used() -> None:
    """V1 的 `tool_demo` 没有分镜表却自称"工具实战演示"。V2 保留产出、把降级写进字段。"""
    demo = generate_draft_script(topic="量子堆肥", template_key="tool_demo")
    tutorial = generate_draft_script(topic="量子堆肥", template_key="tutorial_save_loop")
    assert demo.template_name == "工具实战演示"
    assert demo.beats_template_key == "tutorial_save_loop"
    assert [beat.speech for beat in demo.beats] == [beat.speech for beat in tutorial.beats]


@pytest.mark.parametrize("template_key", template_keys())
def test_mode_is_an_echo_and_never_changes_the_script(template_key: str) -> None:
    """`mode` 在 V1 里就只是回显。要更短的本子得换 `short_fast`，不是换 mode。"""
    short = generate_draft_script(topic="量子堆肥", template_key=template_key, mode="SHORT")
    long = generate_draft_script(topic="量子堆肥", template_key=template_key, mode="LONG")
    assert [beat.speech for beat in short.beats] == [beat.speech for beat in long.beats]
    assert (short.mode, long.mode) == ("SHORT", "LONG")


@pytest.mark.parametrize(("needle", "pain"), RULE_TRIGGERS)
def test_each_rule_trigger_selects_its_pain_pair(needle: str, pain: str) -> None:
    """三条规则的每个关键词都能触发对应痛点，且解法成对出现。"""
    script = generate_draft_script(topic=f"关于{needle}的事", template_key="judgment_first")
    assert pain in script.beats[1].speech
    expected_solution = next(rule.solution for rule in TOPIC_RULES if rule.pain == pain)
    assert expected_solution in script.beats[2].speech


def test_the_first_matching_rule_wins() -> None:
    """同时命中两条规则时按表序取第一条（V1 的 if/elif 链）。"""
    script = generate_draft_script(topic="AI 内容涨粉", template_key="judgment_first")
    assert "传统团队把 AI 当打工外挂" in script.beats[1].speech


def test_no_trigger_means_both_default_lines() -> None:
    script = generate_draft_script(topic="量子堆肥", template_key="tutorial_save_loop")
    assert "很多人面对这个场景还在手动盲测" in script.beats[1].speech
    assert "掌握这套核心底层逻辑与工具链" in script.beats[3].speech


def test_question_prefixes_are_stripped_from_the_topic() -> None:
    """`如何/怎样/为什么` 被剥掉后剩下的那串才是 `{topic}`（V1 原式）。"""
    for word in ("如何", "怎样", "为什么"):
        script = generate_draft_script(topic=f"{word}做量子堆肥")
        assert script.beats[0].speech.count("量子堆肥") == 1
        assert word not in script.beats[0].speech
    stripped = generate_draft_script(topic="如何做量子堆肥")
    plain = generate_draft_script(topic="做量子堆肥")
    assert stripped.beats == plain.beats


def test_duration_estimate_is_a_conversion_of_the_spoken_text() -> None:
    script = generate_draft_script(topic="量子堆肥", template_key="short_fast")
    spoken = sum(len(beat.speech) for beat in script.beats)
    assert script.estimated_duration_seconds == int(spoken / SPEECH_CHARS_PER_SECOND)
    longer = generate_draft_script(topic="量子堆肥", template_key="tutorial_save_loop")
    assert longer.estimated_duration_seconds > script.estimated_duration_seconds


def test_teleprompter_text_is_exactly_the_spoken_lines_in_order() -> None:
    script = generate_draft_script(topic="量子堆肥", template_key="judgment_first")
    blocks = script.teleprompter_text.split("\n\n")
    assert len(blocks) == script.total_beats
    assert blocks == [f"{beat.id}｜{beat.speech}" for beat in script.beats]
    assert "画面" not in script.teleprompter_text, "提词器里不该混进分镜指示"


def test_script_markdown_carries_every_beat_and_card_verbatim() -> None:
    script = generate_draft_script(topic="量子堆肥", template_key="tutorial_save_loop")
    assert script.script_markdown.startswith(f"# 二创导演口播脚本：{script.title}")
    for beat in script.beats:
        assert beat.speech in script.script_markdown
        assert beat.visual_cue in script.script_markdown
        assert f"### {beat.id} · 【{beat.channel}】" in script.script_markdown
    for card in script.cards:
        assert card.content in script.script_markdown
        assert f"### {card.id}：{card.title}" in script.script_markdown
    assert script.script_markdown.count("### S") == script.total_beats


def test_the_source_title_is_used_only_when_the_caller_gives_no_topic() -> None:
    from_title = generate_draft_script(source_title="如何整理口播稿")
    # 标题原样进 title；被剥掉疑问词的只有 {topic} 那处填空
    assert from_title.title == "如何整理口播稿"
    assert "整理口播稿" in from_title.beats[0].speech
    both = generate_draft_script(topic="量子堆肥", source_title="别的标题")
    assert both.title == "量子堆肥"
    neither = generate_draft_script()
    assert neither.title == DEFAULT_TITLE
    assert isinstance(neither, DraftScript)


def test_an_unknown_template_is_refused_not_silently_swapped() -> None:
    """V1 的 `dict.get(默认)`：要 A 给 B 还不说。V2 直接 ValueError。"""
    with pytest.raises(ValueError, match="未知模板键"):
        generate_draft_script(topic="量子堆肥", template_key="不存在的模板")
