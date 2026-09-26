"""配置层。

契约来源：`docs/specs/config-schema.md`（Locked，改动需走 ADR-0007）。

## 加载优先级（spec §1，低 → 高）

1. Pydantic 默认值
2. `config/app.yaml`（全局） / `config/platforms.yaml`（每平台）
3. 环境变量 `INTELLIGENCE_HUB_*`（嵌套用 `__` 分隔）
4. CLI 参数（表现为 `load_app_config()` / `AppConfig()` 的显式关键字参数）

## 一处与 spec §2 的有意偏离

spec 把 `yaml_file="config/app.yaml"` 写进 `AppConfig.model_config`，
让裸 `AppConfig()` 自己去读盘。实现里**没有**这么做，改成由
`load_app_config(path)` / `ConfigManager.load()` 显式喂路径。原因：

- 裸 `AppConfig()` 读盘 = 单元测试的结果取决于仓库里有没有 `config/app.yaml`，
  换个 cwd 跑测试就变绿变红，这是 V1 §7.12「预检查错库」同一类错位。
- 优先级顺序仍然严格是 spec §1 那个：YAML 源排在 env 源之后。

要用默认路径就 `load_app_config()`（不传参 = `config/app.yaml`）。
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from enum import Enum
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from intelligence_hub_v2.errors import ConfigError
from intelligence_hub_v2.logging import get_logger
from intelligence_hub_v2.models import TaskKind
from intelligence_hub_v2.platforms import PLATFORM_CONFIG_SCHEMAS, platform_defaults
from intelligence_hub_v2.platforms.base import (
    PlatformAvailability,
    PlatformConfig,
    resolve_platform_availability,
)

if TYPE_CHECKING:
    from pydantic.fields import FieldInfo

logger = get_logger(__name__)

DEFAULT_APP_YAML = Path("config/app.yaml")
DEFAULT_PLATFORMS_YAML = Path("config/platforms.yaml")

PlatformConfigChanged = Callable[[str, PlatformConfig], None]
"""配置热重载的订阅者签名：`(platform_name, new_config) -> None`。"""


# ---------------------------------------------------------------------------
# AppConfig 各 section（spec §2）
# ---------------------------------------------------------------------------


class AppSection(BaseModel):
    """服务本身。"""

    model_config = ConfigDict(extra="forbid")

    name: str = "Intelligence Hub V2"
    version: str = "0.1.0"

    host: str = "127.0.0.1"
    """**只绑回环**（V1 §1.2 硬约束）。

    改成 `0.0.0.0` 是允许的（用户自己负责），但启动时必须打红色警告：
    CDP 桥会在已登录浏览器里执行任意 JS，工作台能触发它，
    把工作台暴露到局域网等于交出账号。
    """

    port: int = Field(default=8789, ge=1, le=65535)
    """与 V1 同端口，方便迁移期两个版本对照。"""

    cors_origins: list[str] = Field(default_factory=list)
    openapi_url: str = "/openapi.json"
    docs_url: str | None = "/api/docs"
    redoc_url: str | None = None

    show_disabled_platform_history: bool = True
    """平台被禁用时，前端 Feed 是否仍显示它的历史数据。

    True = 关掉平台只是**停采集**，已收的东西不消失。这是用户预期：
    开关管的是"还收不收"，不是"删不删"。删除走 `is_hidden`（V1 §7.25 的墓碑内化）。
    """


class PlatformControlSection(BaseModel):
    """四家平台的**总闸**（ADR-0025）。`scheduler.enabled` 管的是"调度器要不要起"，
    这一段管的是"这四家还可不可以采" —— 后者也管手动点的那一次，所以不是同一件事。

    默认 `True` 是硬要求：新增一道闸门把现网所有平台静默关掉，比没有这道闸门更糟。
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    """关掉 = 四家一律不可用（`*_collect` 从 `/api/tasks` 消失、cron 不再排、
    `single_link`/`add_creator` 认到这些家的链接也拒）。

    **它不改写任何一家自己的 `enabled`**。这一条是整个设计的全部理由：
    批量写四次的话，"重新打开总闸"会把你之前单独关掉的某家悄悄复原，
    而界面上再也分不出"你自己关的"与"被总闸盖住的"。
    """


class DataSection(BaseModel):
    """产物落盘位置。`data/` 永不入库（V1 §1.1 硬约束）。"""

    model_config = ConfigDict(extra="forbid")

    dir: Path = Path("data")
    """所有产物的根。里面是有效会话凭证与真实博主数据，**按凭证对待**。"""

    media_subdir: str = "media"
    manifests_subdir: str = "manifests"
    cookies_subdir: str = "cookies"
    logs_subdir: str = "logs"
    tmp_subdir: str = "tmp"
    """任务专属临时目录（`data/tmp/<task_id>/`），任务结束自动清理。"""

    def resolve_all(self, root: Path | None = None) -> dict[str, Path]:
        """把各子目录解析成绝对路径。**只解析，不创建**（创建是调用方的事）。"""
        base = (root or Path.cwd()) / self.dir
        return {
            "root": base,
            "media": base / self.media_subdir,
            "manifests": base / self.manifests_subdir,
            "cookies": base / self.cookies_subdir,
            "logs": base / self.logs_subdir,
            "tmp": base / self.tmp_subdir,
        }


class StorageSection(BaseModel):
    """SQLite。只有一个库 —— V1 §7.12「预检查的是飞书镜像库不是主库」由此结构性消除。"""

    model_config = ConfigDict(extra="forbid")

    sqlite_file: str = "intelligence_hub.sqlite3"
    wal_mode: bool = True
    busy_timeout_ms: int = Field(default=5000, ge=100)
    event_retention_days: int = Field(default=30, ge=1)
    """已完成任务的事件保留天数。失败任务保留 3 倍（方便排查）。"""


class LoggingSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    format: Literal["json", "console"] = "json"
    file: str | None = "server.log"
    """None = 只写 stdout。"""

    rotate_max_bytes: int = Field(default=52_428_800, ge=1_048_576)
    rotate_backup_count: int = Field(default=5, ge=1)


_CRON_FIELDS = 5
"""五段：分 时 日 月 周。APScheduler 的 `from_crontab` 要的就是这个形状。"""

_CRON_CHARS = frozenset("0123456789*,-/?:LW#")
"""五段 cron 允许出现的字符集（够用且宁窄勿宽：写错要响，不是猜）。"""


class SchedulerSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    timezone: str = "Asia/Shanghai"
    max_concurrent_per_platform: int = Field(default=2, ge=1)
    max_concurrent_global: int = Field(default=4, ge=1)

    health_check_on_startup: bool = True
    """启动时跑一次 preflight。

    缺依赖就如实报红（V1 §1.3），**不许为了让看板变绿而伪造状态** ——
    V1 那个仓库的历史问题就是"看起来在跑"。
    """

    health_check_interval_seconds: int = Field(default=300, ge=10)

    collect_cron: str | None = None
    """**定时采集**的 cron 表达式（五段：`分 时 日 月 周`），None = 不开。

    为什么是一个 cron 串而不是 `daily_at: "08:00"`：一个需求只留一个旋钮
    （V1 §7.11 那一族 —— 同一件事有两种写法，早晚只改一边）。`0 8 * * *` 就是
    "每天 08:00"，而 cron 还能表达"工作日 07:30"这类，APScheduler 原生解析，
    不需要我们自己写一个解析器。Settings 页（第二片）可以给时间选择器，
    但它写出来的**仍然是这一串**。

    为什么放配置而不是建一张 `schedules` 表：这份配置本来就是"这台机器要怎么跑"
    的真源（`scheduler.*` 其余字段同段），而表要引入迁移与第二份口径 ——
    一个只有一份、按平台数量不超过个位数的东西不需要数据库。
    理由与判据见 `docs/adr/0017`。

    **写错要在启动时就炸**：一条解析不出来的 cron 如果只记一条 warning，
    症状是"服务绿着，采集永远不来"，而这正是 V1 的历史问题（§1.3）。
    """

    collect_platforms: list[str] = Field(default_factory=list)
    """cron 触发时给哪些平台跑。空 = **所有启用的平台**（`platforms.yaml` 里 enabled 的）。

    元素必须是已知平台名，写错启动就红 —— 与 `task_timeout_seconds` 的 key 校验同一口径。
    关掉的平台即使写在这里也不会被排 job（与"关掉平台=它的任务消失"那条镜像一致）。
    """

    collect_limit: int | None = Field(default=None, ge=1)
    """每位博主每次最多收几条，进 `CollectParams.limit`。None = 用平台配置的
    `videos_per_creator`（也就是不在定时任务里另立一套默认值）。"""

    task_timeout_seconds: dict[str, int] = Field(default_factory=dict)
    """每类任务的超时（秒），key 必须是 `TaskKind` 的取值。

    缺某一项 = 该类任务不限时（`TaskDefinition.timeout_seconds = None`）。
    超时记 `status='timeout'` 并**写终态清单**（V1 §2 契约二）。
    """

    @field_validator("collect_cron")
    @classmethod
    def _validate_cron(cls, value: str | None) -> str | None:
        """五段、每段非空、字符集限定在 cron 的那几个符号里。

        这里**不**用 apscheduler 的解析器：`core/config.py` 不该依赖一个可选装进来的包
        （`--extra bridge` 那一族），而装没装 apscheduler 不该改变"这份配置对不对"的答案。
        真正的语义解析在排 job 那一刻由 APScheduler 做，它抛错同样红在启动。
        """
        if value is None or not str(value).strip():
            return None
        fields = str(value).split()
        if len(fields) != _CRON_FIELDS:
            msg = (
                f"scheduler.collect_cron 要的是五段 cron（分 时 日 月 周），"
                f"实际是 {value!r}（{len(fields)} 段）"
            )
            raise ValueError(msg)
        bad = sorted({token for token in fields for ch in token if not _CRON_CHARS.issuperset(ch)})
        if bad:
            msg = f"scheduler.collect_cron 里有不认识的字符：{bad}（来自 {value!r}）"
            raise ValueError(msg)
        return " ".join(fields)

    @field_validator("collect_platforms")
    @classmethod
    def _validate_platform_names(cls, value: list[str]) -> list[str]:
        """拼错一个平台名 = 那个平台永远不被定时采集，而库里看不出异常。

        校验的是**配置词汇表**（有哪几个平台），不是"哪几个平台的采集任务已经实现" ——
        后者在 `core/task_registry.py` 那边，配置层不引入它（会成环）。
        所以"配了一个已注册但还没实现的平台"红在排 job 那一刻（`_add_collect_jobs`），
        同样红在启动，只是晚一步。
        """
        unknown = sorted({name for name in value if name not in PLATFORM_CONFIG_SCHEMAS})
        if unknown:
            msg = (
                f"scheduler.collect_platforms 里有 V2 不认识的平台: {', '.join(unknown)}。"
                f"可选：{', '.join(sorted(PLATFORM_CONFIG_SCHEMAS))}"
            )
            raise ValueError(msg)
        return value

    @field_validator("task_timeout_seconds")
    @classmethod
    def _validate_task_kinds(cls, value: dict[str, int]) -> dict[str, int]:
        """key 写错必须启动就炸，不能等到那个任务第一次跑才发现超时没生效。"""
        known = {kind.value for kind in TaskKind}
        unknown = sorted(set(value) - known)
        if unknown:
            msg = (
                f"scheduler.task_timeout_seconds 里有未知的 TaskKind: {', '.join(unknown)}。"
                f"合法取值: {', '.join(sorted(known))}"
            )
            raise ValueError(msg)
        for key, seconds in value.items():
            if seconds < 1:
                msg = f"scheduler.task_timeout_seconds.{key} 必须 >= 1 秒，实际是 {seconds}"
                raise ValueError(msg)
        return value


class BridgeSection(BaseModel):
    """CDP 桥（V1 `cdp_bridge_server.py`，`127.0.0.1:3457`）。"""

    model_config = ConfigDict(extra="forbid")

    url: HttpUrl = Field(default=HttpUrl("http://127.0.0.1:3457"))
    enabled: bool = True
    health_check_interval_seconds: int = Field(default=60, ge=10)
    restart_cooldown_seconds: int = Field(default=30, ge=5)
    """V1 §7.20：自愈重建 context 有冷却，否则每条请求弹一个 Chrome。"""


class AsrSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    engine: Literal["sherpa_sense_voice", "mlx_whisper", "faster_whisper"] = "sherpa_sense_voice"
    """本机只有 sherpa-onnx（SenseVoice）。

    V1 §7.9：SenseVoice **不产标点**，必须按静音切句补 `。`，
    否则整段被当成一句，下游 `split_sentences` 与抽取式爆款拆解字段全废。
    """

    model_dir: Path | None = None
    """None = 按候选路径探测（含仓库内 `data/asr/`）。

    兼容 V1 的 `SENSEVOICE_MODEL_DIR` / `SHERPA_ONNX_MODEL_DIR` 环境变量。
    模型包（233 MB）**不在 git 里也不在 data/ 的默认位置**，是换机器最容易漏的一项。
    """

    num_threads: int = Field(default=4, ge=1)
    device: Literal["cpu", "cuda", "mps"] = "cpu"


class HttpSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_seconds: int = Field(default=30, ge=1)
    follow_redirects: bool = True
    """必须 True：V1 §7.1 抖音 `v.douyin.com/<code>/` 短链要跟一次 302 才拿到 `sec_uid`。"""

    max_retries: int = Field(default=3, ge=0)
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )


class PathsSection(BaseModel):
    """外部二进制的显式路径。None = 走 PATH 查找。

    V1 §7.19：「注册表里有 PATH」≠「进程拿得到」。子进程继承的是**启动方那份环境快照**，
    改注册表只影响之后新开的终端。所以启动时要走 `prepare_runtime_environment()`
    把注册表 Machine+User 两份 Path 里「有、但进程 PATH 没有」的目录追加进去。
    """

    model_config = ConfigDict(extra="forbid")

    ffmpeg: Path | None = None
    ffprobe: Path | None = None
    node: Path | None = None
    yt_dlp: Path | None = None


class AppConfig(BaseSettings):
    """全局配置。

    **注意**：裸 `AppConfig()` 不读 YAML（见模块 docstring）。
    要读盘走 `load_app_config()` 或 `ConfigManager.load()`。
    """

    model_config = SettingsConfigDict(
        env_prefix="INTELLIGENCE_HUB_",
        env_nested_delimiter="__",
        env_file=None,
        extra="ignore",
    )

    app: AppSection = Field(default_factory=AppSection)
    data: DataSection = Field(default_factory=DataSection)
    storage: StorageSection = Field(default_factory=StorageSection)
    logging: LoggingSection = Field(default_factory=LoggingSection)
    scheduler: SchedulerSection = Field(default_factory=SchedulerSection)
    platform_control: PlatformControlSection = Field(default_factory=PlatformControlSection)
    """四家的总闸（ADR-0025）。某家可用 = 总闸 AND 它自己的 `enabled`。"""
    cdp_bridge: BridgeSection = Field(default_factory=BridgeSection)
    asr: AsrSection = Field(default_factory=AsrSection)
    http_client: HttpSection = Field(default_factory=HttpSection)
    paths: PathsSection = Field(default_factory=PathsSection)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,  # noqa: ARG003
        file_secret_settings: PydanticBaseSettingsSource,  # noqa: ARG003
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """优先级：init(CLI) > env > yaml > 默认值。

        `dotenv_settings` / `file_secret_settings` 故意不接：
        `.env` 与 secrets 目录是 V1 没有的东西，引进来等于多一处
        「配置到底从哪来的」的悬案（V1 §7.12 同类问题）。
        """
        yaml_source = _YamlDictSource(settings_cls)
        alias_source = _FlatEnvAliasSource(settings_cls)
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings]
        if not alias_source.is_empty():
            sources.append(alias_source)
        if not yaml_source.is_empty():
            sources.append(yaml_source)
        return tuple(sources)


ENV_FLAT_ALIASES: dict[str, tuple[str, ...]] = {
    "INTELLIGENCE_HUB_DATA_DIR": ("data", "dir"),
}
"""文档承诺的**扁平**环境变量 → 嵌套路径。

`env_nested_delimiter="__"` 意味着 `data.dir` 本只能写成 `INTELLIGENCE_HUB_DATA__DIR`，
而 §6 那张表里 `data.dir` 那行写的是单下划线，且 `AGENTS.md §3`、`config-schema.md §6`
（§7 声明此表对 V3 冻结）、`data-model.md §1`、`config/app.yaml` 四处一致。
两边都认，嵌套那种优先 —— 它是其余每一行的写法。

这一条不是拼写洁癖：把一次性脚本和测试挡在 live `data/`（有效会话 cookie、真实主库、
浏览器 profile）之外靠的就是这个开关，而它失效的方向正好**朝生产数据敞开**、且不报错。
"""


def _flat_alias_values() -> dict[str, Any]:
    """把 `ENV_FLAT_ALIASES` 里设了的项摊成嵌套 dict。空值不算设置（与 env 语义一致）。"""
    out: dict[str, Any] = {}
    for env_name, path in ENV_FLAT_ALIASES.items():
        raw = os.environ.get(env_name)
        if raw is None or not raw.strip():
            continue
        node: dict[str, Any] = out
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = raw.strip()
    return out


class _FlatEnvAliasSource(PydanticBaseSettingsSource):
    """`ENV_FLAT_ALIASES` 的 settings 源。见 `config-schema.md §6` 的「扁平别名」。"""

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:  # noqa: ARG002
        return None, field_name, False

    def is_empty(self) -> bool:
        return not _flat_alias_values()

    def __call__(self) -> dict[str, Any]:
        return _flat_alias_values()


class _YamlDictSource(PydanticBaseSettingsSource):
    """把 `load_app_config()` 预先解析好的 dict 当成一个 settings 源。

    不用 pydantic-settings 自带的 `YamlConfigSettingsSource`，因为那条路
    在 YAML 语法错时抛的是 `yaml.YAMLError`，而我们要抛 `ConfigError`
    并带上文件路径 —— 配置文件坏了必须说清是**哪个文件**坏了。
    """

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:  # noqa: ARG002
        value = _yaml_data().get(field_name)
        return value, field_name, False

    def is_empty(self) -> bool:
        return not _yaml_data()

    def __call__(self) -> dict[str, Any]:
        return dict(_yaml_data())


_YAML_DATA: ContextVar[dict[str, Any] | None] = ContextVar("ih_app_yaml_data", default=None)
"""`load_app_config()` 在构造 AppConfig 期间临时塞进去的 YAML 内容。

用 ContextVar 而不是实例属性：pydantic-settings 的源在 `settings_customise_sources`
里构造，拿不到实例；而 ContextVar 天然按调用栈隔离，并发加载不会互相污染。

默认值是 `None` 而不是 `{}` —— ruff B039：ContextVar 的可变默认值会被所有
未 set 过的上下文共享，谁往里塞一笔就全局脏了。
"""


_EMPTY: dict[str, Any] = {}
"""`_yaml_data()` 的只读兜底。**只读**，谁往里写就是 bug。"""


def _yaml_data() -> dict[str, Any]:
    """当前上下文里的 YAML 内容，没设过就是空 dict。"""
    return _YAML_DATA.get() or _EMPTY


# ---------------------------------------------------------------------------
# YAML 读取
# ---------------------------------------------------------------------------


def read_yaml_mapping(path: Path) -> dict[str, Any]:
    """读一个 YAML 文件当 mapping。文件不存在 = 空 dict（不是错误）。

    坏了就抛 `ConfigError` 并带上路径 —— 绝不静默吞成默认值。
    """
    if not path.is_file():
        return {}

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"读不到配置文件 {path}: {exc}"
        raise ConfigError(msg, path=str(path)) from exc

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        msg = f"配置文件 {path} 不是合法 YAML: {exc}"
        raise ConfigError(msg, path=str(path)) from exc

    if data is None:
        return {}
    if not isinstance(data, dict):
        msg = f"配置文件 {path} 顶层必须是 mapping，实际是 {type(data).__name__}"
        raise ConfigError(msg, path=str(path))

    out: dict[str, Any] = {}
    for key, value in data.items():
        out[str(key)] = value
    return out


def load_app_config(
    yaml_path: Path | str | None = None,
    **cli_overrides: Any,  # noqa: ANN401 - 直接透传给 AppConfig(**overrides)
) -> AppConfig:
    """加载全局配置。

    `yaml_path=None` → 用 `config/app.yaml`（spec 的默认路径）。
    文件不存在不报错，全默认值起得来。
    `cli_overrides` 优先级最高（spec §1 第 5 档）。
    """
    path = DEFAULT_APP_YAML if yaml_path is None else Path(yaml_path)
    data = read_yaml_mapping(path)
    token = _YAML_DATA.set(data)
    try:
        return AppConfig(**cli_overrides)
    finally:
        _YAML_DATA.reset(token)


# ---------------------------------------------------------------------------
# ConfigManager
# ---------------------------------------------------------------------------


def _build_platform_config(platform: str, section: object) -> PlatformConfig:
    """把 platforms.yaml 的一段校验成强类型配置。

    缺字段从注册表补默认（`display_name`），多字段按 `extra='forbid'` 如实报错。
    """
    schema = PLATFORM_CONFIG_SCHEMAS[platform]

    if section is None:
        payload: dict[str, Any] = {}
    elif isinstance(section, dict):
        payload = dict(section)
    else:
        msg = f"platforms.yaml 里 {platform} 段必须是 mapping，实际是 {type(section).__name__}"
        raise ConfigError(msg, path=str(DEFAULT_PLATFORMS_YAML))

    for key, value in platform_defaults(platform).items():
        payload.setdefault(key, value)

    try:
        return schema.model_validate(payload)
    except Exception as exc:  # pydantic.ValidationError 是 ValueError 子类，这里一并兜住
        msg = f"platforms.yaml 里 {platform} 段校验失败: {exc}"
        raise ConfigError(msg, path=str(DEFAULT_PLATFORMS_YAML)) from exc


def _jsonable(value: Any) -> Any:  # noqa: ANN401 - 递归归一化，叶子类型由模型决定
    """把一棵配置树归一成"能安全进 YAML"的形状，**并且只有一种行尾与分隔符的说法**。

    - `Path` → **POSIX 字符串**（`as_posix()`）。这条不是洁癖：`cookies_file` 是
      `Path | None`，而 `model_dump(mode="json")` 走的是 `str(path)` —— Windows 上那会
      把 `data/cookies/x.txt` 写成 `data\\cookies\\x.txt`，落进一份 **tracked、跨平台**的
      `config/platforms.yaml`。Linux 上（含 CI）那个值不再是 `data/cookies/x.txt`，
      而是一个**名字里带反斜杠**的文件。
    - `str` 的子类（`HttpUrl` 这类 pydantic 的 Url）→ 剥成普通 `str`，否则 YAML 会连
      类型标签一起写出来。
    - `Enum` → `.value`；`BaseModel` → 递归（用 `model_dump()` 的 python 档，
      这样 Path 还活着、由这一层统一转 POSIX）。
    """
    if isinstance(value, PurePath):
        return value.as_posix()
    if isinstance(value, BaseModel):
        return {key: _jsonable(item) for key, item in value.model_dump().items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    # 叶子：str 的子类（pydantic 的 Url 那一族）剥成普通 str，其余原样交给 YAML
    return str(value) if isinstance(value, str) else value


def _dump_platform_section(config: BaseModel) -> dict[str, Any]:
    """平台配置 → 要落盘的那份 dict，**丢掉 `ui:hidden` 的键**。

    为什么丢：那个标记的意思就是"这一格声明了，但今天没有任何实现路径"（ADR-0012，
    `retry_max` / `request_timeout_seconds` 这一批）。按整模型 dump 会把它们连默认值一起
    物化进 YAML —— 文件于是长得像"有人配过 retry_max"，而改它什么也不会发生。
    前端不受影响：判据在 JSON Schema 里（`/api/platforms/{name}/schema`），不是在这个文件里。

    与 `_jsonable` 的分工：这一层只管**哪些键进文件**（要读字段元数据，所以只能在模型上走），
    形状归一化（Path / Enum / Url）留给写盘那一个出口。
    """
    out: dict[str, Any] = {}
    for name, field in type(config).model_fields.items():
        extra = field.json_schema_extra
        if isinstance(extra, dict) and extra.get("ui:hidden"):
            continue
        value = getattr(config, name)
        out[name] = _dump_platform_section(value) if isinstance(value, BaseModel) else value
    return out


def _atomic_write_yaml(target: Path, data: dict[str, Any]) -> None:
    """把一份配置原子落到 `target`：同目录 `.tmp` + `Path.replace`（= `os.replace`）。

    **`newline="\n"` 是这一份的唯一真源，不许在调用点各写一遍**：Windows 上
    `write_text` 走文本模式会把 `\n` 翻成 `\r\n`，而这里写的是 **tracked、跨平台**的
    `config/app.yaml` / `config/platforms.yaml` —— 一次界面保存就把仓库里那份变成 CRLF
    （`pre-commit` 的 `mixed-line-ending` 会红，`ruff format --check` 报的是"整份文件每行都要
    重排"，看着像格式化在闹脾气，不像编码事故：`docs/lessons.md` 那条「行尾」就是为这个学的）。
    三个写盘点（platforms / scheduler / platform_control）以前各自复制一遍，第四个人加进来
    漏掉这五个字是迟早的事 —— 收成一处，漏不掉。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f"{target.name}.tmp"
    tmp.write_text(
        yaml.safe_dump(_jsonable(data), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
        newline="\n",
    )
    tmp.replace(target)


class ConfigManager:
    """配置的持有者与写盘者。

    纪律（spec §5）：**运行时不监听文件变化**。用户手改 YAML 不会自动生效，
    必须显式 `reload_platform()` / `load()`。写盘仍然做，为的是审计、备份、可进 git。

    线程安全：`write_platform_config` 持一把进程内写锁。
    `Path.replace` 本身是原子的，所以并发读者永远看到完整的旧文件或完整的新文件，
    不会看到半截。
    """

    def __init__(self, config_dir: Path | str = Path("config")) -> None:
        self._config_dir = Path(config_dir)
        self._app: AppConfig | None = None
        self._platforms: dict[str, PlatformConfig] = {}
        self._loaded = False
        """load() 过没有。写盘要合并盘上内容 + 内存那几段，没 load 过的内存是空的
        （判据与理由见 `write_platform_config`）。"""
        self._subscribers: list[PlatformConfigChanged] = []
        self._write_lock = threading.Lock()

    # ---- 路径 ----

    @property
    def config_dir(self) -> Path:
        return self._config_dir

    @property
    def app_yaml_path(self) -> Path:
        return self._config_dir / "app.yaml"

    @property
    def platforms_yaml_path(self) -> Path:
        return self._config_dir / "platforms.yaml"

    # ---- 加载 ----

    def load(self) -> AppConfig:
        """读 `app.yaml` + `platforms.yaml`，全部走 Pydantic 校验。

        任何一处坏了都抛 `ConfigError`，**不降级成默认值** ——
        用户写了配置但没生效，比启动失败更难查。
        """
        self._app = load_app_config(self.app_yaml_path)
        self._platforms = self._load_platforms()
        self._loaded = True
        logger.info(
            "config.loaded",
            config_dir=str(self._config_dir),
            platforms=list(self._platforms),
            enabled=self.enabled_platforms(),
        )
        return self._app

    def _load_platforms(self) -> dict[str, PlatformConfig]:
        raw = read_yaml_mapping(self.platforms_yaml_path)

        unknown = [key for key in raw if key not in PLATFORM_CONFIG_SCHEMAS]
        if unknown:
            supported = ", ".join(PLATFORM_CONFIG_SCHEMAS)
            msg = (
                f"platforms.yaml 里有本构建不支持的平台: {', '.join(sorted(unknown))}。"
                f"已注册的平台只有: {supported}。"
                f"（V2.0 只做抖音 + B站，小红书/YouTube 在 V2.1 —— 见 ROADMAP.md）"
            )
            raise ConfigError(msg, path=str(self.platforms_yaml_path))

        return {
            platform: _build_platform_config(platform, raw[platform])
            for platform in PLATFORM_CONFIG_SCHEMAS
            if platform in raw
        }

    # ---- 读 ----

    @property
    def app(self) -> AppConfig:
        """全局配置。没 `load()` 过就抛 `ConfigError`，不返回一个默认值糊弄过去。"""
        if self._app is None:
            msg = "ConfigManager 还没 load()，拿不到 AppConfig"
            raise ConfigError(msg, path=str(self.app_yaml_path))
        return self._app

    def get_platform(self, name: str) -> PlatformConfig:
        """取平台配置。未注册或未在 YAML 里配置都抛 `KeyError`。"""
        return self._platforms[name]

    def has_platform(self, name: str) -> bool:
        return name in self._platforms

    def platform_names(self) -> list[str]:
        """已配置的平台名，按注册表顺序。"""
        return [p for p in PLATFORM_CONFIG_SCHEMAS if p in self._platforms]

    def enabled_platforms(self) -> list[str]:
        """**可用**的平台名（总闸 AND 该家自己的开关），按注册表顺序。

        调度器、cron 排程、`/api/tasks` 的过滤、注册表的 `enabled_platforms()` 全读这里 ——
        所以"总闸关掉之后什么都停"这件事**不需要**每个消费点各改一遍。
        历史上这一位只看 `cfg.enabled`；ADR-0025 之后它是那一道 AND。
        """
        return [p for p in self.platform_names() if self.is_platform_available(p)]

    def is_enabled(self, name: str) -> bool:
        """这一家**可用**吗（总闸 AND 自身）。名字保留 `is_enabled` 是因为它已经有四个调用方，
        但语义从"它自己的开关"变成了"可不可用" —— 读它的人要的是后者。
        """
        return self.is_platform_available(name)

    def is_platform_available(self, name: str) -> bool:
        return self.platform_availability(name) == "available"

    def platform_availability(self, name: str) -> PlatformAvailability:
        """**哪一道闸**挡着这一家。四态各自的含义与"为什么要四态"写在
        `platforms/base.py::PlatformAvailability` —— 那里也是 AND 的唯一定义处，
        本方法只是把这份配置的两路输入递进去（`app.platform_control.enabled` 与
        `_platforms` 里那一段）。注册表、`/api/tasks` 的过滤与跑前那道门读的
        都是同一个函数，不是同一句话的三份抄写。
        """
        return resolve_platform_availability(
            self._platforms.get(name),
            master_enabled=self.app.platform_control.enabled,
        )

    # ---- 热重载 ----

    def subscribe(self, callback: PlatformConfigChanged) -> None:
        """注册 `(platform_name, PlatformConfig) -> None` 回调。

        只在 `reload_platform()` 里被调用，**写盘不通知**（spec §5 的顺序是
        写盘 → 热重载 → 发 CONFIG_CHANGED 事件）。
        """
        self._subscribers.append(callback)

    def reload_platform(self, name: str) -> PlatformConfig:
        """重读盘上该平台那一段，替换内存里的配置，然后通知订阅者。

        一个订阅者抛异常不影响其余订阅者，也不影响重载本身 ——
        前端挂了不该把配置层带崩。
        """
        if name not in PLATFORM_CONFIG_SCHEMAS:
            raise KeyError(name)

        raw = read_yaml_mapping(self.platforms_yaml_path)
        config = _build_platform_config(name, raw.get(name))
        self._platforms[name] = config

        logger.info("config.platform_reloaded", platform=name, enabled=config.enabled)

        for callback in list(self._subscribers):
            try:
                callback(name, config)
            except Exception as exc:
                logger.exception(
                    "config.subscriber_failed",
                    platform=name,
                    error=str(exc),
                    error_kind=type(exc).__name__,
                )
        return config

    # ---- 写盘 ----

    def write_platform_config(self, name: str, config: PlatformConfig) -> Path:
        """把某个平台的配置原子写进 `platforms.yaml`，并同步内存。

        原子性：写同目录下的 `<name>.yaml.tmp` 再 `Path.replace`（= `os.replace`）。
        同目录是必须的 —— 跨文件系统的 `replace` 不是原子操作。

        **不通知订阅者**，通知是 `reload_platform()` 的职责（spec §5）。

        敏感字段不走这个文件：飞书 token 在 `config/feishu.yaml`（gitignore），
        `data/cookies/` 是有效会话凭证。这里只有开关、路径、档位。
        """
        if name not in PLATFORM_CONFIG_SCHEMAS:
            raise KeyError(name)

        schema = PLATFORM_CONFIG_SCHEMAS[name]
        if not isinstance(config, schema):
            msg = f"平台 {name} 的配置必须是 {schema.__name__}，实际是 {type(config).__name__}"
            raise TypeError(msg)

        # 没 load() 过的 manager 内存是空的，而写盘要按内存重建文件 —— 一次调用就能把
        # 其他平台的整段配置抹掉（实测 bilibili 段消失、douyin 自己的 cookies_file 归 null）。
        # 注意判据是"有没有 load 过"，不是"注册表里的平台是否齐全"：文件里只写了
        # douyin 是合法配置，那样内存里本来就只有 douyin。
        if not self._loaded:
            msg = (
                f"ConfigManager 还没 load() 就要写 {name} 的配置。写盘是按内存重建 "
                f"platforms.yaml 的，空内存等于把整份文件清空 —— 先 ConfigManager.load()。"
            )
            raise ConfigError(msg, path=str(self.platforms_yaml_path))

        payload = _dump_platform_section(config)

        with self._write_lock:
            data = self._current_platforms_dump()
            data[name] = payload

            target = self.platforms_yaml_path
            _atomic_write_yaml(target, data)

            self._platforms[name] = config

        logger.info("config.platform_written", platform=name, path=str(target))
        return target

    def _current_platforms_dump(self) -> dict[str, Any]:
        """盘上现有内容打底，再盖内存里那几份**校验过的**配置，按注册表顺序。

        为什么不是"只用内存"：那等于假设内存与盘同步，而 load() 之后文件可能被别人改过
        （V2.1 装回来又退回 V2.0、或运维正在编辑）—— 那种情况下按内存重建会把盘上多出来
        的段静默删掉。为什么不是"只用盘"：盘上那份没经过 Pydantic 校验，写盘时拿它当权威
        等于把"改一个开关"变成"顺便被一份坏文件绑架"。
        合起来的语义是：**内存覆盖式更新自己负责的那几段，别人加的段原样保留。**
        """
        merged: dict[str, Any] = dict(read_yaml_mapping(self.platforms_yaml_path))
        for platform in PLATFORM_CONFIG_SCHEMAS:
            if platform in self._platforms:
                merged[platform] = _dump_platform_section(self._platforms[platform])
        return merged

    # ---- 写 `app.yaml` 的 scheduler 段（`/api/schedule` 的唯一写入口，ADR-0017）----

    def write_scheduler(self, section: SchedulerSection) -> Path:
        """把整段 `scheduler` 原子写进 `app.yaml`，并同步内存。

        与 `write_platform_config` 共享同一套纪律，只有一处不同：**这一整段是覆盖式的**。
        `SchedulerSection` 是 `extra="forbid"`，段里出现任何一个我们不认识的键，`load()`
        当场就红了（不会带着它活到写盘这一步），所以"保留盘上别人加的键"在这里没有对象 ——
        真留了反而会把一个已经过期的键写回去。段**以外**的顶层键（`data` / `storage` /
        `platforms.yaml` 那一路）一律原样保留。

        没 `load()` 过就写盘同样是禁止的：那样内存是空的，写出来是一份"只有默认值"的
        scheduler 段，等于把用户手写的 `timezone` / `max_concurrent_*` 抹掉。

        **这里刻意没有配套的 `reload_app()`**：`build_components` 里
        `config = manager.app`，所以 `AppConfig` 那**一个实例**同时被
        `AppState.config` / `TaskRunner._app` / `TaskScheduler._app` / `DepsFactory` 握着。
        原地换 `self._app.scheduler` 一次就到四家；而"重读一份新的再换指针"会造出第二个
        实例，那些没跟着换引用的持有者就悄悄继续用旧配置 —— 症状是"设置页说 08:00，
        定时器还是按昨天那条跑"。平台配置那边必须走 `reload_platform` 是因为它的值
        分散在四份**按平台建的 dict 快照**里，而那三处都留了 `update_config()` 入口；
        `scheduler` 段没有这种快照，只有一个共享实例。两处的形状不同，写法就不同。
        """
        if not self._loaded:
            msg = (
                "ConfigManager 还没 load() 就要写 scheduler 段。写盘是按内存重建 "
                "app.yaml 的，空内存等于把这一段换成默认值 —— 先 ConfigManager.load()。"
            )
            raise ConfigError(msg, path=str(self.app_yaml_path))

        payload = section.model_dump(mode="json")
        with self._write_lock:
            data = dict(read_yaml_mapping(self.app_yaml_path))
            data["scheduler"] = payload
            target = self.app_yaml_path
            _atomic_write_yaml(target, data)
            if self._app is not None:
                self._app.scheduler = section

        logger.info("config.scheduler_written", path=str(target), cron=payload.get("collect_cron"))
        return target

    def write_platform_control(self, enabled: bool) -> Path:
        """写总闸那一段并**原地换内存**（与 `write_scheduler` 同一套纪律，理由也同源）。

        为什么是原地换 `self._app.platform_control` 而不是重读一份新的：`build_components`
        里 `config = manager.app`，同一个 `AppConfig` 实例被 `AppState` / `TaskRunner` /
        `TaskScheduler` 共同握着；换指针会造出第二个实例，没跟着换的持有者继续用旧配置，
        症状是"设置页显示总闸已开，任务列表还是空的"。

        段**以外**的顶层键（`data` / `storage` / `scheduler` …）原样保留：
        这份文件是用户手写的，PUT 只该动它点过的那一格。
        """
        if not self._loaded:
            msg = (
                "ConfigManager 还没 load() 就要写 platform_control 段。写盘是按内存重建 "
                "app.yaml 的，空内存等于把这一段换成默认值 —— 先 ConfigManager.load()。"
            )
            raise ConfigError(msg, path=str(self.app_yaml_path))

        section = PlatformControlSection(enabled=enabled)
        with self._write_lock:
            data = dict(read_yaml_mapping(self.app_yaml_path))
            data["platform_control"] = section.model_dump(mode="json")
            target = self.app_yaml_path
            _atomic_write_yaml(target, data)
            if self._app is not None:
                self._app.platform_control = section

        logger.info("config.platform_control_written", path=str(target), enabled=enabled)
        return target

    def platform_control_keys_shadowed_by_env(self) -> list[str]:
        """总闸有没有被环境变量压着 —— 有则"保存成功"只对到下一次启动为止。

        与 `scheduler_keys_shadowed_by_env` 同一条理由（那里的注释写了症状）。
        单列一个方法而不是复用：那个的入参是 `SchedulerSection` 且段名写死，
        把它改成通用的会顺手改掉调度器那条看护的形状，收益不值。
        """
        prefix = str(AppConfig.model_config.get("env_prefix") or "")
        delimiter = str(AppConfig.model_config.get("env_nested_delimiter") or "__")
        head = f"{prefix}PLATFORM_CONTROL{delimiter}"
        return sorted(
            key
            for key in PlatformControlSection.model_fields
            if f"{head}{key.upper()}" in os.environ
        )

    def scheduler_keys_shadowed_by_env(self, section: SchedulerSection) -> list[str]:
        """哪些 `scheduler.*` 键**环境变量里也有**，因此下一次启动会盖掉盘上这份。

        存在的理由不是形式主义：优先级是 `默认 < yaml < env < CLI`，而 PUT 只能写 YAML。
        于是"保存成功"这句话对一个设过 `INTELLIGENCE_HUB_SCHEDULER__COLLECT_CRON` 的部署
        是**半真**的 —— 这台机器现在按新值跑，下一次启动回到 env 那条。不说出来，
        症状就是"设置页明明改成 08:00，重启后又变回 06:30"，而没有人会想到去查环境变量。

        前缀与分隔符从 `AppConfig.model_config` 取，不在这里再写一遍字面量：
        那两处一漂，这个方法就永远返回空清单，而"返回空"长得和"确实没有遮挡"一模一样。
        """
        prefix = str(AppConfig.model_config.get("env_prefix") or "")
        delimiter = str(AppConfig.model_config.get("env_nested_delimiter") or "__")
        head = f"{prefix}SCHEDULER{delimiter}"
        return sorted(
            key for key in type(section).model_fields if f"{head}{key.upper()}" in os.environ
        )


__all__ = [
    "AppConfig",
    "AppSection",
    "AsrSection",
    "BridgeSection",
    "ConfigManager",
    "DataSection",
    "HttpSection",
    "LoggingSection",
    "PathsSection",
    "PlatformAvailability",
    "PlatformControlSection",
    "SchedulerSection",
    "StorageSection",
    "load_app_config",
    "read_yaml_mapping",
]
