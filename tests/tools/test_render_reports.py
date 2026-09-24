"""`tools/render_reports.py`（T5.4 薄壳）的用例。

薄壳只有一件职责：把一次调用转交给 `ported/reports/` 里的 V1 脚本，并**按产物**判成败。
四条不变量各配一个防空转的前置：

1. **退出码不是判据。** 子进程返回 0 而产物不存在 / 是空壳 / 内嵌 JSON 解析不出来，三种都要红
   （`test_a_zero_exit_without_a_real_artifact_is_not_success` 五条参数化就是变异检查：
   把判据退回成只看退出码，这五条当场全绿）。反向那条 `…_is_success` 挡住"永真断言"。
2. **`cwd` / 产物不指对时拒跑**，而不是让子进程把 `src/` 写脏 —— V1 的 `ROOT` 搬进包里之后
   指向 `src/…/ported/reports/`（T5.1 已踩过一次）。
3. **缺输入时如实失败**：原文 + 修复动作 + 不落任何产物。
4. **argv 只指向搬运区那三份脚本**，每条路径都是绝对路径，映射与目录清单双向对齐。

编排类用例 monkeypatch `_execute`（快），另有两条走**真子进程**：一条证
`-m intelligence_hub_v2.ported.reports.…` 真能跑到 V1 的 `main()` 并如实报错，
一条证产物真成立时判据真的放行。

所有用例的 `--data-dir` 都指到 tmp：**绝不写仓库的 `data/`**（里面有真凭证与主库）。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _entry in (str(_REPO_ROOT), str(_REPO_ROOT / "tools")):
    if _entry not in sys.path:  # tools/ 不是包，测试按脚本入口那样把它接进来
        sys.path.insert(0, _entry)

from intelligence_hub_v2.core.config import AppConfig  # noqa: E402
from intelligence_hub_v2.infra.subprocess import SubprocessTimeoutError  # noqa: E402
from intelligence_hub_v2.ported import reports as ported_reports  # noqa: E402
from intelligence_hub_v2.storage.files import FileStorage  # noqa: E402
from render_reports import (  # noqa: E402
    EMBED_IDS,
    KINDS,
    MIN_ARTIFACT_BYTES,
    MODULE_BY_KIND,
    Plan,
    build_plan,
    extract_embedded_json,
    main,
    outputs_dir,
    parse_args,
    plan_problems,
    resolve_data_dir,
    run_plan,
    verify_artifacts,
)
from render_reports import EXIT_DRY_RUN as REFUSED_DRY  # noqa: E402
from render_reports import EXIT_FAILED as FAILED  # noqa: E402
from render_reports import EXIT_OK as OK  # noqa: E402
from render_reports import EXIT_REFUSED as REFUSED  # noqa: E402

PORTED_DIR = Path(ported_reports.__file__).resolve().parent
PAD = "x" * MIN_ARTIFACT_BYTES
#: 一份**够大**的合法输入包：真包是几十 KB，而薄壳的产物下限挡的就是空壳。夹具自己要是不够大，
#: "成功"那几条测的就是夹具而不是判据。
VALID_PACK = json.dumps(
    {
        "schema_version": "creator-analysis-input-pack/v1",
        "pack_sha256": "a" * 64,
        "videos": [{"record_id": f"v{i}", "chars": PAD} for i in range(12)],
    }
)
NO_SHA_PACK = json.dumps({"schema_version": "creator-analysis-input-pack/v1", "note": PAD})


class FakeChild:
    """替掉 `_execute`：记录调用、按脚本给退出码、必要时替子进程把产物写出来。"""

    def __init__(
        self,
        returncode: int = 0,
        stderr: str = "",
        artifacts: dict[Path, str] | None = None,
    ) -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.artifacts = artifacts or {}
        self.calls: list[Plan] = []

    async def __call__(self, plan: Plan, _timeout: float) -> tuple[int, str, str]:
        self.calls.append(plan)
        for path, text in self.artifacts.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return self.returncode, "", self.stderr


def prepare_argv(tmp_path: Path, *extra: str) -> list[str]:
    """`prepare` 的一条完整可跑 argv（含两个输入源）。"""
    (tmp_path / "mirror.sqlite3").touch()
    return [
        "prepare",
        "--data-dir",
        str(tmp_path),
        "--db",
        str(tmp_path / "mirror.sqlite3"),
        "--manifest-dir",
        str(tmp_path),
        *extra,
    ]


def plan_for(tmp_path: Path, argv: list[str]) -> Plan:
    return build_plan(parse_args(argv), FileStorage(tmp_path))


def dashboard_argv(tmp_path: Path, *extra: str) -> list[str]:
    """`dashboard` 的一条完整可跑 argv（只有一条输入：镜像库）。"""
    (tmp_path / "mirror.sqlite3").touch()
    return [
        "dashboard",
        "--data-dir",
        str(tmp_path),
        "--db",
        str(tmp_path / "mirror.sqlite3"),
        *extra,
    ]


def run_main(monkeypatch: pytest.MonkeyPatch, child: Callable[..., Any], argv: list[str]) -> int:
    monkeypatch.setattr("render_reports._execute", child)
    return main(argv)


# --------------------------------------------------------------------------- #
# 前置：清单与映射双向对齐
# --------------------------------------------------------------------------- #


def test_the_shell_covers_every_script_in_the_port_area() -> None:
    """kind → 模块 的映射必须**双向**对上搬运区的文件清单。

    只写"三个 kind 都在"是空的：有人往 `ported/reports/` 加第四份时它静默不覆盖；
    有人搬走一份时壳里的 argv 指向不存在的模块而照样"跑了"。清单从目录里枚举。
    """
    on_disk = {p.stem for p in PORTED_DIR.glob("*.py") if p.name != "__init__.py"}
    assert on_disk, f"{PORTED_DIR} 里没有搬运脚本"
    mapped = {MODULE_BY_KIND[k].rsplit(".", 1)[-1] for k in KINDS}
    assert on_disk == mapped, f"搬运区是 {sorted(on_disk)}，壳覆盖的是 {sorted(mapped)}"
    assert len(KINDS) == 3 and set(KINDS) == set(MODULE_BY_KIND)
    for kind in KINDS:
        assert MODULE_BY_KIND[kind].startswith("intelligence_hub_v2.ported.reports.")


def test_the_embedded_json_ids_are_the_ones_v1_actually_writes() -> None:
    """判据要解析的 `#report-data` / `#dashboard-data` 是 V1 模板里写死的名字。

    钉成关系而不是抄常量：搬运区那份改了 id（或将来适配时换了名字），这条红，
    而不是让壳永远报"解析不出内嵌 JSON"却没人知道为什么。
    """
    render_src = (PORTED_DIR / "render_creator_analysis_report.py").read_text(encoding="utf-8")
    dash_src = (PORTED_DIR / "generate_creator_insight_dashboard.py").read_text(encoding="utf-8")
    assert f'id="{EMBED_IDS["report"]}"' in render_src
    assert f'id="{EMBED_IDS["dashboard"]}"' in dash_src


# --------------------------------------------------------------------------- #
# argv 形状
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("kind", KINDS)
def test_every_path_handed_to_the_child_is_absolute(tmp_path: Path, kind: str) -> None:
    """V1 里 `--db/--output` 的相对路径拼到 ROOT（= 包目录！）或 cwd 上，一个都不能漏出去。"""
    argv = [
        kind,
        "--data-dir",
        str(tmp_path),
        "--db",
        str(tmp_path / "mirror.sqlite3"),
        "--manifest-dir",
        str(tmp_path),
    ]
    plan = plan_for(tmp_path, argv)
    path_flags = {"--db", "--out-dir", "--output", "--out", "--pack", "--input", "--manifest-dir"}
    values = [plan.argv[i + 1] for i, token in enumerate(plan.argv) if token in path_flags]
    assert values, f"{kind} 的 argv 里一个路径都没传"
    assert all(Path(value).is_absolute() for value in values), values
    assert plan.artifacts and all(p.is_absolute() for p in plan.artifacts)
    assert plan.argv[:4] == (sys.executable, "-X", "utf8", "-m")


# --------------------------------------------------------------------------- #
# 守卫：路径不指对就拒跑，而且不起子进程
# --------------------------------------------------------------------------- #


def test_a_cwd_inside_the_source_tree_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = FakeChild()
    code = run_main(
        monkeypatch,
        child,
        prepare_argv(tmp_path, "--cwd", str(PORTED_DIR)),
    )
    assert code == REFUSED
    assert child.calls == [], "拒跑却还是起了子进程"


def test_an_artifact_inside_the_source_tree_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = FakeChild()
    bad_dir = PORTED_DIR / "outputs"
    code = run_main(monkeypatch, child, prepare_argv(tmp_path, "--out-dir", str(bad_dir)))
    assert code == REFUSED
    assert child.calls == []
    assert not bad_dir.exists(), "守卫没挡住，源码树已经被写脏"


# --------------------------------------------------------------------------- #
# 缺输入
# --------------------------------------------------------------------------- #


def test_a_missing_mirror_db_is_refused_with_a_fix_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    child = FakeChild()
    missing = tmp_path / "nowhere" / "feishu-base.sqlite3"
    argv = ["dashboard", "--data-dir", str(tmp_path), "--db", str(missing)]
    monkeypatch.setattr("render_reports._execute", child)
    code = main(argv)
    plan = plan_for(tmp_path, argv)
    assert code == REFUSED
    assert child.calls == [], "输入都没齐就起了子进程"
    problems = plan_problems(plan)
    assert any(str(missing) in item for item in problems), problems
    assert plan.fix.strip(), "失败没带修复动作"
    err = capsys.readouterr().err
    assert "输入不存在" in err and "修复：" in err
    assert not plan.artifacts[0].exists(), "缺输入却落了产物"


def test_prepare_needs_both_input_sources(tmp_path: Path) -> None:
    """`--cutoff` 与 `--manifest-dir` 是 V1 数据截止点的两个来源；一个都不给就没法判窗口。"""
    (tmp_path / "mirror.sqlite3").touch()
    with pytest.raises(SystemExit) as raised:
        parse_args(["prepare", "--data-dir", str(tmp_path), "--db", str(tmp_path / "m.sqlite3")])
    assert raised.value.code == 2


# --------------------------------------------------------------------------- #
# 退出码不是判据
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("case", "body"),
    [
        ("absent", None),
        ("empty", ""),
        ("tiny", "<html><body>报告</body></html>"),
        ("no-embedded-json", "<html><body>报告</body></html>" + PAD),
        ("broken-embedded-json", '<script id="dashboard-data">{"a":</script>' + PAD),
    ],
)
def test_a_zero_exit_without_a_real_artifact_is_not_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, body: str | None
) -> None:
    """子进程**返回 0**，产物不合格 —— 变异检查：判据退回看退出码时这五条全绿。

    用 `dashboard` 这一路：它的判据走 HTML 内嵌 JSON 那一支，`prepare` 那一支盖不到。
    """
    argv = dashboard_argv(tmp_path)
    plan = plan_for(tmp_path, argv)
    artifact = plan.artifacts[0]
    if body is not None:
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(body, encoding="utf-8")
    child = FakeChild(returncode=0)
    assert run_main(monkeypatch, child, argv) == FAILED, f"{case} 被判成了成功"
    reasons = verify_artifacts(plan)
    assert reasons, f"{case} 的判据是空的"
    assert all("退出码" not in reason for reason in reasons), reasons


def test_a_valid_artifact_with_exit_zero_is_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """反向防空转：产物真成立时不许一路红 —— 那会让上面那组变成永真断言。"""
    argv = prepare_argv(tmp_path)
    plan = plan_for(tmp_path, argv)
    child = FakeChild(returncode=0, artifacts={plan.artifacts[0]: VALID_PACK})
    assert run_main(monkeypatch, child, argv) == OK
    assert verify_artifacts(plan) == []


def test_a_pack_without_its_sha_is_not_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """输入包的 `pack_sha256` 是 prepare 走完确定性校验的凭据，缺了就不是成品。"""
    argv = prepare_argv(tmp_path)
    plan = plan_for(tmp_path, argv)
    body = NO_SHA_PACK
    assert run_main(monkeypatch, FakeChild(returncode=0), argv) == FAILED
    plan.artifacts[0].parent.mkdir(parents=True, exist_ok=True)
    plan.artifacts[0].write_text(body, encoding="utf-8")
    assert any("pack_sha256" in reason for reason in verify_artifacts(plan))
    assert extract_embedded_json(body, EMBED_IDS["report"]) is None


def test_a_nonzero_exit_keeps_the_verbatim_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """V1 脚本自报的失败：原文必须原样进报告，退出码只是其中一条判据。"""
    original = "[错误] 样本不完整：3 条视频缺纯净口播稿"
    argv = prepare_argv(tmp_path)
    child = FakeChild(returncode=2, stderr=original)
    code = run_main(monkeypatch, child, argv)
    captured = capsys.readouterr()
    assert code == FAILED
    assert child.calls[0].argv == plan_for(tmp_path, argv).argv
    payload = json.loads(captured.out.strip().splitlines()[-1])
    assert payload["exit_code"] == 2
    assert payload["stderr_tail"] == original
    assert any("退出码 2" in reason for reason in payload["reasons"]), payload["reasons"]
    assert "修复：" in captured.err


# --------------------------------------------------------------------------- #
# dry-run 与配置失败
# --------------------------------------------------------------------------- #


def test_dry_run_never_claims_success_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    child = FakeChild()
    argv = prepare_argv(tmp_path)
    monkeypatch.setattr("render_reports._execute", child)
    code = main([*argv, "--dry-run"])
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == REFUSED_DRY
    assert payload["ok"] is False and payload["dry_run"] is True
    assert child.calls == [], "dry-run 起了子进程"
    assert not (tmp_path / "outputs").exists(), "dry-run 把产物目录建出来了"


def test_a_broken_config_fails_with_the_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom() -> Any:
        raise ValueError("app.yaml: data.dir 不是合法路径")

    monkeypatch.setattr("render_reports.load_app_config", boom)
    monkeypatch.setattr("render_reports._execute", FakeChild())
    code = main(["dashboard", "--db", str(tmp_path / "m.sqlite3")])
    err = capsys.readouterr().err
    assert code == FAILED
    assert "data.dir 不是合法路径" in err and "修复：" in err


# --------------------------------------------------------------------------- #
# 真子进程
# --------------------------------------------------------------------------- #


def test_the_ported_module_is_reachable_with_dash_m_and_fails_honestly(tmp_path: Path) -> None:
    """真起一次子进程：拿一份不是 SQLite 的"镜像库"喂看板脚本，它必须非零且不写 HTML。

    编不出来的三件事一次验：`-m …ported.reports.generate_creator_insight_dashboard` 真能走到
    V1 的 `main()`（否则薄壳只是看着像能跑）、V1 的失败原文进了 stderr、薄壳没有因为
    "子进程跑完了"就说成功。
    """
    fake_db = tmp_path / "fake.sqlite3"
    # 一座**空的**镜像库：视图一个都没有，正是 V1 文档里"环境或契约缺失"那一档。
    sqlite3.connect(fake_db).close()
    argv = ["dashboard", "--data-dir", str(tmp_path), "--db", str(fake_db), "--days", "7"]
    plan = plan_for(tmp_path, argv)
    verdict = asyncio.run(run_plan(plan, 180.0))
    assert verdict["exit_code"] not in (0, None), verdict
    assert "[错误]" in verdict["stderr_tail"], verdict["stderr_tail"]
    assert verdict["ok"] is False
    assert not plan.artifacts[0].exists(), "失败却把 HTML 落盘了"


def test_the_verdict_accepts_an_artifact_a_real_child_wrote(tmp_path: Path) -> None:
    """另一面：子进程真写了合格产物时判据放行（换的是 argv，不换判据）。"""
    argv = prepare_argv(tmp_path)
    plan = plan_for(tmp_path, argv)
    artifact = plan.artifacts[0]
    script = (
        "import pathlib, sys; "
        "p = pathlib.Path(sys.argv[1]); "
        "p.parent.mkdir(parents=True, exist_ok=True); "
        "p.write_text(sys.argv[2], encoding='utf-8')"
    )
    real = Plan(
        kind=plan.kind,
        argv=(sys.executable, "-X", "utf8", "-c", script, str(artifact), VALID_PACK),
        cwd=plan.cwd,
        artifacts=plan.artifacts,
        required_inputs=(),
        fix=plan.fix,
    )
    verdict = asyncio.run(run_plan(real, 120.0))
    assert verdict["ok"] is True, verdict
    assert artifact.read_text(encoding="utf-8") == VALID_PACK


# --------------------------------------------------------------------------- #
# 判据的其余分支：读不出来 / 解析得出来 / 超时 / 解释器不在
# --------------------------------------------------------------------------- #


def test_an_unreadable_artifact_is_a_failure(tmp_path: Path) -> None:
    """产物在场但**不是文本**（V1 写坏一半的形状）：不能因为"文件挺大"就放行。"""
    argv = dashboard_argv(tmp_path)
    plan = plan_for(tmp_path, argv)
    plan.artifacts[0].parent.mkdir(parents=True, exist_ok=True)
    plan.artifacts[0].write_bytes(b"<html>\xff\xfe" + PAD.encode("utf-8"))
    reasons = verify_artifacts(plan)
    assert any("读不出来" in reason for reason in reasons), reasons


def test_extract_embedded_json_reads_what_v1_actually_writes() -> None:
    """正向盖一次：两种属性顺序（report 是 type 在前、dashboard 是 id 在前）都得解析得出来。"""
    payload = '{"window": "daily", "video_refs": ["BV1"]}'
    cases = [
        (f'<script type="application/json" id="report-data">{payload}</script>', "report-data"),
        (
            f'<script id="dashboard-data" type="application/json">{payload}</script>',
            "dashboard-data",
        ),
        (f'<script id="report-data">{payload}</script>', "report-data"),
    ]
    for html, element_id in cases:
        hit = extract_embedded_json(html, element_id)
        assert hit is not None, html
        assert hit["window"] == "daily"
    assert extract_embedded_json("<html></html>", "report-data") is None


def test_a_manual_cutoff_replaces_the_manifest_dir(tmp_path: Path) -> None:
    """`--cutoff` 是 V1 数据截止点的第二个来源，它顶上之后 manifest 目录就不再是输入。"""
    argv = prepare_argv(tmp_path, "--cutoff", "2026-09-01 20:00:00")
    plan = plan_for(tmp_path, argv)
    assert "--cutoff" in plan.argv and "2026-09-01 20:00:00" in plan.argv
    assert "--manifest-dir" not in plan.argv
    assert plan.required_inputs == (tmp_path / "mirror.sqlite3",)


def test_the_data_dir_defaults_to_the_config_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不给 `--data-dir` 时根从配置算 —— 这条盖的是"默认值那条路"，不是又一个样本。

    只 `build_plan`，不跑子进程也不 `plan_problems`：默认根是仓库的真 `data/`，
    这一条用例一行都不许往那里写。
    """

    def fake_config() -> AppConfig:
        return AppConfig()

    monkeypatch.setattr("render_reports.load_app_config", fake_config)
    argv = ["dashboard", "--db", str(tmp_path / "m.sqlite3")]
    storage = resolve_data_dir(parse_args(argv))
    root = _REPO_ROOT / "data"
    assert storage.root == root, storage.root
    assert outputs_dir(storage) == root / "outputs"
    plan = build_plan(parse_args(argv), storage)
    assert Path(plan.cwd) == root
    assert (root / "outputs" / "creator-insight-dashboard.html") in plan.artifacts


def test_a_timeout_is_a_failure_not_a_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def timeout_child(plan: Plan, _timeout: float) -> tuple[int, str, str]:
        raise SubprocessTimeoutError(plan.argv, 5.0, "", "卡在读镜像库", returncode=None)

    argv = dashboard_argv(tmp_path)
    assert run_main(monkeypatch, timeout_child, argv) == FAILED


def test_a_missing_interpreter_is_reported_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def missing_child(plan: Plan, _timeout: float) -> tuple[int, str, str]:
        raise LookupError("找不到可执行文件 'python'")

    argv = dashboard_argv(tmp_path)
    assert run_main(monkeypatch, missing_child, argv) == FAILED
