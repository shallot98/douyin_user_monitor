"""Loopback archive fixture: real HTTP media reads, never a production provider."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import quote, unquote, urlsplit


class ArchiveMediaServer:
    def __init__(self, files):
        self.files = dict(files)
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                path = unquote(urlsplit(self.path).path).lstrip("/")
                range_header = self.headers.get("Range")
                owner.requests.append((path, range_header))
                if path not in owner.files:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                payload = owner.files[path]
                start, end, status = 0, len(payload) - 1, 200
                if range_header:
                    first, last = range_header.removeprefix("bytes=").split("-", 1)
                    start = int(first)
                    end = min(int(last), end) if last else end
                    if start >= len(payload) or start > end:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{len(payload)}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    status = 206
                self.send_response(status)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(end - start + 1))
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
                self.end_headers()
                self.wfile.write(payload[start:end + 1])

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path):
        return f"http://127.0.0.1:{self.server.server_port}/{quote(path, safe='/')}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
