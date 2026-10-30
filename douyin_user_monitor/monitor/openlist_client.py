"""Resolve archived download files to streamable OpenList URLs.

`scripts/openlist-auto-move` moves everything under `download/` to OpenList's
`remote_base`, preserving the per-user folder structure. So a record's stored
relative path (`download_records[].files[i]`) maps 1:1 onto
`<remote_base>/<relative path>`.

Streaming a file needs a `sign` from `/api/fs/get`; the signed proxy URL
(`/p/<path>?sign=...`) supports Range, which is what makes seeking work.
Observed signs carry a `:0` suffix (no expiry), so they are cached per path.

The API token comes from config when set, otherwise it is read out of OpenList's
own SQLite database — opened read-only so a running OpenList is never disturbed.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import quote

import httpx

from douyin_user_monitor.monitor.path_sanitize import sanitize_relative_path, sanitize_user_dir_only
from douyin_user_monitor.settings import ArchiveSettings

logger = logging.getLogger(__name__)

TOKEN_SETTING_KEY = "token"
SQLITE_TIMEOUT_SECONDS = 5.0
# 上游直链（wopan / d2 这类 web_proxy 关闭的存储）通常带时效，只做短缓存
RAW_URL_CACHE_SECONDS = 300.0
# OpenList 自代理地址由 sign 决定，实测 sign 无有效期（尾部 :0），可以久存
PROXY_URL_CACHE_SECONDS = 1800.0
FIRST_LIST_PAGE = 1
REFRESH_RESULT_PAGE_SIZE = 1
# A stalled provider must not block trying the healthy backup provider for the
# full archive HTTP timeout. The stream route can then return the first usable
# copy instead of leaving the browser in a pending media request.
# 必须作为 httpx 的请求超时传入，不能用 asyncio.wait_for 从外部取消：
# httpcore 1.0.9 在「排队等连接/建连中」被取消时会把连接留在池里永不归还，
# 累积满 100 个后所有查询都卡在取连接上（2026-09-28 线上事故，压测可复现）。
RESOLVE_CANDIDATE_TIMEOUT_SECONDS = 5.0


class ArchiveUnavailableError(RuntimeError):
    """Archive backend is disabled, unreachable, or has no such file."""


class OpenListClient:
    def __init__(self, config: ArchiveSettings, *, clock: Callable[[], float] = time.monotonic):
        self._config = config
        self._token: Optional[str] = None
        self._sign_cache: Dict[str, str] = {}
        self._url_cache: Dict[str, tuple[str, float]] = {}
        self._clock = clock
        self._lock = asyncio.Lock()
        self._http_client: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._config.enabled)

    def _primary_relative(
        self, relative: str, sec_user_id: Optional[str] = None, *, sanitize_filename: bool = True
    ) -> Optional[str]:
        """清理后的相对路径，用于主力盘和备份盘的历史副本。

        WoPan 写入时去掉 emoji，并把全 emoji 用户目录改为 sec_user_id 目录。
        D2 和备份盘也可能保留这些已改名文件，读取时复用同一变换。
        """
        try:
            return sanitize_relative_path(relative, sec_user_id, sanitize_filename=sanitize_filename)
        except ValueError:
            # 中段目录清理后为空，跳过该候选。
            return None

    def remote_paths_for(
        self, relative_path: str, sec_user_id: Optional[str] = None, *, verified_paths: tuple[str, ...] = ()
    ) -> list[str]:
        """主力盘优先，兼容原名与历史清理后的目录和文件名。

        每个盘先查已核实的完整别名，所有主力盘候选均先于备份盘。
        别名保持文件名和空格原样；其余候选沿用原名与历史清理名规则。
        """
        relative = str(relative_path or "").strip().strip("/")
        if not relative:
            raise ArchiveUnavailableError("归档相对路径为空")

        sanitized = self._primary_relative(relative, sec_user_id)
        directory_only = self._primary_relative(relative, sec_user_id, sanitize_filename=False)
        user_dir_only = sanitize_user_dir_only(relative, sec_user_id)
        bases = self._config.read_bases
        primary = self._config.primary_base
        if any(not any(path.startswith(f"{base}/") for base in bases) for path in verified_paths):
            raise ArchiveUnavailableError("已核实归档路径不属于配置的主力盘或备份盘")

        candidates: list[str] = []
        for base in bases:
            candidates.extend(path for path in verified_paths if path.startswith(f"{base}/"))
            if base == primary:
                if not self._config.sanitize_primary_dirs:
                    candidates.append(f"{base}/{relative}")
                elif directory_only is not None:
                    candidates.append(f"{base}/{directory_only}")
            if sanitized is not None:
                candidates.append(f"{base}/{sanitized}")
            if directory_only is not None:
                candidates.append(f"{base}/{directory_only}")
            if base != primary and sanitized != relative:
                candidates.append(f"{base}/{relative}")
            if user_dir_only is not None:
                # 去 emoji 迁移半途：用户目录已清洗、作品目录仍是原名
                candidates.append(f"{base}/{user_dir_only}")
        if not candidates:
            raise ArchiveUnavailableError("归档路径在主力盘与备份盘均无法构造")
        return list(dict.fromkeys(candidates))

    def remote_path_for(self, relative_path: str) -> str:
        """主力盘路径（写入/单点定位用）。"""
        relative = str(relative_path or "").strip().strip("/")
        if not relative:
            raise ArchiveUnavailableError("归档相对路径为空")
        return f"{self._config.primary_base}/{relative}"

    def _read_token_from_db(self) -> str:
        db_path: Path = self._config.openlist_db_path
        if not db_path.is_file():
            raise ArchiveUnavailableError(f"OpenList 数据库不存在: {db_path}")
        # 只读打开，避免干扰正在运行的 OpenList
        uri = f"file:{quote(str(db_path))}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=SQLITE_TIMEOUT_SECONDS)
        except sqlite3.Error as exc:
            raise ArchiveUnavailableError(f"打开 OpenList 数据库失败: {exc}") from exc
        try:
            row = connection.execute(
                "SELECT value FROM x_setting_items WHERE key = ? LIMIT 1",
                (TOKEN_SETTING_KEY,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise ArchiveUnavailableError(f"读取 OpenList token 失败: {exc}") from exc
        finally:
            connection.close()
        token = str((row or [""])[0] or "").strip()
        if not token:
            raise ArchiveUnavailableError("OpenList token 为空")
        return token

    async def _get_token(self) -> str:
        if self._config.openlist_token:
            return self._config.openlist_token
        if self._token:
            return self._token
        token = await asyncio.to_thread(self._read_token_from_db)
        self._token = token
        return token

    async def _get_http_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=self._config.timeout_seconds)
        return self._http_client

    async def close(self) -> None:
        client, self._http_client = self._http_client, None
        if client is not None and not client.is_closed:
            await client.aclose()

    async def _recycle_http_client(self, stale: httpx.AsyncClient) -> None:
        """取连接超时说明连接池已被占满，换新 client 自愈，不让播放一直卡死。"""
        if self._http_client is not stale:
            return
        self._http_client = None
        logger.warning("OpenList 连接池取连接超时，重建 HTTP client")
        await stale.aclose()

    async def refresh_directory(self, remote_path: str) -> None:
        """Refresh metadata after files were written directly to the provider."""
        if not self.enabled:
            raise ArchiveUnavailableError("归档回放未启用")
        token = await self._get_token()
        try:
            client = await self._get_http_client()
            response = await client.post(
                f"{self._config.openlist_base_url}/api/fs/list",
                headers={"Authorization": token, "Content-Type": "application/json"},
                json={"path": remote_path, "password": "", "page": FIRST_LIST_PAGE,
                      "per_page": REFRESH_RESULT_PAGE_SIZE, "refresh": True},
            )
        except httpx.HTTPError as exc:
            raise ArchiveUnavailableError(f"OpenList 目录刷新请求失败: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise ArchiveUnavailableError(f"OpenList 目录刷新响应异常: status={response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ArchiveUnavailableError("OpenList 目录刷新返回内容不是 JSON") from exc
        code = payload.get("code") if isinstance(payload, dict) else None
        if type(code) is not int or code != 200:
            diagnostic = str(code) if type(code) is int else "invalid"
            raise ArchiveUnavailableError(f"OpenList 目录刷新失败: code={diagnostic}")

    async def _fs_get(self, remote_path: str, *, timeout_seconds: float | None = None) -> Dict[str, Any]:
        if not self.enabled:
            raise ArchiveUnavailableError("归档回放未启用")
        token = await self._get_token()
        client = await self._get_http_client()
        timeout = httpx.Timeout(timeout_seconds) if timeout_seconds is not None else httpx.USE_CLIENT_DEFAULT
        try:
            response = await client.post(
                f"{self._config.openlist_base_url}/api/fs/get",
                headers={"Authorization": token, "Content-Type": "application/json"},
                json={"path": remote_path},
                timeout=timeout,
            )
        except httpx.PoolTimeout as exc:
            await self._recycle_http_client(client)
            raise ArchiveUnavailableError("OpenList 查询超时（连接池已满）") from exc
        except httpx.TimeoutException as exc:
            raise ArchiveUnavailableError("OpenList 查询超时") from exc
        except httpx.HTTPError as exc:
            raise ArchiveUnavailableError(f"OpenList 请求失败: {exc}") from exc
        if response.status_code != 200:
            raise ArchiveUnavailableError(f"OpenList 响应异常: status={response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ArchiveUnavailableError("OpenList 返回内容不是 JSON") from exc
        if payload.get("code") != 200:
            raise ArchiveUnavailableError(f"归档文件不可用: {payload.get('message') or 'unknown'}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ArchiveUnavailableError("OpenList 返回缺少 data")
        return data

    def _self_proxy_prefix(self) -> str:
        return f"{self._config.openlist_base_url}/p"

    async def resolve_sign(self, remote_path: str) -> str:
        if not self.enabled:
            raise ArchiveUnavailableError("归档回放未启用")
        cached = self._sign_cache.get(remote_path)
        if cached:
            return cached

        token = await self._get_token()
        client = await self._get_http_client()
        try:
            response = await client.post(
                f"{self._config.openlist_base_url}/api/fs/get",
                headers={"Authorization": token, "Content-Type": "application/json"},
                json={"path": remote_path},
            )
        except httpx.HTTPError as exc:
            raise ArchiveUnavailableError(f"OpenList 请求失败: {exc}") from exc

        if response.status_code != 200:
            raise ArchiveUnavailableError(f"OpenList 响应异常: status={response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ArchiveUnavailableError("OpenList 返回内容不是 JSON") from exc
        if payload.get("code") != 200:
            raise ArchiveUnavailableError(f"归档文件不可用: {payload.get('message') or 'unknown'}")

        data = payload.get("data")
        if not isinstance(data, dict):
            raise ArchiveUnavailableError("OpenList 返回缺少 data")
        sign = str(data.get("sign") or "").strip()
        if not sign:
            raise ArchiveUnavailableError("OpenList 未返回 sign，可能未开启签名")
        async with self._lock:
            self._sign_cache[remote_path] = sign
        return sign

    def build_stream_url(self, remote_path: str, sign: str) -> str:
        # 路径逐段编码：中文/emoji 目录名常见，但 '/' 必须保留
        encoded_path = quote(remote_path, safe="/")
        return f"{self._config.openlist_base_url}/p{encoded_path}?sign={quote(sign, safe='')}"

    async def _resolve_one(self, remote_path: str, *, timeout_seconds: float | None = None) -> str:
        """单个远端路径 → 可直接流式读取的 URL。

        存储开了 web_proxy 时 OpenList 自己代理（raw_url 指回 /p）；关闭时 raw_url
        是上游直链（wopan / d2 都是这种），此时必须用直链——打 /p 会 403。
        """
        cached = self._url_cache.get(remote_path)
        if cached is not None:
            url, expires_at = cached
            if self._clock() < expires_at:
                return url

        data = await self._fs_get(remote_path, timeout_seconds=timeout_seconds)
        raw_url = str(data.get("raw_url") or "").strip()
        if raw_url and not raw_url.startswith(self._self_proxy_prefix()):
            # 上游直链通常带时效，只做短缓存
            self._url_cache[remote_path] = (raw_url, self._clock() + RAW_URL_CACHE_SECONDS)
            return raw_url

        sign = str(data.get("sign") or "").strip()
        if not sign and not raw_url:
            raise ArchiveUnavailableError("OpenList 既未返回 raw_url 也未返回 sign")
        url = raw_url or self.build_stream_url(remote_path, sign)
        if sign:
            self._sign_cache[remote_path] = sign
        self._url_cache[remote_path] = (url, self._clock() + PROXY_URL_CACHE_SECONDS)
        return url

    async def resolve_candidate_url(self, remote_path: str, *, refresh: bool = False) -> str:
        """Resolve one server-selected replica, optionally renewing only its cached URL."""
        if refresh:
            self._url_cache.pop(remote_path, None)
            self._sign_cache.pop(remote_path, None)
        return await self._resolve_one(remote_path, timeout_seconds=RESOLVE_CANDIDATE_TIMEOUT_SECONDS)

    async def resolve_stream_url(
        self, relative_path: str, sec_user_id: Optional[str] = None, *, verified_paths: tuple[str, ...] = ()
    ) -> str:
        """按主力盘 → 备份盘顺序找第一个可读的副本。"""
        candidates = self.remote_paths_for(relative_path, sec_user_id, verified_paths=verified_paths)
        errors: list[str] = []
        for remote_path in candidates:
            try:
                return await self.resolve_candidate_url(remote_path)
            except ArchiveUnavailableError as exc:
                errors.append(f"{remote_path}: {exc}")
        raise ArchiveUnavailableError("归档文件在主力盘与备份盘均不可用: " + " | ".join(errors))

    def invalidate(
        self, relative_path: str, sec_user_id: Optional[str] = None, *, verified_paths: tuple[str, ...] = ()
    ) -> None:
        """直链过期时清掉缓存，让下次重新解析。"""
        for remote_path in self.remote_paths_for(relative_path, sec_user_id, verified_paths=verified_paths):
            self._url_cache.pop(remote_path, None)
            self._sign_cache.pop(remote_path, None)
