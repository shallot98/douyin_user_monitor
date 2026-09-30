import tempfile
import unittest
from pathlib import Path

from douyin_user_monitor.monitor.content_filter import RESULT_VERSION
from douyin_user_monitor.monitor.storage import MonitorStorage
from scripts.content_filter_backfill import (
    commit_results,
    is_gateway_outage,
    needs_label,
    outages_only,
    select_pending,
)

V2_OK = {"version": RESULT_VERSION, "status": "ok", "verdict": "keep", "category": "normal"}


class BackfillSelectionTests(unittest.TestCase):
    def test_needs_label(self):
        self.assertTrue(needs_label({"aweme_id": "1"}, retry_errors=False))
        self.assertTrue(needs_label({"content_filter": {"status": "ok"}}, retry_errors=False))  # v1 结构
        self.assertFalse(needs_label({"content_filter": V2_OK}, retry_errors=False))
        failed = {"content_filter": {"version": RESULT_VERSION, "status": "error"}}
        self.assertFalse(needs_label(failed, retry_errors=False))
        self.assertTrue(needs_label(failed, retry_errors=True))

    def test_select_pending_filters_user(self):
        records = {"a": [{"aweme_id": "1"}, {"aweme_id": "2", "content_filter": V2_OK}], "b": [{"aweme_id": "3"}]}
        users = [{"id": "a"}, {"id": "b"}]

        all_pending = select_pending(users, lambda uid: records[uid], retry_errors=False)
        only_b = select_pending(users, lambda uid: records[uid], retry_errors=False, user_id="b")

        self.assertEqual([r["aweme_id"] for _, r in all_pending], ["1", "3"])
        self.assertEqual([r["aweme_id"] for _, r in only_b], ["3"])


class GatewayOutageTests(unittest.TestCase):
    def test_majority_5xx_is_outage_other_errors_are_not(self):
        bad = {"status": "error", "error": "模型接口 HTTP 503：上游服务暂不可用"}
        media = {"status": "error", "error": "归档文件在主力盘与备份盘均不可用"}
        ok = {"status": "ok"}
        mostly_down = [(None, None, bad)] * 3 + [(None, None, ok)]
        media_errors = [(None, None, media)] * 4

        self.assertTrue(is_gateway_outage(outages_only(mostly_down), 4))
        self.assertFalse(is_gateway_outage(outages_only(media_errors), 4))
        self.assertFalse(is_gateway_outage(outages_only([(None, None, bad), (None, None, ok)]), 2))


class BackfillCommitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.storage = MonitorStorage(Path(temporary.name) / "monitor_users.json")
        state = self.storage.load_state()
        state["users"] = [{"id": "u1", "nickname": "n"}]
        self.storage.save_state(state)

    def test_commit_patches_by_aweme_id_and_keeps_other_fields(self):
        self.storage.records.put("u1", [{"aweme_id": "1", "desc": "a"}, {"aweme_id": "2", "desc": "b"}])

        written = commit_results(self.storage, {"u1": {"2": {**V2_OK, "category": "ad"}}}, retry_errors=False)

        records = self.storage.records.get("u1")
        self.assertEqual(written, 1)
        self.assertNotIn("content_filter", records[0])
        self.assertEqual(records[1]["desc"], "b")
        self.assertEqual(records[1]["content_filter"]["category"], "ad")

    def test_commit_does_not_overwrite_newer_result(self):
        # 补打任务拿到结论之前，同步流程已给这条写了 v2 结论
        self.storage.records.put("u1", [{"aweme_id": "1", "content_filter": {**V2_OK, "category": "text_only"}}])

        written = commit_results(self.storage, {"u1": {"1": {**V2_OK, "category": "normal"}}}, retry_errors=False)

        self.assertEqual(written, 0)
        self.assertEqual(self.storage.records.get("u1")[0]["content_filter"]["category"], "text_only")

    def test_commit_skips_records_removed_meanwhile(self):
        self.storage.records.put("u1", [{"aweme_id": "9"}])

        written = commit_results(self.storage, {"u1": {"1": V2_OK}, "ghost": {"x": V2_OK}}, retry_errors=False)

        self.assertEqual(written, 0)
        self.assertEqual(self.storage.records.get("u1"), [{"aweme_id": "9"}])


if __name__ == "__main__":
    unittest.main()
