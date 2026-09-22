"""`safe_filename` 的属性测试（hypothesis + 定向用例）。

契约来源：docs/specs/contract-tests.md 的 V1 §7.8 那一栏。

**为什么用 hypothesis 而不是只写十几条用例**：昵称与标题是外部输入，
攻击面是"任意 Unicode 字符串"。人写的用例只会覆盖自己想得到的那几种，
而 V1 §7.8 那句"不止换 `/`"的教训本身就是"以为想全了"。

**为什么这个函数值得单独一个文件**：它是全仓库唯一一处"把不可信字符串
变成路径段"的地方。改它等于改所有历史目录名的可读性 —— 迁移脚本（Task 15）
搬 V1 的目录名时依赖兜底值 `unnamed` 与 V1 一致，那一处也测在这里。
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from intelligence_hub_v2.storage.files import DEFAULT_FALLBACK, FileStorage, safe_filename

_ILLEGAL = re.compile(r'[/\\?:"<>|\x00-\x1f*]')
_WINDOWS_DEVICE = re.compile(r"^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(\..*)?$", re.IGNORECASE)

# 全字符空间的 text 会让 hypothesis 在 surrogates 上炸（Windows 上
# os.fspath 不接受 lone surrogate，而它们不是合法的输入）。
innocent_text = st.text(
    alphabet=st.characters(exclude_categories=("Cs",)),
    max_size=200,
)

safe_names = st.integers(min_value=1, max_value=200)


# ---------------------------------------------------------------------------
# 不变式（对任意外部输入都必须成立）
# ---------------------------------------------------------------------------


@settings(max_examples=300, deadline=None)
@given(innocent_text)
def test_result_is_never_empty(name: str) -> None:
    assert safe_filename(name) != ""


@settings(max_examples=300, deadline=None)
@given(innocent_text)
def test_result_contains_no_illegal_characters(name: str) -> None:
    result = safe_filename(name)
    assert not _ILLEGAL.search(result), f"{result!r} 仍含非法字符"


@settings(max_examples=300, deadline=None)
@given(innocent_text)
def test_result_is_never_a_path_segment_escape(name: str) -> None:
    """`.` / `..` 会让 `<media_root>/douyin/<这个>/` 穿越到上一级。"""
    result = safe_filename(name)
    assert result not in {".", ".."}
    assert ".." not in result.split("/")


@settings(max_examples=300, deadline=None)
@given(innocent_text)
def test_result_is_never_a_bare_windows_device_name(name: str) -> None:
    """Windows 上 `CON` 能建出目录但打不开里面的文件 —— 建出来那一刻没人报错。"""
    assert not _WINDOWS_DEVICE.match(safe_filename(name))


@settings(max_examples=300, deadline=None)
@given(innocent_text, safe_names)
def test_result_respects_the_length_budget(name: str, max_length: int) -> None:
    """净化后的**正文**不超过 `max_length + 1`。

    `+1` 是设备名兜底加的那个 `_` 前缀 —— 有意的代价：`"CON"` 在 `max_length=3`
    下必须变 4 字符，否则要么截成一个仍然像设备名的串，要么返回空。
    真实的 255 上限远大于这里的任何取值。

    两个例外都是**字面量**，不受预算约束：
    - 空结果的兜底名（`"unnamed"` 是 7 个字符，`max_length=1` 也照样是 7）；
    - 调用方自己传进来的长 `fallback`。
    把它们算进预算会得到一个更糟的结果：净化函数把兜底值也截坏。
    """
    result = safe_filename(name, max_length=max_length)
    if result == DEFAULT_FALLBACK:
        return  # 兜底是字面量，不是"净化出来的正文"
    assert len(result) <= max_length + 1


@settings(max_examples=300, deadline=None)
@given(innocent_text)
def test_result_is_always_a_single_path_segment(name: str) -> None:
    """拼路径时的硬判据：结果只能是**一段**，不能把父目录往上挪。

    `Path("media/douyin") / result` 的段数必须正好比左边多 1。
    这一条比"不含 `..` 子串"准确 —— `"测试:上"` 与 `"../.."` 净化后都会留下
    `..` 这两个点（`.._..`），但它们是名字的一部分而不是两个段，
    所以不构成穿越。**只看子串会误报，只看段数不会。**
    """
    result = safe_filename(name)
    base = Path("data") / "media" / "douyin"
    joined = base / result
    assert len(joined.parts) == len(base.parts) + 1
    assert result not in {".", ".."}
    assert joined.parent == base


@settings(max_examples=300, deadline=None)
@given(innocent_text)
def test_result_does_not_end_with_dot_or_space(name: str) -> None:
    """Windows 会**静默剥掉**结尾的点与空格，于是 `foo.` 与 `foo` 撞成同一个目录。"""
    result = safe_filename(name)
    if result != DEFAULT_FALLBACK:
        assert not result.endswith((".", " "))


@settings(max_examples=300, deadline=None)
@given(innocent_text)
def test_function_is_idempotent(name: str) -> None:
    """净化过的名字再净化一次必须原样 —— 否则"重扫磁盘回填"会把目录名改掉。"""
    once = safe_filename(name)
    assert safe_filename(once) == once


@settings(max_examples=300, deadline=None)
@given(innocent_text)
def test_result_is_usable_as_a_real_path(name: str) -> None:
    """**终判据**：拿它建一个真目录、写一个真文件。

    上面那些正则都是"我认为 Windows 会拒绝什么"，这条是直接问操作系统。
    用 `TemporaryDirectory` 而不是 pytest 的 `tmp_path`：hypothesis 每条样例
    都要重放一次函数，而 function-scoped fixture 只会被建一次（那条会直接
    报 `HypothesisFunctionError`）。
    """
    result = safe_filename(name)
    with tempfile.TemporaryDirectory() as raw:
        target = Path(raw) / result
        target.mkdir(parents=True, exist_ok=True)
        (target / "f.txt").write_text("ok", encoding="utf-8")
        assert (target / "f.txt").read_text(encoding="utf-8") == "ok"


# ---------------------------------------------------------------------------
# 定向用例（每条都是 V1 真实踩过的形状）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given_name", "expected"),
    [
        ("姜胡说", "姜胡说"),
        ("MS4wLjABAAAA4Cz2kzAJ3ZjX4kPF5UzXkw", "MS4wLjABAAAA4Cz2kzAJ3ZjX4kPF5UzXkw"),
        ("BV1cSec6tEux", "BV1cSec6tEux"),
    ],
)
def test_ordinary_names_pass_through_unchanged(given_name: str, expected: str) -> None:
    """中文、BV 号、sec_uid 都不该被动 —— 目录名是给人看的。"""
    assert safe_filename(given_name) == expected


@pytest.mark.parametrize("separator", ["/", "\\", "?", ":", "<", ">", "|", '"', "*"])
def test_each_illegal_character_becomes_an_underscore(separator: str) -> None:
    assert safe_filename(f"a{separator}b") == "a_b"


@pytest.mark.parametrize(
    "traversal", ["../..", "../../etc/passwd", "..\\..\\windows", "a/../b", "/etc/passwd"]
)
def test_path_traversal_cannot_survive(traversal: str) -> None:
    """穿越风险来自**分隔符**，而分隔符一律换成 `_`。

    注意结果里可能**残留两个点**：`"../.."` → `".._.."` → rstrip 结尾的点
    → `".._"`。它是**一个段**，不是"上两级"，所以不构成穿越。
    判据因此是"段数 + 不等于 `.`/`..`"，不是"不含 `..` 子串"——
    后者会把合法名字（`"测试:上"` → `"测试_上"`）也判成问题。
    """
    result = safe_filename(traversal)
    assert "/" not in result and "\\" not in result
    base = Path("data") / "media"
    assert len((base / result).parts) == len(base.parts) + 1
    assert (base / result).parent == base


@pytest.mark.parametrize(
    "device",
    [
        "CON",
        "con",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "COM9",
        "LPT1",
        "LPT9",
        "nul.txt",  # 带扩展名同样打不开
        "COM3.json",
    ],
)
def test_windows_device_names_get_prefixed(device: str) -> None:
    result = safe_filename(device)
    assert result == f"_{device}"
    assert not _WINDOWS_DEVICE.match(result)


@pytest.mark.parametrize("device", ["CONSOLE", "NULLPOINT", "COMPUTER", "LPT10", "COPR"])
def test_names_that_merely_resemble_a_device_are_left_alone(device: str) -> None:
    """`COM[0-9]` 是单字符。把 `CONSOLE` 也加前缀等于给正常博主名字凭空换个写法。"""
    assert safe_filename(device) == device


@pytest.mark.parametrize("control", ["\x00", "\x07", "\x1b[31m", "a\nb", "a\tb", "a\r\nb"])
def test_control_characters_are_replaced(control: str) -> None:
    """控制字符在终端里显示成乱码，日志就没办法读了。"""
    result = safe_filename(control)
    assert not re.search(r"[\x00-\x1f]", result)


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n", ".....", "...", ".", "..", "  . .  "])
def test_anything_that_collapses_to_nothing_uses_the_fallback(blank: str) -> None:
    assert safe_filename(blank) == DEFAULT_FALLBACK


def test_a_full_width_stop_is_not_stripped() -> None:
    """`。` 不是 `.`。rstrip 只剥 ASCII 的点与空格 —— 中文昵称/标题的结尾标点要留着。

    写成用例是因为这个区别**很容易"顺手改对"**：把 `。` 也加进 `_TRAILING_JUNK`
    看起来更像"清理"，实际会把 `姜胡说。` 这类名字改写到与 `姜胡说` 撞在一起。
    """
    assert safe_filename(" 。 ") == "。"
    assert safe_filename("姜胡说。") == "姜胡说。"


def test_fallback_matches_v1_so_migration_does_not_rename() -> None:
    """兜底值改了 = V1 那批目录在 V2 里凭空多出一层重命名（Task 15 依赖这条）。"""
    assert DEFAULT_FALLBACK == "unnamed"
    assert safe_filename("") == "unnamed"


def test_custom_fallback_is_honoured() -> None:
    """兜底只在"净化完什么都不剩"时启用。

    `"///"` **不会**走兜底 —— 分隔符是被换成 `_` 的，结果是 `"___"`（三个字符的合法名字）。
    用 `""` / `"..."` 这类真正塌成空串的输入来验这条。
    """
    assert safe_filename("...", fallback="unknown") == "unknown"
    assert safe_filename("", fallback="unknown") == "unknown"
    assert safe_filename("///") == "___"


def test_truncation_happens_before_the_trailing_strip() -> None:
    """**顺序有讲究**。反过来（先 rstrip 再截断）会得到一个以点结尾的名字：
    `"a"*200 + "..."` 截到 120 位时结尾正好是三个点。"""
    result = safe_filename("a" * 200 + "...", max_length=120)
    assert len(result) == 120
    assert not result.endswith(".")


def test_long_names_are_capped() -> None:
    assert len(safe_filename("长" * 500)) <= 120
    assert len(safe_filename("长" * 500, max_length=0)) == 500  # 0 = 不限


@pytest.mark.parametrize("name", ["  姜胡说  ", "姜胡说  ", "  姜胡说"])
def test_surrounding_whitespace_is_stripped(name: str) -> None:
    assert safe_filename(name) == "姜胡说"


def test_internal_whitespace_is_collapsed_to_a_single_space() -> None:
    assert safe_filename("姜胡说   第 12 期") == "姜胡说 第 12 期"


def test_two_different_creators_cannot_collide_after_sanitizing() -> None:
    """V1 §7.8 的自检清单："会不会把已有目录变成第二个同名博主"。

    这一条是**目录布局**的责任而非 `safe_filename` 的（`media_dir()` 靠
    `video_id` 前缀保证唯一），但它说明了净化函数的边界：它只求合法，不求唯一。
    """
    assert safe_filename("测试:上") == safe_filename("测试_上")
    # 所以落盘时必须由调用方带上唯一前缀（见 test_files.py 的 media_dir 用例）。
    storage = FileStorage(Path("data"))
    first = storage.media_dir("douyin", "姜胡说", "7001", "测试:上")
    second = storage.media_dir("douyin", "姜胡说", "7002", "测试_上")
    assert first != second


def test_windows_reserved_names_are_still_prefixed_even_though_this_machine_allows_them() -> None:
    """**2026-09-22 本机实测**（写这条是因为结论反直觉）。

    我原本打算写一条"拿 `CON` 建目录、往里写文件会失败"的用例，把"加 `_` 前缀
    不是洁癖"钉在操作系统行为上。结果在这台机器上（Win11 + Python 3.12）：

    ```
    (tmp/CON).mkdir()            → 成功
    (tmp/CON / 'x.txt').write_text(...) → 成功，读回来也对
    ```

    也就是说**这条规则在当前环境里没有可观测的失败后果**（Win32 命名解析行为随
    版本与路径长度前缀而变，不是"任何时候都会炸"）。所以这里不写那条 OS 断言
    —— 一条断言了错误系统行为的测试比没有测试更糟，它会让人以为已经验证过了。

    保留加前缀的理由改成纯契约性的：**同一个名字在别的机器/别的调用方
    （资源管理器、`\\?\\` 短路径、备份工具、某些杀软）眼里可能仍是设备名**，
    而我们没有任何办法在本机验证那批消费方。代价是 `_CON` 这种名字略丑。
    看护落在 `test_windows_device_names_get_prefixed`（函数层）与下面的幂等性上。
    """
    assert safe_filename("CON") == "_CON"
    assert safe_filename("_CON") == "_CON"  # 加过前缀的不会被再加一层
    assert Path("data") / safe_filename("CON") != Path("data") / "CON"
