from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from douyin_user_monitor.monitor.cookie_download_liveness import (
    DOWNLOAD_SOURCE_HISTORY,
    DOWNLOAD_SOURCE_INCREMENTAL,
    collect_incremental_download_samples,
)
from douyin_user_monitor.monitor.cookie_liveness import (
    STATUS_DEGRADED,
    STATUS_EXPIRED,
    STATUS_HEALTHY,
    STATUS_UNKNOWN,
    CookieLivenessConfig,
    evaluate_cookie_liveness,
    evaluate_download_liveness,
    extract_latest_create_time,
    parse_iso_datetime,
    select_probe_users,
    should_send_cookie_alert,
)
from douyin_user_monitor.monitor.cookie_liveness_service import CookieLivenessService
from douyin_user_monitor.monitor.hermes_weixin_sender import HermesWeixinConfig, HermesWeixinSender
from douyin_user_monitor.monitor.profile_parser import ACCOUNT_STATUS_BANNED, ACCOUNT_STATUS_NORMAL

CHINA_TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=CHINA_TZ)
NOW_TS = int(NOW.timestamp())
DAY = 86400


class EvaluateCookieLivenessTests(unittest.TestCase):
    def test_healthy_when_probe_reachable_even_if_posts_old(self):
        samples = [
            {"latest_create_time": NOW_TS - 30 * DAY, "reachable": True, "error": None},
            {"latest_create_time": NOW_TS - 20 * DAY, "reachable": True, "error": None},
            {"latest_create_time": NOW_TS - 15 * DAY, "reachable": True, "error": None},
        ]
        result = evaluate_cookie_liveness(
            samples,
            now_ts=NOW_TS,
            stale_seconds=7 * DAY,
            min_samples=3,
        )
        self.assertEqual(result.status, STATUS_HEALTHY)
        self.assertIn("可达", result.reason)

    def test_expired_when_probe_all_failed(self):
        samples = [
            {"latest_create_time": None, "reachable": False, "error": "login required"},
            {"latest_create_time": None, "reachable": False, "error": "auth"},
            {"latest_create_time": None, "reachable": False, "error": "forbidden"},
        ]
        result = evaluate_cookie_liveness(
            samples,
            now_ts=NOW_TS,
            stale_seconds=7 * DAY,
            min_samples=3,
        )
        self.assertEqual(result.status, STATUS_EXPIRED)


    def test_expired_when_all_probes_empty_list(self):
        samples = [
            {"kind": "probe", "reachable": False, "error": "empty aweme_list"},
            {"kind": "probe", "reachable": False, "error": "empty aweme_list"},
            {"kind": "probe", "reachable": False, "error": "empty aweme_list"},
        ]
        result = evaluate_cookie_liveness(
            samples,
            now_ts=NOW_TS,
            stale_seconds=7 * DAY,
            min_samples=3,
        )
        self.assertEqual(result.status, STATUS_EXPIRED)

    def test_expired_when_reachable_false_without_error(self):
        samples = [
            {"kind": "probe", "reachable": False, "error": None},
            {"kind": "probe", "reachable": False, "error": None},
            {"kind": "probe", "reachable": False, "error": None},
        ]
        result = evaluate_cookie_liveness(
            samples,
            now_ts=NOW_TS,
            stale_seconds=7 * DAY,
            min_samples=3,
        )
        self.assertEqual(result.status, STATUS_EXPIRED)

    def test_unknown_when_reachable_below_min(self):
        samples = [
            {"latest_create_time": NOW_TS - 1 * DAY, "reachable": True, "error": None},
            {"latest_create_time": None, "reachable": False, "error": "timeout"},
            {"latest_create_time": None, "reachable": False, "error": "timeout"},
        ]
        result = evaluate_cookie_liveness(
            samples,
            now_ts=NOW_TS,
            stale_seconds=7 * DAY,
            min_samples=3,
        )
        self.assertEqual(result.status, STATUS_UNKNOWN)

    def test_extract_latest_create_time(self):
        value = extract_latest_create_time(
            [
                {"create_time": 100},
                {"create_time": "200"},
                {"create_time": 150},
            ]
        )
        self.assertEqual(value, 200)


class SelectProbeUsersTests(unittest.TestCase):
    def test_skips_disabled_and_banned(self):
        users = [
            {"id": "b", "enabled": True, "account_status": ACCOUNT_STATUS_NORMAL, "sec_user_id": "sec-b"},
            {"id": "a", "enabled": False, "account_status": ACCOUNT_STATUS_NORMAL, "sec_user_id": "sec-a"},
            {"id": "c", "enabled": True, "account_status": ACCOUNT_STATUS_BANNED, "sec_user_id": "sec-c"},
            {"id": "d", "enabled": True, "account_status": ACCOUNT_STATUS_NORMAL, "sec_user_id": "sec-d"},
        ]
        selected = select_probe_users(users, limit=5)
        self.assertEqual([item["id"] for item in selected], ["b", "d"])

    def test_prefers_users_with_downloads(self):
        users = [
            {"id": "a", "enabled": True, "account_status": ACCOUNT_STATUS_NORMAL, "sec_user_id": "sec-a", "downloaded_count": 1},
            {"id": "b", "enabled": True, "account_status": ACCOUNT_STATUS_NORMAL, "sec_user_id": "sec-b", "downloaded_count": 20},
            {"id": "c", "enabled": True, "account_status": ACCOUNT_STATUS_NORMAL, "sec_user_id": "sec-c", "downloaded_count": 5},
        ]
        selected = select_probe_users(users, limit=2)
        self.assertEqual([item["id"] for item in selected], ["b", "c"])


class AlertCooldownTests(unittest.TestCase):
    def test_alerts_on_transition_to_expired(self):
        self.assertTrue(
            should_send_cookie_alert(
                status=STATUS_EXPIRED,
                previous_status=STATUS_HEALTHY,
                last_alert_at=None,
                now=NOW,
                cooldown_hours=12,
            )
        )

    def test_skips_within_cooldown(self):
        last = (NOW - timedelta(hours=1)).isoformat()
        self.assertFalse(
            should_send_cookie_alert(
                status=STATUS_EXPIRED,
                previous_status=STATUS_EXPIRED,
                last_alert_at=last,
                now=NOW,
                cooldown_hours=12,
            )
        )

    def test_allows_after_cooldown(self):
        last = (NOW - timedelta(hours=13)).isoformat()
        self.assertTrue(
            should_send_cookie_alert(
                status=STATUS_EXPIRED,
                previous_status=STATUS_EXPIRED,
                last_alert_at=last,
                now=NOW,
                cooldown_hours=12,
            )
        )


class FakeCrawler:
    def __init__(self, mapping: Dict[str, Any]):
        self.mapping = mapping

    async def fetch_user_post_videos(self, sec_user_id: str, max_cursor: int, count: int):
        _ = max_cursor, count
        value = self.mapping[sec_user_id]
        if isinstance(value, Exception):
            raise value
        return value


class RecordingAlerter:
    def __init__(self):
        self.messages: List[str] = []

    async def send(self, message: str) -> None:
        self.messages.append(message)


class CookieLivenessServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_maybe_run_marks_expired_and_alerts(self):
        crawler = FakeCrawler(
            {
                "sec-1": RuntimeError("login required"),
                "sec-2": RuntimeError("auth expired"),
                "sec-3": RuntimeError("forbidden"),
            }
        )
        alerter = RecordingAlerter()
        service = CookieLivenessService(
            crawler=crawler,
            config=CookieLivenessConfig(
                enabled=True,
                interval_hours=6,
                stale_days=7,
                sample_user_count=5,
                min_samples=3,
                alert_cooldown_hours=12,
            ),
            alerter=alerter,
            now_provider=lambda: NOW,
        )
        state = {
            "users": [
                {
                    "id": f"u{i}",
                    "nickname": f"n{i}",
                    "enabled": True,
                    "account_status": ACCOUNT_STATUS_NORMAL,
                    "sec_user_id": f"sec-{i}",
                }
                for i in range(1, 4)
            ],
            "monitoring": {},
        }

        payload = await service.maybe_run(state)

        self.assertIsNotNone(payload)
        self.assertEqual(payload["status"], STATUS_EXPIRED)
        self.assertTrue(payload["alerted"])
        self.assertEqual(len(alerter.messages), 1)
        self.assertIn("Cookie 疑似失效", alerter.messages[0])

        # second run within interval should skip
        payload2 = await service.maybe_run(state)
        self.assertIsNone(payload2)
        self.assertEqual(len(alerter.messages), 1)


class RecordingRunner:
    def __init__(self, code: int = 0):
        self.code = code
        self.commands: List[List[str]] = []

    async def run(self, command, *, timeout_seconds: float):
        _ = timeout_seconds
        self.commands.append(list(command))
        return self.code, "ok", ""




class EvaluateDownloadLivenessTests(unittest.TestCase):
    def test_healthy_when_any_incremental_lag_within_stale(self):
        samples = [
            {"lag_seconds": 10 * DAY, "is_fresh": False, "publish_ts": NOW_TS - 11 * DAY},
            {"lag_seconds": 2 * 3600, "is_fresh": True, "publish_ts": NOW_TS - DAY},
            {"lag_seconds": 9 * DAY, "is_fresh": False, "publish_ts": NOW_TS - 10 * DAY},
        ]
        result = evaluate_download_liveness(samples, stale_seconds=7 * DAY, min_samples=3)
        self.assertEqual(result.status, STATUS_HEALTHY)

    def test_expired_when_all_incremental_lags_exceed_stale(self):
        samples = [
            {"lag_seconds": 10 * DAY, "is_fresh": False, "publish_ts": NOW_TS - 20 * DAY},
            {"lag_seconds": 8 * DAY, "is_fresh": False, "publish_ts": NOW_TS - 15 * DAY},
            {"lag_seconds": 30 * DAY, "is_fresh": False, "publish_ts": NOW_TS - 40 * DAY},
        ]
        result = evaluate_download_liveness(samples, stale_seconds=7 * DAY, min_samples=3)
        self.assertEqual(result.status, STATUS_EXPIRED)

    def test_unknown_when_stale_but_below_min_samples(self):
        samples = [
            {"lag_seconds": 10 * DAY, "is_fresh": False, "publish_ts": NOW_TS - 20 * DAY},
        ]
        result = evaluate_download_liveness(samples, stale_seconds=7 * DAY, min_samples=3)
        self.assertEqual(result.status, STATUS_UNKNOWN)

    def test_collect_skips_history_source(self):
        users = [
            {
                "id": "u1",
                "nickname": "n1",
                "download_records": [
                    {
                        "aweme_id": "a1",
                        "source": DOWNLOAD_SOURCE_HISTORY,
                        "publish_time": (NOW - timedelta(days=20)).isoformat(timespec="seconds"),
                        "downloaded_at": NOW.isoformat(timespec="seconds"),
                    },
                    {
                        "aweme_id": "a2",
                        "source": DOWNLOAD_SOURCE_INCREMENTAL,
                        "publish_time": (NOW - timedelta(hours=3)).isoformat(timespec="seconds"),
                        "downloaded_at": NOW.isoformat(timespec="seconds"),
                    },
                ],
            }
        ]
        rows = collect_incremental_download_samples(
            users,
            now=NOW,
            lookback_seconds=14 * DAY,
            stale_seconds=7 * DAY,
            parse_iso_datetime=parse_iso_datetime,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["aweme_id"], "a2")
        self.assertTrue(rows[0]["is_fresh"])


class DownloadFirstRunCheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_check_prefers_fresh_download_over_stale_probe(self):
        class StaleProbeCrawler:
            async def fetch_user_post_videos(self, sec_user_id, max_cursor, count):
                return {"aweme_list": [{"create_time": NOW_TS - 30 * DAY}]}

        users = [
            {
                "id": f"u{i}",
                "nickname": f"n{i}",
                "enabled": True,
                "account_status": ACCOUNT_STATUS_NORMAL,
                "sec_user_id": f"sec-{i}",
                "download_records": [
                    {
                        "aweme_id": f"aw{i}",
                        "source": DOWNLOAD_SOURCE_INCREMENTAL,
                        "publish_time": (NOW - timedelta(hours=2)).isoformat(timespec="seconds"),
                        "downloaded_at": (NOW - timedelta(hours=1)).isoformat(timespec="seconds"),
                    }
                    for _ in range(1)
                ],
            }
            for i in range(1, 4)
        ]
        # ensure min samples via three users each with one fresh download
        service = CookieLivenessService(
            crawler=StaleProbeCrawler(),
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
        result = await service.run_check(users)
        self.assertEqual(result.status, STATUS_HEALTHY)
        self.assertIn("发布-下载间隔", result.reason)


class StaleDownloadProbeCombineTests(unittest.IsolatedAsyncioTestCase):
    def _stale_users(self, n: int = 3):
        return [
            {
                "id": f"u{i}",
                "nickname": f"n{i}",
                "enabled": True,
                "account_status": ACCOUNT_STATUS_NORMAL,
                "sec_user_id": f"sec-{i}",
                "download_records": [
                    {
                        "aweme_id": f"aw{i}",
                        "source": DOWNLOAD_SOURCE_INCREMENTAL,
                        "publish_time": (NOW - timedelta(days=20)).isoformat(timespec="seconds"),
                        "downloaded_at": (NOW - timedelta(days=1)).isoformat(timespec="seconds"),
                    }
                ],
            }
            for i in range(1, n + 1)
        ]

    async def test_stale_downloads_with_reachable_probe_is_degraded(self):
        class ReachableCrawler:
            async def fetch_user_post_videos(self, sec_user_id, max_cursor, count):
                return {"aweme_list": [{"create_time": NOW_TS - 40 * DAY}]}

        service = CookieLivenessService(
            crawler=ReachableCrawler(),
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
        result = await service.run_check(self._stale_users())
        self.assertEqual(result.status, STATUS_DEGRADED)
        self.assertIn("探针可达", result.reason)


    async def test_stale_downloads_with_unknown_probe_is_degraded(self):
        class EmptyCrawler:
            async def fetch_user_post_videos(self, sec_user_id, max_cursor, count):
                return {"aweme_list": []}

        # 3 stale downloads → expired signal; only 1 probe sample < min_samples → probe unknown
        service = CookieLivenessService(
            crawler=EmptyCrawler(),
            config=CookieLivenessConfig(
                enabled=True,
                interval_hours=6,
                stale_days=7,
                sample_user_count=1,
                min_samples=3,
                alert_cooldown_hours=12,
            ),
            now_provider=lambda: NOW,
        )
        result = await service.run_check(self._stale_users(3))
        self.assertEqual(result.status, STATUS_DEGRADED)

    async def test_stale_downloads_with_failed_probe_is_expired(self):
        class FailCrawler:
            async def fetch_user_post_videos(self, sec_user_id, max_cursor, count):
                raise RuntimeError("login required")

        service = CookieLivenessService(
            crawler=FailCrawler(),
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
        result = await service.run_check(self._stale_users())
        self.assertEqual(result.status, STATUS_EXPIRED)

    async def test_alert_message_includes_lag_stats(self):
        class FailCrawler:
            async def fetch_user_post_videos(self, sec_user_id, max_cursor, count):
                raise RuntimeError("login required")

        alerter = RecordingAlerter()
        service = CookieLivenessService(
            crawler=FailCrawler(),
            config=CookieLivenessConfig(
                enabled=True,
                interval_hours=6,
                stale_days=7,
                sample_user_count=5,
                min_samples=3,
                alert_cooldown_hours=12,
            ),
            alerter=alerter,
            now_provider=lambda: NOW,
        )
        state = {"users": self._stale_users(), "monitoring": {}}
        payload = await service.maybe_run(state)
        self.assertEqual(payload["status"], STATUS_EXPIRED)
        self.assertTrue(payload["alerted"])
        self.assertIn("增量lag", alerter.messages[0])
        self.assertIn("信号:", alerter.messages[0])


class HermesWeixinSenderTests(unittest.IsolatedAsyncioTestCase):
    async def test_builds_ssh_hermes_command(self):
        runner = RecordingRunner(code=0)
        sender = HermesWeixinSender(
            HermesWeixinConfig(
                enabled=True,
                ssh_host="hermes.example.test",
                ssh_user="root",
                hermes_home="/opt/hermes",
                hermes_bin="/opt/hermes/bin/hermes",
                target="weixin",
                timeout_seconds=30,
            ),
            runner=runner,
        )
        await sender.send("hello world")
        self.assertEqual(len(runner.commands), 1)
        cmd = runner.commands[0]
        self.assertEqual(cmd[0], "ssh")
        self.assertEqual(cmd[-2], "root@hermes.example.test")
        self.assertIn("hermes", cmd[-1])
        self.assertIn("hello world", cmd[-1])

    async def test_raises_on_nonzero_exit(self):
        runner = RecordingRunner(code=1)
        sender = HermesWeixinSender(
            HermesWeixinConfig(
                enabled=True,
                ssh_host="hermes.example.test",
                ssh_user="root",
                hermes_home="/opt/hermes",
                hermes_bin="/bin/hermes",
                target="weixin",
                timeout_seconds=30,
            ),
            runner=runner,
        )
        with self.assertRaises(RuntimeError):
            await sender.send("fail")


if __name__ == "__main__":
    unittest.main()
