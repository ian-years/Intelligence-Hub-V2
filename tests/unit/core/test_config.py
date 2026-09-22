"""配置层单元测试。

看护 docs/specs/config-schema.md 的两条硬契约：
1. §1 加载优先级 defaults < app.yaml < 环境变量 < CLI
2. §5 写盘原子 + 热重载显式通知（不允许"手改 YAML 等服务感知"）
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from intelligence_hub_v2.core import config as config_module
from intelligence_hub_v2.core.config import AppConfig, ConfigManager, load_app_config
from intelligence_hub_v2.errors import ConfigError
from intelligence_hub_v2.platforms.base import Capabilities, PlatformConfig, RateLimitConfig
from intelligence_hub_v2.platforms.bilibili.config import BilibiliConfig
from intelligence_hub_v2.platforms.douyin.config import DouyinConfig

REPO_ROOT = Path(__file__).resolve().parents[3]
SHIPPED_CONFIG_DIR = REPO_ROOT / "config"

# ---------------------------------------------------------------------------
# AppConfig 默认值
# ---------------------------------------------------------------------------


def test_app_config_defaults() -> None:
    cfg = AppConfig()
    assert cfg.app.host == "127.0.0.1"  # V1 §1.2 硬约束：只绑回环
    assert cfg.app.port == 8789
    assert cfg.storage.wal_mode is True
    assert cfg.storage.busy_timeout_ms == 5000
    assert cfg.scheduler.max_concurrent_global == 4
    assert cfg.scheduler.max_concurrent_per_platform == 2
    assert cfg.asr.engine == "sherpa_sense_voice"
    assert cfg.http_client.follow_redirects is True


def test_app_config_rejects_non_loopback_silently() -> None:
    """改成 0.0.0.0 是允许的（spec 说"启动时打红色警告"），但不能被静默改写。"""
    cfg = AppConfig(app={"host": "0.0.0.0"})  # type: ignore[arg-type]
    assert cfg.app.host == "0.0.0.0"


def test_app_config_port_range_validated() -> None:
    with pytest.raises(ValueError, match="port"):
        AppConfig(app={"port": 70000})  # type: ignore[arg-type, dict-item]


# ---------------------------------------------------------------------------
# spec §1：加载优先级 defaults < app.yaml < env < CLI
# ---------------------------------------------------------------------------


def test_yaml_beats_default(tmp_path: Path) -> None:
    yaml_file = tmp_path / "app.yaml"
    yaml_file.write_text("app:\n  port: 9999\nlogging:\n  level: DEBUG\n", encoding="utf-8")
    cfg = load_app_config(yaml_file)
    assert cfg.app.port == 9999
    assert cfg.logging.level == "DEBUG"
    assert cfg.app.host == "127.0.0.1"  # 未覆盖的字段仍走默认


def test_env_beats_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    yaml_file = tmp_path / "app.yaml"
    yaml_file.write_text("app:\n  port: 9999\n", encoding="utf-8")
    monkeypatch.setenv("INTELLIGENCE_HUB_APP__PORT", "8123")
    cfg = load_app_config(yaml_file)
    assert cfg.app.port == 8123


def test_cli_overrides_beat_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    yaml_file = tmp_path / "app.yaml"
    yaml_file.write_text("app:\n  port: 9999\n", encoding="utf-8")
    monkeypatch.setenv("INTELLIGENCE_HUB_APP__PORT", "8123")
    cfg = load_app_config(yaml_file, app={"port": 7000})
    assert cfg.app.port == 7000


def test_missing_yaml_file_falls_back_to_defaults(tmp_path: Path) -> None:
    """配置文件不存在不是错误 —— 全默认值起得来。"""
    cfg = load_app_config(tmp_path / "does-not-exist.yaml")
    assert cfg.app.port == 8789


def test_broken_yaml_raises_config_error(tmp_path: Path) -> None:
    yaml_file = tmp_path / "app.yaml"
    yaml_file.write_text("app: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_app_config(yaml_file)


def test_unknown_yaml_key_is_ignored(tmp_path: Path) -> None:
    """extra='ignore'：用户 YAML 里多写一段不能让服务起不来。"""
    yaml_file = tmp_path / "app.yaml"
    yaml_file.write_text("app:\n  port: 9999\nnot_a_section:\n  foo: bar\n", encoding="utf-8")
    cfg = load_app_config(yaml_file)
    assert cfg.app.port == 9999


def test_v1_compat_env_vars_are_not_read_by_app_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """V1 的 DOUYIN_YTDLP_COOKIES_FILE 映射到平台配置，不是 AppConfig。

    这里锁的是"AppConfig 不吃非 INTELLIGENCE_HUB_ 前缀的变量"，
    避免 V1 那套环境变量到处乱串（spec §6）。
    """
    monkeypatch.setenv("PORT", "1234")
    assert AppConfig().app.port == 8789


# ---------------------------------------------------------------------------
# PlatformConfig
# ---------------------------------------------------------------------------


def test_platform_config_requires_display_name() -> None:
    with pytest.raises(ValueError, match="display_name"):
        PlatformConfig(  # type: ignore[call-arg]
            media_strategy="yt_dlp",
            list_strategy="api",
        )


def test_rate_limit_defaults() -> None:
    rl = RateLimitConfig()
    assert rl.per_minute == 30
    assert rl.per_creator_seconds == 1.0


def test_douyin_config_defaults() -> None:
    cfg = DouyinConfig(display_name="抖音")
    assert cfg.enabled is True
    assert cfg.media_strategy == "yt_dlp_with_fallback"
    assert cfg.list_strategy == "browser_scroll"
    assert cfg.use_cdp_bridge is True
    assert cfg.fallback_to_page_play_url is True
    # V1 §7.2：yt-dlp 对抖音从来没产出过媒体，兜底是常态不是故障
    assert cfg.advanced.retry_max == 3


def test_douyin_cookie_ladder_order_is_not_a_config_field() -> None:
    """ADR-0011：cookie 阶梯的**顺序**不许出现在平台配置里，它只归 `Capabilities`。

    这条是结构看护，不是取值看护 —— 多加一个 `xxx_cookie_priority` 字段不会让任何
    现有用例变红（它自带默认值），但它会重新造出 Task 6 收掉的那个形状：
    同一条阶梯两处排顺序，而 `download_media` 的契约只认其中一处。
    """
    for field_name in DouyinConfig.model_fields:
        assert "priority" not in field_name, field_name
        assert "order" not in field_name, field_name
    # V1 §7.3：Windows 上浏览器档永远读不出来，所以抖音默认**没有**这一档
    # （B站 默认 "chrome"，两边不同是有意的）
    assert DouyinConfig(display_name="抖音").ytdlp_cookies_from_browser is None
    assert BilibiliConfig(display_name="B站").ytdlp_cookies_from_browser == "chrome"


def test_bilibili_config_defaults() -> None:
    cfg = BilibiliConfig(display_name="B站")
    assert cfg.prefer_subtitles is True
    assert cfg.media_strategy == "yt_dlp"
    # Task 7 收的：设计文档写 'api'，实测空间列表那个接口匿名回 HTML 风控页，
    # V1 跑通的从来是 yt-dlp --flat-playlist。留一个实现不了的取值就是骗前端。
    assert cfg.list_strategy == "yt_dlp_flat"
    assert cfg.use_cdp_bridge is False
    assert cfg.advanced.dash_split_handling == "auto"


def test_bilibili_config_declares_no_cookie_ladder_order() -> None:
    """ADR-0011：顺序只归 `BilibiliAdapter.capabilities`，配置里不许有第二份。

    `cookie_variant_order` 被删是因为它与 `capabilities.cookie_variants`
    是**同一份顺序写两遍**（值目前一致，所以它只是漂移风险而不是已发生的矛盾 ——
    抖音那边已经矛盾了，见 ADR-0011 的背景）。
    """
    for field_name in BilibiliConfig.model_fields:
        assert "priority" not in field_name, field_name
        assert "order" not in field_name, field_name
    with pytest.raises(ValueError, match="cookie_variant_order"):
        BilibiliConfig(display_name="B站", cookie_variant_order=("exported_file",))  # type: ignore[call-arg]


def test_capabilities_is_frozen() -> None:
    caps = Capabilities(
        needs_browser=True,
        needs_cookies=True,
        cookie_variants=("exported_file", "none"),
        supports_subtitles=False,
        supports_dash_split=False,
        list_strategy="browser_scroll",
        media_strategy="yt_dlp_with_fallback",
    )
    with pytest.raises(AttributeError):
        caps.needs_browser = False  # type: ignore[misc]


def test_platform_config_json_schema_roundtrip() -> None:
    """spec §4：前端按 JSON Schema 自动渲染表单，schema 必须能生成。"""
    schema = DouyinConfig.model_json_schema()
    assert schema["properties"]["enabled"]["default"] is True
    assert "fallback_to_page_play_url" in schema["properties"]


# ---------------------------------------------------------------------------
# ConfigManager
# ---------------------------------------------------------------------------


def _write_platforms_yaml(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "platforms.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_config_manager_load_platforms(tmp_path: Path) -> None:
    _write_platforms_yaml(
        tmp_path,
        "douyin:\n  enabled: true\n  display_name: 抖音\n"
        "bilibili:\n  enabled: false\n  display_name: B站\n",
    )
    mgr = ConfigManager(config_dir=tmp_path)
    cfg = mgr.load()
    assert isinstance(cfg, AppConfig)
    assert mgr.get_platform("douyin").enabled is True
    assert mgr.get_platform("bilibili").enabled is False
    assert mgr.enabled_platforms() == ["douyin"]


def test_config_manager_empty_dir_yields_no_platforms(tmp_path: Path) -> None:
    mgr = ConfigManager(config_dir=tmp_path)
    mgr.load()
    assert mgr.enabled_platforms() == []
    with pytest.raises(KeyError):
        mgr.get_platform("douyin")


def test_config_manager_unknown_platform_raises_key_error(tmp_path: Path) -> None:
    mgr = ConfigManager(config_dir=tmp_path)
    mgr.load()
    with pytest.raises(KeyError):
        mgr.get_platform("nonexistent")


def test_config_manager_unregistered_platform_key_raises(tmp_path: Path) -> None:
    """platforms.yaml 里写了本构建不支持的平台 → 如实失败，不静默忽略。"""
    _write_platforms_yaml(tmp_path, "myspace:\n  display_name: 某站\n")
    mgr = ConfigManager(config_dir=tmp_path)
    with pytest.raises(ConfigError, match="myspace"):
        mgr.load()


def test_config_manager_defaults_applied_when_section_partial(tmp_path: Path) -> None:
    """YAML 只写 enabled，其余字段必须走 DouyinConfig 默认值。"""
    _write_platforms_yaml(tmp_path, "douyin:\n  enabled: false\n")
    mgr = ConfigManager(config_dir=tmp_path)
    mgr.load()
    cfg = mgr.get_platform("douyin")
    assert cfg.enabled is False
    assert cfg.display_name == "抖音"  # 注册表提供的默认 display_name
    assert cfg.media_strategy == "yt_dlp_with_fallback"


def test_config_manager_write_is_atomic_and_rereadable(tmp_path: Path) -> None:
    _write_platforms_yaml(tmp_path, "douyin:\n  enabled: true\n  display_name: 抖音\n")
    mgr = ConfigManager(config_dir=tmp_path)
    mgr.load()

    mgr.write_platform_config("douyin", DouyinConfig(display_name="抖音", enabled=False))

    # 盘上真的落了
    raw = (tmp_path / "platforms.yaml").read_text(encoding="utf-8")
    assert "enabled: false" in raw
    # 没留下临时文件
    assert [p.name for p in tmp_path.iterdir()] == ["platforms.yaml"]

    mgr2 = ConfigManager(config_dir=tmp_path)
    mgr2.load()
    assert mgr2.get_platform("douyin").enabled is False


def test_config_manager_write_preserves_other_platforms(tmp_path: Path) -> None:
    _write_platforms_yaml(
        tmp_path,
        "douyin:\n  enabled: true\n  display_name: 抖音\n"
        "bilibili:\n  enabled: true\n  display_name: B站\n",
    )
    mgr = ConfigManager(config_dir=tmp_path)
    mgr.load()
    mgr.write_platform_config("douyin", DouyinConfig(display_name="抖音", enabled=False))
    assert mgr.get_platform("bilibili").enabled is True
    assert "bilibili:" in (tmp_path / "platforms.yaml").read_text(encoding="utf-8")


def test_config_manager_write_unknown_platform_raises(tmp_path: Path) -> None:
    mgr = ConfigManager(config_dir=tmp_path)
    mgr.load()
    with pytest.raises(KeyError):
        mgr.write_platform_config("nonexistent", DouyinConfig(display_name="抖音"))


def test_write_does_not_notify_only_reload_does(tmp_path: Path) -> None:
    """spec §5 的顺序：写盘 → 热重载 → 通知。只写盘不通知。"""
    _write_platforms_yaml(tmp_path, "douyin:\n  enabled: true\n  display_name: 抖音\n")
    mgr = ConfigManager(config_dir=tmp_path)
    mgr.load()
    received: list[tuple[str, bool]] = []
    mgr.subscribe(lambda name, cfg: received.append((name, cfg.enabled)))

    mgr.write_platform_config("douyin", DouyinConfig(display_name="抖音", enabled=False))
    assert received == []

    mgr.reload_platform("douyin")
    assert received == [("douyin", False)]
    assert mgr.get_platform("douyin").enabled is False


def test_reload_platform_ignores_external_file_edits(tmp_path: Path) -> None:
    """V1 纪律：运行时不监听文件变化。

    手改 YAML 之后 reload_platform 确实会读到新值（因为它就是重读盘），
    但**不会自动发生** —— 没有 watcher。这条测试锁的是"没有后台线程在偷偷重读"。
    """
    yaml_path = _write_platforms_yaml(tmp_path, "douyin:\n  enabled: true\n  display_name: 抖音\n")
    mgr = ConfigManager(config_dir=tmp_path)
    mgr.load()

    yaml_path.write_text("douyin:\n  enabled: false\n  display_name: 抖音\n", encoding="utf-8")
    assert mgr.get_platform("douyin").enabled is True  # 内存里还是旧值
    mgr.reload_platform("douyin")
    assert mgr.get_platform("douyin").enabled is False


def test_reload_unknown_platform_raises(tmp_path: Path) -> None:
    mgr = ConfigManager(config_dir=tmp_path)
    mgr.load()
    with pytest.raises(KeyError):
        mgr.reload_platform("nonexistent")


def test_subscriber_exception_does_not_break_reload(tmp_path: Path) -> None:
    """一个订阅者抛异常不能把配置热重载整条链路带崩。"""
    _write_platforms_yaml(tmp_path, "douyin:\n  enabled: true\n  display_name: 抖音\n")
    mgr = ConfigManager(config_dir=tmp_path)
    mgr.load()

    def boom(_name: str, _cfg: PlatformConfig) -> None:
        raise RuntimeError("subscriber is broken")

    received: list[str] = []
    mgr.subscribe(boom)
    mgr.subscribe(lambda name, _cfg: received.append(name))

    mgr.write_platform_config("douyin", DouyinConfig(display_name="抖音", enabled=False))
    mgr.reload_platform("douyin")

    assert received == ["douyin"]
    assert mgr.get_platform("douyin").enabled is False


def test_app_yaml_is_also_loaded_from_config_dir(tmp_path: Path) -> None:
    """ConfigManager 一个目录里同时吃 app.yaml 和 platforms.yaml。"""
    (tmp_path / "app.yaml").write_text("app:\n  port: 9911\n", encoding="utf-8")
    _write_platforms_yaml(tmp_path, "douyin:\n  enabled: true\n  display_name: 抖音\n")
    mgr = ConfigManager(config_dir=tmp_path)
    cfg = mgr.load()
    assert cfg.app.port == 9911


def test_data_dir_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTELLIGENCE_HUB_DATA__DIR", str(tmp_path / "elsewhere"))
    cfg = AppConfig()
    assert cfg.data.dir == tmp_path / "elsewhere"


def test_config_dir_is_created_on_write(tmp_path: Path) -> None:
    """config/ 不存在时写盘要能自己长出来（V1 §4.1 的 mkdir 纪律）。"""
    nested = tmp_path / "cfg"
    mgr = ConfigManager(config_dir=nested)
    mgr.load()
    mgr.write_platform_config("douyin", DouyinConfig(display_name="抖音"))
    assert (nested / "platforms.yaml").is_file()


def test_write_platform_config_uses_exact_path_no_wildcards() -> None:
    """安全约束（V1 §1）：删除/覆盖文件只允许精确路径，禁止通配符。

    配置层唯一的写盘动作是「写 <name>.yaml.tmp → Path.replace 覆盖 platforms.yaml」，
    这条静态自查锁住它不会退化成 shutil.rmtree / glob 批量删。
    """
    src = Path(config_module.__file__).read_text(encoding="utf-8")
    assert "shutil.rmtree" not in src
    assert "unlink(" not in src
    assert ".glob(" not in src
    assert "tmp.replace(target)" in src


# ---------------------------------------------------------------------------
# 新增的 scheduler / app 字段
# ---------------------------------------------------------------------------


def test_show_disabled_platform_history_defaults_true() -> None:
    """关掉平台只是停采集，已收的东西不消失。"""
    assert AppConfig().app.show_disabled_platform_history is True


def test_task_timeout_seconds_rejects_unknown_kind() -> None:
    """key 写错必须启动就炸，不能等那个任务第一次跑才发现超时没生效。"""
    with pytest.raises(ValueError, match="TaskKind"):
        AppConfig(scheduler={"task_timeout_seconds": {"not_a_kind": 10}})


def test_task_timeout_seconds_rejects_nonpositive() -> None:
    with pytest.raises(ValueError, match=">= 1"):
        AppConfig(scheduler={"task_timeout_seconds": {"preflight": 0}})


def test_task_timeout_seconds_accepts_real_kinds() -> None:
    cfg = AppConfig(
        scheduler={"task_timeout_seconds": {"preflight": 30, "single_link": 600}},
    )
    assert cfg.scheduler.task_timeout_seconds == {"preflight": 30, "single_link": 600}


# ---------------------------------------------------------------------------
# 仓库自带 config/ 的 drift 看护
# ---------------------------------------------------------------------------


def test_shipped_config_files_load() -> None:
    """仓库自带的 `config/*.yaml` 必须能被当前 schema 吃下去。

    这条是 drift 看护。设计期写的 YAML 与实施期写的 Pydantic 模型会各走各的：
    2026-09-22 就真漂过一次 —— `platforms.yaml` 套了一层 `platforms:`，
    还带了个顶层 `defaults:` 段，`ConfigManager.load()` 直接 ConfigError。
    没有这条测试，这种漂移要等到第一次起服务才暴露。
    """
    mgr = ConfigManager(config_dir=SHIPPED_CONFIG_DIR)
    cfg = mgr.load()

    assert cfg.app.host == "127.0.0.1"  # V1 §1.2 硬约束
    assert cfg.app.port == 8789
    assert cfg.data.dir == Path("data")
    assert cfg.scheduler.task_timeout_seconds["preflight"] == 30

    # V2.0 只注册抖音 + B站
    assert mgr.platform_names() == ["douyin", "bilibili"]
    assert mgr.enabled_platforms() == ["douyin", "bilibili"]
    # 发货的 YAML 里不许再躺着一个没人读的键（extra="forbid" 会当场红，
    # 但这条断言的红比 ConfigError 好读得多）
    raw = (Path("config/platforms.yaml")).read_text(encoding="utf-8")
    assert "cookie_variant_order" not in raw
    assert "ytdlp_cookie_priority" not in raw

    douyin = mgr.get_platform("douyin")
    assert douyin.media_strategy == "yt_dlp_with_fallback"
    assert douyin.use_cdp_bridge is True
    assert douyin.cookies_file == Path("data/cookies/douyin.com.txt")

    bili = mgr.get_platform("bilibili")
    assert bili.list_strategy == "yt_dlp_flat"
    assert bili.use_cdp_bridge is False
    assert bili.prefer_subtitles is True


def test_shipped_config_has_no_unregistered_platform_sections() -> None:
    """小红书 / YouTube 在 V2.1 才注册，它们的段必须是**注释掉的**，不是被静默忽略。

    配置层对未注册平台名是硬失败，所以这条与上一条一起构成"新克隆能起服务"的保证。
    """
    raw = (SHIPPED_CONFIG_DIR / "platforms.yaml").read_text(encoding="utf-8")
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        if stripped.startswith("xiaohongshu:") or stripped.startswith("youtube:"):
            msg = f"V2.0 未注册的平台出现在 platforms.yaml 的有效行里: {stripped}"
            raise AssertionError(msg)


def test_shipped_platforms_yaml_survives_write_roundtrip(tmp_path: Path) -> None:
    """把仓库自带的 platforms.yaml 逐平台写回一遍，内容必须等价。

    看护 `model_dump(mode="json")` → YAML → 重新校验 这条回路：
    `Path` / `HttpUrl` / `tuple` / `None` 在 JSON 化之后还得能读回来。
    tuple 尤其危险 —— YAML 里没有 tuple，写出去是 list，读回来要靠 Pydantic 再收窄。
    """
    shutil.copy(SHIPPED_CONFIG_DIR / "platforms.yaml", tmp_path / "platforms.yaml")

    mgr = ConfigManager(config_dir=tmp_path)
    mgr.load()
    names = mgr.platform_names()
    before = {name: mgr.get_platform(name).model_dump(mode="json") for name in names}

    for name in names:
        mgr.write_platform_config(name, mgr.get_platform(name))

    mgr2 = ConfigManager(config_dir=tmp_path)
    mgr2.load()
    after = {name: mgr2.get_platform(name).model_dump(mode="json") for name in names}

    assert after == before
    assert mgr2.get_platform("bilibili").ytdlp_cookies_from_browser == "chrome"
