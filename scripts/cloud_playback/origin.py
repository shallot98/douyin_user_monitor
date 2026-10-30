"""Serve explicit compute-node derivatives to D2 through private bearer URLs."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import socket
import sys
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_PORT = 18765
COPY_BUFFER_BYTES = 65536
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
ASSET_PATTERN = re.compile(r"[0-9]+\.(?:h264\.mp4|cover\.jpg)")


@dataclass(frozen=True)
class MediaOrigin:
    base_url: str
    entries: Path
    media_root: Path

    def publish(self, source_path, basename):
        path = Path(source_path).resolve()
        if not path.is_relative_to(self.media_root.resolve()) or not ASSET_PATTERN.fullmatch(basename):
            raise ValueError("Only named repair derivatives may be exposed by the media origin")
        size = path.stat().st_size
        if size <= 0 or urlsplit(self.base_url).scheme != "https":
            raise ValueError("Origin needs HTTPS and a nonempty completed derivative")
        self.entries.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(32)
        retained = self.entries / "objects" / token / basename
        retained.parent.mkdir(parents=True)
        before = path.stat()
        with path.open("rb") as source, retained.open("xb") as target:
            shutil.copyfileobj(source, target, COPY_BUFFER_BYTES)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("Derivative changed while making its immutable origin copy")
        retained.chmod(0o400)
        record = {"path": str(retained.resolve()), "size": size, "name": basename}
        descriptor = os.open(self.entries / (token + ".json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(record, handle)
        return self.base_url.rstrip("/") + "/" + token + "/" + basename


def byte_range(header, size):
    if header is None:
        return 0, size - 1
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header)
    if not match or not any(match.groups()):
        raise ValueError("Only a single byte range is supported")
    first, last = match.groups()
    if not first:
        count = int(last)
        if count <= 0:
            raise ValueError("Suffix range must contain bytes")
        return max(0, size - count), size - 1
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start >= size or end < start:
        raise ValueError("Range is outside the derivative")
    return start, end


class OriginServer(ThreadingHTTPServer):
    def __init__(self, address, *, entries):
        self.entries = Path(entries).resolve()
        super().__init__(address, OriginHandler)


class OriginHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format, *args):
        status = args[1] if len(args) > 1 else "error"
        sys.stderr.write(json.dumps({"method": self.command, "status": status}) + "\n")

    def _asset(self):
        parts = urlsplit(self.path).path.strip("/").split("/")
        if len(parts) != 2 or not TOKEN_PATTERN.fullmatch(parts[0]) or not ASSET_PATTERN.fullmatch(parts[1]):
            raise FileNotFoundError("Unknown derivative")
        entry = json.loads((self.server.entries / (parts[0] + ".json")).read_text())
        path = Path(entry["path"]).resolve()
        if parts[1] != entry["name"] or not path.is_relative_to(self.server.entries / "objects"):
            raise FileNotFoundError("Unknown derivative")
        if path.stat().st_size != entry["size"]:
            raise ValueError("Published derivative changed")
        return path, entry

    def _serve(self, *, body):
        try:
            path, entry = self._asset()
        except FileNotFoundError:
            self.send_error(404)
            return
        size = entry["size"]
        header = self.headers.get("Range")
        try:
            start, end = byte_range(header, size)
        except ValueError:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(206 if header else 200)
        self.send_header("Content-Type", "video/mp4" if entry["name"].endswith(".mp4") else "image/jpeg")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "private, no-store, no-transform")
        if header:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if body:
            self._copy(path, start, end)

    def _copy(self, path, start, end):
        remaining = end - start + 1
        with path.open("rb") as source:
            source.seek(start)
            while remaining:
                block = source.read(min(COPY_BUFFER_BYTES, remaining))
                if not block:
                    raise OSError("Published derivative ended before its declared length")
                self.wfile.write(block)
                remaining -= len(block)

    def do_GET(self):
        self._serve(body=True)

    def do_HEAD(self):
        self._serve(body=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entries", required=True, type=Path)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    if socket.gethostname().split(".", 1)[0] == "hdz":
        raise RuntimeError("Historical media origin must run on the independent compute node")
    args.entries.mkdir(parents=True, exist_ok=True)
    with OriginServer(("127.0.0.1", args.port), entries=args.entries) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
