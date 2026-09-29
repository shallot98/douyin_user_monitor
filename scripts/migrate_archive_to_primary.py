#!/usr/bin/env python3
"""把备份盘（夸克）上的抖音归档复制到主力盘（wopan）。

复制走 OpenList 的服务端 copy，字节不经过本机——比「下载再上传」省一半流量。

安全性：**只复制，绝不删除**，对备份盘是只读操作。任何一步失败都只是跳过该文件，
下次续跑重试；状态文件记录已确认完成的文件，可以随时中断。

主力盘（wopan）拒绝含 emoji 的目录名，所以目标目录名会去 emoji——与 mover 的
PRIMARY_SANITIZE_EMOJI 和网页侧 archive.sanitize_primary_dirs 用的是同一个函数。

用法:
    scripts/migrate_archive_to_primary.py --dry-run            # 只看计划
    scripts/migrate_archive_to_primary.py                      # 全量续跑
    scripts/migrate_archive_to_primary.py --only 鱼幼薇        # 只迁指定用户目录
    scripts/migrate_archive_to_primary.py --max-files 200      # 本次只处理 200 个文件
    scripts/migrate_archive_to_primary.py --workers 3          # 3 个目录并发处理
    scripts/migrate_archive_to_primary.py --walk-workers 6     # 6 线程并发扫描目录树
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from douyin_user_monitor.monitor.nickname_history import derive_archive_dir_names  # noqa: E402
from douyin_user_monitor.monitor.record_store import load_state_with_records  # noqa: E402
from douyin_user_monitor.monitor.openlist_client import OpenListClient  # noqa: E402
from douyin_user_monitor.monitor.path_sanitize import (  # noqa: E402
    fallback_dir_for,
    sanitize_component,
    sanitize_relative_dir,
)
from douyin_user_monitor.settings_loaders import load_settings  # noqa: E402

DEFAULT_STATE_PATH = REPO_ROOT / "runtime" / "archive-migration.json"
LIST_PAGE_SIZE = 1000
# wopan 对不兼容路径回的是误导性的「登录失败」，单独识别以免下次又照搬去查认证
WOPAN_PATH_ERROR_MARKER = "8005"
# 单请求总时限兜底：urlopen 的 timeout 是单次 recv，OpenList 对大目录 refresh 慢流式
# 响应时每段字节都重置计时器，能把单请求拖到十分钟级卡死长跑。daemon 线程 + join
# 这个总时限，超了就放弃（调用方拿 code -1 当失败跳过该子树，夸克留原名不丢数据）。
_REQUEST_HARD_DEADLINE = 120
# rename_one / 上一层 batch_rename 返回成功后，OpenList 的目录改名是异步同步的——
# 紧接着对**新路径**做 batch_rename 会撞「object not found」（实测同步 11~60s+，随
# 目录规模）。在 _RENAME_SYNC_WINDOW 秒内轮询重试等同步就绪；不靠 refresh=true
# 强刷（那会在大用户目录上 wedge）。_REQUEST_HARD_DEADLINE 兜底单次请求不会被
# 慢流式无限拖死。
_RENAME_SYNC_WINDOW = 90
_RENAME_SYNC_DELAY = 3


def log(message: str) -> None:
    print(f"[migrate] {time.strftime('%H:%M:%S')} {message}", flush=True)


def describe_error(message: str) -> str:
    """把 wopan 的误导性错误翻译成真实原因。"""
    text = str(message or "")
    if WOPAN_PATH_ERROR_MARKER in text and "登录失败" in text:
        return f"疑似路径不兼容（wopan 拒绝该目录名，报的却是登录失败）: {text[:160]}"
    return text[:200]


class OpenListSync:
    """同步版最小 OpenList 客户端（迁移脚本是长跑的批处理，不需要异步）。"""

    def __init__(self, base_url: str, token: str, timeout: float):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def _request(self, path: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """发请求并解析 JSON。

        urlopen 的 ``timeout`` 是单次 recv 超时——OpenList 对大目录 ``refresh=true`` 慢流式
        响应时每收到一段字节就重置计时器，能把单请求拖到十分钟级卡死长跑。这里用 daemon
        线程 + ``_REQUEST_HARD_DEADLINE`` 总 deadline 兜底：超限就放弃，调用方拿到
        ``code -1`` 当失败跳过该子树（夸克留原名不丢数据）。
        """
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=data,
            headers={"Authorization": self._token, "Content-Type": "application/json"},
        )
        box: Dict[str, Any] = {}

        def _worker() -> None:
            try:
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    box["data"] = json.load(response)
            except urllib.error.HTTPError as exc:  # noqa: BLE001
                box["data"] = {"code": exc.code, "message": f"HTTP {exc.code}"}
            except Exception as exc:  # noqa: BLE001 — 网络抖动不该中断长跑
                box["data"] = {"code": -1, "message": f"{type(exc).__name__}: {exc}"}

        worker = threading.Thread(target=_worker, daemon=True)
        worker.start()
        worker.join(_REQUEST_HARD_DEADLINE)
        if worker.is_alive():
            log(f"  请求超 {_REQUEST_HARD_DEADLINE}s 硬限，放弃: {path}")
            return {"code": -1, "message": f"hard timeout {_REQUEST_HARD_DEADLINE}s"}
        return box.get("data") or {"code": -1, "message": "no response"}

    def list_dir(self, path: str, *, refresh: bool = False) -> Optional[List[Dict[str, Any]]]:
        """返回目录条目；None 表示查询失败（与「空目录」区分开）。"""
        entries: List[Dict[str, Any]] = []
        page = 1
        while True:
            payload = {
                "path": path,
                "page": page,
                "per_page": LIST_PAGE_SIZE,
                "refresh": refresh,
            }
            response = self._request("/api/fs/list", payload)
            if response.get("code") != 200:
                message = str(response.get("message") or "")
                # 目录不存在是正常情况（目标端还没建），当成空目录
                if "not found" in message.lower() or "object not found" in message.lower():
                    return []
                log(f"  列目录失败 {path}: {describe_error(message)}")
                return None
            data = response.get("data") or {}
            content = data.get("content") or []
            entries.extend(content)
            total = data.get("total")
            if not content or not isinstance(total, int) or page * LIST_PAGE_SIZE >= total:
                return entries
            page += 1

    def mkdir(self, path: str) -> bool:
        response = self._request("/api/fs/mkdir", {"path": path})
        if response.get("code") == 200:
            return True
        log(f"  建目录失败 {path}: {describe_error(response.get('message'))}")
        return False

    def copy(self, src_dir: str, dst_dir: str, names: List[str]) -> bool:
        payload = {"src_dir": src_dir, "dst_dir": dst_dir, "names": names}
        response = self._request("/api/fs/copy", payload)
        if response.get("code") == 200:
            return True
        log(f"  提交复制失败 {src_dir} -> {dst_dir}: {describe_error(response.get('message'))}")
        return False

    def undone_copy_tasks(self) -> int:
        response = self._request("/api/task/copy/undone")
        data = response.get("data")
        return len(data) if isinstance(data, list) else 0

    def rename_one(self, path: str, new_name: str) -> bool:
        """同盘原子改名（目录改名后子路径自动跟随）；失败返 False 不中断长跑。"""
        response = self._request("/api/fs/rename", {"path": path, "name": new_name, "overwrite": False})
        if response.get("code") == 200:
            return True
        log(f"  改名失败 {path} -> {new_name}: {describe_error(response.get('message'))}")
        return False

    def batch_rename(
        self,
        src_dir: str,
        rename_objects: List[Dict[str, str]],
        *,
        retry_sync: bool = False,
    ) -> bool:
        """一个目录下批量改名（{src_name, new_name}）。

        两种「object not found」要分开处理：

        - **改名后新路径**（``retry_sync=True``）：OpenList 改名是异步同步的，立即
          对新路径 batch_rename 会撞 ``object not found``（实测同步 11~60s+，随目录
          规模）。在 ``_RENAME_SYNC_WINDOW`` 内轮询重试等同步就绪。
        - **稳定路径**（``retry_sync=False``，父目录本 run 未改名）：这里的
          ``object not found`` 是永久性 item 缺失——batch_rename 对某些异字符
          src_name（实测 emoji / ᗜ 等）解析不了，但**单条 rename_one 能处理**。
          逐条 rename_one 回退，不空等同步窗口。

        撞同名等其它错误立刻失败跳过该子树（夸克留原名不丢数据）。单次请求受
        ``_REQUEST_HARD_DEADLINE`` 兜底，不会被慢响应无限拖死。
        """
        payload = {"src_dir": src_dir, "rename_objects": rename_objects}
        deadline = time.time() + _RENAME_SYNC_WINDOW
        message = ""
        while True:
            response = self._request("/api/fs/batch_rename", payload)
            if response.get("code") == 200:
                return True
            message = str(response.get("message") or "")
            if "not found" not in message.lower():
                break  # 撞同名 / 权限等，不重试不回退
            if not retry_sync:
                break  # 稳定路径上的 not found = 永久性，去逐条回退
            if time.time() >= deadline:
                break  # 同步窗口用尽，去逐条回退兜底
            time.sleep(_RENAME_SYNC_DELAY)
        # 逐条 rename_one 回退：batch_rename 对某些异字符 src_name 解析失败，单条
        # rename_one 实测能处理（emoji / ᗜ / 💢 等）。撞同名等非 not-found 错不回退。
        if "not found" in message.lower():
            log(f"  batch 碰到异字符，逐条 rename_one 回退: {src_dir}")
            ok_any = False
            for obj in rename_objects:
                if self.rename_one(f"{src_dir}/{obj['src_name']}", obj["new_name"]):
                    ok_any = True
                else:
                    log(f"    逐条改名失败: {obj['src_name']}")
            return ok_any
        log(f"  批量改名失败 {src_dir}: {describe_error(message)}")
        return False


@dataclass
class RenameOp:
    """单条改名计划：``parent_dir`` 下的 ``src_name`` → ``new_name``。"""

    parent_dir: str
    src_name: str
    new_name: str
    is_dir: bool


@dataclass
class MigrationState:
    """已确认完成的文件与失败原因；支持随时中断续跑。

    多 worker 并发处理目录时也安全：所有读写都走同一把锁。
    ``renames`` 记录源端改名 pass 的 old→new 映射，供 ``--rollback-renames`` 反向回滚。
    """

    path: Path
    done: Dict[str, int] = field(default_factory=dict)
    failed: Dict[str, str] = field(default_factory=dict)
    renames: Dict[str, str] = field(default_factory=dict)
    lock: Any = field(default_factory=threading.Lock, repr=False, compare=False)

    @classmethod
    def load(cls, path: Path) -> "MigrationState":
        if not path.is_file():
            return cls(path=path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log(f"状态文件损坏，从零开始: {path}")
            return cls(path=path)
        return cls(
            path=path,
            done=dict(raw.get("done") or {}),
            failed=dict(raw.get("failed") or {}),
            renames=dict(raw.get("renames") or {}),
        )

    def save(self) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            payload = {
                "done": self.done,
                "failed": self.failed,
                "renames": self.renames,
                "updated_at": time.time(),
            }
            temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temp.replace(self.path)

    def mark_done(self, relative_path: str, size: int) -> None:
        with self.lock:
            self.done[relative_path] = size
            self.failed.pop(relative_path, None)

    def mark_failed(self, relative_path: str, reason: str) -> None:
        with self.lock:
            self.failed[relative_path] = reason[:200]

    def is_done(self, relative_path: str, size: int) -> bool:
        with self.lock:
            recorded = self.done.get(relative_path)
            return recorded is not None and recorded == size

    def is_failed(self, relative_path: str) -> bool:
        with self.lock:
            return relative_path in self.failed

    def mark_renamed(self, old_path: str, new_path: str) -> None:
        with self.lock:
            self.renames[old_path] = new_path

    def is_renamed(self, old_path: str) -> bool:
        with self.lock:
            return old_path in self.renames

    def new_path_for(self, old_path: str) -> Optional[str]:
        with self.lock:
            return self.renames.get(old_path)


@dataclass
class Totals:
    scanned_files: int = 0
    already_present: int = 0
    copied: int = 0
    failed: int = 0
    skipped_unsanitisable: int = 0
    emoji_rejected: int = 0
    renamed_dirs: int = 0
    renamed_files: int = 0
    copied_bytes: int = 0
    lock: Any = field(default_factory=threading.Lock, repr=False, compare=False)


def walk_source(
    api: OpenListSync, base: str, only: Optional[str], list_workers: int = 1
) -> Iterator[Tuple[str, List[Tuple[str, int]]]]:
    """深度优先遍历备份盘，产出 (相对目录, [(文件名, 大小), ...]) 的完整目录批次。

    懒遍历：边走边处理，中断后靠状态文件续跑，不必先枚举 5 万个文件。
    目录批次保持原子——一个目录的文件不会拆进两批。list_workers>1 时并发
    列目录（大部分请求会命中 OpenList 的目录缓存，不会放大夸克侧压力），
    目录产出顺序从严格 DFS 变成近似，批次本身仍完整。
    """
    top = api.list_dir(base)
    if top is None:
        return
    top_dirs = sorted(entry["name"] for entry in top if entry.get("is_dir"))
    if only:
        top_dirs = [name for name in top_dirs if only in name]
        log(f"按 --only 过滤后剩 {len(top_dirs)} 个用户目录")

    dirs_to_list: "queue.Queue[Optional[str]]" = queue.Queue()
    listings: "queue.Queue[Tuple[str, Optional[List[Dict[str, Any]]]]]" = queue.Queue()

    def lister() -> None:
        while True:
            relative_dir = dirs_to_list.get()
            try:
                if relative_dir is None:
                    return
                listings.put((relative_dir, api.list_dir(f"{base}/{relative_dir}")))
            except Exception as exc:  # noqa: BLE001 — 单目录列表失败不拖死遍历
                listings.put((relative_dir, None))
                log(f"  列目录异常 {relative_dir}: {exc}")
            finally:
                dirs_to_list.task_done()

    lister_threads = []
    for _ in range(max(1, list_workers)):
        thread = threading.Thread(target=lister, daemon=True)
        thread.start()
        lister_threads.append(thread)

    try:
        outstanding = 0
        for name in reversed(top_dirs):
            dirs_to_list.put(name)
            outstanding += 1
        while outstanding > 0:
            relative_dir, entries = listings.get()
            outstanding -= 1
            if entries is None:
                continue
            subdirs: List[str] = []
            files: List[Tuple[str, int]] = []
            for entry in entries:
                name = str(entry.get("name") or "")
                if not name:
                    continue
                if entry.get("is_dir"):
                    subdirs.append(f"{relative_dir}/{name}")
                else:
                    files.append((name, int(entry.get("size") or 0)))
            for subdir in subdirs:
                dirs_to_list.put(subdir)
                outstanding += 1
            if files:
                yield relative_dir, files
    finally:
        for _ in lister_threads:
            dirs_to_list.put(None)


def primary_dir_for(relative_dir: str, *, sanitize: bool) -> Optional[str]:
    """主力盘上的目标相对目录；None 表示去 emoji 后为空、无法落地。"""
    if not sanitize:
        return relative_dir
    try:
        return sanitize_relative_dir(relative_dir)
    except ValueError:
        return None


def _rename_target(
    name: str,
    is_dir: bool,
    *,
    is_top_level: bool,
    dir_to_sid: Dict[str, str],
    wopan_rejected_dirs: set,
) -> Optional[str]:
    """算改名目标名；None 表示不改名（已合规）或无法救（全空文件名）。

    顶层用户目录：去 emoji 后空、或在 wopan 拒收名单里 → ``fallback_dir_for(sec_user_id)``
    兜底（sec_user_id 缺失则返 None 跳过）；去 emoji 后非空≠原名 → 用清理版。
    子项：去 emoji 后空 → 文件返 None（占位防 panic，实测下载器文件名恒带时间戳前缀
    不可达）；去 emoji 后≠原名 → 用清理版。撞名目录（椰椰蛋卷×2）去 emoji 后非空==
    原名→不改名→两目录各自原名共存，本方案不引入新冲突。
    """
    cleaned = sanitize_component(name)
    if is_top_level and is_dir:
        sid = dir_to_sid.get(name)
        if not cleaned or name in wopan_rejected_dirs:
            return fallback_dir_for(sid) if sid else None
        return cleaned if cleaned != name else None
    if not cleaned:
        return None
    return cleaned if cleaned != name else None


def _wopan_rejected_top_dirs(state: MigrationState) -> set:
    """从失败记录抽被 wopan 拒收的顶层目录名（8005 内容审核 / 目录创建失败 / 全 emoji）。

    复用 copy pass 的失败原因，零额外探针：这些目录即使去 emoji 也会被 wopan 拒，
    所以 rename pass 要把它们改成 ``sec_<sid>`` 兜底名绕过审核。
    """
    rejected: set = set()
    for path, reason in state.failed.items():
        reason_text = str(reason or "")
        if (
            "8005" in reason_text
            or "目录创建失败" in reason_text
            or "去 emoji 后为空" in reason_text
            or "目录名全是 emoji" in reason_text
        ):
            top = str(path).split("/", 1)[0]
            if top:
                rejected.add(top)
    return rejected


def build_archive_dir_to_sec_uid_map(users: List[Dict[str, Any]]) -> Dict[str, str]:
    """从 DB 用户表建「归档目录名 → sec_user_id」映射。

    用 ``derive_archive_dir_names`` 从 ``download_records[].files`` 顶层目录反推——
    这才是网盘上真实存在的目录名，且 11 个全 emoji 目录各自唯一可定位。
    当前 ``nickname`` 不可靠（撞名 + 用户改名后失配），不能用来反查。
    """
    mapping: Dict[str, str] = {}
    for user in users:
        sid = str(user.get("sec_user_id") or "").strip()
        if not sid:
            continue
        for dir_name in derive_archive_dir_names(user):
            if dir_name and dir_name not in mapping:
                mapping[dir_name] = sid
    return mapping


def _translate_path(path: str, applied: Dict[str, str]) -> str:
    """把原路径按已应用的改名映射翻译成当前路径。

    ``applied`` 键=原完整目录路径、值=新完整目录路径。从最长原前缀往短找，命中即
    用新前缀替换、拼上剩余段——保证多层嵌套改名（顶层 + 子目录各自改名）都能正确
    翻译到当前路径，且未改名的中段目录保留原名。
    """
    if not path or not applied:
        return path
    segs = path.split("/")
    for i in range(len(segs), 0, -1):
        prefix = "/".join(segs[:i])
        if prefix in applied:
            new_prefix = applied[prefix]
            rest = "/".join(segs[i:])
            return f"{new_prefix}/{rest}" if rest else new_prefix
    return path


def _collect_subtree_ops(
    api: OpenListSync,
    root_path: str,
    dir_to_sid: Dict[str, str],
    wopan_rejected_dirs: set,
) -> List[RenameOp]:
    """在原路径（warm 服务端缓存）递归收齐 ``root_path`` 子树所有改名项。

    收集阶段只读不改名，全程 ``refresh=False`` 命中缓存（原路径在 copy pass 里已列过）；
    冷目录回退一次 ``refresh=True``（受 ``_REQUEST_HARD_DEADLINE`` 兜底）。返回扁平
    ``RenameOp`` 列表，``parent_dir`` 是原路径——execute 阶段用累积改名映射翻译到
    当前路径再 ``batch_rename``，无需对改名后的新路径再 list（避开 cold 缓存 wedge）。
    """
    ops: List[RenameOp] = []
    stack: List[str] = [root_path]
    while stack:
        cur = stack.pop()
        entries = api.list_dir(cur, refresh=False)
        if entries is None:
            entries = api.list_dir(cur, refresh=True)
        if entries is None:
            log(f"  列目录失败，跳过子树: {cur}")
            continue
        for entry in entries:
            name = str(entry.get("name") or "")
            if not name:
                continue
            is_dir = bool(entry.get("is_dir"))
            target = _rename_target(
                name,
                is_dir,
                is_top_level=False,
                dir_to_sid=dir_to_sid,
                wopan_rejected_dirs=wopan_rejected_dirs,
            )
            if target:
                ops.append(RenameOp(cur, name, target, is_dir))
            if is_dir:
                stack.append(f"{cur}/{name}")
    return ops


def _execute_rename_plan(
    api: OpenListSync,
    state: MigrationState,
    top_op: Optional[RenameOp],
    sub_ops: List[RenameOp],
    *,
    dry_run: bool,
    totals: Totals,
) -> None:
    """执行收齐的改名计划：顶层 ``rename_one`` + 各深度 ``batch_rename``。

    顶层改名失败跳过整个用户子树（夸克留原名，copy pass 备份-orig 兜底不 404）。
    子项按 ``parent_dir`` 深度升序逐层 ``batch_rename``：父层先改名并记入 ``applied``，
    子层翻译 parent 时用上，指向改名后的新目录；``batch_rename`` 失败则把该目录加入
    ``failed_prefixes``，更深层落在其下的批次一并跳过（夸克留原名不丢数据）。
    """
    applied: Dict[str, str] = {}  # 原完整目录路径 -> 新完整目录路径
    failed_prefixes: List[str] = []
    # 1. 顶层用户目录改名
    if top_op:
        old_full = f"{top_op.parent_dir}/{top_op.src_name}"
        new_full = f"{top_op.parent_dir}/{top_op.new_name}"
        if dry_run:
            log(f"[dry-run] 改名顶层目录 {old_full} -> {new_full}")
            with totals.lock:
                totals.renamed_dirs += 1
        elif api.rename_one(old_full, top_op.new_name):
            state.mark_renamed(old_full, new_full)
            applied[old_full] = new_full
            with totals.lock:
                totals.renamed_dirs += 1
        else:
            log(f"  顶层目录改名失败，跳过子树: {top_op.src_name}")
            return
    # 2. 子项按深度升序逐层 batch_rename
    sub_ops_sorted = sorted(sub_ops, key=lambda o: o.parent_dir.count("/"))
    depth = lambda o: o.parent_dir.count("/")  # noqa: E731
    pos = 0
    n = len(sub_ops_sorted)
    while pos < n:
        cur_depth = depth(sub_ops_sorted[pos])
        # 取出同一深度的整段
        group: List[RenameOp] = []
        while pos < n and depth(sub_ops_sorted[pos]) == cur_depth:
            group.append(sub_ops_sorted[pos])
            pos += 1
        # 按翻译后的当前 parent 分组 batch（保持首次出现顺序）
        batches: Dict[str, List[RenameOp]] = {}
        order: List[str] = []
        for op in group:
            cur_parent = _translate_path(op.parent_dir, applied)
            if cur_parent not in batches:
                batches[cur_parent] = []
                order.append(cur_parent)
            batches[cur_parent].append(op)
        for cur_parent in order:
            if any(
                cur_parent == fp or cur_parent.startswith(f"{fp}/")
                for fp in failed_prefixes
            ):
                continue  # 上层 batch_rename 失败，该子树整体跳过
            ops_in = batches[cur_parent]
            if dry_run:
                for op in ops_in:
                    log(f"  [dry-run] 改名 {cur_parent}/{op.src_name} -> {op.new_name}")
                with totals.lock:
                    for op in ops_in:
                        if op.is_dir:
                            totals.renamed_dirs += 1
                        else:
                            totals.renamed_files += 1
            else:
                payload = [
                    {"src_name": op.src_name, "new_name": op.new_name} for op in ops_in
                ]
                # cur_parent 是本 run 刚改名的目录（在 applied 值里）→ 新路径需等
                # OpenList 异步同步；否则是稳定路径，object not found 走逐条回退。
                retry_sync = cur_parent in applied.values()
                if api.batch_rename(cur_parent, payload, retry_sync=retry_sync):
                    with totals.lock:
                        for op in ops_in:
                            if op.is_dir:
                                totals.renamed_dirs += 1
                            else:
                                totals.renamed_files += 1
                    # 记入 applied 供更深层翻译（键=原完整路径，值=新完整路径）
                    for op in ops_in:
                        if op.is_dir:
                            old_full = f"{op.parent_dir}/{op.src_name}"
                            new_full = f"{cur_parent}/{op.new_name}"
                            state.mark_renamed(old_full, new_full)
                            applied[old_full] = new_full
                else:
                    log(f"  批量改名失败，跳过子树: {cur_parent}")
                    failed_prefixes.append(cur_parent)


def run_rename_pass(
    api: OpenListSync,
    state: MigrationState,
    src_base: str,
    dir_to_sid: Dict[str, str],
    wopan_rejected_dirs: set,
    only: Optional[str],
    dry_run: bool,
    totals: Totals,
) -> None:
    """顶层入口：list 源端用户目录 → 收齐子树改名项 → 执行（顶层 rename_one + 各层 batch_rename）。

    collect-then-execute：先把每个用户目录的整棵子树在原路径（warm 缓存）一次性收齐，
    再按深度升序执行；execute 阶段零 list，不对改名后的新路径再列目录，避开 OpenList
    对 cold 缓存目录 ``refresh=true`` 慢流式响应的 wedge。顶层改名失败则跳过该用户子树
    （夸克留原名，copy pass 的备份-orig 兜底不 404）。
    """
    top = api.list_dir(src_base, refresh=False)
    if top is None:
        top = api.list_dir(src_base, refresh=True)
    if top is None:
        log("列源端顶层目录失败")
        return
    top_dirs = sorted(str(e["name"]) for e in top if e.get("is_dir"))
    if only:
        top_dirs = [n for n in top_dirs if only in n]
        log(f"按 --only 过滤后剩 {len(top_dirs)} 个用户目录")
    for name in top_dirs:
        top_target = _rename_target(
            name,
            is_dir=True,
            is_top_level=True,
            dir_to_sid=dir_to_sid,
            wopan_rejected_dirs=wopan_rejected_dirs,
        )
        top_op = RenameOp(src_base, name, top_target, True) if top_target else None
        # 收齐子树（原路径 warm 缓存）再执行；改名失败/无改名项时 _execute_rename_plan 内部短路
        sub_ops = _collect_subtree_ops(
            api, f"{src_base}/{name}", dir_to_sid, wopan_rejected_dirs
        )
        _execute_rename_plan(
            api, state, top_op, sub_ops, dry_run=dry_run, totals=totals
        )
    if not dry_run:
        state.save()


def rollback_renames(
    api: OpenListSync, state: MigrationState, dry_run: bool, totals: Totals
) -> int:
    """反向 rename：把 ``state.renames`` 的 new 路径改回 old basename。

    按路径深度降序（子先父后）：文件名先回滚，目录名后回滚（目录改名后子路径自动跟随，
    父目录回滚时子路径已自然回旧）。best-effort：若父目录已先回滚导致子路径失效，
    rename_one 失败跳过并 log，不中断。
    """
    items = sorted(
        state.renames.items(), key=lambda kv: kv[0].count("/"), reverse=True
    )
    if not items:
        log("没有 rename 记录可回滚")
        return 0
    log(f"回滚 {len(items)} 条 rename 记录（子先父后，best-effort）")
    rolled = 0
    for old_full, new_full in items:
        old_name = old_full.rsplit("/", 1)[-1]
        if dry_run:
            log(f"  [dry-run] 回滚 {new_full} -> {old_name}")
        elif api.rename_one(new_full, old_name):
            rolled += 1
        else:
            log(f"  回滚失败（可能父目录已回滚致路径失效）: {new_full}")
    if not dry_run:
        state.renames.clear()
        state.save()
        log(f"已回滚 {rolled}/{len(items)} 条，清空 rename 记录")
    return 0


def _load_users(state_path: Path) -> List[Dict[str, Any]]:
    """读 DB 用户表（只读，用于建目录→sec_user_id 映射）。"""
    if not state_path.is_file():
        log(f"DB 用户表不存在: {state_path}，无法建 sec_user_id 映射（全 emoji 目录将无法兜底）")
        return []
    try:
        raw = load_state_with_records(state_path)
    except (ValueError, OSError) as exc:
        log(f"读 DB 用户表失败: {exc}")
        return []
    users = raw.get("users") if isinstance(raw, dict) else raw
    return users if isinstance(users, list) else []


def wait_for_capacity(api: OpenListSync, max_undone: int) -> None:
    """OpenList 的 copy 任务队列不宜堆太多，堆满就等它消化。"""
    while True:
        undone = api.undone_copy_tasks()
        if undone < max_undone:
            return
        log(f"  复制队列已有 {undone} 个任务，等待消化…")
        time.sleep(15)


def verify_batch(
    api: OpenListSync,
    dst_dir: str,
    expected: Dict[str, int],
    *,
    tolerance: int,
    attempts: int,
    interval: float,
) -> Dict[str, int]:
    """轮询目标目录，返回已确认（名称+大小都对）的文件。"""
    confirmed: Dict[str, int] = {}
    for _ in range(attempts):
        entries = api.list_dir(dst_dir, refresh=True)
        if entries is not None:
            present = {
                str(entry.get("name")): int(entry.get("size") or 0)
                for entry in entries
                if not entry.get("is_dir")
            }
            for name, size in expected.items():
                if name in confirmed:
                    continue
                remote_size = present.get(name)
                if remote_size is None:
                    continue
                if size <= 0 or abs(remote_size - size) <= tolerance:
                    confirmed[name] = remote_size
            if len(confirmed) == len(expected):
                return confirmed
        time.sleep(interval)
    return confirmed


def format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def process_directory(
    api: OpenListSync,
    *,
    args: argparse.Namespace,
    state: MigrationState,
    totals: Totals,
    src_base: str,
    dst_base: str,
    relative_dir: str,
    files: List[Tuple[str, int]],
    sanitize: bool,
) -> None:
    """处理一个源目录下的一批文件。"""
    primary_dir = primary_dir_for(relative_dir, sanitize=sanitize)
    if primary_dir is None:
        for name, _size in files:
            state.mark_failed(f"{relative_dir}/{name}", "目录名去 emoji 后为空，主力盘无法落地")
        with totals.lock:
            totals.skipped_unsanitisable += len(files)
        log(f"  跳过（目录名全是 emoji）: {relative_dir}")
        return

    src_dir = f"{src_base}/{relative_dir}"
    dst_dir = f"{dst_base}/{primary_dir}"

    # 目标端已有的先认掉，避免重复复制（mover 新搬的文件也会命中这里）
    existing_entries = api.list_dir(dst_dir)
    existing = (
        {
            str(entry.get("name")): int(entry.get("size") or 0)
            for entry in existing_entries
            if not entry.get("is_dir")
        }
        if existing_entries is not None
        else {}
    )

    todo: Dict[str, int] = {}
    for name, size in files:
        relative_path = f"{relative_dir}/{name}"
        # wopan 静默拒绝含 emoji/exotic 的文件名：copy API 返回 200 但文件不落盘，
        # verify 按原名找不到会空转整轮 verify（16 分钟/批）。提前判死，跳过 copy+verify。
        # 判据与 mover/网页侧同源（path_sanitize.sanitize_component），状态文件里
        # reason 带固定标记，便于末尾区分「emoji 死局」与「可重试失败」。
        if sanitize_component(name) != name:
            if not args.dry_run:
                state.mark_failed(relative_path, "文件名含 emoji/exotic，wopan 静默拒绝落盘")
            with totals.lock:
                totals.emoji_rejected += 1
            continue
        remote_size = existing.get(name)
        if remote_size is not None and (size <= 0 or abs(remote_size - size) <= args.tolerance):
            state.mark_done(relative_path, size)
            with totals.lock:
                totals.already_present += 1
            continue
        todo[name] = size

    if not todo:
        return

    if args.dry_run:
        log(f"  [dry-run] 将复制 {len(todo)} 个文件: {src_dir} -> {dst_dir}")
        with totals.lock:
            totals.copied += len(todo)
            totals.copied_bytes += sum(todo.values())
        return

    if not api.mkdir(dst_dir):
        for name in todo:
            state.mark_failed(f"{relative_dir}/{name}", "目标目录创建失败")
        with totals.lock:
            totals.failed += len(todo)
        return

    names = list(todo)
    for index in range(0, len(names), args.batch_size):
        chunk = names[index : index + args.batch_size]
        wait_for_capacity(api, args.max_undone)
        if not api.copy(src_dir, dst_dir, chunk):
            for name in chunk:
                state.mark_failed(f"{relative_dir}/{name}", "提交复制失败")
            with totals.lock:
                totals.failed += len(chunk)
            continue

        expected = {name: todo[name] for name in chunk}
        confirmed = verify_batch(
            api,
            dst_dir,
            expected,
            tolerance=args.tolerance,
            attempts=args.verify_attempts,
            interval=args.verify_interval,
        )
        newly_copied = 0
        newly_bytes = 0
        newly_failed = 0
        for name in chunk:
            relative_path = f"{relative_dir}/{name}"
            if name in confirmed:
                state.mark_done(relative_path, todo[name])
                newly_copied += 1
                newly_bytes += todo[name]
            else:
                state.mark_failed(relative_path, "复制后未在限时内确认，下次续跑重试")
                newly_failed += 1
        with totals.lock:
            totals.copied += newly_copied
            totals.copied_bytes += newly_bytes
            totals.failed += newly_failed
        state.save()
        log(
            f"  {relative_dir}: 已确认 {len(confirmed)}/{len(chunk)} "
            f"(累计复制 {totals.copied} 个 / {format_bytes(totals.copied_bytes)})"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="把备份盘归档复制到主力盘（只复制，不删除）")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不实际复制")
    parser.add_argument("--only", default="", help="只处理名字包含该子串的顶层用户目录")
    parser.add_argument("--max-files", type=int, default=0, help="本次最多处理多少个待复制文件（0=不限）")
    parser.add_argument("--batch-size", type=int, default=20, help="单次 copy 提交的文件数")
    parser.add_argument("--max-undone", type=int, default=30, help="复制队列待办任务上限，超过则等待")
    parser.add_argument("--workers", type=int, default=1, help="并发处理的目录数（1=串行）")
    parser.add_argument("--walk-workers", type=int, default=1, help="并发列目录线程数（加快扫描已完成区域）")
    parser.add_argument("--sort-by-size", action="store_true", help="先扫完目录树，按待复制字节量降序处理（大用户先跑满带宽）")
    parser.add_argument("--verify-attempts", type=int, default=40, help="每批复制后的确认轮询次数")
    parser.add_argument("--verify-interval", type=float, default=6.0, help="确认轮询间隔（秒）")
    parser.add_argument("--tolerance", type=int, default=0, help="允许的大小误差字节数")
    parser.add_argument("--state", default=str(DEFAULT_STATE_PATH), help="状态文件路径")
    parser.add_argument("--retry-failed", action="store_true", help="连同上次失败的文件一起重试")
    parser.add_argument(
        "--rename-source",
        action="store_true",
        help="先在源端（夸克）rename 去 emoji（全空/被拒目录用 sec_ 兜底），跑完退出不 copy",
    )
    parser.add_argument(
        "--rollback-renames",
        action="store_true",
        help="反向 rename 回滚 --rename-source 的改名，清空 state.renames，跑完退出",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    full_settings = load_settings()
    settings = full_settings.archive
    if not settings.enabled:
        log("archive.enabled 为 false，先在 config.yaml 里打开")
        return 2

    # token 来源与运行时一致（配置里没写就从 OpenList 自己的 SQLite 只读读取）
    token = settings.openlist_token
    if not token:
        try:
            token = OpenListClient(settings)._read_token_from_db()  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            log(f"读取 OpenList token 失败: {exc}")
            return 2

    api = OpenListSync(settings.openlist_base_url, token, settings.timeout_seconds)
    state = MigrationState.load(Path(args.state))
    totals = Totals()

    src_base = settings.backup_base
    dst_base = settings.primary_base
    log(f"源(备份盘): {src_base}")
    log(f"目标(主力盘): {dst_base}   目录去 emoji: {settings.sanitize_primary_dirs}")
    log(f"状态文件: {args.state}（已记录完成 {len(state.done)} 个，失败 {len(state.failed)} 个）")
    if args.dry_run:
        log("dry-run 模式：不会实际复制")

    # rollback 只动 state.renames，先于其余状态改动
    if args.rollback_renames:
        return rollback_renames(api, state, args.dry_run, totals)

    # rename pass 必须在 --retry-failed 之前：它要读 state.failed 构建 wopan 拒收名单
    if args.rename_source:
        users = _load_users(full_settings.monitor.state_path)
        dir_to_sid = build_archive_dir_to_sec_uid_map(users)
        rejected = _wopan_rejected_top_dirs(state)
        log(
            f"载入 {len(dir_to_sid)} 个归档目录→sec_user_id 映射；"
            f"{len(rejected)} 个被 wopan 拒绝的顶层目录将用 sec_ 兜底"
        )
        run_rename_pass(
            api,
            state,
            src_base,
            dir_to_sid,
            rejected,
            args.only or None,
            args.dry_run,
            totals,
        )
        log("=" * 60)
        log(f"  改名目录: {totals.renamed_dirs}")
        log(f"  改名文件: {totals.renamed_files}")
        return 0

    if args.retry_failed:
        log(f"清空 {len(state.failed)} 条失败记录以便重试")
        state.failed.clear()

    started_at = time.time()
    budget_exhausted = False

    # 目录级并发：walk 逐目录产出，多个 worker 同时做「建目录→提交复制→轮询确认」，
    # 每个目录只归一个 worker，互不重叠；共享的只有状态文件和 Totals 计数（各有锁）。
    work_queue: "queue.Queue[tuple[str, List[Tuple[str, int]]] | None]" = queue.Queue()

    def worker() -> None:
        while True:
            item = work_queue.get()
            try:
                if item is None:
                    return
                relative_dir, files = item
                process_directory(
                    api,
                    args=args,
                    state=state,
                    totals=totals,
                    src_base=src_base,
                    dst_base=dst_base,
                    relative_dir=relative_dir,
                    files=files,
                    sanitize=settings.sanitize_primary_dirs,
                )
            except Exception as exc:  # noqa: BLE001 — 单目录异常只标失败，不拖死流水线
                if item is not None:
                    for name, _size in item[1]:
                        state.mark_failed(f"{item[0]}/{name}", f"处理异常: {exc}")
                    log(f"  处理 {item[0]} 异常: {exc}")
            finally:
                work_queue.task_done()

    worker_threads = []
    for _ in range(max(1, args.workers)):
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        worker_threads.append(thread)

    def submit(relative_dir: Optional[str], files: List[Tuple[str, int]]) -> None:
        if relative_dir is not None and files:
            work_queue.put((relative_dir, files))

    pending_batches: List[Tuple[str, List[Tuple[str, int]]]] = []
    for relative_dir, files in walk_source(api, src_base, args.only or None, args.walk_workers):
        pending_files: List[Tuple[str, int]] = []
        for name, size in files:
            totals.scanned_files += 1
            relative_path = f"{relative_dir}/{name}"
            if state.is_done(relative_path, size):
                continue
            if not args.retry_failed and state.is_failed(relative_path):
                continue
            pending_files.append((name, size))
        if pending_files:
            pending_batches.append((relative_dir, pending_files))

        if not args.sort_by_size:
            submit(relative_dir, pending_files)
            # copied/failed 由 workers 并发更新，这里只做近似预算判断
            if args.max_files and (totals.copied + totals.failed) >= args.max_files:
                budget_exhausted = True
                break

    if args.sort_by_size:
        # 大目录先处理：视频用户先顶满夸克下载带宽，小目录的每目录固定开销
        # （列目标端/确认轮询）只用来在末尾填缝，不再拖慢整体字节吞吐
        pending_batches.sort(key=lambda item: (-sum(s for _, s in item[1]), item[0]))
        log(f"扫描完成，共 {len(pending_batches)} 个待处理目录，按字节量降序投递")
        for relative_dir, files in pending_batches:
            submit(relative_dir, files)
            if args.max_files and (totals.copied + totals.failed) >= args.max_files:
                budget_exhausted = True
                break

    work_queue.join()  # 等所有目录处理完
    for _ in worker_threads:
        work_queue.put(None)
    for thread in worker_threads:
        thread.join()
    state.save()

    elapsed = max(1.0, time.time() - started_at)
    log("=" * 60)
    log(f"扫描文件 {totals.scanned_files} 个")
    log(f"  目标端已存在（跳过）: {totals.already_present}")
    log(f"  本次复制成功        : {totals.copied}  ({format_bytes(totals.copied_bytes)})")
    log(f"  失败（下次重试）    : {totals.failed}")
    log(f"  文件名含 emoji 拒绝 : {totals.emoji_rejected}")
    log(f"  目录名全 emoji 跳过 : {totals.skipped_unsanitisable}")
    log(f"  耗时 {elapsed / 60:.1f} 分钟，平均 {format_bytes(int(totals.copied_bytes / elapsed))}/s")
    if budget_exhausted:
        log("已达 --max-files 上限，重跑本脚本可继续")
    if state.failed:
        emoji_dead = sum(1 for r in state.failed.values() if "emoji/exotic" in r)
        retryable = len(state.failed) - emoji_dead
        log(
            f"状态文件失败 {len(state.failed)} 个："
            f"文件名 emoji 死局 {emoji_dead}（改名/放弃待人工）、可重试 {retryable}"
        )
        if retryable:
            log("可重试示例:")
            for path, reason in list(state.failed.items()):
                if "emoji/exotic" not in reason:
                    log(f"    {path}: {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
