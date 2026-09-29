"""Verified historical archive paths, separate from immutable download history.

The optional archive_path_aliases.json lives beside the monitor state. An
installed file must contain schema_version=1 and an entries list. Each entry
binds one exact user/aweme/original path to a verified absolute OpenList path.
source_revision contains sec_user_id and downloaded_at; appended playback or
cover files and new verification metadata do not invalidate the original alias.
The audit source_fingerprint is provenance, not a hash of future record changes.

build_archive_path_aliases(evidence, manifest, users) returns (document, report)
without writing state or media. Install its document atomically and restart the
API to load it. A missing optional file means no aliases are installed; malformed
existing files and stale source revisions fail explicitly.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from douyin_user_monitor.monitor.archive_feed import find_record, list_archive_files

ARCHIVE_PATH_ALIASES_FILENAME = "archive_path_aliases.json"
ARCHIVE_PATH_ALIASES_VERSION = 1


class ArchivePathAliasError(ValueError):
    """An installed alias is invalid or no longer identifies the recorded source."""


def _required_text(values: Mapping[str, Any], field: str) -> str:
    value = values.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ArchivePathAliasError(f"归档路径映射缺少有效字段: {field}")
    return value


def _validate_path(path: str, *, absolute: bool) -> None:
    relative = path[1:] if absolute else path
    if path.startswith("/") != absolute or "\0" in path:
        raise ArchivePathAliasError(f"归档映射路径格式无效: {path!r}")
    if any(part in ("", ".", "..") for part in relative.split("/")):
        raise ArchivePathAliasError(f"归档映射路径包含无效目录: {path!r}")


@dataclass(frozen=True)
class ArchivePathAlias:
    user_id: str
    aweme_id: str
    original_relative_path: str
    verified_remote_path: str
    sec_user_id: str
    downloaded_at: str | None

    @classmethod
    def from_entry(cls, entry: Mapping[str, Any]) -> ArchivePathAlias:
        revision = entry.get("source_revision")
        if not isinstance(revision, dict) or "downloaded_at" not in revision:
            raise ArchivePathAliasError("归档路径映射缺少 source_revision")
        downloaded_at = revision["downloaded_at"]
        if downloaded_at is not None and not isinstance(downloaded_at, str):
            raise ArchivePathAliasError("归档路径映射的 downloaded_at 无效")
        relative = _required_text(entry, "original_relative_path")
        remote = _required_text(entry, "verified_remote_path")
        _validate_path(relative, absolute=False)
        _validate_path(remote, absolute=True)
        return cls(
            user_id=_required_text(entry, "user_id"),
            aweme_id=_required_text(entry, "aweme_id"),
            original_relative_path=relative,
            verified_remote_path=remote,
            sec_user_id=_required_text(revision, "sec_user_id"),
            downloaded_at=downloaded_at,
        )

    @property
    def key(self) -> tuple[str, str, str]:
        return self.user_id, self.aweme_id, self.original_relative_path

    def stale_reason(self, user: dict[str, Any] | None) -> str | None:
        if user is None:
            return "user_missing"
        if str(user.get("sec_user_id") or "") != self.sec_user_id:
            return "source_identity_changed"
        record = find_record(user, self.aweme_id)
        if record is None:
            return "source_record_missing"
        if self.original_relative_path not in list_archive_files(user, self.aweme_id):
            return "source_path_removed"
        if record.get("downloaded_at") != self.downloaded_at:
            return "download_revision_changed"
        return None


class ArchivePathAliases:
    def __init__(self, entries: Iterable[Mapping[str, Any]] = ()):
        aliases: dict[tuple[str, str, str], ArchivePathAlias] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise ArchivePathAliasError("归档路径映射条目必须是对象")
            alias = ArchivePathAlias.from_entry(entry)
            if alias.key in aliases:
                raise ArchivePathAliasError(f"归档路径映射重复: {alias.key}")
            aliases[alias.key] = alias
        self._aliases = MappingProxyType(aliases)

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> ArchivePathAliases:
        if not isinstance(document, dict) or document.get("schema_version") != ARCHIVE_PATH_ALIASES_VERSION:
            raise ArchivePathAliasError("归档路径映射 schema_version 不受支持")
        entries = document.get("entries")
        if not isinstance(entries, list):
            raise ArchivePathAliasError("归档路径映射缺少 entries 列表")
        return cls(entries)

    @classmethod
    def from_file(cls, path: Path) -> ArchivePathAliases:
        return cls.from_document(json.loads(path.read_text(encoding="utf-8")))

    def paths_for(self, user: dict[str, Any], aweme_id: str, relative_path: str) -> tuple[str, ...]:
        key = str(user.get("id") or ""), aweme_id, relative_path
        alias = self._aliases.get(key)
        if alias is None:
            return ()
        reason = alias.stale_reason(user)
        if reason:
            raise ArchivePathAliasError(f"归档路径映射已过期: {aweme_id}: {reason}")
        return (alias.verified_remote_path,)


def load_archive_path_aliases(state_path: Path) -> ArchivePathAliases:
    """Load the optional registry once; an existing invalid file is an error."""
    path = state_path.with_name(ARCHIVE_PATH_ALIASES_FILENAME)
    return ArchivePathAliases.from_file(path) if path.exists() else ArchivePathAliases()


def _snapshot_mismatch(row: Mapping[str, Any], snapshot: Mapping[str, Any] | None) -> str | None:
    if snapshot is None:
        return "snapshot_missing"
    if row["source_fingerprint"] != snapshot["fingerprint"]:
        return "audit_fingerprint_mismatch"
    if row["original_relative_path"] != snapshot["relative_path"]:
        return "audit_source_path_mismatch"
    return None


def _entry_from_evidence(row: Mapping[str, Any], snapshot: Mapping[str, Any]) -> dict[str, Any]:
    remote_path = _required_text(row, "verified_remote_path")
    if remote_path != f"{row['drive']}/{row['verified_relative_path']}":
        raise ArchivePathAliasError("已核实的绝对路径与相对路径不一致")
    if not row["matching_basis"] or not isinstance(row["size"], int) or row["size"] <= 0:
        raise ArchivePathAliasError("已核实路径缺少匹配依据或有效文件大小")
    return {
        "user_id": row["user_id"],
        "aweme_id": row["aweme_id"],
        "original_relative_path": row["original_relative_path"],
        "verified_remote_path": remote_path,
        "source_revision": {
            "sec_user_id": snapshot["sec_user_id"],
            "downloaded_at": snapshot.get("downloaded_at"),
        },
        "source_fingerprint": _required_text(row, "source_fingerprint"),
        "verified_at": _required_text(row, "verified_at"),
        "verified_size": row["size"],
        "matching_basis": deepcopy(row["matching_basis"]),
    }


def build_archive_path_aliases(
    evidence: Mapping[str, Any], manifest: Mapping[str, Any], users: Iterable[dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build reviewed mappings only; report every skipped or stale source."""
    snapshots = {(row["user_id"], row["aweme_id"]): row for row in manifest["items"]}
    current_users = {str(user["id"]): user for user in users}
    entries: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    not_recovered = 0
    for row in evidence["items"]:
        if row["status"] != "recovered":
            not_recovered += 1
            continue
        key = row["user_id"], row["aweme_id"]
        snapshot = snapshots.get(key)
        reason = _snapshot_mismatch(row, snapshot)
        if reason is None:
            entry = _entry_from_evidence(row, snapshot)
            reason = ArchivePathAlias.from_entry(entry).stale_reason(current_users.get(row["user_id"]))
        if reason:
            stale.append({"user_id": key[0], "aweme_id": key[1], "reason": reason})
            continue
        entries.append(entry)
    document = {"schema_version": ARCHIVE_PATH_ALIASES_VERSION, "entries": entries}
    ArchivePathAliases.from_document(document)
    report = {
        "total": len(evidence["items"]),
        "imported": len(entries),
        "not_recovered": not_recovered,
        "stale": len(stale),
        "stale_entries": stale,
    }
    return document, report
