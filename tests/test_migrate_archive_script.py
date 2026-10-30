"""迁移脚本的纯逻辑：路径变换、状态续跑、错误翻译。

真实复制行为已在实机验证；这里锁住那些出错会导致「静默漏文件」或
「重复搬运」的判断逻辑。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "migrate_archive_to_primary.py"


def load_script_module():
    spec = importlib.util.spec_from_file_location("migrate_archive_to_primary", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mig = load_script_module()


class PrimaryDirTests(unittest.TestCase):
    def test_strips_emoji_from_every_segment(self):
        self.assertEqual(mig.primary_dir_for("鱼幼薇🐱", sanitize=True), "鱼幼薇")
        self.assertEqual(
            mig.primary_dir_for("柒柒🐿️/图片/标题🍬", sanitize=True),
            "柒柒/图片/标题",
        )

    def test_passthrough_when_disabled(self):
        self.assertEqual(mig.primary_dir_for("鱼幼薇🐱", sanitize=False), "鱼幼薇🐱")

    def test_all_emoji_dir_is_unusable(self):
        # 返回 None 让调用方显式跳过，而不是产出一个空目录名的坏路径
        self.assertIsNone(mig.primary_dir_for("🐱🍬", sanitize=True))
        self.assertIsNone(mig.primary_dir_for("🐱🍬/图片", sanitize=True))

    def test_plain_names_untouched(self):
        self.assertEqual(mig.primary_dir_for("纯中文/图片/标题", sanitize=True), "纯中文/图片/标题")


class MigrationStateTests(unittest.TestCase):
    def test_roundtrip_persists_done_and_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            state = mig.MigrationState.load(path)
            state.mark_done("u/a.mp4", 123)
            state.mark_failed("u/b.mp4", "boom")
            state.save()

            reloaded = mig.MigrationState.load(path)
            self.assertEqual(reloaded.done, {"u/a.mp4": 123})
            self.assertEqual(reloaded.failed, {"u/b.mp4": "boom"})

    def test_is_done_requires_matching_size(self):
        state = mig.MigrationState(path=Path("/tmp/x.json"))
        state.mark_done("u/a.mp4", 100)
        self.assertTrue(state.is_done("u/a.mp4", 100))
        # 大小变了说明源文件被换过，必须重新复制
        self.assertFalse(state.is_done("u/a.mp4", 200))
        self.assertFalse(state.is_done("u/other.mp4", 100))

    def test_marking_done_clears_previous_failure(self):
        state = mig.MigrationState(path=Path("/tmp/x.json"))
        state.mark_failed("u/a.mp4", "boom")
        state.mark_done("u/a.mp4", 10)
        self.assertNotIn("u/a.mp4", state.failed)

    def test_corrupt_state_file_starts_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{not json", encoding="utf-8")
            state = mig.MigrationState.load(path)
            self.assertEqual(state.done, {})
            self.assertEqual(state.failed, {})

    def test_save_is_atomic_via_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "state.json"
            state = mig.MigrationState.load(path)
            state.mark_done("a", 1)
            state.save()
            self.assertTrue(path.is_file())
            self.assertFalse(path.with_suffix(".tmp").exists())
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["done"], {"a": 1})


class ErrorTranslationTests(unittest.TestCase):
    def test_translates_wopan_misleading_login_error(self):
        raw = 'request failed with rsp_code: 8005,rep_desc: 登录失败！'
        translated = mig.describe_error(raw)
        self.assertIn("疑似路径不兼容", translated)
        self.assertIn("8005", translated)

    def test_passes_other_errors_through(self):
        self.assertIn("object not found", mig.describe_error("object not found"))

    def test_handles_empty(self):
        self.assertEqual(mig.describe_error(None), "")


class FormatBytesTests(unittest.TestCase):
    def test_scales_units(self):
        self.assertEqual(mig.format_bytes(512), "512.0B")
        self.assertEqual(mig.format_bytes(2048), "2.0KB")
        self.assertEqual(mig.format_bytes(5 * 1024 * 1024), "5.0MB")
        self.assertEqual(mig.format_bytes(3 * 1024**3), "3.0GB")


class SafetyTests(unittest.TestCase):
    def test_script_never_deletes(self):
        """迁移必须是只复制：脚本里不该出现任何删除类调用。"""
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertNotIn("/api/fs/remove", source)
        self.assertNotIn("fs/move", source)
        self.assertNotIn("unlink", source)


class FakeListApi:
    """最小化 OpenListSync 替身：按路径返回预置目录项（None=列目录失败）。"""

    def __init__(self, tree):
        self.tree = tree

    def list_dir(self, path, refresh=False):
        return self.tree.get(path)


TREE = {
    "src": [
        {"name": "u1", "is_dir": True},
        {"name": "u2", "is_dir": True},
        {"name": "loose.mp4", "size": 9, "is_dir": False},
    ],
    "src/u1": [
        {"name": "a.mp4", "size": 1, "is_dir": False},
        {"name": "图片", "is_dir": True},
        {"name": "子目录", "is_dir": True},
    ],
    "src/u1/图片": [{"name": "p1", "is_dir": True}],
    "src/u1/图片/p1": [{"name": "1.jpg", "size": 2, "is_dir": False}],
    "src/u1/子目录": [],  # 空目录：应产出空批次或被跳过，但不能死循环
    "src/u2": [{"name": "b.mp4", "size": 3, "is_dir": False}],
}


class WalkSourceTests(unittest.TestCase):
    def test_concurrent_walk_yields_complete_batches(self):
        """并发列目录时每个目录的文件必须完整落在同一批，且全树无遗漏。"""
        for workers in (1, 4):
            with self.subTest(workers=workers):
                batches = dict(
                    mig.walk_source(FakeListApi(TREE), "src", None, list_workers=workers)
                )
                self.assertEqual(batches["u1"], [("a.mp4", 1)])
                self.assertEqual(batches["u1/图片/p1"], [("1.jpg", 2)])
                self.assertEqual(batches["u2"], [("b.mp4", 3)])
                self.assertEqual(len(batches), 3)  # 空目录不产出

    def test_only_filter_limits_top_dirs(self):
        batches = dict(
            mig.walk_source(FakeListApi(TREE), "src", "u1", list_workers=2)
        )
        self.assertEqual(set(batches), {"u1", "u1/图片/p1"})

    def test_failed_listing_skips_subtree(self):
        tree = {"src": [{"name": "bad", "is_dir": True}], "src/bad": None}
        batches = list(mig.walk_source(FakeListApi(tree), "src", None, list_workers=2))
        self.assertEqual(batches, [])


class RenameTargetTests(unittest.TestCase):
    def test_top_level_emoji_dir_sanitises(self):
        target = mig._rename_target(
            "鱼幼薇🐱", True, is_top_level=True,
            dir_to_sid={"鱼幼薇🐱": "sid-1"}, wopan_rejected_dirs=set(),
        )
        self.assertEqual(target, "鱼幼薇")

    def test_top_level_all_emoji_uses_sec_fallback(self):
        target = mig._rename_target(
            "🐱🍬", True, is_top_level=True,
            dir_to_sid={"🐱🍬": "sid-2"}, wopan_rejected_dirs=set(),
        )
        self.assertEqual(target, "sec_sid-2")

    def test_top_level_all_emoji_without_sec_returns_none(self):
        target = mig._rename_target(
            "🐱🍬", True, is_top_level=True,
            dir_to_sid={}, wopan_rejected_dirs=set(),
        )
        self.assertIsNone(target)

    def test_top_level_rejected_uses_fallback_even_if_cleaned(self):
        # 8005 内容审核 / 纯数学字母名：即使去 emoji 后非空也要走 sec 兜底绕过审核
        target = mig._rename_target(
            "纯情小妈", True, is_top_level=True,
            dir_to_sid={"纯情小妈": "sid-3"}, wopan_rejected_dirs={"纯情小妈"},
        )
        self.assertEqual(target, "sec_sid-3")

    def test_top_level_plain_unchanged(self):
        target = mig._rename_target(
            "plain", True, is_top_level=True,
            dir_to_sid={"plain": "sid-4"}, wopan_rejected_dirs=set(),
        )
        self.assertIsNone(target)

    def test_subtree_filename_sanitised(self):
        target = mig._rename_target(
            "video🍭.mp4", False, is_top_level=False,
            dir_to_sid={}, wopan_rejected_dirs=set(),
        )
        self.assertEqual(target, "video.mp4")

    def test_subtree_emoji_only_filename_returns_none(self):
        target = mig._rename_target(
            "🐱🍬", False, is_top_level=False,
            dir_to_sid={}, wopan_rejected_dirs=set(),
        )
        self.assertIsNone(target)

    def test_subtree_dir_sanitised(self):
        target = mig._rename_target(
            "标题🍬", True, is_top_level=False,
            dir_to_sid={}, wopan_rejected_dirs=set(),
        )
        self.assertEqual(target, "标题")


class BuildArchiveDirMapTests(unittest.TestCase):
    def test_maps_dir_name_to_sec_uid(self):
        users = [
            {"sec_user_id": "sid-1", "download_records": [
                {"files": ["鱼幼薇🐱/图片/1.jpg", "鱼幼薇🐱/视频/2.mp4"]}
            ]},
            {"sec_user_id": "sid-2", "download_records": [
                {"files": ["🐱🍬/x.mp4"]}
            ]},
        ]
        mapping = mig.build_archive_dir_to_sec_uid_map(users)
        self.assertEqual(mapping["鱼幼薇🐱"], "sid-1")
        self.assertEqual(mapping["🐱🍬"], "sid-2")

    def test_skips_users_without_sec_uid(self):
        users = [{"nickname": "无sid", "download_records": [{"files": ["无sid/1.mp4"]}]}]
        self.assertEqual(mig.build_archive_dir_to_sec_uid_map(users), {})


class MigrationStateRenamesTests(unittest.TestCase):
    def test_roundtrip_persists_renames(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            state = mig.MigrationState.load(path)
            state.mark_renamed("src/🐱", "src/sec_sid")
            state.save()
            reloaded = mig.MigrationState.load(path)
            self.assertEqual(reloaded.renames, {"src/🐱": "src/sec_sid"})

    def test_is_renamed_and_new_path_for(self):
        state = mig.MigrationState(path=Path("/tmp/x.json"))
        state.mark_renamed("a/b", "a/c")
        self.assertTrue(state.is_renamed("a/b"))
        self.assertFalse(state.is_renamed("a/x"))
        self.assertEqual(state.new_path_for("a/b"), "a/c")
        self.assertIsNone(state.new_path_for("a/x"))


class RenameTrackingApi:
    """记录 rename 调用并模拟同盘 rename：rename 后 tree 键同步更新，子路径跟随。"""

    def __init__(self, tree):
        self.tree = dict(tree)
        self.rename_calls = []
        self.batch_calls = []
        self.rename_fail = set()
        self.batch_fail = set()

    def list_dir(self, path, refresh=False):
        return self.tree.get(path)

    def rename_one(self, path, new_name):
        self.rename_calls.append((path, new_name))
        if path in self.rename_fail:
            return False
        parent, _, _ = path.rpartition("/")
        new_path = f"{parent}/{new_name}" if parent else new_name
        self._relocate(path, new_path)
        return True

    def batch_rename(self, src_dir, rename_objects, *, retry_sync=False):
        self.batch_calls.append((src_dir, [dict(o) for o in rename_objects], retry_sync))
        if src_dir in self.batch_fail:
            return False
        for obj in rename_objects:
            old = f"{src_dir}/{obj['src_name']}"
            new = f"{src_dir}/{obj['new_name']}"
            if old in self.tree:  # 是目录，子路径跟随
                self._relocate(old, new)
        return True

    def _relocate(self, old, new):
        if old in self.tree:
            self.tree[new] = self.tree.pop(old)
        prefix = old + "/"
        for key in list(self.tree.keys()):
            if key.startswith(prefix):
                self.tree[new + key[len(old):]] = self.tree.pop(key)


RENAME_TREE = {
    "src": [
        {"name": "鱼幼薇🐱", "is_dir": True},
        {"name": "🐱🍬", "is_dir": True},
        {"name": "plain", "is_dir": True},
        {"name": "stable", "is_dir": True},
    ],
    "src/鱼幼薇🐱": [
        {"name": "标题🍬", "is_dir": True},
        {"name": "video🍭.mp4", "size": 1, "is_dir": False},
    ],
    "src/鱼幼薇🐱/标题🍬": [{"name": "1️⃣.jpg", "size": 2, "is_dir": False}],
    "src/🐱🍬": [{"name": "x.mp4", "size": 3, "is_dir": False}],
    "src/plain": [{"name": "keep.mp4", "size": 4, "is_dir": False}],
    "src/stable": [{"name": "f🍭.mp4", "size": 5, "is_dir": False}],
}


class RenamePassTests(unittest.TestCase):
    _DIR_TO_SID = {"鱼幼薇🐱": "sid-1", "🐱🍬": "sid-2", "plain": "sid-3", "stable": "sid-4"}

    def _run(self, tree=RENAME_TREE, rejected=None, **kw):
        state = mig.MigrationState.load(Path(self._tmp) / "state.json")
        api = RenameTrackingApi(tree)
        totals = mig.Totals()
        mig.run_rename_pass(
            api, state, "src", self._DIR_TO_SID, rejected or set(),
            totals=totals, **kw,
        )
        return api, state

    def setUp(self):
        import tempfile
        self._tmp = tempfile.mkdtemp()

    def test_renames_top_level_emoji_and_all_emoji(self):
        api, state = self._run(only=None, dry_run=False)
        self.assertIn(("src/鱼幼薇🐱", "鱼幼薇"), api.rename_calls)
        self.assertIn(("src/🐱🍬", "sec_sid-2"), api.rename_calls)
        self.assertNotIn(("src/plain", "plain"), api.rename_calls)
        self.assertEqual(state.renames["src/鱼幼薇🐱"], "src/鱼幼薇")
        self.assertEqual(state.renames["src/🐱🍬"], "src/sec_sid-2")

    def test_subtree_batch_rename_payload(self):
        api, _ = self._run(only=None, dry_run=False)
        batch = next(b for d, b, _ in api.batch_calls if d == "src/鱼幼薇")
        self.assertIn({"src_name": "标题🍬", "new_name": "标题"}, batch)
        self.assertIn({"src_name": "video🍭.mp4", "new_name": "video.mp4"}, batch)
        batch2 = next(b for d, b, _ in api.batch_calls if d == "src/鱼幼薇/标题")
        self.assertEqual(batch2, [{"src_name": "1️⃣.jpg", "new_name": "1.jpg"}])

    def test_subtree_follows_renamed_parent_path(self):
        # 自顶向下：父改名后子树用新路径 src/鱼幼薇，不持旧父路径 src/鱼幼薇🐱
        api, _ = self._run(only=None, dry_run=False)
        self.assertTrue(any(d == "src/鱼幼薇" for d, _, _ in api.batch_calls))
        self.assertFalse(any(d == "src/鱼幼薇🐱" for d, _, _ in api.batch_calls))

    def test_retry_sync_flag_distinguishes_renamed_and_stable(self):
        # 顶层改名后的深度1 batch 在新路径 src/鱼幼薇 → retry_sync=True（等同步）
        # 顶层未改名的 stable 子树 batch 在稳定路径 src/stable → retry_sync=False
        api, _ = self._run(only=None, dry_run=False)
        renamed = {d: rs for d, _, rs in api.batch_calls}
        # src/鱼幼薇 是改名后新路径（rename_one 把 鱼幼薇🐱→鱼幼薇），需等同步
        self.assertTrue(renamed["src/鱼幼薇"])
        # src/鱼幼薇/标题 是刚改名的子目录（标题🍬→标题），也需等同步
        self.assertTrue(renamed["src/鱼幼薇/标题"])
        # src/stable 顶层未改名，父路径稳定，不重试同步（走逐条 rename_one 回退）
        self.assertFalse(renamed["src/stable"])

    def test_dry_run_makes_no_api_calls(self):
        api, state = self._run(only=None, dry_run=True)
        self.assertEqual(api.rename_calls, [])
        self.assertEqual(api.batch_calls, [])
        self.assertEqual(state.renames, {})

    def test_top_level_rename_failure_skips_subtree(self):
        tree = dict(RENAME_TREE)
        api = RenameTrackingApi(tree)
        api.rename_fail.add("src/鱼幼薇🐱")
        state = mig.MigrationState.load(Path(self._tmp) / "state.json")
        totals = mig.Totals()
        mig.run_rename_pass(
            api, state, "src", self._DIR_TO_SID, set(),
            only=None, dry_run=False, totals=totals,
        )
        self.assertFalse(any(d == "src/鱼幼薇" for d, _, _ in api.batch_calls))
        self.assertNotIn("src/鱼幼薇🐱", state.renames)

    def test_batch_rename_failure_skips_subtree(self):
        tree = dict(RENAME_TREE)
        api = RenameTrackingApi(tree)
        api.batch_fail.add("src/鱼幼薇")
        state = mig.MigrationState.load(Path(self._tmp) / "state.json")
        totals = mig.Totals()
        mig.run_rename_pass(
            api, state, "src", self._DIR_TO_SID, set(),
            only=None, dry_run=False, totals=totals,
        )
        self.assertFalse(any(d == "src/鱼幼薇/标题" for d, _, _ in api.batch_calls))


class RollbackTests(unittest.TestCase):
    def test_rollback_reverses_child_before_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = mig.MigrationState.load(Path(tmp) / "state.json")
            state.mark_renamed("src/🐱", "src/sec_sid")
            state.mark_renamed("src/sec_sid/标题🍬", "src/sec_sid/标题")
            api = RenameTrackingApi({})
            totals = mig.Totals()
            mig.rollback_renames(api, state, dry_run=False, totals=totals)
            calls = api.rename_calls
            # 子先父后：先回滚 sec_sid/标题→标题🍬，再回滚 sec_sid→🐱
            self.assertEqual(calls[0], ("src/sec_sid/标题", "标题🍬"))
            self.assertEqual(calls[1], ("src/sec_sid", "🐱"))
            self.assertEqual(state.renames, {})

    def test_rollback_empty_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = mig.MigrationState.load(Path(tmp) / "state.json")
            api = RenameTrackingApi({})
            totals = mig.Totals()
            rc = mig.rollback_renames(api, state, dry_run=False, totals=totals)
            self.assertEqual(rc, 0)
            self.assertEqual(api.rename_calls, [])

    def test_rollback_dry_run_keeps_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = mig.MigrationState.load(Path(tmp) / "state.json")
            state.mark_renamed("src/🐱", "src/sec_sid")
            api = RenameTrackingApi({})
            totals = mig.Totals()
            mig.rollback_renames(api, state, dry_run=True, totals=totals)
            self.assertEqual(api.rename_calls, [])
            self.assertEqual(state.renames, {"src/🐱": "src/sec_sid"})


class RenameParserTests(unittest.TestCase):
    def test_rename_flags_default_false(self):
        args = mig.build_parser().parse_args([])
        self.assertFalse(args.rename_source)
        self.assertFalse(args.rollback_renames)

    def test_rename_source_can_be_set(self):
        args = mig.build_parser().parse_args(["--rename-source"])
        self.assertTrue(args.rename_source)


if __name__ == "__main__":
    unittest.main()
