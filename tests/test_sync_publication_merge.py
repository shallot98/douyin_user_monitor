from copy import deepcopy

import pytest
from test_cloud_playback_publish import fixture as publication_fixture

from douyin_user_monitor.monitor.service import MonitorService
from douyin_user_monitor.monitor.record_store import RecordStore
from douyin_user_monitor.monitor.storage import MonitorStorage
from scripts.cloud_playback.publish import publish_verified


@pytest.mark.parametrize("coverage", [False, True])
def test_sync_preserves_concurrent_publication_and_new_downloads(tmp_path, coverage):
    storage, publication = publication_fixture.__wrapped__(tmp_path)

    def seed(state):
        user = state["users"][0]
        user["downloaded_aweme_ids"] = ["work"]
        user["downloaded_count"] = 1

    storage.update_state(seed)
    snapshot = deepcopy(storage.records.attach(storage.load_state()["users"][0]))
    snapshot["download_records"].insert(0, {"aweme_id": "new", "files": ["new.mp4"]})
    snapshot["downloaded_aweme_ids"].append("new")
    snapshot["downloaded_count"] = 2
    publisher = MonitorStorage(storage.state_file)
    published = publish_verified(publisher, [publication])
    repaired = deepcopy(published["users"][0]["download_records"][0])

    service = object.__new__(MonitorService)
    service._storage = storage
    service._coverage_priority_user_ids = []
    summary = {"checked_users": 1, "downloaded_items": 1, "errors": []}
    if coverage:
        service._commit_coverage_user_sync(
            user_id="user", user_snapshot=snapshot, processed_user_ids={"user"},
            summary=summary, cycle_started_at="2026-09-29T00:00:00+08:00",
        )
    else:
        service._commit_user_sync_results([snapshot], summary)

    reader = MonitorStorage(storage.state_file)
    user = reader.records.attach(reader.load_state()["users"][0])
    records = {record["aweme_id"]: record for record in user["download_records"]}
    assert records["work"] == repaired
    assert records["new"]["files"] == ["new.mp4"]
    assert user["download_records"][0]["aweme_id"] == "new"
    assert user["downloaded_aweme_ids"] == ["work", "new"]
    assert user["downloaded_count"] == 2


def _store_with(tmp_path, disk):
    """Move the disk user's records into a RecordStore, as the split layout keeps them."""
    store = RecordStore(tmp_path / "monitor_records.db")
    store.put(disk["id"], disk.pop("download_records"))
    return store


def test_merge_keeps_disk_only_downloads_and_counts_each_new_id_once(tmp_path):
    disk = {"id": "u", "download_records": [{"aweme_id": "concurrent", "files": ["current.mp4"]},
                                             {"aweme_id": "old", "files": ["repaired.mp4"]}],
            "downloaded_aweme_ids": ["old", "concurrent"], "downloaded_count": 2}
    store = _store_with(tmp_path, disk)
    snapshot = {"download_records": [{"aweme_id": "new", "files": ["new.mp4"]},
                                     {"aweme_id": "old", "files": ["old.mp4"]}],
                "downloaded_aweme_ids": ["old", "new"], "downloaded_count": 2}
    MonitorService._merge_user_sync_fields(disk, snapshot, store)
    assert [row["aweme_id"] for row in store.get("u")] == ["new", "concurrent", "old"]
    assert store.get("u")[-1]["files"] == ["repaired.mp4"]
    assert set(disk["downloaded_aweme_ids"]) == {"old", "concurrent", "new"}
    assert disk["downloaded_count"] == 3
    MonitorService._merge_user_sync_fields(disk, snapshot, store)
    assert disk["downloaded_count"] == 3
    assert [row["aweme_id"] for row in store.get("u")] == ["new", "concurrent", "old"]


def test_merge_does_not_exceed_record_limit_or_reverse_snapshot_order(tmp_path):
    disk = {"id": "u", "download_records": [{"aweme_id": str(index), "files": [f"{index}.mp4"]}
                                            for index in range(300)],
            "downloaded_aweme_ids": [str(index) for index in range(300)], "downloaded_count": 300}
    snapshot = deepcopy(disk)
    store = _store_with(tmp_path, disk)
    snapshot["download_records"] = [{"aweme_id": "new-2"}, {"aweme_id": "new-1"}] + snapshot["download_records"][:298]
    snapshot["downloaded_aweme_ids"] += ["new-1", "new-2"]
    snapshot["downloaded_count"] += 2
    MonitorService._merge_user_sync_fields(disk, snapshot, store)
    assert len(store.get("u")) == 300
    assert [row["aweme_id"] for row in store.get("u")[:3]] == ["new-2", "new-1", "0"]
    assert disk["downloaded_count"] == 302
