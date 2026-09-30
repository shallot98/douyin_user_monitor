"""给历史作品批量补打 AI 标签（详细标签 + 广告/纯文字/截图判定）。

    python -m scripts.content_filter_backfill --limit 20          # 试跑 20 个
    python -m scripts.content_filter_backfill --concurrency 3     # 全量（可随时 Ctrl+C / SIGTERM 停）

- 选取：没有 content_filter、或结构版本低于当前版本的记录；--retry-errors 另外重试检测失败的。
- 媒体：本机还在就读本机，否则从网盘读（只顺序读关键帧，不整片下载）。
- 写回：每攒 BATCH_SIZE 条，在 MonitorStorage 的状态文件锁内重读该用户最新记录，
  只给仍需要的记录填结论（不覆盖同步流程刚写的新结论），再 RecordStore.put。
  服务进程靠 SQLite data_version 自动感知，不会被内存缓存覆盖。
- 进度：runtime/content_filter_backfill.json；中断后重跑会自动跳过已完成的。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from douyin_user_monitor.monitor.archive_path_aliases import load_archive_path_aliases
from douyin_user_monitor.monitor.content_filter import RESULT_VERSION, ContentFilter
from douyin_user_monitor.monitor.content_filter_media import ArchiveMediaLoader
from douyin_user_monitor.monitor.cover_store import CoverStore
from douyin_user_monitor.monitor.openlist_client import OpenListClient
from douyin_user_monitor.monitor.storage import MonitorStorage
from douyin_user_monitor.monitor.timeutils import cn_now
from douyin_user_monitor.settings_loaders import load_settings

BATCH_SIZE = 24
OUTAGE_BACKOFF_SECONDS = 60
OUTAGE_BACKOFF_MAX_SECONDS = 600
logger = logging.getLogger("content_filter_backfill")


def outages_only(outcomes):
    """挑出因模型网关 5xx 失败的条目（抽帧/网盘等其他失败不算网关故障）。"""
    return [item for item in outcomes if str(item[2].get("error") or "").startswith("模型接口 HTTP 5")]


def is_gateway_outage(failed_results, batch_size: int) -> bool:
    return batch_size > 0 and len(failed_results) * 2 > batch_size


def needs_label(record: Dict[str, Any], *, retry_errors: bool) -> bool:
    result = record.get("content_filter")
    if not isinstance(result, dict) or int(result.get("version") or 0) < RESULT_VERSION:
        return True
    return retry_errors and result.get("status") == "error"


def select_pending(users: List[Dict[str, Any]], records_of, *, retry_errors: bool,
                   user_id: str | None = None) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    pending = []
    for user in users:
        if user_id and str(user.get("id")) != user_id:
            continue
        for record in records_of(str(user.get("id") or "")):
            if isinstance(record, dict) and record.get("aweme_id") and needs_label(record, retry_errors=retry_errors):
                pending.append((user, record))
    return pending


def commit_results(storage: MonitorStorage, results: Dict[str, Dict[str, Dict[str, Any]]], *,
                   retry_errors: bool) -> int:
    """results: user_id → aweme_id → 结论。返回实际写入条数。"""
    written = 0

    def _apply(_state: Dict[str, Any]) -> None:
        nonlocal written
        for user_id, by_aweme in results.items():
            current = storage.records.get(user_id)
            changed = False
            updated = []
            for record in current:
                aweme_id = str(record.get("aweme_id") or "") if isinstance(record, dict) else ""
                # 锁内重新判断：同步流程可能刚给这条写了新结论，不能被旧任务覆盖
                if aweme_id in by_aweme and needs_label(record, retry_errors=retry_errors):
                    updated.append({**record, "content_filter": by_aweme[aweme_id]})
                    changed = True
                    written += 1
                else:
                    updated.append(record)
            if changed:
                storage.records.put(user_id, updated)

    storage.update_state(_apply)
    return written


class Progress:
    def __init__(self, path: Path, total: int):
        self.path = path
        self.data = {"started_at": cn_now(), "total": total, "done": 0, "ok": 0, "error": 0,
                     "reject": 0, "written": 0, "categories": {}, "status": "running"}

    def add(self, result: Dict[str, Any]) -> None:
        self.data["done"] += 1
        if result.get("status") == "ok":
            self.data["ok"] += 1
            self.data["reject"] += result.get("verdict") == "reject"
            category = result.get("category")
            self.data["categories"][category] = self.data["categories"].get(category, 0) + 1
        else:
            self.data["error"] += 1

    def save(self, **extra: Any) -> None:
        self.data.update(extra, updated_at=cn_now())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2))
        tmp.replace(self.path)


async def run(args: argparse.Namespace) -> int:
    settings = load_settings()
    if not settings.content_filter.api_key:
        logger.error("未配置 DYMON_FILTER_API_KEY，退出")
        return 2
    state_path = settings.monitor.state_path
    storage = MonitorStorage(state_path)
    openlist = OpenListClient(settings.archive)
    loader = ArchiveMediaLoader(settings.monitor.download_root, openlist, load_archive_path_aliases(state_path),
                                CoverStore(state_path.parent / "covers"))
    content_filter = ContentFilter(settings.content_filter, settings.monitor.download_root,
                                   concurrency=args.concurrency)

    # 路径映射校验要按用户的 download_records 找记录，所以要带上记录（状态 JSON 里没有）
    users = storage.records.attach_all(storage.load_state()["users"])
    pending = select_pending(users, storage.records.get, retry_errors=args.retry_errors, user_id=args.user_id)
    if args.limit:
        pending = pending[: args.limit]
    progress = Progress(Path(args.progress), len(pending))
    progress.save()
    logger.info("待补打 %d 条（并发 %d）", len(pending), args.concurrency)

    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopping.set)

    async def label(user, record):
        detail = {"desc": record.get("desc") or ""}  # 历史作品没存完整详情，只有标题
        result = await content_filter.evaluate(user, detail, record, loader)
        logger.info("%s %s %s/%s %s %.0f%% %dms %s", user.get("nickname"), record["aweme_id"],
                    result.get("status"), result.get("verdict", "-"), result.get("category", "-"),
                    100 * float(result.get("confidence") or 0), result["elapsed_ms"],
                    result.get("error") or "、".join(result.get("tags", {}).get("content", [])))
        return user, record, result

    started = time.monotonic()
    try:
        for start in range(0, len(pending), BATCH_SIZE):
            if stopping.is_set():
                break
            batch = pending[start : start + BATCH_SIZE]
            outcomes = await asyncio.gather(*(label(user, record) for user, record in batch))
            # 模型网关整体挂了（大面积 5xx）：这批不落库，退避后重试，免得把整个队列都标成失败
            wait = OUTAGE_BACKOFF_SECONDS
            while is_gateway_outage([result for _, _, result in outages_only(outcomes)], len(batch)) and not stopping.is_set():
                logger.warning("模型网关疑似不可用（本批 %d/%d 为 5xx），%ds 后重试本批", len(outages_only(outcomes)), len(batch), wait)
                progress.save(status="waiting_gateway")
                try:
                    await asyncio.wait_for(stopping.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass
                if stopping.is_set():
                    break
                wait = min(wait * 2, OUTAGE_BACKOFF_MAX_SECONDS)
                outcomes = await asyncio.gather(*(label(user, record) for user, record in batch))
            if stopping.is_set() and is_gateway_outage([r for _, _, r in outages_only(outcomes)], len(batch)):
                break
            progress.data["status"] = "running"
            by_user: Dict[str, Dict[str, Dict[str, Any]]] = {}
            for user, record, result in outcomes:
                progress.add(result)
                by_user.setdefault(str(user["id"]), {})[str(record["aweme_id"])] = result
            if not args.dry_run:
                progress.data["written"] += await asyncio.to_thread(
                    commit_results, storage, by_user, retry_errors=args.retry_errors)
            rate = progress.data["done"] / max(1e-6, time.monotonic() - started)
            remaining = (len(pending) - progress.data["done"]) / max(rate, 1e-6)
            progress.save(rate_per_min=round(rate * 60, 1), eta_hours=round(remaining / 3600, 2))
    finally:
        progress.save(status="stopped" if stopping.is_set() else "finished")
        await openlist.close()
    logger.info("结束：%s", json.dumps(progress.data, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少条（试跑用），0=全部")
    parser.add_argument("--concurrency", type=int, default=3, help="同时处理几个作品")
    parser.add_argument("--user-id", help="只处理这个用户")
    parser.add_argument("--retry-errors", action="store_true", help="也重试之前检测失败的")
    parser.add_argument("--dry-run", action="store_true", help="只打日志，不写回记录")
    parser.add_argument("--progress", default="runtime/content_filter_backfill.json")
    args = parser.parse_args(argv)
    if args.concurrency < 1:
        parser.error("--concurrency 必须 >= 1")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
