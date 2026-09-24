"""`tasks/reference.py`：V1 那三个纯本地函数的搬运判据。

一条总纲贯穿全部用例：**这套东西只搬运原文，不产出内容**。
所以最硬的一条断言是"每个候选句都是原文的子串" —— 生成式摘要会当场把它弄红，
而人眼看 markdown 是看不出区别的（V1 把这句话写在文件第一行就是这个道理）。
"""

from __future__ import annotations

from intelligence_hub_v2.tasks.reference import (
    KEY_POINT_LIMIT,
    MAX_SUMMARY_CHARS,
    MIN_TRANSCRIPT_CHARS,
    extractive_reference,
    normalize_transcript,
    speech_length,
    split_sentences,
)

_LONG_ENOUGH = "这是一条足够长的口播句子用来通过最短长度那道闸。"  # > SENTENCE_MIN_CHARS


def _text(*sentences: str) -> str:
    return "\n".join(sentences)


def _reference(text: str, **kwargs: object):
    return extractive_reference(
        title=kwargs.get("title", "测试作品"),  # type: ignore[arg-type]
        video_id="7",
        platform=str(kwargs.get("platform", "douyin")),
        url="https://www.douyin.com/video/7",
        cleaned_text=text,
        note_text=str(kwargs.get("note_text", "")),
    )


# --------------------------------------------------------------------------- #
# normalize_transcript
# --------------------------------------------------------------------------- #


def test_credits_and_blank_runs_are_dropped_but_paragraph_breaks_survive() -> None:
    raw = "第一行话   带着\t空白\n\n\n\n字幕BY翻译组\n第二行话"
    out = normalize_transcript(raw)
    assert out == "第一行话 带着 空白\n\n第二行话"
    assert "BY" not in out


def test_normalizing_is_idempotent() -> None:
    """归一两次必须与一次相同 —— 否则"要不要再归一一遍"会变成第二处真相。"""
    raw = "a  b\n\n\n\n字幕BYx\nc"
    once = normalize_transcript(raw)
    assert normalize_transcript(once) == once


def test_a_line_beginning_with_credits_goes_away_but_real_content_about_it_stays() -> None:
    """两条署名正则的边界：以"字幕"开头的整行、以及"…字幕/翻译 by …"。

    反过来，一句正常话里出现"翻译"两个字**不该**被当署名删掉 —— 那已经在改内容了，
    而这个模块的全部承诺就是"只搬运"。
    """
    assert normalize_transcript("字幕组压制 感谢分享\n正文一句很长很长") == "正文一句很长很长"
    assert normalize_transcript("本片中文字幕 by 某某\n正文一句很长很长") == "正文一句很长很长"
    assert normalize_transcript("这段翻译做得很烂，正文一句很长很长") == (
        "这段翻译做得很烂，正文一句很长很长"
    )


# --------------------------------------------------------------------------- #
# split_sentences 与长度口径
# --------------------------------------------------------------------------- #


def test_sentences_split_only_on_punctuation_and_short_fragments_are_dropped() -> None:
    """标点**留在句尾**（V1 的正则用的是向后看），碎片按字符数丢。

    标点留不留不是小事：`key_points` 与稿子都要能被下游 `split_sentences` 再切开。
    """
    text = _text("嗯。", "啊?", _LONG_ENOUGH)
    assert split_sentences(text) == [_LONG_ENOUGH]
    assert all(part[-1] in "。！？" for part in split_sentences(text))


def test_newlines_are_not_sentence_boundaries_for_the_splitter() -> None:
    """换行不算断句（V1 原式里先 `replace("\n", " ")`）：ASR 一句一行，
    若换行也算断点，`。` 补不补都无所谓了，§7.9 那条看护就空转了。"""
    assert split_sentences("第一行话很长很长\n第二行话也很长很长") == [
        "第一行话很长很长 第二行话也很长很长"
    ]


def test_speech_length_ignores_whitespace() -> None:
    """门限数的是**非空白字符**：换行与空格不该算内容。"""
    assert speech_length("我。 我。\n我。") == 6
    assert speech_length("   \n ") == 0
    filler = "字" * (MIN_TRANSCRIPT_CHARS - 1)
    assert speech_length(f"{filler}\n\n") < MIN_TRANSCRIPT_CHARS
    assert speech_length(filler + "字") >= MIN_TRANSCRIPT_CHARS


# --------------------------------------------------------------------------- #
# extractive_reference：**只搬运**
# --------------------------------------------------------------------------- #


def test_every_candidate_sentence_is_a_substring_of_the_source() -> None:
    """这条是"不是生成"的硬判据：任何一句改写、压缩、拼接都会红。"""
    text = _text(
        f"{_LONG_ENOUGH}第二句讲 AI 工具怎么用。",
        "第三句是流程与案例，也算内容。",
        "第四句跟视频无关但足够长，用来占位凑满八句候选。",
    )
    reference = _reference(text)
    for line in reference.key_points.splitlines():
        sentence = line.removeprefix("- ")
        assert sentence in text
    assert reference.content_summary == " ".join(text.split())[:MAX_SUMMARY_CHARS]


def test_the_file_says_what_it_is_not_on_the_first_screen() -> None:
    markdown = _reference(_text(_LONG_ENOUGH)).markdown
    assert "本地自动抽取参考，不是最终内容摘要或关键要点" in markdown
    assert markdown.startswith("# 测试作品")
    assert "- 平台: douyin" in markdown and "- 链接: https://www.douyin.com/video/7" in markdown


def test_keyword_hits_decide_which_sentences_get_picked() -> None:
    """提示词命中数 + 越靠前越高分（V1 原式）。删掉打分就等于"挑前八句"。"""
    filler = "这一句没有任何关键词只是把长度凑够而已。"
    hit = "这一句里有 AI 与工具两个关键词，长度也够。"
    reference = _reference(_text(filler, filler + filler, hit))
    assert hit in reference.key_points
    assert len(reference.key_points.splitlines()) >= 1


def test_candidates_keep_the_original_order_and_are_capped() -> None:
    sentences = [f"第{index}句讲 AI 工具与流程，长度足够通过最短判据。" for index in range(12)]
    reference = _reference(_text(*sentences))
    picked = [line.removeprefix("- ") for line in reference.key_points.splitlines()]
    assert len(picked) == KEY_POINT_LIMIT
    indexes = [int(sentence[1]) for sentence in picked]
    assert indexes == sorted(indexes), "候选句被打乱了顺序：读者看不到叙事线"


def test_content_summary_is_capped() -> None:
    long_text = _LONG_ENOUGH * 60
    assert len(_reference(long_text).content_summary) <= MAX_SUMMARY_CHARS


def test_an_empty_transcript_still_produces_a_file_without_inventing_content() -> None:
    """空稿子 → 空要点，但文件头仍在（"这条转过了，只是没内容"也要能被看出来）。"""
    reference = _reference("")
    assert reference.key_points == ""
    assert reference.content_summary == ""
    assert reference.summary_method == "local-extractive"
    assert "本文件性质" in reference.markdown


def test_the_note_text_is_used_when_there_is_no_speech() -> None:
    """V1 的兜底：没口播时用作品原文案顶上，但**照抄**不改写。"""
    note = "这是作品的原文案，长度也够当摘要用一段。"
    reference = _reference("", note_text=note)
    assert reference.content_summary == note
    assert "## 作品原文案" in reference.markdown


def test_normalizing_an_asr_transcript_does_not_change_its_line_count() -> None:
    """handler 里那句"`sentence_count` 与 `segments` 不会分叉"的前提，就在这条用例里。

    ASR 的稿子一行一句、没有署名行也没有空行；`normalize_transcript` 对它是恒等变换。
    哪天真变了（比如引擎开始输出多行句子），这条会红，那时 `sentence_count` 的口径要重定。
    """
    asr_out = _text("今天讲三件事。", "第一件是这个工具。", "第二件是流程与方法。")
    normalized = normalize_transcript(asr_out)
    assert len(normalized.splitlines()) == len(asr_out.splitlines())
    assert [line for line in normalized.splitlines() if line.strip()] == asr_out.splitlines()
