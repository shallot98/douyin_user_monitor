from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

import yaml

CONFIG_ENV = "DYMON_CONFIG"
CRAWLER_CONFIG_ENV = "DYMON_CRAWLER_CONFIG"
STATE_PATH_ENV = "DYMON_STATE_PATH"
DOWNLOAD_ROOT_ENV = "DYMON_DOWNLOAD_ROOT"
TG_ENABLED_ENV = "DYMON_TG_ENABLED"
TG_BOT_TOKEN_ENV = "DYMON_TG_BOT_TOKEN"
TG_CHAT_ID_ENV = "DYMON_TG_CHAT_ID"
TG_API_BASE_ENV = "DYMON_TG_API_BASE"
TG_TIMEOUT_ENV = "DYMON_TG_TIMEOUT"
ARCHIVE_TOKEN_ENV = "DYMON_ARCHIVE_TOKEN"

DEFAULT_CRAWLER_CONFIG_PATH = "config/douyin_web.yaml"
DEFAULT_AVATAR_TIMEOUT_SECONDS = 30.0
DEFAULT_STATE_PATH = "data/monitor_users.json"
DEFAULT_DOWNLOAD_ROOT = "download"
DEFAULT_TG_ENABLED = False
DEFAULT_TG_API_BASE = "https://api.telegram.org"
DEFAULT_TG_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class CrawlerSettings:
    config_path: Path
    timeout_seconds: float


@dataclass(frozen=True)
class MonitorSettings:
    state_path: Path
    download_root: Path


@dataclass(frozen=True)
class TelegramSettings:
    enabled: bool
    bot_token: str
    chat_id: str
    api_base: str
    timeout_seconds: float


@dataclass(frozen=True)
class HermesWeixinSettings:
    enabled: bool
    ssh_host: str
    ssh_user: str
    hermes_home: str
    hermes_bin: str
    target: str
    timeout_seconds: float


@dataclass(frozen=True)
class NotificationSettings:
    telegram: TelegramSettings
    hermes_weixin: HermesWeixinSettings


@dataclass(frozen=True)
class CookieLivenessSettings:
    enabled: bool
    interval_hours: float
    stale_days: float
    sample_user_count: int
    min_samples: int
    alert_cooldown_hours: float


@dataclass(frozen=True)
class CookieRotationSettings:
    enabled: bool
    skip_expired: bool


@dataclass(frozen=True)
class ArchiveSettings:
    """已下载作品的归档存放位置（OpenList/网盘），用于作品页回放。

    primary_base 是主力盘（网页读它），backup_base 是备份盘。读取按
    primary → backup 顺序回落：历史作品只存在备份盘上，回落保证它们仍可播放。
    """

    enabled: bool
    openlist_base_url: str
    openlist_db_path: Path
    openlist_token: str
    primary_base: str
    backup_base: str
    timeout_seconds: float
    # 主力盘是否要求清理 emoji；关闭时优先原名，也查找历史清理后的副本
    sanitize_primary_dirs: bool

    @property
    def read_bases(self) -> tuple[str, ...]:
        """去重后的读取顺序。"""
        bases = [base for base in (self.primary_base, self.backup_base) if base]
        unique: list[str] = []
        for base in bases:
            if base not in unique:
                unique.append(base)
        return tuple(unique)


@dataclass(frozen=True)
class Settings:
    project_root: Path
    crawler: CrawlerSettings
    monitor: MonitorSettings
    notifications: NotificationSettings
    cookie_liveness: CookieLivenessSettings
    cookie_rotation: CookieRotationSettings
    archive: ArchiveSettings


DEFAULT_COOKIE_LIVENESS_ENABLED = True
DEFAULT_COOKIE_LIVENESS_INTERVAL_HOURS = 6.0
DEFAULT_COOKIE_LIVENESS_STALE_DAYS = 7.0
DEFAULT_COOKIE_LIVENESS_SAMPLE_USER_COUNT = 5
DEFAULT_COOKIE_LIVENESS_MIN_SAMPLES = 3
DEFAULT_COOKIE_LIVENESS_ALERT_COOLDOWN_HOURS = 12.0
DEFAULT_COOKIE_ROTATION_ENABLED = True
DEFAULT_COOKIE_ROTATION_SKIP_EXPIRED = True
DEFAULT_HERMES_WEIXIN_ENABLED = False
DEFAULT_HERMES_WEIXIN_SSH_HOST = ""
DEFAULT_HERMES_WEIXIN_SSH_USER = "root"
DEFAULT_HERMES_WEIXIN_HOME = ""
DEFAULT_HERMES_WEIXIN_BIN = ""
DEFAULT_HERMES_WEIXIN_TARGET = "weixin"
DEFAULT_HERMES_WEIXIN_TIMEOUT_SECONDS = 60.0
# 归档回放：mover 把 download/ 下的文件搬到 OpenList 的 remote_base 下，保留用户目录结构
DEFAULT_ARCHIVE_ENABLED = True
DEFAULT_ARCHIVE_OPENLIST_BASE_URL = "http://127.0.0.1:5244"
DEFAULT_ARCHIVE_OPENLIST_DB_PATH = "/opt/openlist/data/data.db"
# 主力盘 D2：网页播放优先读取现有归档目录
DEFAULT_ARCHIVE_PRIMARY_BASE = "/d2/vps/抖音视频"
# 备份盘 kuake：历史作品都在这里，读取回落到它
DEFAULT_ARCHIVE_BACKUP_BASE = "/kuake/抖音视频"
DEFAULT_ARCHIVE_TIMEOUT_SECONDS = 30.0
DEFAULT_ARCHIVE_SANITIZE_PRIMARY_DIRS = False


def _resolve_config_path(project_root: Path) -> Path:
    path_value = os.getenv(CONFIG_ENV, "")
    if path_value:
        return Path(path_value).expanduser()
    return project_root / "config.yaml"


def _load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return _ensure_dict(data or {}, str(path))


def _ensure_dict(value: Any, name: str) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TypeError(f"配置项 {name} 必须是对象(dict)")
    return value


def _resolve_path(project_root: Path, raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path
    return (project_root / path).resolve()


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(f"布尔配置值无效: {value}")


def _parse_positive_float(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是数字") from exc
    if number <= 0:
        raise ValueError(f"{name} 必须大于 0")
    return number


def _parse_positive_int(value: Any, name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是整数") from exc
    if number <= 0:
        raise ValueError(f"{name} 必须大于 0")
    return number
