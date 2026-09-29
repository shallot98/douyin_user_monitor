from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict

from douyin_user_monitor.monitor.nickname_history import (
    MAX_NICKNAME_HISTORY,
    build_former_nicknames,
    derive_archive_dir_names,
    normalize_nickname_history,
    record_nickname_change,
)
from douyin_user_monitor.monitor.record_store import RecordStore
from douyin_user_monitor.monitor.service import MonitorService
from douyin_user_monitor.monitor.storage import normalize_user_entry


def user_with_records(*dir_names: str, nickname: str = "现昵称") -> Dict[str, Any]:
    return {
        "nickname": nickname,
        "download_records": [
            {"aweme_id": f"a{index}", "files": [f"{name}/20260101_120000_x.mp4"]}
            for index, name in enumerate(dir_names)
        ],
    }


class NormalizeTests(unittest.TestCase):
    def test_missing_field_becomes_empty_list(self):
        user = {"nickname": "A"}
        self.assertEqual(normalize_nickname_history(user), [])
        self.assertEqual(user["nickname_history"], [])

    def test_non_list_is_replaced(self):
        user = {"nickname": "A", "nickname_history": "junk"}
        self.assertEqual(normalize_nickname_history(user), [])

    def test_dedupes_blanks_and_current_nickname(self):
        user = {"nickname": "A", "nickname_history": ["B", "  ", "B", "A", "C", None]}
        self.assertEqual(normalize_nickname_history(user), ["B", "C"])

    def test_caps_length(self):
        user = {"nickname": "cur", "nickname_history": [f"n{i}" for i in range(50)]}
        self.assertEqual(len(normalize_nickname_history(user)), MAX_NICKNAME_HISTORY)

    def test_storage_normalizes_user_entry(self):
        user = {"nickname": "A", "account_status": "normal"}
        normalize_user_entry(user)
        self.assertEqual(user["nickname_history"], [])


class RecordChangeTests(unittest.TestCase):
    def test_records_previous_name_newest_first(self):
        user = {"nickname": "B"}
        record_nickname_change(user, "A")
        self.assertEqual(user["nickname_history"], ["A"])
        user["nickname"] = "C"
        record_nickname_change(user, "B")
        self.assertEqual(user["nickname_history"], ["B", "A"])

    def test_no_op_when_nickname_unchanged(self):
        user = {"nickname": "A", "nickname_history": ["Z"]}
        record_nickname_change(user, "A")
        self.assertEqual(user["nickname_history"], ["Z"])

    def test_ignores_blank_previous_or_current(self):
        user = {"nickname": "A"}
        record_nickname_change(user, "")
        self.assertEqual(user["nickname_history"], [])
        blank_current = {"nickname": "", "nickname_history": []}
        record_nickname_change(blank_current, "A")
        self.assertEqual(blank_current["nickname_history"], [])

    def test_rename_back_drops_current_name_from_history(self):
        user = {"nickname": "B", "nickname_history": ["A"]}
        record_nickname_change(user, "B")  # A -> B -> A 回到原名
        user["nickname"] = "A"
        record_nickname_change(user, "B")
        self.assertEqual(user["nickname_history"], ["B"])

    def test_moves_existing_entry_to_front_without_duplicating(self):
        user = {"nickname": "C", "nickname_history": ["A", "B"]}
        record_nickname_change(user, "B")
        self.assertEqual(user["nickname_history"], ["B", "A"])


class DeriveFromArchiveTests(unittest.TestCase):
    def test_collects_top_level_dirs_in_record_order(self):
        user = user_with_records("新名", "旧名", "更旧名")
        self.assertEqual(derive_archive_dir_names(user), ["新名", "旧名", "更旧名"])

    def test_handles_image_post_nested_paths(self):
        user = {
            "nickname": "现昵称",
            "download_records": [{"aweme_id": "a", "files": ["旧名/图片/标题/1.jpg", "旧名/图片/标题/2.jpg"]}],
        }
        self.assertEqual(derive_archive_dir_names(user), ["旧名"])

    def test_tolerates_malformed_records(self):
        user = {"nickname": "A", "download_records": ["junk", {"files": "x"}, {}, {"files": [""]}]}
        self.assertEqual(derive_archive_dir_names(user), [])

    def test_missing_records_is_empty(self):
        self.assertEqual(derive_archive_dir_names({"nickname": "A"}), [])


class BuildFormerNicknamesTests(unittest.TestCase):
    def test_unions_persisted_history_and_archive_dirs(self):
        user = user_with_records("现昵称", "旧名", nickname="现昵称")
        user["nickname_history"] = ["很久以前"]
        self.assertEqual(build_former_nicknames(user), ["很久以前", "旧名"])

    def test_excludes_current_nickname(self):
        user = user_with_records("现昵称", nickname="现昵称")
        self.assertEqual(build_former_nicknames(user), [])

    def test_derives_for_users_renamed_before_the_field_existed(self):
        # 只有归档目录能证明改过名：持久化字段还是空的
        user = user_with_records("张pray", "柒柒", nickname="🐿️")
        self.assertEqual(build_former_nicknames(user), ["张pray", "柒柒"])

    def test_persisted_history_survives_record_eviction(self):
        # download_records 有 300 条上限，老目录名会被淘汰，持久化字段兜住
        user = {"nickname": "新名", "nickname_history": ["旧名"], "download_records": []}
        self.assertEqual(build_former_nicknames(user), ["旧名"])

    def test_no_duplicates_across_sources(self):
        user = user_with_records("旧名", nickname="现昵称")
        user["nickname_history"] = ["旧名"]
        self.assertEqual(build_former_nicknames(user), ["旧名"])


class MergeIntegrationTests(unittest.TestCase):
    """改名必须在锁内合并处按磁盘旧值落盘，不能靠锁外快照。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.records = RecordStore(Path(tmp.name) / "monitor_records.db")
        self.addCleanup(self.records.close)

    def test_merge_records_rename_from_disk_previous_value(self):
        disk_user = {"id": "u1", "nickname": "旧名", "nickname_history": []}
        synced = {"id": "u1", "nickname": "新名"}
        MonitorService._merge_user_sync_fields(disk_user, synced, self.records)
        self.assertEqual(disk_user["nickname"], "新名")
        self.assertEqual(disk_user["nickname_history"], ["旧名"])

    def test_merge_leaves_history_alone_without_rename(self):
        disk_user = {"id": "u1", "nickname": "同名", "nickname_history": ["旧名"]}
        MonitorService._merge_user_sync_fields(disk_user, {"id": "u1", "nickname": "同名"}, self.records)
        self.assertEqual(disk_user["nickname_history"], ["旧名"])

    def test_stale_snapshot_history_does_not_overwrite_disk(self):
        # nickname_history 刻意不在 USER_SYNC_MERGE_FIELDS 里：锁外快照的旧副本不得覆盖磁盘
        disk_user = {"id": "u1", "nickname": "旧名", "nickname_history": ["更早的名字"]}
        synced = {"id": "u1", "nickname": "新名", "nickname_history": []}
        MonitorService._merge_user_sync_fields(disk_user, synced, self.records)
        self.assertEqual(disk_user["nickname_history"], ["旧名", "更早的名字"])


if __name__ == "__main__":
    unittest.main()
