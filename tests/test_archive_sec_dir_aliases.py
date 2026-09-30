import unittest

from douyin_user_monitor.monitor.archive_path_aliases import ArchivePathAliases
from scripts.archive_sec_dir_aliases import plan_user, sanitize_rest

D2, QUARK = "/d2/vps/抖音视频", "/kuake/抖音视频"
BASES = [D2, QUARK]


class FakeClient:
    def remote_paths_for(self, relative, sec_user_id=None, *, verified_paths=()):
        return [f"{D2}/{relative}", f"{QUARK}/{relative}"]


def user(*records):
    return {"id": "u1", "sec_user_id": "SID", "nickname": "八月", "download_records": list(records)}


def record(aweme_id, *files):
    return {"aweme_id": aweme_id, "downloaded_at": "2026-05-01T00:00:00+08:00", "files": list(files)}


class PlanUserTests(unittest.TestCase):
    def test_file_only_in_sec_dir_gets_primary_alias(self):
        target = user(record("1", "八月/图片/作品/1.jpg"))
        sec = {D2: {"图片/作品/1.jpg": 100}, QUARK: {"图片/作品/1.jpg": 100}}
        trees = {f"{D2}/sec_SID/图片/作品/1.jpg": 100}

        entries, stats = plan_user(target, FakeClient(), {}, trees, sec, BASES)

        self.assertEqual(stats["aliased_primary"], 1)
        self.assertEqual(entries[0]["verified_remote_path"], f"{D2}/sec_SID/图片/作品/1.jpg")
        self.assertEqual(entries[0]["source_revision"], {"sec_user_id": "SID", "downloaded_at": "2026-05-01T00:00:00+08:00"})
        # 生成的条目能被现有映射加载器接受，且对当前记录不算过期
        aliases = ArchivePathAliases(entries)
        self.assertEqual(aliases.paths_for(target, "1", "八月/图片/作品/1.jpg"), (f"{D2}/sec_SID/图片/作品/1.jpg",))

    def test_reachable_file_is_not_aliased(self):
        target = user(record("1", "八月/a.mp4"))
        sec = {D2: {"a.mp4": 100}, QUARK: {}}
        trees = {f"{D2}/八月/a.mp4": 100, f"{D2}/sec_SID/a.mp4": 100}

        entries, stats = plan_user(target, FakeClient(), {}, trees, sec, BASES)

        self.assertEqual(entries, [])
        self.assertEqual(stats["reachable"], 1)

    def test_backup_used_when_primary_sec_copy_missing_or_empty(self):
        target = user(record("1", "八月/a.mp4"))
        sec = {D2: {"a.mp4": 0}, QUARK: {"a.mp4": 50}}

        entries, stats = plan_user(target, FakeClient(), {}, {}, sec, BASES)

        self.assertEqual(stats["aliased_backup"], 1)
        self.assertEqual(entries[0]["verified_remote_path"], f"{QUARK}/sec_SID/a.mp4")

    def test_sanitized_name_in_sec_dir_matches(self):
        target = user(record("1", "八月/图片/标题🍬/1.jpg"))
        sec = {D2: {"图片/标题/1.jpg": 10}, QUARK: {}}

        entries, _ = plan_user(target, FakeClient(), {}, {}, sec, BASES)

        self.assertEqual(entries[0]["verified_remote_path"], f"{D2}/sec_SID/图片/标题/1.jpg")
        self.assertTrue(entries[0]["matching_basis"]["renamed"])

    def test_existing_alias_and_missing_files_reported(self):
        target = user(record("1", "八月/a.mp4"), record("2", "八月/b.mp4"))
        existing = {("u1", "1", "八月/a.mp4"): {}}

        entries, stats = plan_user(target, FakeClient(), existing, {}, {D2: {}, QUARK: {}}, BASES)

        self.assertEqual(entries, [])
        self.assertEqual((stats["already_aliased"], stats["missing_everywhere"]), (1, 1))

    def test_sanitize_rest(self):
        self.assertEqual(sanitize_rest("图片/标题🍬/1.jpg"), "图片/标题/1.jpg")
        self.assertIsNone(sanitize_rest("图片/🍬/1.jpg"))


if __name__ == "__main__":
    unittest.main()
