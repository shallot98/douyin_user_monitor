import orjson
import pytest

from douyin_user_monitor.monitor.record_store import RecordStore, load_state_with_records, record_db_path
from douyin_user_monitor.monitor.storage import LegacyRecordsInStateError, MonitorStorage, build_default_state
from scripts.split_download_records import SplitError, migrate, rollback


def _rows(*ids):
    return [{"aweme_id": aweme_id, "files": [f"{aweme_id}.mp4"]} for aweme_id in ids]


@pytest.fixture
def store(tmp_path):
    store = RecordStore(tmp_path / "monitor_records.db")
    yield store
    store.close()


def test_put_get_keeps_exact_order_and_shape(store):
    records = _rows("3", "1", "2")
    store.put("u1", records)
    assert store.get("u1") == records
    assert store.get("missing") == []


def test_attach_is_a_view_and_leaves_input_untouched(store):
    store.put("u1", _rows("a"))
    user = {"id": "u1", "nickname": "n"}
    view = store.attach(user)
    assert view["download_records"] == _rows("a")
    assert "download_records" not in user
    assert [u["download_records"] for u in store.attach_all([user, {"id": "u2"}])] == [_rows("a"), []]


def test_other_connection_writes_are_picked_up(tmp_path, store):
    store.put("u1", _rows("a"))
    store.put("u2", _rows("b"))
    before = store.current_revision()
    other = RecordStore(tmp_path / "monitor_records.db")
    try:
        other.put("u1", _rows("a2", "a"))
        other.delete("u2")
    finally:
        other.close()
    assert store.get("u1") == _rows("a2", "a")
    assert store.get("u2") == []
    assert store.current_revision() > before


def test_bad_batch_writes_nothing_and_keeps_cache(store):
    store.put("u1", _rows("a"))

    class Boom:
        def __iter__(self):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        store.put_many({"u1": _rows("x"), "u2": Boom()})
    assert store.get("u1") == _rows("a")
    reopened = RecordStore(store.db_path)
    try:
        assert reopened.get("u1") == _rows("a")
        assert reopened.get("u2") == []
    finally:
        reopened.close()


def test_storage_refuses_inline_records_on_read_and_write(tmp_path):
    path = tmp_path / "monitor_users.json"
    storage = MonitorStorage(path)
    state = build_default_state()
    state["users"] = [{"id": "u1", "download_records": []}]
    with pytest.raises(LegacyRecordsInStateError):
        storage.save_state(state)
    path.write_bytes(orjson.dumps(state))
    with pytest.raises(LegacyRecordsInStateError):
        MonitorStorage(path).load_state()


def test_load_state_with_records_requires_db(tmp_path):
    path = tmp_path / "monitor_users.json"
    path.write_bytes(orjson.dumps(build_default_state()))
    with pytest.raises(FileNotFoundError):
        load_state_with_records(path)


def _legacy_state():
    state = build_default_state()
    state["users"] = [
        {"id": "u1", "nickname": "a", "downloaded_aweme_ids": ["1"], "download_records": _rows("2", "1")},
        {"id": "u2", "nickname": "b", "downloaded_aweme_ids": [], "download_records": []},
    ]
    return state


def test_migrate_then_rollback_round_trips(tmp_path):
    path = tmp_path / "monitor_users.json"
    original = _legacy_state()
    path.write_bytes(orjson.dumps(original))

    result = migrate(path)
    assert result["status"] == "migrated"
    assert (result["users_with_records"], result["records"]) == (1, 2)
    split = orjson.loads(path.read_bytes())
    assert all("download_records" not in user for user in split["users"])
    # 去重依据补齐：记录里有的 id 必须在 downloaded_aweme_ids 里
    assert split["users"][0]["downloaded_aweme_ids"] == ["1", "2"]
    assert load_state_with_records(path)["users"][0]["download_records"] == _rows("2", "1")
    assert migrate(path)["status"] == "already_split"

    assert rollback(path)["status"] == "rolled_back"
    restored = orjson.loads(path.read_bytes())
    assert [u["download_records"] for u in restored["users"]] == [_rows("2", "1"), []]
    # 回滚后 DB 仍有行：不清理就再次迁移必须拒绝，而不是合并两份来源
    with pytest.raises(SplitError):
        migrate(path)


def test_rollback_refuses_when_json_already_has_records(tmp_path):
    path = tmp_path / "monitor_users.json"
    path.write_bytes(orjson.dumps(_legacy_state()))
    record_db_path(path).touch()
    with pytest.raises(SplitError):
        rollback(path)
