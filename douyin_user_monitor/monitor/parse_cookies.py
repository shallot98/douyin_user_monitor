"""解析 API 专用的 Cookie：和监控用的 Cookie 池分开。

单作品详情接口对 Cookie 要求很低（过期的游客 Cookie 也能过），所以解析走这里的
Cookie，不消耗监控 Cookie 池里的登录态。目录下每个 *.txt 是一条 Cookie 请求头串，
轮流使用；目录内容变了（增删改文件）自动重新读取。没有可用 Cookie 时直接报错，
不偷偷回落到监控 Cookie。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import List, Optional, Tuple


class ParseCookiesUnavailable(RuntimeError):
    """解析专用 Cookie 目录为空或不存在。"""


class ParseCookiePool:
    def __init__(self, directory: Path) -> None:
        self._dir = Path(directory)
        self._lock = threading.Lock()
        self._stamp: Optional[Tuple[Tuple[str, int, int], ...]] = None
        self._cookies: List[Tuple[str, str]] = []
        self._cursor = 0

    @property
    def directory(self) -> Path:
        return self._dir

    def _scan(self) -> Tuple[Tuple[str, int, int], ...]:
        if not self._dir.is_dir():
            return ()
        entries = []
        for path in sorted(self._dir.glob("*.txt")):
            stat = path.stat()
            entries.append((path.name, stat.st_mtime_ns, stat.st_size))
        return tuple(entries)

    def _refresh(self) -> None:
        stamp = self._scan()
        if stamp == self._stamp:
            return
        cookies = []
        for name, _, _ in stamp:
            text = (self._dir / name).read_text(encoding="utf-8", errors="replace").strip()
            # 只认请求头串（name=value; ...），一行一条；空文件跳过
            text = " ".join(line.strip() for line in text.splitlines() if line.strip())
            if "=" in text:
                cookies.append((name, text))
        self._cookies = cookies
        self._stamp = stamp
        self._cursor %= max(1, len(cookies))

    def count(self) -> int:
        with self._lock:
            self._refresh()
            return len(self._cookies)

    def take(self) -> Tuple[str, str]:
        """轮流返回 (文件名, Cookie)。"""
        with self._lock:
            self._refresh()
            if not self._cookies:
                raise ParseCookiesUnavailable(f"未配置解析专用 Cookie：{self._dir} 下没有 *.txt")
            item = self._cookies[self._cursor % len(self._cookies)]
            self._cursor = (self._cursor + 1) % len(self._cookies)
            return item
