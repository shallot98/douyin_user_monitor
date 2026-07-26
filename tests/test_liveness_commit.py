import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from douyin_user_monitor.monitor.cookie_liveness import (
    STATUS_HEALTHY,
    CookieLivenessConfig,
)
from douyin_user_monitor.monitor.cookie_liveness_service import CookieLivenessService
from douyin_user_monitor.monitor.history_sync import (
    HISTORY_SYNC_STATUS_PENDING,
    build_history_sync_state,
)
from douyin_user_monitor.monitor.notifier import NoopMonitorNotifier
from douyin_user_monitor.monitor.service import MonitorService
from douyin_user_monitor.monitor.storage import MonitorStorage

CHINA_TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=CHINA_TZ)
NOW_TS = int(NOW.timestamp())


class IdleCrawler:
    """MonitorService 构造需要的爬虫；本套用例不经过它，仅占位。"""

    async def get_sec_user_id(self, url: str) -> str:
        await asyncio.sleep(0)
        return url

    async def handler_user_profile(self, sec_user_id: str):
        await asyncio.sleep(0)
        return {"user": {"nickname": f"昵称-{sec_user_id}", "avatar_thumb": {"url_list": []}}}

    async def fetch_user_post_videos(self, sec_user_id: str, max_cursor: int, count: int):
        _ = sec_user_id, max_cursor, count
        return {"aweme_list": [], "has_more": 0, "max_cursor": 0}


class ProbeCrawler:
    """cookie liveness 探针爬虫：首个 fetch 的 await 挂起点注入并发写回调。"""

    def __init__(self):
        self.on_first_fetch: Optional[Callable[[], None]] = None
        self._fired = False

    async def fetch_user_post_videos(self, sec_user_id: str, max_cursor: int, count: int):
        _ = sec_user_id, max_cursor, count
        await asyncio.sleep(0)
        if not self._fired and self.on_first_fetch is not None:
            self._fired = True
            self.on_first_fetch()
        # 可达且非空 → 探测判定 healthy，避免触发告警分支
        return {"aweme_list": [{"create_time": NOW_TS - 3600}]}


def _build_service(temp_path: Path, liveness: CookieLivenessService) -> MonitorService:
    return MonitorService(
        crawler=IdleCrawler(),
        storage=MonitorStorage(temp_path / "monitor_users.json"),
        download_root=temp_path / "download",
        notifier=NoopMonitorNotifier(),
        cookie_liveness_service=liveness,
    )


def _build_liveness(crawler: ProbeCrawler) -> CookieLivenessService:
    return CookieLivenessService(
        crawler=crawler,
        config=CookieLivenessConfig(
            enabled=True,
            interval_hours=6,
            stale_days=7,
            sample_user_count=5,
            min_samples=3,
            alert_cooldown_hours=12,
        ),
        now_provider=lambda: NOW,
    )


def _seed_user(service: MonitorService, user_id: str) -> None:
    def _append(state: Dict[str, Any]) -> None:
        state["users"].append(
            {
                "id": user_id,
                "sec_user_id": f"sec-{user_id}",
                "nickname": user_id,
                "enabled": True,
                "avatar_url": "https://example.com/avatar.png",
                "account_status": "normal",
                "downloaded_count": 0,
                "downloaded_aweme_ids": [],
                "download_records": [],
                "history_sync": build_history_sync_state(status=HISTORY_SYNC_STATUS_PENDING),
            }
        )

    service._storage.update_state(_append)


def _seed_cookie_pool_item(service: MonitorService, item_id: str) -> None:
    def _apply(state: Dict[str, Any]) -> None:
        pool = state["monitoring"].setdefault("cookie_pool", {"items": []})
        pool.setdefault("items", []).append({"id": item_id, "name": item_id, "cookie": f"ck-{item_id}"})

    service._storage.update_state(_apply)


class LivenessCommitAtomicityTests(unittest.IsolatedAsyncioTestCase):
    async def test_liveness_merge_preserves_concurrent_user_and_cookie_writes(self):
        """探测 await 期间并发删除的 user 与新增的 cookie_pool 项，不被陈旧快照覆盖。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            crawler = ProbeCrawler()
            service = _build_service(Path(temp_dir), _build_liveness(crawler))
            _seed_user(service, "user-1")
            _seed_user(service, "user-2")
            _seed_user(service, "user-3")
            _seed_cookie_pool_item(service, "cookie-A")

            def _concurrent_write() -> None:
                # 探测锁外快照拍摄之后，另一路径删除用户并新增 cookie 池项
                service.remove_user("user-2")
                _seed_cookie_pool_item(service, "cookie-B")

            crawler.on_first_fetch = _concurrent_write

            # 传入锁外快照（此刻仍含 user-2、仅含 cookie-A），复现探测期间的竞态窗口
            await service._maybe_run_cookie_liveness(service._storage.load_state())

            disk = service._storage.load_state()
            disk_ids = [u["id"] for u in disk["users"]]
            self.assertNotIn("user-2", disk_ids)
            self.assertIn("user-1", disk_ids)
            self.assertIn("user-3", disk_ids)

            pool_ids = [row["id"] for row in disk["monitoring"]["cookie_pool"]["items"]]
            self.assertIn("cookie-A", pool_ids)
            self.assertIn("cookie-B", pool_ids)

            liveness = disk["monitoring"]["cookie_liveness"]
            self.assertEqual(liveness["status"], STATUS_HEALTHY)
            self.assertTrue(liveness.get("last_check_at"))


if __name__ == "__main__":
    unittest.main()
