"""Upload a completed worker-local file directly to D2 in bounded-size blocks."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlsplit

from .d2_cid import CID_VERSION, DEFAULT_CODEC, DEFAULT_HASH_TYPE, HASH_NAMES, block_cid, decimal_integer
from .d2_client import (
    INSECURE_BLOCK_HOSTS,
    REQUEST_TIMEOUT_SECONDS,
    Credentials,
    D2Client,
    D2Error,
    D2HTTPError,
    HttpTransport,
    check_status,
    json_body,
    response_json,
)
from .quota import upload_capacity

FINALIZE_TIMEOUT_SECONDS = 120
CREATE_UPLOAD_ENDPOINT = "/v6/userfile/create_upload_task"
GET_FILE_ENDPOINT = "/v6/userfile/get"


@dataclass(frozen=True)
class UploadTask:
    base_url: str = field(repr=False)
    block_size: int
    codec: int
    hash_type: int

    @classmethod
    def from_response(cls, data):
        block_size = decimal_integer(data.get("block_size"), "block_size")
        codec = decimal_integer(data.get("block_codec", 0), "block_codec") or DEFAULT_CODEC
        hash_type = decimal_integer(data.get("block_hash_type", 0), "block_hash_type") or DEFAULT_HASH_TYPE
        version = decimal_integer(data.get("block_version", 0), "block_version")
        if not block_size or version not in (0, CID_VERSION):
            raise D2Error("D2 upload task has an unsupported block_size or block_version")
        if hash_type not in HASH_NAMES:
            raise D2Error(f"D2 block hash type {hash_type} is unsupported")
        return cls(base_url=_task_address(data), block_size=block_size, codec=codec, hash_type=hash_type)


def _task_address(data):
    address = data.get("upload_address")
    task = data.get("task")
    if not isinstance(address, str) or not isinstance(task, str) or not task:
        raise D2Error("D2 upload task lacks upload_address or task")
    parsed = urlsplit(address)
    if parsed.scheme not in ("https", "http") or not parsed.netloc or parsed.query or parsed.fragment:
        raise D2Error("D2 upload task has an invalid upload_address")
    # D2 task lookup requires literal colon separators, as in the official client.
    return address.rstrip("/") + "/" + quote(task, safe=":")


def _verify_contents(data, destination, size):
    if not isinstance(data, dict):
        raise D2Error("D2 file response must be an object")
    if data.get("path") != destination:
        raise D2Error("D2 completed file path differs from the requested path")
    if decimal_integer(data.get("size"), "size") != size:
        raise D2Error("D2 completed file size differs from the source size")
    if not isinstance(data.get("content_identity"), str) or not data["content_identity"]:
        raise D2Error("D2 completed file has no content identity")


def _verify_file(data, destination, size):
    _verify_contents(data, destination, size)
    if not isinstance(data.get("identity"), str) or not data["identity"]:
        raise D2Error("D2 completed file has no identity")
    return {"identity": data["identity"], "path": destination, "size": size,
            "content_identity": data["content_identity"]}


def _send_block(transport, task, block):
    cid = block_cid(block, codec=task.codec, hash_type=task.hash_type)
    url = task.base_url + "/" + cid
    reply = transport.request("GET", url, headers={"Accept": "application/json"}, timeout=REQUEST_TIMEOUT_SECONDS)
    check_status(reply, "block presence", allowed=(200,))
    exists = response_json(reply, "block presence")
    if type(exists) is not bool:
        raise D2Error("D2 block presence response must be a JSON boolean")
    if exists:
        return cid, False
    reply = transport.request(
        "POST", url, body=block, timeout=REQUEST_TIMEOUT_SECONDS,
        headers={"Accept": "application/json", "Content-Type": "application/octet-stream"},
    )
    check_status(reply, "block upload")
    return cid, True


def _stat_identity(stat):
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _upload_blocks(transport, task, source):
    cids = []
    uploaded = 0
    while block := source.read(task.block_size):
        cid, sent = _send_block(transport, task, block)
        cids.append(cid)
        uploaded += int(sent)
    return cids, uploaded


def _finalize(transport, task, cids):
    reply = transport.request(
        "POST", task.base_url, body=json_body(cids), timeout=FINALIZE_TIMEOUT_SECONDS,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    check_status(reply, "finalize")
    return response_json(reply, "finalize")


def _validate_destination(destination):
    if not isinstance(destination, str) or not destination.startswith("/") or destination.endswith("/"):
        raise D2Error("Destination must be an absolute D2 file path")
    if "\0" in destination or any(part in (".", "..") for part in destination.split("/")):
        raise D2Error("Destination contains an invalid path component")


def upload_file(client, source_path, destination):
    """Return verified upload metadata; playback validation remains separate."""
    _validate_destination(destination)
    with Path(source_path).open("rb") as source:
        before = os.fstat(source.fileno())
        if not before.st_size:
            raise D2Error("Source file is empty")
        data = client.post(CREATE_UPLOAD_ENDPOINT, {"path": destination, "size": str(before.st_size)})
        if not isinstance(data, dict) or type(data.get("created", False)) is not bool:
            raise D2Error("D2 upload task response has an invalid shape")
        if data.get("created"):
            existing = client.post(GET_FILE_ENDPOINT, {"path": destination})
            _verify_file(existing, destination, before.st_size)
            raise D2Error("D2 reported an already-created file; matching metadata does not verify source contents")
        task = UploadTask.from_response(data)
        cids, uploaded = _upload_blocks(client.transport, task, source)
        if _stat_identity(os.fstat(source.fileno())) != _stat_identity(before):
            raise D2Error("Source file changed during upload; finalization was not attempted")
        completed = _finalize(client.transport, task, cids)
        _verify_contents(completed, destination, before.st_size)
        metadata = _verify_file(client.post(GET_FILE_ENDPOINT, {"path": destination}), destination, before.st_size)
        if metadata["content_identity"] != completed["content_identity"]:
            raise D2Error("D2 completed file content identity differs from finalization")
    return {"status": "uploaded", **metadata, "blocks_total": len(cids), "blocks_uploaded": uploaded,
            "blocks_reused": len(cids) - uploaded}


def _load_credentials(path):
    try:
        with Path(path).open(encoding="utf-8") as source:
            return Credentials.from_mapping(json.load(source))
    except (ValueError, UnicodeError):
        raise D2Error("Credentials file is not valid JSON") from None


def _load_credentials_from_openlist_db(db_path, mount_path="/d2"):
    try:
        uri = f"file:{Path(db_path).resolve()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            row = conn.execute(
                "SELECT addition FROM x_storages WHERE mount_path = ? LIMIT 1",
                (mount_path,),
            ).fetchone()
        if not row or not row[0]:
            raise D2Error(f"OpenList storage mount {mount_path} not found")
        addition = json.loads(row[0])
        if not isinstance(addition, dict):
            raise D2Error("OpenList storage addition is not a JSON object")
        return Credentials.from_mapping({
            "host": addition.get("host", ""),
            "client_id": addition.get("client_id", ""),
            "client_secret": addition.get("client_secret", ""),
        })
    except (sqlite3.Error, ValueError, UnicodeError):
        raise D2Error("Failed to load D2 credentials from OpenList database") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    cred_group = parser.add_mutually_exclusive_group(required=True)
    cred_group.add_argument("--credentials", type=Path, help="Private D2 credentials JSON")
    cred_group.add_argument("--openlist-db", type=Path, help="OpenList SQLite database containing /d2 storage")
    parser.add_argument("--mount-path", default="/d2", help="OpenList mount path for D2 storage (default: /d2)")
    parser.add_argument("--file", required=True, type=Path, help="Completed media file on the compute host")
    parser.add_argument("--path", required=True, help="Exact destination path native to D2 (without /d2 mount prefix)")
    parser.add_argument("--insecure-block-tls", action="store_true",
                        help="Skip TLS verification only for the expired D2 block upload proxy host")
    parser.add_argument("--check-quota", action="store_true",
                        help="Verify remaining D2 daily upload quota before uploading")
    parser.add_argument("--exit-2-on-existing", action="store_true",
                        help="Exit with code 2 when D2 reports the destination path already exists")
    args = parser.parse_args(argv)
    try:
        credentials = (
            _load_credentials(args.credentials)
            if args.credentials is not None
            else _load_credentials_from_openlist_db(args.openlist_db, args.mount_path)
        )
        insecure_hosts = INSECURE_BLOCK_HOSTS if args.insecure_block_tls else ()
        client = D2Client(credentials, transport=HttpTransport(insecure_hosts=insecure_hosts))
        if args.check_quota:
            file_size = args.file.stat().st_size
            capacity = upload_capacity(client)
            if capacity["remaining"] < file_size:
                raise D2Error(
                    f"USER_DAILY_UPLOAD_QUOTA_EXCEEDED: remaining={capacity['remaining']} < size={file_size}"
                )
        result = upload_file(client, args.file, args.path)
    except D2Error as exc:
        msg = str(exc)
        if isinstance(exc, D2HTTPError) and exc.message:
            msg = f"{msg} ({exc.message})"
        if args.exit_2_on_existing and "already-created file" in msg:
            print(json.dumps({"status": "exists", "error": msg}, ensure_ascii=False), file=sys.stderr)
            return 2
        print(json.dumps({"status": "error", "error": msg}, ensure_ascii=False), file=sys.stderr)
        return 1
    except OSError as exc:
        print(json.dumps({"status": "error", "error": f"Local file operation failed ({type(exc).__name__})"}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
