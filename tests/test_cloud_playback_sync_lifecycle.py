"""The configured receipt publisher must own one HTTP event loop per batch."""

import asyncio
import json
import threading
import unittest
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from douyin_user_monitor.monitor.openlist_client import ArchiveUnavailableError, OpenListClient
from douyin_user_monitor.settings import ArchiveSettings
from scripts.cloud_playback import sync_results


class SyncLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.paths = []
        self.status = 200
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.paths.append(payload["path"])
                body = b'{"code":200,"data":{"content":[]}}'
                self.send_response(owner.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.archive = ArchiveSettings(
            enabled=True, openlist_base_url=f"http://127.0.0.1:{self.server.server_port}",
            openlist_db_path=self.root / "unused.db", openlist_token="test-only", primary_base="/primary",
            backup_base="/backup", timeout_seconds=2, sanitize_primary_dirs=False,
        )
        self.loops = []
        self.closed = []

        class RecordingClient(OpenListClient):
            async def refresh_directory(self, path):
                owner.loops.append(asyncio.get_running_loop())
                return await super().refresh_directory(path)

            async def close(self):
                owner.closed.append(asyncio.get_running_loop())
                await super().close()

        self.client = RecordingClient(self.archive)
        credentials = self.root / "credentials.json"
        credentials.write_text("{}")
        self.config = {"credentials": str(credentials), "state_file": str(self.root / "state.json"),
                       "website_url": "http://unused.test", "remote": {
                           "node": "test", "code_root": "/unused", "workspace": "/unused"}}
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.object(sync_results.Credentials, "from_mapping", return_value=object()))
        self.patches.enter_context(patch.object(sync_results, "D2Client"))
        self.patches.enter_context(patch.object(sync_results, "publish_verified", return_value={"users": []}))
        self.patches.enter_context(patch("scripts.cloud_playback.website_check.verify_published", return_value={"ok": True}))
        self.patches.enter_context(patch("douyin_user_monitor.settings_loaders.load_settings",
                                        return_value=SimpleNamespace(archive=self.archive)))
        self.patches.enter_context(patch("douyin_user_monitor.monitor.openlist_client.OpenListClient",
                                        return_value=self.client))

    def test_two_different_users_reuse_one_loop_and_close_client_in_that_loop(self):
        with ExitStack() as stack:
            _, _, publish = sync_results._configured(self.config, stack)
            publish([{"cover_file": "covers/user-a/1.jpg"}])
            publish([{"cover_file": "covers/user-b/2.jpg"}])
            self.assertIn("/primary/covers/user-b", self.paths)
            self.assertTrue(all(loop is self.loops[0] for loop in self.loops))
            self.assertFalse(self.loops[0].is_closed())
        self.assertEqual(self.closed, [self.loops[0]])
        self.assertTrue(self.loops[0].is_closed())
        self.assertIsNone(self.client._http_client)

    def test_failed_refresh_closes_resources_without_publishing_or_acknowledging(self):
        self.status = 503
        publication = {"cover_file": "covers/user/1.jpg", "verified_at": "test-time"}
        page = {"offset": 0, "rows": [{"offset": 12, "receipt": {"status": "succeeded"}}]}
        with patch.object(sync_results, "publish_verified") as publish_state:
            with self.assertRaises(ArchiveUnavailableError):
                with ExitStack() as stack:
                    _, _, publish = sync_results._configured(self.config, stack)
                    sync_results.sync_once(self.root / "batch", read_remote=lambda offset: page,
                                           verify=lambda receipt: publication, publish=publish)
            publish_state.assert_not_called()
        self.assertFalse((self.root / "batch/checkpoint.json").exists())
        self.assertEqual(self.closed, [self.loops[0]])
        self.assertTrue(self.loops[0].is_closed())
        self.assertIsNone(self.client._http_client)
