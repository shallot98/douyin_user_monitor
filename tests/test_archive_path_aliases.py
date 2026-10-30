from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from douyin_user_monitor.monitor.archive_path_aliases import (
    ARCHIVE_PATH_ALIASES_FILENAME,
    ArchivePathAliasError,
    ArchivePathAliases,
    build_archive_path_aliases,
    load_archive_path_aliases,
)

ORIGINAL_PATH = "八月/20241005_200457_新文件名.mp4"
VERIFIED_RELATIVE = "sec_sec-1/20241005_200457_原标题  保留空格.mp4"
D2_BASE = "/d2/vps/抖音视频"
VERIFIED_PATH = f"{D2_BASE}/{VERIFIED_RELATIVE}"
DOWNLOADED_AT = "2026-04-10T02:06:08+08:00"


def source_user():
    return {
        "id": "user-1",
        "sec_user_id": "sec-1",
        "download_records": [
            {"aweme_id": "7001", "downloaded_at": DOWNLOADED_AT, "files": [ORIGINAL_PATH]}
        ],
    }


def source_evidence():
    row = {
        "user_id": "user-1",
        "aweme_id": "7001",
        "status": "recovered",
        "original_relative_path": ORIGINAL_PATH,
        "source_fingerprint": "audit-source-digest",
        "verified_relative_path": VERIFIED_RELATIVE,
        "verified_remote_path": VERIFIED_PATH,
        "drive": D2_BASE,
        "size": 1641754,
        "verified_at": "2026-09-13T14:44:35+08:00",
        "matching_basis": [{"kind": "exact_migration_done", "key": VERIFIED_RELATIVE}],
    }
    snapshot = {
        "user_id": "user-1",
        "aweme_id": "7001",
        "sec_user_id": "sec-1",
        "relative_path": ORIGINAL_PATH,
        "downloaded_at": DOWNLOADED_AT,
        "fingerprint": "audit-source-digest",
    }
    return {"items": [row]}, {"items": [snapshot]}


def registry_document():
    evidence, manifest = source_evidence()
    document, _ = build_archive_path_aliases(evidence, manifest, [source_user()])
    return document


class ArchivePathAliasesTests(unittest.TestCase):
    def test_imports_only_verified_paths_without_changing_source_data(self):
        evidence, manifest = source_evidence()
        evidence["items"].append({"status": "not_found", "user_id": "user-2", "aweme_id": "7002"})
        user = source_user()
        before = deepcopy((evidence, manifest, user))
        document, report = build_archive_path_aliases(evidence, manifest, [user])
        self.assertEqual((evidence, manifest, user), before)
        self.assertEqual(report, {"total": 2, "imported": 1, "not_recovered": 1, "stale": 0, "stale_entries": []})
        self.assertEqual(document["entries"][0]["matching_basis"], evidence["items"][0]["matching_basis"])

    def test_preserves_historical_filename_directory_and_double_spaces(self):
        aliases = ArchivePathAliases.from_document(registry_document())
        self.assertEqual(aliases.paths_for(source_user(), "7001", ORIGINAL_PATH), (VERIFIED_PATH,))
        self.assertIn("原标题  保留空格", VERIFIED_PATH)
        self.assertNotIn(Path(ORIGINAL_PATH).name, VERIFIED_PATH)

    def test_unknown_filename_is_not_fuzzily_matched(self):
        aliases = ArchivePathAliases.from_document(registry_document())
        unknown = ORIGINAL_PATH.replace("新文件名", "新文件名2")
        user = source_user()
        user["download_records"][0]["files"].append(unknown)
        self.assertEqual(aliases.paths_for(user, "7001", unknown), ())
        self.assertEqual(aliases.paths_for(user, "7002", ORIGINAL_PATH), ())

    def test_source_revision_change_is_rejected_at_import_and_read(self):
        evidence, manifest = source_evidence()
        user = source_user()
        user["download_records"][0]["downloaded_at"] = "2026-09-13T16:00:00+08:00"
        document, report = build_archive_path_aliases(evidence, manifest, [user])
        self.assertEqual(document["entries"], [])
        self.assertEqual(report["stale_entries"][0]["reason"], "download_revision_changed")
        aliases = ArchivePathAliases.from_document(registry_document())
        with self.assertRaisesRegex(ArchivePathAliasError, "download_revision_changed"):
            aliases.paths_for(user, "7001", ORIGINAL_PATH)

    def test_added_playback_cover_and_verification_do_not_invalidate_original(self):
        evidence, manifest = source_evidence()
        user = source_user()
        record = user["download_records"][0]
        record["files"].extend(["八月/play.fast.mp4", "八月/play.cover.jpg"])
        record.update(
            fast_play_file="八月/play.fast.mp4",
            fast_play_source={"codec": "h264"},
            playback_verified_at="2026-09-13T16:00:00+08:00",
            total_size_bytes=2000000,
        )
        document, report = build_archive_path_aliases(evidence, manifest, [user])
        self.assertEqual(report["imported"], 1)
        self.assertEqual(ArchivePathAliases.from_document(document).paths_for(user, "7001", ORIGINAL_PATH), (VERIFIED_PATH,))

    def test_identity_changes_and_removed_source_paths_are_explicit(self):
        aliases = ArchivePathAliases.from_document(registry_document())
        user = source_user()
        user["sec_user_id"] = "different-account"
        with self.assertRaisesRegex(ArchivePathAliasError, "source_identity_changed"):
            aliases.paths_for(user, "7001", ORIGINAL_PATH)
        user = source_user()
        user["download_records"][0]["files"] = ["八月/replacement.mp4"]
        with self.assertRaisesRegex(ArchivePathAliasError, "source_path_removed"):
            aliases.paths_for(user, "7001", ORIGINAL_PATH)

    def test_audit_fingerprint_mismatch_is_reported_without_importing(self):
        evidence, manifest = source_evidence()
        evidence["items"][0]["source_fingerprint"] = "different-source"
        document, report = build_archive_path_aliases(evidence, manifest, [source_user()])
        self.assertEqual(document["entries"], [])
        self.assertEqual(report["stale_entries"][0]["reason"], "audit_fingerprint_mismatch")

    def test_missing_snapshot_or_current_user_is_reported(self):
        evidence, manifest = source_evidence()
        _, report = build_archive_path_aliases(evidence, {"items": []}, [source_user()])
        self.assertEqual(report["stale_entries"][0]["reason"], "snapshot_missing")
        _, report = build_archive_path_aliases(evidence, manifest, [])
        self.assertEqual(report["stale_entries"][0]["reason"], "user_missing")

    def test_mismatching_verified_absolute_and_relative_paths_fail(self):
        evidence, manifest = source_evidence()
        evidence["items"][0]["verified_remote_path"] = f"{D2_BASE}/another/file.mp4"
        with self.assertRaisesRegex(ArchivePathAliasError, "绝对路径与相对路径不一致"):
            build_archive_path_aliases(evidence, manifest, [source_user()])

    def test_registry_duplicates_and_unsafe_paths_are_rejected(self):
        document = registry_document()
        document["entries"].append(deepcopy(document["entries"][0]))
        with self.assertRaisesRegex(ArchivePathAliasError, "重复"):
            ArchivePathAliases.from_document(document)
        document = registry_document()
        document["entries"][0]["verified_remote_path"] = f"{D2_BASE}/../other/file.mp4"
        with self.assertRaisesRegex(ArchivePathAliasError, "无效目录"):
            ArchivePathAliases.from_document(document)

    def test_optional_registry_absence_and_missing_explicit_file_are_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "monitor_users.json"
            aliases = load_archive_path_aliases(state_path)
            self.assertEqual(aliases.paths_for(source_user(), "7001", ORIGINAL_PATH), ())
            with self.assertRaises(FileNotFoundError):
                ArchivePathAliases.from_file(Path(tmp) / ARCHIVE_PATH_ALIASES_FILENAME)

    def test_existing_invalid_registry_is_not_silently_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / ARCHIVE_PATH_ALIASES_FILENAME
            registry_path.write_text('{"schema_version": 2, "entries": []}', encoding="utf-8")
            with self.assertRaisesRegex(ArchivePathAliasError, "schema_version"):
                load_archive_path_aliases(Path(tmp) / "monitor_users.json")
            registry_path.write_text("invalid json", encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                load_archive_path_aliases(Path(tmp) / "monitor_users.json")

    def test_registry_file_round_trip_preserves_exact_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / ARCHIVE_PATH_ALIASES_FILENAME
            registry_path.write_text(json.dumps(registry_document(), ensure_ascii=False), encoding="utf-8")
            aliases = load_archive_path_aliases(Path(tmp) / "monitor_users.json")
            self.assertEqual(aliases.paths_for(source_user(), "7001", ORIGINAL_PATH), (VERIFIED_PATH,))


if __name__ == "__main__":
    unittest.main()
