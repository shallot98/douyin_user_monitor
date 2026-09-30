"""给“被挪进 sec_<sec_user_id>/ 兜底目录”的归档文件补已核实路径映射。

背景：网盘去 emoji 迁移时，部分用户的顶层目录被整体改名为 ``sec_<sec_user_id>``
（见 path_sanitize.fallback_dir_for），但下载记录里仍是原昵称路径；这些用户之后的新作品
又回到昵称目录。普通中文昵称不会触发 sanitize 的 sec_ 兜底，于是旧作品在网站上 404。

做法（只读网盘，不移动/删除任何文件）：
1. 列出每个用户在主力盘/备份盘上的 昵称目录 与 sec_ 目录（顶层文件 + 图片/<作品>/ 下文件）。
2. 对每条记录的每个文件：若按现有候选规则（remote_paths_for）在列出的文件里一个都找不到，
   而同名文件（原名或去 emoji 名）在该用户 sec_ 目录里存在且大小 > 0，就生成一条映射，
   主力盘优先。已有映射的 key 不动。
3. 默认只输出报告；--write 时把新条目合并进 data/archive_path_aliases.json（原子替换，
   先备份到 runtime/），之后重启 API 生效。

    .venv/bin/python -m scripts.archive_sec_dir_aliases               # 只看报告
    .venv/bin/python -m scripts.archive_sec_dir_aliases --write       # 写入映射
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from douyin_user_monitor.monitor.archive_feed import list_archive_files
from douyin_user_monitor.monitor.archive_path_aliases import (
    ARCHIVE_PATH_ALIASES_FILENAME,
    ARCHIVE_PATH_ALIASES_VERSION,
    ArchivePathAliases,
)
from douyin_user_monitor.monitor.openlist_client import OpenListClient
from douyin_user_monitor.monitor.path_sanitize import fallback_dir_for, sanitize_component
from douyin_user_monitor.monitor.storage import MonitorStorage
from douyin_user_monitor.monitor.timeutils import cn_now
from douyin_user_monitor.settings_loaders import load_settings

logger = logging.getLogger("archive_sec_dir_aliases")
LIST_CONCURRENCY = 4
SOURCE_FINGERPRINT = "sec-dir-scan-v1"
IMAGE_ROOT = "图片"


class Lister:
    """OpenList fs/list 封装：目录不存在返回 None，其余错误直接抛出（不静默当成空目录）。"""

    def __init__(self, client: OpenListClient, base_url: str):
        self._client = client
        self._base_url = base_url
        self._slots = asyncio.Semaphore(LIST_CONCURRENCY)
        self.calls = 0

    async def list(self, path: str) -> Optional[Dict[str, Dict[str, Any]]]:
        async with self._slots:
            http = await self._client._get_http_client()  # noqa: SLF001 - 复用服务同一个客户端/token
            token = await self._client._get_token()  # noqa: SLF001
            response = await http.post(f"{self._base_url}/api/fs/list",
                                       json={"path": path, "page": 1, "per_page": 0},
                                       headers={"Authorization": token}, timeout=120)
            self.calls += 1
        data = response.json()
        if data.get("code") != 200:
            if "not found" in str(data.get("message", "")):
                return None
            raise RuntimeError(f"fs/list {path} 失败：{data.get('code')} {data.get('message')}")
        return {item["name"]: item for item in (data["data"].get("content") or [])}

    async def tree(self, root: str) -> Optional[Dict[str, int]]:
        """root 下的 顶层文件 + 图片/<作品目录>/文件 → {相对路径: 大小}；root 不存在返回 None。"""
        top = await self.list(root)
        if top is None:
            return None
        files = {name: int(item.get("size") or 0) for name, item in top.items() if not item.get("is_dir")}
        if IMAGE_ROOT in top and top[IMAGE_ROOT].get("is_dir"):
            works = await self.list(f"{root}/{IMAGE_ROOT}") or {}
            subdirs = [name for name, item in works.items() if item.get("is_dir")]
            listings = await asyncio.gather(*(self.list(f"{root}/{IMAGE_ROOT}/{name}") for name in subdirs))
            for name, listing in zip(subdirs, listings):
                for file_name, item in (listing or {}).items():
                    if not item.get("is_dir"):
                        files[f"{IMAGE_ROOT}/{name}/{file_name}"] = int(item.get("size") or 0)
        return files


def sanitize_rest(rest: str) -> Optional[str]:
    parts = [sanitize_component(part) for part in rest.split("/")]
    return "/".join(parts) if all(parts) else None


def plan_user(user: Dict[str, Any], client: OpenListClient, existing: Dict[Tuple[str, str, str], Any],
              trees: Dict[str, Dict[str, int]], sec_trees: Dict[str, Dict[str, int]], bases: List[str]):
    """返回 (新映射条目列表, 统计 Counter)。trees: 绝对路径 → 大小（已列出的全部文件）。"""
    stats: Counter = Counter()
    entries = []
    sid = str(user.get("sec_user_id") or "")
    for record in user.get("download_records") or []:
        aweme_id = str(record.get("aweme_id") or "")
        for relative in list_archive_files(user, aweme_id):
            stats["files"] += 1
            key = (str(user["id"]), aweme_id, relative)
            if key in existing:
                stats["already_aliased"] += 1
                continue
            candidates = client.remote_paths_for(relative, sid)
            if any(path in trees for path in candidates):
                stats["reachable"] += 1
                continue
            rest = relative.split("/", 1)[1] if "/" in relative else relative
            hit = None
            for base in bases:
                tree = sec_trees.get(base) or {}
                for variant in dict.fromkeys(filter(None, (rest, sanitize_rest(rest)))):
                    if tree.get(variant, 0) > 0:
                        hit = (base, variant, tree[variant])
                        break
                if hit:
                    break
            if hit is None:
                stats["missing_everywhere"] += 1
                continue
            base, variant, size = hit
            stats[f"aliased_{'primary' if base == bases[0] else 'backup'}"] += 1
            entries.append({
                "user_id": str(user["id"]),
                "aweme_id": aweme_id,
                "original_relative_path": relative,
                "verified_remote_path": f"{base}/{fallback_dir_for(sid)}/{variant}",
                "source_revision": {"sec_user_id": sid, "downloaded_at": record.get("downloaded_at")},
                "source_fingerprint": SOURCE_FINGERPRINT,
                "verified_at": cn_now(),
                "verified_size": size,
                "matching_basis": {"method": "same_user_sec_dir_same_name", "renamed": variant != rest},
            })
    return entries, stats


async def scan(args) -> int:
    settings = load_settings()
    state_path = settings.monitor.state_path
    storage = MonitorStorage(state_path)
    alias_path = state_path.with_name(ARCHIVE_PATH_ALIASES_FILENAME)
    document = json.loads(alias_path.read_text(encoding="utf-8")) if alias_path.exists() else \
        {"schema_version": ARCHIVE_PATH_ALIASES_VERSION, "entries": []}
    ArchivePathAliases.from_document(document)  # 现有文件先校验，坏了就别往上叠
    existing = {(e["user_id"], e["aweme_id"], e["original_relative_path"]): e for e in document["entries"]}

    client = OpenListClient(settings.archive)
    lister = Lister(client, settings.archive.openlist_base_url)
    bases = list(settings.archive.read_bases)
    users = storage.records.attach_all(storage.load_state()["users"])
    if args.user:
        users = [u for u in users if u.get("nickname") == args.user or u.get("id") == args.user]
    totals: Counter = Counter()
    new_entries: List[Dict[str, Any]] = []
    per_user: List[Tuple[str, int, int]] = []
    try:
        for index, user in enumerate(users, 1):
            sid = str(user.get("sec_user_id") or "")
            if not sid or not user.get("download_records"):
                continue
            sec_dir = fallback_dir_for(sid)
            sec_trees = {base: await lister.tree(f"{base}/{sec_dir}") for base in bases}
            if not any(tree for tree in sec_trees.values()):
                continue  # 没有 sec_ 目录：这类问题与该用户无关
            tops = {files[0].split("/")[0] for files in (r.get("files") for r in user["download_records"]) if files}
            tops |= {sanitize_component(top) for top in tops if sanitize_component(top)}
            trees: Dict[str, int] = {}
            for base in bases:
                for top in tops:
                    listed = await lister.tree(f"{base}/{top}")
                    trees.update({f"{base}/{top}/{rel}": size for rel, size in (listed or {}).items()})
                trees.update({f"{base}/{sec_dir}/{rel}": size for rel, size in (sec_trees[base] or {}).items()})
            entries, stats = plan_user(user, client, existing, trees, sec_trees, bases)
            totals.update(stats)
            totals["users_with_sec_dir"] += 1
            new_entries.extend(entries)
            if entries or stats["missing_everywhere"]:
                per_user.append((str(user.get("nickname")), len(entries), stats["missing_everywhere"]))
            logger.info("[%d/%d] %s 新映射 %d 仍缺 %d（fs/list 累计 %d 次）", index, len(users), user.get("nickname"),
                        len(entries), stats["missing_everywhere"], lister.calls)
    finally:
        await client.close()

    print(json.dumps(dict(totals), ensure_ascii=False, indent=1))
    print("按用户（新映射, 仍缺）：", sorted(per_user, key=lambda x: -x[1])[:40])
    if not args.write:
        print(f"报告模式：{len(new_entries)} 条新映射未写入（加 --write 写入）")
        return 0
    merged = {"schema_version": ARCHIVE_PATH_ALIASES_VERSION, "entries": [*document["entries"], *new_entries]}
    ArchivePathAliases.from_document(merged)  # 重复 key / 格式问题在写之前暴露
    backup = Path("runtime") / f"{ARCHIVE_PATH_ALIASES_FILENAME}.bak.{cn_now()[:19].replace(':', '')}"
    if alias_path.exists():
        shutil.copy2(alias_path, backup)
    tmp = alias_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(alias_path)
    print(f"已写入 {len(new_entries)} 条（总 {len(merged['entries'])}），原文件备份 {backup}；重启 API 生效")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--write", action="store_true", help="把新映射写入 archive_path_aliases.json")
    parser.add_argument("--user", help="只处理这个昵称或用户 id")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stdout)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return asyncio.run(scan(args))


if __name__ == "__main__":
    raise SystemExit(main())
