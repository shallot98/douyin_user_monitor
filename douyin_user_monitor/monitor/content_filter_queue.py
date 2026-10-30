"""新作品内容标注的后台队列：下载流程只入队，标注在 run_lock 之外逐条执行。

以前 `user_sync._download_posts` 在同步流程里 await 模型，coverage 模式下整段都持有
`MonitorService._run_lock`，手动 run_once / 启停要等标注完。现在下载记录先提交，
标注结果之后在 `update_state` 锁内按 aweme_id 补进记录（与补打脚本同一写法），
不会覆盖同步流程或补打任务已经写好的新结论。

队列只在内存里；进程重启后由 `enqueue_unlabeled` 从记录里把缺结论的新作品补回来。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

from douyin_user_monitor.monitor.content_filter import RESULT_VERSION

logger = logging.getLogger(__name__)

# 启动时只补最近这么多天下载的作品，更早的交给补打脚本
RESUME_WINDOW_DAYS = 3
QUEUE_MAX = 2000
# 下载流程先入队、后提交记录（同一用户的整轮同步结束才落盘），记录可能晚到
RECORD_WAIT_SECONDS = (5, 15, 30, 60, 120, 300)


def needs_label(record: Dict[str, Any]) -> bool:
    result = record.get("content_filter")
    return not isinstance(result, dict) or int(result.get("version") or 0) < RESULT_VERSION


class ContentFilterQueue:
    def __init__(self, content_filter, storage, loader, *, maxsize: int = QUEUE_MAX,
                 record_wait_seconds: Tuple[float, ...] = RECORD_WAIT_SECONDS):
        self._filter = content_filter
        self._storage = storage
        self._loader = loader
        self._queue: asyncio.Queue[Tuple[str, str, Dict[str, Any]]] = asyncio.Queue(maxsize=maxsize)
        self._queued: set[Tuple[str, str]] = set()
        self._task: Optional[asyncio.Task] = None
        self._record_wait_seconds = record_wait_seconds

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    def applies_to(self, record: Dict[str, Any]) -> bool:
        return self._filter.applies_to(record)

    def submit(self, user: Dict[str, Any], record: Dict[str, Any], aweme_detail: Dict[str, Any]) -> bool:
        """下载流程调用：只入队，不等结果。队列满时丢弃并记日志，补打脚本会兜底。"""
        if not self.applies_to(record):
            return False
        key = (str(user.get("id") or ""), str(record.get("aweme_id") or ""))
        if not all(key) or key in self._queued:
            return False
        try:
            self._queue.put_nowait((key[0], key[1], self._detail_fields(aweme_detail)))
        except asyncio.QueueFull:
            logger.warning("内容标注队列已满（%d），作品 %s 留给补打脚本", self._queue.maxsize, key[1])
            return False
        self._queued.add(key)
        return True

    def enqueue_unlabeled(self, users: Iterable[Dict[str, Any]], *, since: str) -> int:
        """启动时补队列：`since` 之后下载、还没有当前版本结论的作品。"""
        added = 0
        for user in users:
            for record in self._storage.records.get(str(user.get("id") or "")):
                if not isinstance(record, dict) or not needs_label(record):
                    continue
                if str(record.get("downloaded_at") or "") < since:
                    continue
                if self.submit(user, record, {"desc": record.get("desc") or ""}):
                    added += 1
        return added

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="content-filter-queue")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def drain(self) -> None:
        """测试用：等队列里已有的条目处理完。"""
        await self._queue.join()

    @staticmethod
    def _detail_fields(aweme_detail: Dict[str, Any]) -> Dict[str, Any]:
        # 只留打标要用的字段（广告信号与标题），不在内存里长期挂整份详情
        keys = ("desc", "text_extra", "is_ads", "is_from_ad_auth", "promotions", "anchors", "anchor_info",
                "commerce_config_data")
        return {key: aweme_detail[key] for key in keys if key in aweme_detail}

    async def _run(self) -> None:
        while True:
            user_id, aweme_id, detail = await self._queue.get()
            try:
                await self._label_one(user_id, aweme_id, detail)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 单条失败不能停掉整个队列
                logger.exception("内容标注队列处理失败 user=%s aweme=%s", user_id, aweme_id)
            finally:
                self._queued.discard((user_id, aweme_id))
                self._queue.task_done()

    def _find(self, user_id: str, aweme_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        users = self._storage.load_state().get("users", [])
        user = next((u for u in users if str(u.get("id") or "") == user_id), None)
        if user is None:
            return None, None
        record = next((r for r in self._storage.records.get(user_id)
                       if isinstance(r, dict) and str(r.get("aweme_id") or "") == aweme_id), None)
        return user, record

    async def _label_one(self, user_id: str, aweme_id: str, detail: Dict[str, Any]) -> None:
        user, record = await asyncio.to_thread(self._find, user_id, aweme_id)
        for delay in self._record_wait_seconds:
            if user is None or record is not None:
                break
            await asyncio.sleep(delay)
            user, record = await asyncio.to_thread(self._find, user_id, aweme_id)
        if record is None and user is not None:
            logger.warning("内容标注：作品 %s 的下载记录迟迟未落盘，跳过（下次启动补回）", aweme_id)
        if user is None or record is None or not needs_label(record):
            # 用户/作品已删除，或补打任务已经写了结论
            return
        # 路径映射校验要按用户的 download_records 找记录
        user = self._storage.records.attach(user)
        result = await self._filter.evaluate(user, detail, record, self._loader)
        await asyncio.to_thread(self._commit, user_id, aweme_id, result)

    def _commit(self, user_id: str, aweme_id: str, result: Dict[str, Any]) -> None:
        def _apply(_state: Dict[str, Any]) -> None:
            current: List[Dict[str, Any]] = self._storage.records.get(user_id)
            updated, changed = [], False
            for record in current:
                if (isinstance(record, dict) and str(record.get("aweme_id") or "") == aweme_id
                        and needs_label(record)):
                    updated.append({**record, "content_filter": result})
                    changed = True
                else:
                    updated.append(record)
            if changed:
                self._storage.records.put(user_id, updated)

        self._storage.update_state(_apply)
