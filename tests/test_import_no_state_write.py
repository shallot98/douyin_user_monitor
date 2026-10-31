"""导入 app / 构造 MonitorService 不写状态文件；写盘只在启动（auto_resume）时发生。

以前 MonitorService.__init__ 会 save_state，任何脚本 import main 都会改写线上 state。
"""

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from douyin_user_monitor.monitor.notifier import NoopMonitorNotifier
from douyin_user_monitor.monitor.service import MonitorService
from douyin_user_monitor.monitor.storage import MonitorStorage

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeCrawler:
    pass


class ImportNoStateWriteTests(unittest.IsolatedAsyncioTestCase):
    def test_constructor_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = MonitorStorage(Path(tmp) / "monitor_users.json")
            state = storage.load_state()
            state["monitoring"]["is_running"] = True
            state["monitoring"]["desired_running"] = True
            storage.save_state(state)
            before = (Path(tmp) / "monitor_users.json").stat().st_mtime_ns

            service = MonitorService(crawler=FakeCrawler(), storage=storage, download_root=Path(tmp) / "d",
                                     notifier=NoopMonitorNotifier())

            self.assertTrue(service._was_running)  # noqa: SLF001
            self.assertEqual((Path(tmp) / "monitor_users.json").stat().st_mtime_ns, before)

    async def test_auto_resume_resets_running_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = MonitorStorage(Path(tmp) / "monitor_users.json")
            state = storage.load_state()
            state["monitoring"]["is_running"] = True
            state["monitoring"]["desired_running"] = False
            storage.save_state(state)
            service = MonitorService(crawler=FakeCrawler(), storage=storage, download_root=Path(tmp) / "d",
                                     notifier=NoopMonitorNotifier())
            await service.auto_resume()
            monitoring = storage.load_state()["monitoring"]
            self.assertFalse(monitoring["is_running"])
            self.assertFalse(monitoring["desired_running"])

    def test_importing_main_leaves_state_file_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "monitor_users.json"
            seed = textwrap.dedent(f"""
                from pathlib import Path
                from douyin_user_monitor.monitor.storage import MonitorStorage
                s = MonitorStorage(Path({str(state_path)!r}))
                st = s.load_state(); st["monitoring"]["is_running"] = True; s.save_state(st)
            """)
            env = {**os.environ, "DYMON_STATE_PATH": str(state_path)}
            subprocess.run([sys.executable, "-c", seed], cwd=REPO_ROOT, env=env, check=True)
            before = state_path.stat().st_mtime_ns
            subprocess.run([sys.executable, "-c", "import douyin_user_monitor.main"], cwd=REPO_ROOT, env=env,
                           check=True, capture_output=True)
            self.assertEqual(state_path.stat().st_mtime_ns, before)


if __name__ == "__main__":
    unittest.main()
