"""网页里自助管理的 API Key：增删 + 按 Key 记使用次数。

- 只存 Key 的 SHA-256，明文只在创建时返回一次；列表里显示前缀便于辨认。
- 权限分两档：parse（只能调解析接口，给下载插件/快捷指令用）与 full（等同管理员）。
- 环境变量 DYMON_API_KEY 是“系统 Key”，不能在网页删除，但同样计数（id 固定为 system）。
- 计数写 SQLite（data/api_keys.db），单进程服务，一把锁足够。
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from douyin_user_monitor.monitor.timeutils import CHINA_TZ

SCOPES = ("parse", "full")
SYSTEM_KEY_ID = "system"
KEY_PREFIX = "dk_"
NAME_MAX_LENGTH = 40
PREFIX_SHOWN = 8

_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    scope TEXT NOT NULL,
    key_hash TEXT UNIQUE,
    prefix TEXT NOT NULL,
    created_at TEXT NOT NULL,
    use_count INTEGER NOT NULL DEFAULT 0,
    last_used_at TEXT,
    last_ip TEXT
)
"""


def hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(CHINA_TZ).isoformat(timespec="seconds")


class ApiKeyStore:
    def __init__(self, db_path: Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(_SCHEMA)
            self._conn.execute(
                "INSERT OR IGNORE INTO api_keys (id, name, scope, key_hash, prefix, created_at) VALUES (?, ?, ?, NULL, ?, ?)",
                (SYSTEM_KEY_ID, "系统 Key（DYMON_API_KEY）", "full", "", _now()),
            )
        # key_hash → (id, scope)；只在增删时刷新
        self._by_hash: Dict[str, tuple[str, str]] = {}
        self._reload()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _reload(self) -> None:
        with self._lock:
            rows = self._conn.execute("SELECT id, scope, key_hash FROM api_keys WHERE key_hash IS NOT NULL").fetchall()
        self._by_hash = {row["key_hash"]: (row["id"], row["scope"]) for row in rows}

    def create(self, name: str, scope: str) -> tuple[Dict[str, Any], str]:
        """返回（记录，明文 Key）；明文只有这一次。"""
        clean = str(name or "").strip()
        if not clean:
            raise ValueError("请填写 Key 名称")
        if len(clean) > NAME_MAX_LENGTH:
            raise ValueError(f"Key 名称最多 {NAME_MAX_LENGTH} 个字")
        if scope not in SCOPES:
            raise ValueError(f"权限只能是 {'/'.join(SCOPES)}：{scope!r}")
        plaintext = KEY_PREFIX + secrets.token_urlsafe(24)
        key_id = secrets.token_hex(8)
        with self._lock:
            self._conn.execute(
                "INSERT INTO api_keys (id, name, scope, key_hash, prefix, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (key_id, clean, scope, hash_key(plaintext), plaintext[: len(KEY_PREFIX) + PREFIX_SHOWN], _now()),
            )
        self._reload()
        return self.get(key_id), plaintext

    def delete(self, key_id: str) -> Dict[str, Any]:
        if key_id == SYSTEM_KEY_ID:
            raise ValueError("系统 Key 来自环境变量 DYMON_API_KEY，不能在网页删除")
        record = self.get(key_id)
        if record is None:
            raise KeyError("Key 不存在")
        with self._lock:
            self._conn.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
        self._reload()
        return record

    def get(self, key_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
        return self._public(row) if row is not None else None

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM api_keys ORDER BY id = ? DESC, created_at DESC", (SYSTEM_KEY_ID,)
            ).fetchall()
        return [self._public(row) for row in rows]

    def match(self, plaintext: str) -> Optional[tuple[str, str]]:
        """明文 Key → (id, scope)；不匹配返回 None。"""
        if not plaintext or not plaintext.startswith(KEY_PREFIX):
            return None
        return self._by_hash.get(hash_key(plaintext))

    def record_use(self, key_id: str, ip: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE api_keys SET use_count = use_count + 1, last_used_at = ?, last_ip = ? WHERE id = ?",
                (_now(), str(ip or "")[:64], key_id),
            )

    @staticmethod
    def _public(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "scope": row["scope"],
            "prefix": row["prefix"],
            "created_at": row["created_at"],
            "use_count": row["use_count"],
            "last_used_at": row["last_used_at"],
            "last_ip": row["last_ip"],
            "system": row["id"] == SYSTEM_KEY_ID,
        }


_STORE: Optional[ApiKeyStore] = None


def configure(db_path: Path) -> ApiKeyStore:
    """api/monitor 启动时指定库位置（状态文件同目录 data/api_keys.db）。"""
    global _STORE
    if _STORE is not None:
        _STORE.close()
    _STORE = ApiKeyStore(db_path)
    return _STORE


def get_store() -> Optional[ApiKeyStore]:
    """未配置（例如脚本只导入 auth）时返回 None：只认系统 Key。"""
    return _STORE
