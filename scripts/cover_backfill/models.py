"""Manifest contracts and fair, bounded page ordering."""

from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from dataclasses import dataclass
from itertools import islice
from pathlib import PurePosixPath

from scripts.cloud_playback.media_remote import ARCHIVE_ROOT

SCHEMA_VERSION = 1
DEFAULT_WORKERS = 2
DEFAULT_PAGE_SIZE = 20
DEFAULT_MEDIA_TIMEOUT = 120
DEFAULT_BATCH_COVERS = 500
IDENTITY_FIELDS = ("user_id", "aweme_id", "sec_user_id", "downloaded_at", "source_relative_path")


class BackfillError(RuntimeError):
    """A local contract failure whose diagnostic contains no private input."""


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def cover_relative_path(user_id, aweme_id):
    if not isinstance(user_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", user_id):
        raise BackfillError("user_id must be a stable, path-safe identifier")
    if not isinstance(aweme_id, str) or not re.fullmatch(r"[0-9]+", aweme_id):
        raise BackfillError("aweme_id must contain digits only")
    return f"covers/{user_id}/{aweme_id}.jpg"


@dataclass(frozen=True, kw_only=True)
class Job:
    user_id: str
    aweme_id: str
    sec_user_id: str
    downloaded_at: str
    source_relative_path: str
    source_paths: tuple[str, ...]
    nickname: str | None = None

    def payload(self):
        fields = {field: getattr(self, field) for field in IDENTITY_FIELDS}
        optional = {} if self.nickname is None else {"nickname": self.nickname}
        return {**fields, "source_paths": list(self.source_paths), **optional}

    @property
    def input_revision(self):
        return hashlib.sha256(json_bytes(self.payload())).hexdigest()

    @property
    def key(self):
        return self.user_id, self.aweme_id, self.input_revision

    @property
    def cover_path(self):
        return cover_relative_path(self.user_id, self.aweme_id)


def parse_job(value):
    if not isinstance(value, dict):
        raise BackfillError("Each manifest job must be an object")
    allowed = {*IDENTITY_FIELDS, "source_paths", "nickname"}
    if value.keys() - allowed:
        raise BackfillError("Manifest job contains unsupported fields")
    if any(not isinstance(value.get(field), str) for field in IDENTITY_FIELDS):
        raise BackfillError("Manifest identity fields must all be strings")
    cover_relative_path(value["user_id"], value["aweme_id"])
    if not value["sec_user_id"]:
        raise BackfillError("sec_user_id must be nonempty")
    paths = _source_paths(value.get("source_paths"))
    nickname = value.get("nickname")
    if nickname is not None and not isinstance(nickname, str):
        raise BackfillError("nickname must be a string or null")
    return Job(**{field: value[field] for field in IDENTITY_FIELDS},
               source_paths=paths, nickname=nickname)


def _source_paths(paths):
    if not isinstance(paths, list) or not paths:
        raise BackfillError("source_paths must be a nonempty ordered list")
    for path in paths:
        if (not isinstance(path, str) or not path.startswith(ARCHIVE_ROOT + "/")
                or str(PurePosixPath(path)) != path or ".." in path.split("/")):
            raise BackfillError("Source candidates must be canonical native D2 archive paths")
    if len(paths) != len(set(paths)):
        raise BackfillError("Source candidate paths must be unique")
    return tuple(paths)


def load_manifest(path):
    document = json.loads(path.read_bytes())
    if not isinstance(document, dict) or not isinstance(document.get("jobs"), list):
        raise BackfillError("Manifest must contain a jobs array")
    jobs = tuple(parse_job(value) for value in document["jobs"])
    keys = {(job.user_id, job.aweme_id) for job in jobs}
    if len(keys) != len(jobs):
        raise BackfillError("Manifest repeats a user_id and aweme_id")
    return jobs


def fair_jobs(jobs, page_size):
    """Preserve newest-first input within each user, then rotate whole pages."""
    if page_size <= 0:
        raise BackfillError("page_size must be positive")
    grouped = {}
    for job in jobs:
        grouped.setdefault(job.user_id, []).append(job)
    pending = deque(iter(group) for group in grouped.values())
    while pending:
        group = pending.popleft()
        page = tuple(islice(group, page_size))
        if page:
            yield from page
            pending.append(group)
