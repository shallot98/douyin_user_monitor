"""Independent, revision-aware cover index and immutable JPEG objects.

publish(user_id, record, source) validates an image before committing its index
entry. lookup/lookup_many match aweme_id and the exact downloaded_at revision;
legacy records without that field use None. Playback repair may append files or
change cover_file without invalidating this index. No download record is edited.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Iterable, Iterator, Mapping

from douyin_user_monitor.monitor.cover_image import normalize_cover

SQLITE_TIMEOUT_SECONDS = 30
IDENTITY_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
VERSION_PATTERN = re.compile(r"[0-9a-f]{64}")


class CoverIntegrityError(ValueError):
    """An indexed cover no longer identifies its verified JPEG object."""


@dataclass(frozen=True)
class CoverMetadata:
    user_id: str
    aweme_id: str
    downloaded_at: str | None
    version: str
    path: Path
    width: int
    height: int
    size_bytes: int


def _identity(value: Any) -> str:
    text = str(value or "")
    if not IDENTITY_PATTERN.fullmatch(text):
        raise ValueError("封面用户或作品 ID 无效")
    return text


def _record_key(user_id: str, record: Mapping[str, Any]) -> tuple[str, str, str]:
    downloaded_at = record.get("downloaded_at")
    if downloaded_at is not None and not isinstance(downloaded_at, str):
        raise ValueError("封面记录 downloaded_at 无效")
    return _identity(user_id), _identity(record.get("aweme_id")), json.dumps(downloaded_at)


def _write_object(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix=".cover-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class CoverStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "index.sqlite3"
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS covers (
                    user_id TEXT NOT NULL,
                    aweme_id TEXT NOT NULL,
                    record_revision TEXT NOT NULL,
                    version TEXT NOT NULL,
                    width INTEGER NOT NULL,
                    height INTEGER NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    PRIMARY KEY (user_id, aweme_id, record_revision)
                )
            """)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.db_path, timeout=SQLITE_TIMEOUT_SECONDS)) as connection:
            connection.row_factory = sqlite3.Row
            with connection:
                yield connection

    def _object_path(self, key: tuple[str, str, str], version: str) -> Path:
        if not VERSION_PATTERN.fullmatch(version):
            raise CoverIntegrityError("封面对象版本无效")
        path = (self.root / "objects" / key[0] / key[1] / f"{version}.jpg").resolve()
        if not path.is_relative_to(self.root):
            raise CoverIntegrityError("封面对象路径超出存储目录")
        return path

    def publish(self, user_id: str, record: Mapping[str, Any], source: Path) -> CoverMetadata:
        key = _record_key(user_id, record)
        image = normalize_cover(source)
        version = hashlib.sha256(image.data).hexdigest()
        path = self._object_path(key, version)
        _write_object(path, image.data)
        metadata = CoverMetadata(
            user_id=key[0], aweme_id=key[1], downloaded_at=record.get("downloaded_at"),
            version=version, path=path, width=image.width, height=image.height,
            size_bytes=len(image.data),
        )
        self._publish_index(key, metadata)
        return metadata

    def _publish_index(self, key: tuple[str, str, str], metadata: CoverMetadata) -> None:
        with self._connect() as connection:
            connection.execute("""
                INSERT INTO covers VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (user_id, aweme_id, record_revision) DO UPDATE SET
                    version=excluded.version, width=excluded.width,
                    height=excluded.height, size_bytes=excluded.size_bytes
            """, (*key, metadata.version, metadata.width, metadata.height, metadata.size_bytes))

    def lookup(self, user_id: str, record: Mapping[str, Any]) -> CoverMetadata | None:
        return self.lookup_many(user_id, (record,)).get(str(record.get("aweme_id") or ""))

    def lookup_many(
        self, user_id: str, records: Iterable[Mapping[str, Any]],
    ) -> dict[str, CoverMetadata]:
        """Read one page with a single connection, without reading media bytes."""
        covers = {}
        with self._connect() as connection:
            for record in records:
                key = _record_key(user_id, record)
                row = connection.execute(
                    "SELECT * FROM covers WHERE user_id=? AND aweme_id=? AND record_revision=?", key,
                ).fetchone()
                if row is not None:
                    covers[key[1]] = self._metadata(key, row)
        return covers

    def _metadata(self, key: tuple[str, str, str], row: sqlite3.Row) -> CoverMetadata:
        path = self._object_path(key, row["version"])
        if not path.is_file() or path.stat().st_size != row["size_bytes"]:
            raise CoverIntegrityError("已索引封面文件缺失或大小不符")
        return CoverMetadata(
            user_id=key[0], aweme_id=key[1], downloaded_at=json.loads(key[2]),
            version=row["version"], path=path, width=row["width"], height=row["height"],
            size_bytes=row["size_bytes"],
        )

    def read(self, metadata: CoverMetadata) -> bytes:
        """Return only the bytes verified by the published content hash."""
        key = _record_key(metadata.user_id, {
            "aweme_id": metadata.aweme_id, "downloaded_at": metadata.downloaded_at,
        })
        data = self._object_path(key, metadata.version).read_bytes()
        if len(data) != metadata.size_bytes or hashlib.sha256(data).hexdigest() != metadata.version:
            raise CoverIntegrityError("封面对象校验失败")
        return data
