import http.client
import threading
from urllib.parse import urlsplit

import pytest

from scripts.cloud_playback.origin import MediaOrigin, OriginServer, byte_range


@pytest.mark.parametrize("header,expected", [
    (None, (0, 9)), ("bytes=2-4", (2, 4)), ("bytes=7-", (7, 9)),
    ("bytes=-3", (7, 9)), ("bytes=8-99", (8, 9)),
])
def test_byte_ranges(header, expected):
    assert byte_range(header, 10) == expected


@pytest.mark.parametrize("header", ["bytes=10-", "bytes=-0", "bytes=4-2", "bytes=1-2,4-5", "bytes=-"])
def test_invalid_ranges_remain_explicit(header):
    with pytest.raises(ValueError):
        byte_range(header, 10)


def test_only_registered_file_is_served_with_head_and_range(tmp_path):
    media_root = tmp_path / "media"
    media_root.mkdir()
    source = media_root / "output.mp4"
    source.write_bytes(b"0123456789")
    entries = tmp_path / "entries"
    origin = MediaOrigin("https://test.example", entries, media_root)
    path = urlsplit(origin.publish(source, "123.h264.mp4")).path
    source.unlink()
    server = OriginServer(("127.0.0.1", 0), entries=entries)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection(*server.server_address)
        connection.request("HEAD", path)
        reply = connection.getresponse()
        assert reply.status == 200 and reply.getheader("Content-Length") == "10" and reply.read() == b""
        connection.request("GET", path, headers={"Range": "bytes=2-4"})
        reply = connection.getresponse()
        assert reply.status == 206 and reply.getheader("Content-Range") == "bytes 2-4/10"
        assert reply.read() == b"234"
        connection.request("GET", "/")
        reply = connection.getresponse()
        assert reply.status == 404
        reply.read()
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_other_host_files_cannot_be_registered(tmp_path):
    source = tmp_path / "private.txt"
    source.write_text("private")
    origin = MediaOrigin("https://test.example", tmp_path / "entries", tmp_path / "media")
    with pytest.raises(ValueError):
        origin.publish(source, "123.h264.mp4")
