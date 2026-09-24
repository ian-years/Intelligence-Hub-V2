"""`core/runtime_env.py`：V1 §7.19 那条"注册表里有 ≠ 进程拿得到"的正解。

判据全部写成**关系**，不写"这台机器上 ffmpeg 在哪"：那种断言会在用户重开宿主之后
变成一条假红（PATH 补齐了，测试反而失败）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from intelligence_hub_v2.core import runtime_env as env_mod
from intelligence_hub_v2.core.config import AppConfig, PathsSection
from intelligence_hub_v2.core.runtime_env import (
    TOOL_COMMANDS,
    apply_runtime_environment,
    merge_path_entries,
    prepare_runtime_environment,
    registry_path_values,
    sync_path_from_registry,
)
from intelligence_hub_v2.tasks.preflight import _TOOLS


def _dirs(*paths: str) -> list[str]:
    return [os.pathsep.join(paths)] if paths else []


# --------------------------------------------------------------------------- #
# merge_path_entries
# --------------------------------------------------------------------------- #


def test_only_missing_directories_are_appended_and_the_existing_order_survives(
    tmp_path: Path,
) -> None:
    keep, fresh = tmp_path / "keep", tmp_path / "fresh"
    keep.mkdir()
    fresh.mkdir()
    added, merged = merge_path_entries(str(keep), _dirs(str(fresh)))
    assert added == [str(fresh)]
    parts = merged.split(os.pathsep)
    assert parts[0] == str(keep), "进程 PATH 是启动方刻意设置的那份，不许被盖到后面"
    assert parts[-1] == str(fresh)


def test_directories_that_no_longer_exist_are_not_brought_in(tmp_path: Path) -> None:
    """注册表里写着早已被删掉的目录是常态，搬进来只会让 `which` 多绕一圈。"""
    ghost = str(tmp_path / "never-created")
    added, merged = merge_path_entries("", _dirs(ghost))
    assert added == []
    assert ghost not in merged


def test_the_same_directory_written_differently_is_not_added_twice(tmp_path: Path) -> None:
    """Windows 路径大小写不敏感、斜杠两种都合法、结尾分隔符可有可无。"""
    real = tmp_path / "bin"
    real.mkdir()
    process = str(real)
    variants = [str(real).upper(), f"{real}{os.sep}", str(real).replace("\\", "/")]
    added, _ = merge_path_entries(process, _dirs(*variants))
    assert added == [], f"被当成不同目录重复追加：{added}"


def test_empty_path_entries_are_left_alone(tmp_path: Path) -> None:
    """Windows 上 PATH 里的空项表示当前目录，不参与比对，也不该被搬来搬去。"""
    real = tmp_path / "bin"
    real.mkdir()
    added, merged = merge_path_entries(f"{os.pathsep}{real!s}", _dirs(f"{os.pathsep}{real!s}"))
    assert added == []
    assert merged.split(os.pathsep)[0] == "", "原有那个空项被吃掉了"


# --------------------------------------------------------------------------- #
# 注册表那一侧
# --------------------------------------------------------------------------- #


def test_a_non_windows_platform_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "name", "posix")
    env = {"PATH": "/usr/bin"}
    assert sync_path_from_registry(env) == []
    assert env == {"PATH": "/usr/bin"}


def test_a_registry_that_cannot_be_read_never_breaks_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """组策略把键拿走、权限不够、值不存在：都只是补不全，不能让服务起不来。"""
    monkeypatch.setattr(os, "name", "nt")

    def boom() -> list[str]:
        raise OSError("registry unavailable")

    monkeypatch.setattr(env_mod, "registry_path_values", boom)
    env = {"PATH": "C:\\Existing\\bin"}
    assert sync_path_from_registry(env) == []
    assert env == {"PATH": "C:\\Existing\\bin"}


@pytest.mark.skipif(os.name != "nt", reason="只有 Windows 有注册表 PATH")
def test_reading_the_registry_yields_path_blobs() -> None:
    values = registry_path_values()
    assert values, "Windows 上至少该读到 Machine 或 User 那一份 Path"
    assert all(os.sep in value or "/" in value for value in values)


# --------------------------------------------------------------------------- #
# config.paths.* 那组显式覆盖
# --------------------------------------------------------------------------- #


def test_an_explicit_binary_directory_is_prepended_and_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(env_mod, "registry_path_values", lambda: [])  # 只测显式路径那一半
    binary = tmp_path / "ffmpeg-home" / "bin" / "ffmpeg.exe"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"\x00")
    config = AppConfig(paths={"ffmpeg": str(binary)})
    env = {"PATH": "C:\\Windows\\System32"}

    report = apply_runtime_environment(config, env)

    assert report.forced == {"ffmpeg": str(binary.parent.resolve())}
    assert env["PATH"].startswith(str(binary.parent.resolve()))
    resolved = report.resolved["ffmpeg"]
    assert resolved is not None
    # `shutil.which` 交回的是**盘上的大小写**（这台机器上写着 `ffmpeg.EXE`），
    # 所以比名字要比 casefold，比位置要比父目录。
    assert Path(resolved).name.casefold() == "ffmpeg.exe"
    assert str(Path(resolved).parent) == str(binary.parent.resolve())
    assert report.problems == ()


def test_a_configured_path_pointing_at_nothing_is_a_problem_not_a_silent_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """配了路径却悄悄用别人的二进制 = 最难查的"成功"。"""
    monkeypatch.setattr(env_mod, "registry_path_values", lambda: [])
    missing = tmp_path / "nope" / "ffprobe.exe"
    config = AppConfig(paths={"ffprobe": str(missing)})
    env = {"PATH": "C:\\Windows\\System32"}

    report = apply_runtime_environment(config, env)

    assert len(report.problems) == 1 and "ffprobe" in report.problems[0]
    assert "paths.ffprobe" in report.problems[0]
    assert report.forced == {}
    assert env == {"PATH": "C:\\Windows\\System32"}


def test_an_explicit_directory_already_on_the_path_is_not_inserted_twice(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "bin" / "node.exe"
    binary.parent.mkdir()
    binary.write_bytes(b"\x00")
    config = AppConfig(paths={"node": str(binary)})
    env = {"PATH": str(binary.parent)}

    report = apply_runtime_environment(config, env)

    assert report.forced == {"node": str(binary.parent)}
    assert env["PATH"].split(os.pathsep).count(str(binary.parent)) == 1


def test_applying_to_a_given_env_never_touches_the_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`apply_*` 只改交给它的那份 dict；改 `os.environ` 是 `prepare_*` 一个人的活。

    否则测试与调用方会互相污染（这条函数要在 preflight 里被反复问，不能每次改掉全进程）。
    """
    before = os.environ.get("PATH", "")
    binary = tmp_path / "bin" / "yt-dlp.exe"
    binary.parent.mkdir()
    binary.write_bytes(b"\x00")
    monkeypatch.setattr(env_mod, "registry_path_values", lambda: ["C:\\nonsense-not-there"])
    apply_runtime_environment(AppConfig(paths={"yt_dlp": str(binary)}), {"PATH": ""})
    assert os.environ.get("PATH", "") == before


def test_prepare_writes_into_the_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """注册表里一个真目录 + 一个早没了的目录：只补前者，且写进 `os.environ`。"""
    real = tmp_path / "Program Files" / "Anything"
    real.mkdir(parents=True)
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setenv("PATH", "C:\\Windows\\System32")
    monkeypatch.setattr(
        env_mod, "registry_path_values", lambda: [f"{real}{os.pathsep}{tmp_path / 'gone'}"]
    )

    report = prepare_runtime_environment(AppConfig())

    assert report.added == (str(real),)
    assert os.environ["PATH"].endswith(str(real))
    # 幂等：第二轮什么都不再加
    assert prepare_runtime_environment(AppConfig()).added == ()


# --------------------------------------------------------------------------- #
# 一张表对三处
# --------------------------------------------------------------------------- #


def test_the_path_knobs_are_the_tools_we_probe() -> None:
    """`config.paths.*` 的字段名、这张表、preflight 的探针清单必须同源。

    少一个字段 = 配置里那个旋钮没有读者（它会安静地骗人：写了 ffprobe 路径却没人用）；
    多一个字段 = preflight 报的红与实际用的二进制对不上。
    """
    assert set(TOOL_COMMANDS) == set(PathsSection.model_fields)
    assert tuple(TOOL_COMMANDS.items()) == _TOOLS


def test_resolved_covers_every_command_even_when_nothing_is_found(tmp_path: Path) -> None:
    """`resolved` 的键必须是全集：漏掉一个键与"那个键是 None"在调用方看起来完全不同。"""
    report = apply_runtime_environment(AppConfig(), {"PATH": str(tmp_path / "empty")})
    assert set(report.resolved) == set(TOOL_COMMANDS.values())


def test_apply_survives_an_unexpected_registry_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """注册表值理论上还可能是 REG_EXPAND_SZ 之外的怪类型；不能因此让预检崩掉。"""
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(env_mod, "registry_path_values", lambda: [None])  # type: ignore[list-item]
    report = apply_runtime_environment(AppConfig(), {"PATH": ""})
    assert report.added == ()


def test_report_changed_is_false_for_an_empty_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(env_mod, "registry_path_values", lambda: [])
    sink: Any = {"PATH": str(tmp_path)}
    assert apply_runtime_environment(AppConfig(), sink).changed is False
