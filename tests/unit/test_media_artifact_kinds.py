"""产物类型的看护：`MediaArtifact` 加成员时，入库层必须把每条路径都落地。

这里没有一条用例是"某个输入产出某个输出"。它断言的是一个**关系**：
`typing.get_args(MediaArtifact)` 里每一种产物，它自己声明的 `Path` 字段
（`collect._artifact_fields()` 摊平时读的就是那几个）必须**一个不落地**出现在
入库草稿的 `media_path` + `media_aux_paths_json` 里。

为什么值得单独钉：`MediaArtifact` 是判别联合，而摊平它的那段代码用 `isinstance` 分支。
新加一种产物时最坏的不是忘改分支（那会 AttributeError），而是新产物**恰好**带着
分支认得的字段名 —— 于是它按另一族的形状入库，多出来的那几条路径静默变成空值。
"库里少一列内容而采集全绿"正是本仓库反复付学费的那一族（AGENTS.md §5、V1 §7.11）。

联合成员从类型里取，不在测试里再抄一份清单：抄的那一份会漂。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import UnionType
from typing import Any, Literal, get_args, get_origin

import pytest
from pydantic import BaseModel

from intelligence_hub_v2.models.creator import CreatorRef
from intelligence_hub_v2.models.media import MediaArtifact, total_size_bytes
from intelligence_hub_v2.models.video import VideoMeta
from intelligence_hub_v2.tasks import collect
from intelligence_hub_v2.tasks.collect import build_video_draft

DATA = Path("data-root")

ARTIFACT_CLASSES: tuple[type[BaseModel], ...] = get_args(MediaArtifact)
"""直接从联合类型取，**不许在这里硬写成员清单** —— 那份清单本身会漂。"""

_PATH_FIELDS = ("path", "video_path", "audio_path", "extra_paths")


def _is_path_field(annotation: object) -> bool:
    return annotation is Path or (
        get_origin(annotation) is tuple and get_args(annotation) == (Path, ...)
    )


def _sample(model: type[BaseModel]) -> Any:
    """按模型自己声明的字段造一个"每条路径都不重样"的实例。

    从 `model_fields` 推而不是给每个类手写一份构造：手写的那份在新加字段时会悄悄
    沿用旧形状（少给一个可选字段照样构造成功），而这里要的恰恰是"字段一多就逼我看一眼"。
    """
    values: dict[str, Any] = {}
    for name, field in model.model_fields.items():
        annotation = field.annotation
        if annotation is Path:
            values[name] = DATA / f"{model.__name__}__{name}"
        elif _is_path_field(annotation):
            values[name] = (DATA / f"{name}__1", DATA / f"{name}__2")
        elif get_origin(annotation) is Literal:
            # `media_source` 这类字面量枚举：取第一个合法值。挑哪个都不影响这条看护的判据
            # （它看的是路径落没落地），但**不许**在这里抄一份清单 —— 那是第二处真相。
            values[name] = get_args(annotation)[0]
        elif get_origin(annotation) is UnionType and type(None) in get_args(annotation):
            values[name] = None
        elif annotation is int:
            values[name] = 7
        elif annotation is float:
            values[name] = 7.0
    return model.model_validate(values)


def _video_meta() -> VideoMeta:
    ref = CreatorRef(
        platform="probe",
        platform_id="c1",
        profile_url="https://example.com/c1",  # type: ignore[arg-type]
    )
    return VideoMeta(
        platform="probe",
        platform_video_id="p1",
        creator_ref=ref,
        title="标题",
        webpage_url="https://example.com/p1",  # type: ignore[arg-type]
    )


class _Files:
    """只够入库层用到的那半个 `FileStorage`：绝对 → 相对 `data/`。"""

    def rel(self, path: Path) -> str:
        return rel(path)


def rel(path: Path) -> str:
    return path.relative_to(DATA).as_posix()


def test_the_union_yields_at_least_two_classes_so_this_guard_is_not_a_no_op() -> None:
    """`A | B` 拼成别的写法时 `get_args` 会给空元组，而空参数的 parametrize **零收集不报错**。

    没有这一条，下面所有用例可以在契约被改坏的同时安静地一条都不跑。
    """
    assert len(ARTIFACT_CLASSES) >= 2, f"联合里只剩 {ARTIFACT_CLASSES!r}"
    names = {model.__name__ for model in ARTIFACT_CLASSES}
    assert names.isdisjoint({"object", "NoneType"}), names


@pytest.mark.parametrize("model", ARTIFACT_CLASSES, ids=lambda m: m.__name__)
def test_every_path_a_kind_can_carry_reaches_the_video_row(model: type[BaseModel]) -> None:
    """产物声明的路径字段 ⊆ 入库后能看见的路径。缺一条就红，并点名缺的那条。"""
    declared = [
        name for name, field in model.model_fields.items() if _is_path_field(field.annotation)
    ]
    assert declared, f"{model.__name__} 一个路径字段都没有，这条看护对它没有意义"

    artifact = _sample(model)
    ctx = type("Ctx", (), {"files": _Files()})()
    row = build_video_draft(_video_meta(), creator_id=1, artifact=artifact, ctx=ctx)

    aux = json.loads(row.media_aux_paths_json)
    landed = {row.media_path, *(str(item) for item in aux)}
    expected: set[str | None] = set()
    for name in declared:
        value = getattr(artifact, name)
        for path in value if isinstance(value, tuple) else [value]:
            expected.add(rel(path))

    missing = expected - landed
    assert not missing, (
        f"{model.__name__} 声明了 {declared}，入库后少了 {sorted(missing)} —— "
        f"补 `collect._artifact_fields()` 的分支。`media_aux_paths_json` 那一列装的是"
        f"「同一条作品的其他产物文件」，不是只有音频轨（ADR-0019）。"
    )


@pytest.mark.parametrize("model", ARTIFACT_CLASSES, ids=lambda m: m.__name__)
def test_every_kind_answers_the_size_question_with_a_real_number(model: type[BaseModel]) -> None:
    """体积不许有第三种答案（0 或抛）。

    算成 0 的失效方式是"清单说这轮写了 0 字节，磁盘上却躺着一堆文件"——
    那笔账查不下去（`total_size_bytes` 的口径见 ADR-0019 决定 3）。
    """
    assert total_size_bytes(_sample(model)) > 0


def test_the_field_names_this_guard_relies_on_are_still_the_ones_collect_reads() -> None:
    """看护自己也要防空转。

    `_artifact_fields()` 改成读别的字段名（比如把 `extra_paths` 更名）时，
    上面那条"⊆"断言会因为两边同时变空而仍然成立 —— 那才是最难发现的假绿。
    """
    source = Path(collect.__file__).read_text(encoding="utf-8")
    for field in _PATH_FIELDS:
        assert field in source, f"collect 里已经不再出现 {field}，这份看护的判据该跟着换"
