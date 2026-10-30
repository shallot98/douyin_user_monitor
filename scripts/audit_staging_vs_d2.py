#!/usr/bin/env python3
"""核对 download/.backup-staging 里的文件在主力盘 /d2 上的真实状态，并算出「现在删掉能回收多少字节」。

只读：本脚本不删除、不改名、不向任何端提交任务。

与 v1 的关键差别（v1 的结论会把空间算多）：
  * 列表一律 refresh=true，不吃 OpenList 目录缓存，避免拿旧列表把「已确认」算高。
  * 按 inode 链接数区分暂存条目的性质：
      nlink == 1  本地原路径已被 move 删除，暂存链接是本地唯一副本 —— 删掉才真正释放磁盘
      nlink >= 2  本地原件还在（硬链接仍在），删掉只把 nlink 减 1，释放 0 字节
    因此「已确认」不等于「可回收」。
  * 分页按 OpenList 返回的 total（含目录项）收敛，不按已收集的文件数收敛。
  * 单个目录查询失败只影响该目录，且失败项单独归类，不会被当成「未确认」。
"""

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

PROJECT = "/root/douyin_user_monitor"
API_URL = "http://127.0.0.1:5244"
OPENLIST_DB = "/opt/openlist/data/data.db"
SOURCE_ROOT = os.path.join(PROJECT, "download")
STAGING = os.path.join(SOURCE_ROOT, ".backup-staging")
STAGING_NAME = ".backup-staging"
TARGET_BASE = "/d2/vps/抖音视频"
MAX_PAGES = 40


def get_token():
    con = sqlite3.connect(f"file:{OPENLIST_DB}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT value FROM x_setting_items WHERE key='token' LIMIT 1;"
        ).fetchone()
    finally:
        con.close()
    if not row:
        raise SystemExit("读不到 OpenList token")
    return row[0]


def api_post(token, path, payload, timeout=90):
    req = urllib.request.Request(
        API_URL + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": token, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def dir_target_path(rel_dir, target_base):
    rel_dir = rel_dir.strip("./") or "."
    if rel_dir == ".":
        return target_base
    return f"{target_base}/{rel_dir}"


def list_dir_files(token, rel_dir, target_base):
    """返回 (files, raw_seen, total, error)。files 为 {name: size}，只含非目录项。

    total 是 OpenList 给出的条目总数（含目录项），用它决定还要不要翻页。
    """
    files = {}
    page = 1
    total = None
    raw_seen = 0
    while page <= MAX_PAGES:
        payload = {
            "path": dir_target_path(rel_dir, target_base),
            "page": page,
            "per_page": 1000,
            "refresh": True,
            "store": False,
        }
        resp = api_post(token, "/api/fs/list", payload)
        code = resp.get("code")
        message = str(resp.get("message") or "")
        if code == 404 or "not found" in message.lower():
            if page == 1:
                return files, 0, 0, "missing_dir"
            break
        if code != 200:
            return files, raw_seen, total or 0, f"code={code}: {message}"
        data = resp.get("data") or {}
        content = data.get("content") or []
        if total is None:
            total = data.get("total") or 0
        raw_seen += len(content)
        for item in content:
            if item.get("dir"):
                continue
            files[item.get("name")] = item.get("size")
        if not content or raw_seen >= total:
            break
        page += 1
    else:
        return files, raw_seen, total or 0, f"too_many_entries(page>{MAX_PAGES})"
    return files, raw_seen, total or 0, None


def collect_staged():
    """按目录分组暂存文件，附带 inode/nlink。"""
    by_dir = defaultdict(list)
    for dirpath, dirnames, filenames in os.walk(STAGING):
        dirnames.sort()
        rel_dir = os.path.relpath(dirpath, STAGING)
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            try:
                st = os.stat(path)
            except OSError as exc:
                by_dir[rel_dir].append(
                    {"path": path, "name": name, "size": 0, "inode": None,
                     "nlink": None, "stat_error": str(exc)}
                )
                continue
            by_dir[rel_dir].append(
                {"path": path, "name": name, "size": st.st_size,
                 "inode": st.st_ino, "nlink": st.st_nlink, "stat_error": None}
            )
    return by_dir


def find_local_original(entry):
    """暂存条目对应的本地原路径（download/<rel>/<name>），存在则返回 (path, inode)。"""
    rel = os.path.relpath(entry["path"], STAGING)
    cand = os.path.join(SOURCE_ROOT, rel)
    if os.path.exists(cand):
        return cand, os.stat(cand).st_ino
    return None, None


def classify(entries, remote, rel_dir):
    """把该目录下的暂存条目分成若干类。"""
    out = defaultdict(list)
    for e in entries:
        if e["stat_error"]:
            out["stat_error"].append(e)
            continue
        nlink = e["nlink"] or 1
        orig_path, orig_ino = find_local_original(e)
        e["local_original"] = orig_path
        e["shares_inode"] = bool(orig_ino and orig_path and orig_ino == e["inode"])
        if remote is None:
            out["query_failed"].append(e)
            continue
        if e["name"] not in remote:
            out["missing_on_d2"].append(e)
            continue
        rsize = remote[e["name"]]
        if rsize == e["size"]:
            if nlink == 1:
                out["confirmed_releasable"].append(e)
            else:
                out["confirmed_but_hardlinked"].append(e)
            continue
        if orig_path and e["shares_inode"]:
            out["conflict_size_local_original_present"].append(e)
        else:
            out["conflict_size_local_only"].append(e)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target-base", default=TARGET_BASE)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default=os.path.join(
        PROJECT, "runtime", "_archived-notes-20260915", "d2-staging-audit-v2.json"))
    args = ap.parse_args()

    token = get_token()
    by_dir = collect_staged()
    rel_dirs = sorted(by_dir)
    total_files = sum(len(v) for v in by_dir.values())
    print(f"暂存文件 {total_files} 个 / 目录 {len(rel_dirs)} 个（refresh=true 实测）", flush=True)

    remote = {}
    failures = {}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futs = {pool.submit(list_dir_files, token, rd, args.target_base): rd for rd in rel_dirs}
        for i, fut in enumerate(as_completed(futs), 1):
            rd = futs[fut]
            try:
                files, seen, tot, err = fut.result()
            except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
                failures[rd] = f"exception: {exc}"
                remote[rd] = None
                continue
            if err:
                failures[rd] = err
                remote[rd] = None
            else:
                remote[rd] = files
            if i % 40 == 0:
                print(f"  已列 {i}/{len(rel_dirs)} 目录", flush=True)

    buckets = defaultdict(list)
    for rd in rel_dirs:
        for cls, items in classify(by_dir[rd], remote[rd], rd).items():
            buckets[cls].extend(items)

    def size_sum(items):
        return sum(i["size"] for i in items)

    gb = 2 ** 30
    order = [
        ("confirmed_releasable", "已确认且 nlink=1 —— 删掉真正回收"),
        ("confirmed_but_hardlinked", "已确认但 nlink>=2 —— 删掉回收 0 字节"),
        ("conflict_size_local_original_present", "同名不同大小，本地原件仍在（多为重生成衍生文件）"),
        ("conflict_size_local_only", "同名不同大小，本地唯一副本"),
        ("missing_on_d2", "d2 无同名文件"),
        ("query_failed", "d2 目录查询失败（结论不可用）"),
        ("stat_error", "本地 stat 失败"),
    ]
    print()
    for key, label in order:
        items = buckets.get(key, [])
        if not items:
            continue
        print(f"{len(items):5d} 个 / {size_sum(items)/gb:6.2f} GB  {label}")

    reclaim = size_sum(buckets.get("confirmed_releasable", []))
    print(f"\n现在删除暂存文件可回收: {reclaim/gb:.2f} GB "
          f"（{len(buckets.get('confirmed_releasable', []))} 个 nlink=1 且 d2 已同名同大小）")

    hard = buckets.get("confirmed_but_hardlinked", [])
    if hard:
        print(f"注意: 其中 {len(hard)} 个是硬链接（本地原件还在），删除不释放空间，"
              f"只会把它们踢出夸克补传队列")

    if failures:
        print(f"\n查询失败目录 {len(failures)} 个:")
        for rd, err in sorted(failures.items())[:10]:
            print(f"  {rd[:80]}: {err[:120]}")

    report = {
        "target_base": args.target_base,
        "staged_files": total_files,
        "staged_dirs": len(rel_dirs),
        "reclaimable_bytes": reclaim,
        "buckets": {
            key: [
                {
                    "path": i["path"],
                    "size": i["size"],
                    "inode": i["inode"],
                    "nlink": i["nlink"],
                    "local_original": i.get("local_original"),
                    "d2_size": (remote.get(os.path.relpath(os.path.dirname(i["path"]), STAGING)) or {}).get(i["name"]),
                }
                for i in sorted(buckets.get(key, []), key=lambda x: x["path"])
            ]
            for key, _ in order
        },
        "dir_failures": failures,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
    print(f"\n明细: {args.out}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
