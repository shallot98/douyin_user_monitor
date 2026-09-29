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
