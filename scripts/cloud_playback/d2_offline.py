"""Explicit offline import on the compute host, with complete byte verification.

origin.publish(path, basename) must retain its own immutable copy until the
coordinator cleans it up. This adapter never removes origin assets, even on error.
Task status numbers are recorded, not interpreted as completion.
"""

from __future__ import annotations

import hashlib
import http.client
import math
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from .d2_cid import decimal_integer
from .d2_client import REQUEST_TIMEOUT_SECONDS, D2Error, _NoRedirect
from .d2_upload import _stat_identity, _validate_destination
from .media_remote import _lookup, assert_source, direct_url, source_record
from .media_tools import MediaJobError

ADD_ENDPOINT = "/v6/offline_task/add"
LIST_ENDPOINT = "/v6/offline_task/list"
LIST_PAGE_SIZE = 100
HASH_CHUNK_SIZE = 1_048_576
DEFAULT_TIMEOUT_SECONDS = 600
DEFAULT_POLL_SECONDS = 5
TASK_FIELDS = ("identity", "task_identity", "type", "status", "code", "message", "progress",
               "bytes_total", "bytes_processed", "size", "name", "save_path")
TASK_NUMBERS = {"type", "status", "code", "progress", "bytes_total", "bytes_processed", "size"}


class OfflineImportError(D2Error):
    def __init__(self, message, *, task_identity=None, history=()):
        super().__init__(message)
        self.task_identity = task_identity
        self.history = tuple(history)


@dataclass(frozen=True)
class SourceSnapshot:
    path: Path
    fingerprint: tuple
    sha256: str

    @property
    def size(self):
        return self.fingerprint[2]


@dataclass(frozen=True)
class PendingImport:
    source: SourceSnapshot
    destination: str
    task: dict


def _safe_text(value):
    return re.sub(r"https?://[^\s]+", "[private URL]", value, flags=re.IGNORECASE)


def _task_number(value, name):
    if type(value) not in (int, str) or not re.fullmatch(r"-?[0-9]+", str(value)):
        raise D2Error(f"Offline task has invalid {name} metadata")
    return int(value)


def _task_metadata(data):
    if not isinstance(data, dict):
        raise D2Error("Offline add/list response must be an object")
    result = {}
    for name in TASK_FIELDS:
        if name not in data:
            continue
        value = data[name]
        if name in TASK_NUMBERS:
            _task_number(value, name)
        elif not isinstance(value, str):
            raise D2Error(f"Offline task has invalid {name} metadata")
        result[name] = _safe_text(value) if isinstance(value, str) else value
    return result


def _check_task_error(task):
    code = _task_number(task.get("code", 0), "code")
    if code:
        message = task.get("message") or "provider message unavailable"
        raise D2Error(f"Offline provider reported code {code}: {message}")


def _added_task(response):
    task = _task_metadata(response)
    try:
        _check_task_error(task)
        if not task.get("identity"):
            raise D2Error("Offline add response lacks a task identity")
    except D2Error as error:
        raise OfflineImportError(str(error), task_identity=task.get("identity"), history=(task,)) from None
    return task


def _hash_stream(stream, expected_size, *, deadline=None, monotonic=time.monotonic):
    digest = hashlib.sha256()
    count = 0
    while True:
        if deadline is not None and monotonic() >= deadline:
            raise D2Error("Offline content verification timed out")
        chunk = stream.read(HASH_CHUNK_SIZE)
        if not chunk:
            break
        count += len(chunk)
        if count > expected_size:
            raise D2Error("Content exceeds the expected byte count")
        digest.update(chunk)
    if count != expected_size:
        raise D2Error(f"Content byte count mismatch: actual={count} expected={expected_size}")
    return digest.hexdigest()


def _assert_source_unchanged(source):
    if _stat_identity(source.path.stat()) != source.fingerprint:
        raise D2Error("Local derivative changed during offline import")


def _snapshot(source_path):
    path = Path(source_path)
    before = _stat_identity(path.stat())
    if before[2] <= 0:
        raise D2Error("Offline import source is empty")
    with path.open("rb") as stream:
        digest = _hash_stream(stream, before[2])
    result = SourceSnapshot(path=path, fingerprint=before, sha256=digest)
    _assert_source_unchanged(result)
    return result


def _open_stream(address):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    request = urllib.request.Request(address, headers={"Accept-Encoding": "identity"}, method="GET")
    response = opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS)
    if response.status != 200:
        response.close()
        raise D2Error(f"D2 verification GET returned HTTP {response.status}")
    return response


def _remote_digest(open_stream, address, expected_size, *, deadline, monotonic):
    try:
        with open_stream(address) as stream:
            return _hash_stream(stream, expected_size, deadline=deadline, monotonic=monotonic)
    except urllib.error.HTTPError as error:
        raise D2Error(f"D2 verification read failed: HTTP {error.code}") from None
    except (OSError, ValueError, http.client.HTTPException) as error:
        raise D2Error(f"D2 verification read failed ({type(error).__name__}); private request withheld") from None


def _origin_address(origin, source, basename):
    try:
        address = origin.publish(source.path, basename)
        parsed = urlsplit(address)
    except (OSError, ValueError, TypeError) as error:
        raise D2Error(f"Origin publication failed ({type(error).__name__}); private details withheld") from None
    if (not isinstance(address, str) or parsed.scheme != "https" or not parsed.hostname or parsed.username
            or unquote(PurePosixPath(parsed.path).name) != basename):
        raise D2Error("Origin must return an HTTPS address with the destination basename")
    _assert_source_unchanged(source)
    return address


@dataclass(frozen=True, kw_only=True)
class OfflineUploader:
    origin: object = field(repr=False)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    poll_interval_seconds: float = DEFAULT_POLL_SECONDS
    open_stream: object = field(default=_open_stream, repr=False)
    monotonic: object = field(default=time.monotonic, repr=False)
    sleep: object = field(default=time.sleep, repr=False)

    def __post_init__(self):
        if any(not math.isfinite(value) or value <= 0 for value in (self.timeout_seconds, self.poll_interval_seconds)):
            raise ValueError("Offline polling timeout and interval must be finite positive seconds")

    def __call__(self, client, source_path, destination):
        _validate_destination(destination)
        if _lookup(client, destination) is not None:
            raise OfflineImportError("Offline destination already exists; no task submitted")
        source = _snapshot(source_path)
        address = _origin_address(self.origin, source, PurePosixPath(destination).name)
        if _lookup(client, destination) is not None:
            raise OfflineImportError("Offline destination appeared before add; no task submitted")
        response = client.post(ADD_ENDPOINT, {"url": address, "save_path": str(PurePosixPath(destination).parent)})
        task = _added_task(response)
        return self._wait(client, PendingImport(source=source, destination=destination, task=task))

    def _find_task(self, client, identity, *, deadline):
        token = ""
        seen = set()
        while True:
            if self.monotonic() >= deadline:
                raise D2Error("Offline import polling timed out")
            data = client.post(LIST_ENDPOINT, {"list_info": {"limit": str(LIST_PAGE_SIZE), "token": token}})
            if not isinstance(data, dict) or not isinstance(data.get("tasks", []), list):
                raise D2Error("Offline list returned an invalid task list")
            for task in data.get("tasks", []):
                if not isinstance(task, dict):
                    raise D2Error("Offline list returned an invalid task entry")
                if task.get("identity") == identity:
                    return _task_metadata(task)
            info = data.get("list_info") or {}
            if not isinstance(info, dict):
                raise D2Error("Offline list returned invalid pagination metadata")
            token = info.get("token", "")
            if not token:
                return None
            if not isinstance(token, str) or token in seen:
                raise D2Error("Offline list pagination did not advance")
            seen.add(token)

    def _verified_receipt(self, client, pending, metadata, *, deadline):
        record = source_record(metadata)
        address = direct_url(client, record)
        digest = _remote_digest(self.open_stream, address, pending.source.size, deadline=deadline, monotonic=self.monotonic)
        if digest != pending.source.sha256:
            raise D2Error("D2 offline content SHA-256 differs from the verified local derivative")
        _assert_source_unchanged(pending.source)
        after = source_record(_lookup(client, pending.destination))
        assert_source(after, record)
        return {"status": "uploaded", "transport": "offline", **record,
                "sha256": digest, "content_verified": True}

    def _wait(self, client, pending):
        history = (pending.task,)
        identity = pending.task["identity"]
        deadline = self.monotonic() + self.timeout_seconds
        try:
            _check_task_error(pending.task)
            while self.monotonic() < deadline:
                task = self._find_task(client, identity, deadline=deadline)
                if task is not None:
                    history += (task,)
                    _check_task_error(task)
                _assert_source_unchanged(pending.source)
                metadata = _lookup(client, pending.destination)
                if metadata is not None and decimal_integer(metadata.get("size"), "size") == pending.source.size:
                    receipt = self._verified_receipt(client, pending, metadata, deadline=deadline)
                    return {**receipt, "offline_task": history[-1], "offline_history": history}
                remaining = deadline - self.monotonic()
                if remaining > 0:
                    self.sleep(min(self.poll_interval_seconds, remaining))
            raise D2Error("Offline import timed out before matching D2 content was verified")
        except (D2Error, MediaJobError, OSError) as error:
            raise OfflineImportError(_safe_text(str(error)), task_identity=identity, history=history) from None
