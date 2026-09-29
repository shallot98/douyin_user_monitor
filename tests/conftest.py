"""Point the app's module-level storage at a throwaway state file.

`douyin_user_monitor.api.monitor` builds its MonitorStorage at import time from
DYMON_STATE_PATH / config.yaml; without this the suite reads (and creates the
record DB next to) the production `data/monitor_users.json`.
"""

import os
import tempfile
from pathlib import Path

_STATE_DIR = Path(tempfile.mkdtemp(prefix="dymon-test-state-"))
os.environ["DYMON_STATE_PATH"] = str(_STATE_DIR / "monitor_users.json")


import pytest


class _NoMediaCache:
    """默认关掉归档媒体缓存，让代理路径的测试看到真实的逐请求回源。"""

    async def get_or_fetch(self, key, suffix, opener):
        return None


@pytest.fixture(autouse=True)
def _archive_media_cache_disabled(monkeypatch):
    # 延迟导入：必须在上面设置 DYMON_STATE_PATH 之后才能加载 monitor 模块
    from douyin_user_monitor.api import monitor as monitor_api

    monkeypatch.setattr(monitor_api, "ARCHIVE_MEDIA_CACHE", _NoMediaCache())
