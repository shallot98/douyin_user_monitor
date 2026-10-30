import asyncio
import logging
from typing import Any, Dict, List, Optional, Set

from douyin_user_monitor.monitor.crawler_protocol import MonitorCrawlerProtocol
from douyin_user_monitor.monitor.downloader import AwemeAssetDownloader
from douyin_user_monitor.monitor.errors import sanitize_public_error
from douyin_user_monitor.monitor.history_sync import (
    ACTIVE_HISTORY_SYNC_STATUSES,
    complete_history_sync,
    normalize_history_sync_state,
    refresh_history_sync_status,
    update_history_sync_progress,
)
from douyin_user_monitor.monitor.notifier import MonitorNotifierProtocol
from douyin_user_monitor.monitor.parse_cookies import ParseCookiePool, ParseCookiesUnavailable
from douyin_user_monitor.monitor.profile_parser import (
    ACCOUNT_STATUS_BANNED,
    ACCOUNT_STATUS_DELETED,
    ACCOUNT_STATUS_NORMAL,
    UserProfileSnapshot,
    build_profile_snapshot_fields,
    extract_account_status,
    extract_avatar_url,
    extract_nickname,
)
from douyin_user_monitor.monitor.sync_utils import (
    format_publish_time,
    merge_download_records,
    merge_downloaded_ids,
)
from douyin_user_monitor.monitor.timeutils import cn_now, safe_int

INCREMENTAL_FETCH_POSTS = 20
HISTORY_RETRY_LIMIT = 5


# 应用没单独配日志输出；借 uvicorn 已配好的通道，回落登录态的记录才进 journal
LOGGER = logging.getLogger("uvicorn.error")


class UserSyncService:
    def __init__(
        self,
        crawler: MonitorCrawlerProtocol,
        downloader: AwemeAssetDownloader,
        notifier: MonitorNotifierProtocol,
        cookie_rotation_session=None,
        *,
        cover_publisher=None,
        content_filter=None,
        detail_cookie_pool: Optional[ParseCookiePool] = None,
    ):
        self._crawler = crawler
        # 过期 Cookie：取作品详情、原画地址和下载文件用它，登录态只留给拉作品列表
        self._detail_cookie_pool = detail_cookie_pool
        self._downloader = downloader
        self._notifier = notifier
        self._cookie_rotation_session = cookie_rotation_session
        self._cover_publisher = cover_publisher
        self._content_filter = content_filter

    async def resolve_profile_snapshot(self, sec_user_id: str) -> UserProfileSnapshot:
        profile_data = await self._crawler.handler_user_profile(sec_user_id)
        account_status = extract_account_status(profile_data)
        nickname = extract_nickname(profile_data) or sec_user_id[:12]
        avatar_url = extract_avatar_url(profile_data)
        return UserProfileSnapshot(
            nickname=nickname,
            avatar_url=avatar_url,
            account_status=str(account_status["account_status"]),
            account_status_label=str(account_status["account_status_label"]),
            account_status_reason=account_status["account_status_reason"],
        )

    async def sync_users(self, users: List[Dict[str, Any]], gaps: List[float]) -> Dict[str, Any]:
        summary = {"checked_users": 0, "downloaded_items": 0, "errors": []}
        for index, user in enumerate(users):
            summary["checked_users"] += 1
            await self.sync_one_user(user, summary)
            if index < len(gaps) and gaps[index] > 0:
                await asyncio.sleep(gaps[index])
        return summary

    async def sync_one_user(self, user: Dict[str, Any], summary: Dict[str, Any]) -> None:
        try:
            await self.run_with_cookie_rotation(lambda: self._sync_user_with_summary(user, summary))
        except Exception as exc:
            safe_error = sanitize_public_error(exc)
            user["last_error"] = safe_error
            summary["errors"].append(
                {"user_id": user["id"], "nickname": user["nickname"], "error": safe_error}
            )
        finally:
            user["last_checked_at"] = cn_now()
            user["updated_at"] = cn_now()

    async def run_with_cookie_rotation(self, operation):
        if self._cookie_rotation_session is None:
            return await operation()
        async with self._cookie_rotation_session() as _selected:
            return await operation()

    async def _sync_user_with_summary(self, user: Dict[str, Any], summary: Dict[str, Any]) -> None:
        old_status = str(user.get("account_status") or ACCOUNT_STATUS_NORMAL).strip()
        old_nickname = str(user.get("nickname") or "").strip()
        snapshot = await self.resolve_profile_snapshot(user["sec_user_id"])
        user.update(
            build_profile_snapshot_fields(
                snapshot,
                updated_at=cn_now(),
            )
        )
        # Preserve existing nickname when API returns empty (deleted/banned users)
        # resolve_profile_snapshot falls back to sec_user_id[:12] when nickname is empty
        if old_nickname and snapshot.nickname == user["sec_user_id"][:12]:
            user["nickname"] = old_nickname
        new_status = snapshot.account_status
        await self._notify_if_status_changed(user, old_status, new_status, snapshot.account_status_reason)
        # Auto-pause monitoring when account is deleted or banned
        if old_status == ACCOUNT_STATUS_NORMAL and new_status in (ACCOUNT_STATUS_DELETED, ACCOUNT_STATUS_BANNED):
            user["enabled"] = False
        summary["downloaded_items"] += await self._sync_user_latest(user, summary)
        summary["downloaded_items"] += await self.sync_history_backfill_step(user)
        user["last_error"] = None

    async def _notify_if_status_changed(
        self,
        user: Dict[str, Any],
        old_status: str,
        new_status: str,
        reason: str | None,
    ) -> None:
        if old_status == ACCOUNT_STATUS_NORMAL and new_status in (ACCOUNT_STATUS_DELETED, ACCOUNT_STATUS_BANNED):
            try:
                await self._notifier.notify_account_status_changed(
                    user_nickname=str(user.get("nickname") or ""),
                    old_status=old_status,
                    new_status=new_status,
                    reason=reason,
                )
            except Exception:
                pass

    async def _sync_user_latest(self, user: Dict[str, Any], summary: Dict[str, Any]) -> int:
        account_status = str(user.get("account_status") or ACCOUNT_STATUS_NORMAL).strip()
        if account_status in (ACCOUNT_STATUS_DELETED, ACCOUNT_STATUS_BANNED):
            return 0
        aweme_list = (await self._fetch_posts_page(
            user["sec_user_id"],
            max_cursor=0,
            count=INCREMENTAL_FETCH_POSTS,
        ))["aweme_list"]
        if not aweme_list:
            return 0
        downloaded_ids = set(user.get("downloaded_aweme_ids", []))
        new_posts = self._select_new_posts(aweme_list, downloaded_ids)
        if not new_posts:
            user["last_aweme_id"] = str(aweme_list[0].get("aweme_id", ""))
            return 0

        headers = (await self._crawler.get_douyin_headers())["headers"]
        # 单个作品失败不丢弃本批已完成的下载：已落盘的作品记入 downloaded_aweme_ids，
        # 否则下一轮会重新选中并重复推送通知。失败作品记入 summary，下轮自然重试。
        downloaded_records, errors = await self._download_posts(
            user=user,
            posts=new_posts,
            headers=headers,
            notify=True,
            continue_on_error=True,
        )
        for error in errors:
            summary["errors"].append(
                {"user_id": user["id"], "nickname": user["nickname"], "error": error}
            )
        return self._apply_download_results(
            user,
            downloaded_records,
            last_aweme_id=str(aweme_list[0].get("aweme_id", "")),
        )

    async def sync_history_backfill_step(self, user: Dict[str, Any]) -> int:
        history_sync = normalize_history_sync_state(user)
        if history_sync["status"] not in ACTIVE_HISTORY_SYNC_STATUSES:
            return 0

        # Fetch before mutating progress: a page-level failure must retain its cursor and retry queue.
        page = None
        if history_sync["has_more"]:
            page = await self._fetch_posts_page(
                user["sec_user_id"],
                max_cursor=int(history_sync["next_cursor"]),
                count=int(history_sync["page_size"]),
            )
        aweme_list = page["aweme_list"] if page is not None else []
        downloaded_ids = set(user.get("downloaded_aweme_ids", []))
        pending = [item for item in history_sync["pending_awemes"] if item["aweme_id"] not in downloaded_ids]
        retries = pending[:HISTORY_RETRY_LIMIT]
        new_posts = self._select_new_posts(aweme_list, downloaded_ids | {item["aweme_id"] for item in pending})
        posts = [{"aweme_id": item["aweme_id"]} for item in retries] + new_posts
        downloaded_records: List[Dict[str, Any]] = []
        errors: List[str] = []
        failed_posts: Dict[str, str] = {}
        if posts:
            headers = (await self._crawler.get_douyin_headers())["headers"]
            downloaded_records, errors = await self._download_posts(
                user=user,
                posts=posts,
                headers=headers,
                notify=False,
                continue_on_error=True,
                failed_posts=failed_posts,
            )

        # Move attempted failures to the tail so permanently broken works cannot starve later retries.
        history_sync["pending_awemes"] = pending[HISTORY_RETRY_LIMIT:] + [
            {"aweme_id": aweme_id, "error": error} for aweme_id, error in failed_posts.items()
        ]
        downloaded_count = self._apply_download_results(user, downloaded_records)
        if page is not None and aweme_list:
            self._update_history_sync_progress(
                page=page,
                history_sync=history_sync,
                scanned_count=len(aweme_list),
                downloaded_count=downloaded_count,
                errors=errors,
            )
        else:
            history_sync["downloaded_items"] += downloaded_count
            if page is not None:
                complete_history_sync(history_sync, now=cn_now())
            else:
                refresh_history_sync_status(history_sync, now=cn_now())
        return downloaded_count

    async def _download_posts(
        self,
        user: Dict[str, Any],
        posts: List[Dict[str, Any]],
        headers: Dict[str, str],
        *,
        notify: bool,
        continue_on_error: bool,
        failed_posts: Dict[str, str] | None = None,
    ) -> tuple[List[Dict[str, Any]], List[str]]:
        downloaded_records: List[Dict[str, Any]] = []
        errors: List[str] = []
        for post in reversed(posts):
            aweme_id = str(post.get("aweme_id", ""))
            try:
                aweme_detail, download_headers = await self._fetch_detail_for_download(aweme_id, headers)
                if notify:
                    await self._notifier.notify_new_aweme_detected(
                        user_nickname=user["nickname"],
                        aweme_detail=aweme_detail,
                    )
                asset_result = await self._downloader.download_aweme_assets(
                    aweme_id=aweme_id,
                    sec_user_id=user["sec_user_id"],
                    user_nickname=user["nickname"],
                    aweme_detail=aweme_detail,
                    headers=download_headers,
                )
                record = self._build_download_record(
                    aweme_detail,
                    asset_result,
                    source="incremental" if notify else "history",
                )
                if self._cover_publisher is not None:
                    await self._cover_publisher(user, record)
                if self._content_filter is not None:
                    # 自身吞掉并记录失败，结论写进 record["content_filter"]
                    await self._content_filter(user, aweme_detail, record)
                if notify:
                    await self._notifier.notify_download_completed(
                        user_nickname=user["nickname"],
                        record=record,
                    )
                downloaded_records.append(record)
            except Exception as exc:
                if not continue_on_error:
                    raise
                safe_error = sanitize_public_error(exc)
                errors.append(f"{aweme_id or '-'}: {safe_error}")
                if failed_posts is not None:
                    failed_posts[aweme_id] = safe_error
        return downloaded_records, errors

    async def _fetch_detail_for_download(
        self, aweme_id: str, login_headers: Dict[str, str]
    ) -> tuple[Dict[str, Any], Dict[str, str]]:
        """先用过期 Cookie 取详情（下载也带它）；取不到再用登录态重试一次并记日志，免得漏作品。"""
        pool = self._detail_cookie_pool
        if pool is not None:
            try:
                name, cookie = pool.take()
            except ParseCookiesUnavailable as exc:
                LOGGER.warning("过期 Cookie 不可用，作品 %s 改用登录态: %s", aweme_id, exc)
            else:
                try:
                    data = await self._crawler.fetch_one_video(aweme_id, cookie=cookie)
                    detail = data.get("aweme_detail") if isinstance(data, dict) else None
                    if isinstance(detail, dict):
                        return detail, (await self._crawler.get_douyin_headers(cookie))["headers"]
                    reason = "缺少 aweme_detail"
                except Exception as exc:  # noqa: BLE001
                    reason = sanitize_public_error(exc)
                LOGGER.warning("过期 Cookie %s 取作品 %s 详情失败（%s），改用登录态重试", name, aweme_id, reason)
        detail_data = await self._crawler.fetch_one_video(aweme_id)
        aweme_detail = detail_data.get("aweme_detail")
        if not isinstance(aweme_detail, dict):
            raise ValueError(f"作品 {aweme_id} 缺少 aweme_detail")
        return aweme_detail, login_headers

    async def _fetch_posts_page(
        self,
        sec_user_id: str,
        *,
        max_cursor: int,
        count: int,
    ) -> Dict[str, Any]:
        data = await self._crawler.fetch_user_post_videos(
            sec_user_id,
            max_cursor=max_cursor,
            count=count,
        )
        aweme_list = self._normalize_aweme_list(data.get("aweme_list"))
        aweme_list.sort(key=lambda item: item.get("create_time", 0), reverse=True)
        return {**data, "aweme_list": aweme_list}

    def _normalize_aweme_list(self, raw_aweme_list: Any) -> List[Dict[str, Any]]:
        if raw_aweme_list is None:
            return []
        if not isinstance(raw_aweme_list, list):
            raise ValueError("接口返回的 aweme_list 不是列表")
        if not all(isinstance(item, dict) for item in raw_aweme_list):
            raise ValueError("接口返回的 aweme_list 项格式无效")
        return raw_aweme_list

    def _select_new_posts(self, aweme_list: List[Dict[str, Any]], downloaded_ids: Set[str]) -> List[Dict[str, Any]]:
        result = []
        for item in aweme_list:
            aweme_id = str(item.get("aweme_id", ""))
            if aweme_id and aweme_id not in downloaded_ids:
                result.append(item)
        return result

    def _apply_download_results(
        self,
        user: Dict[str, Any],
        downloaded_records: List[Dict[str, Any]],
        *,
        last_aweme_id: str | None = None,
    ) -> int:
        downloaded_post_ids = [
            str(record.get("aweme_id", "")).strip()
            for record in downloaded_records
            if str(record.get("aweme_id", "")).strip()
        ]
        if last_aweme_id is not None:
            user["last_aweme_id"] = last_aweme_id
        if not downloaded_post_ids:
            return 0

        user["last_download_at"] = cn_now()
        user["downloaded_count"] = int(user.get("downloaded_count", 0)) + len(downloaded_post_ids)
        user["downloaded_aweme_ids"] = merge_downloaded_ids(
            user.get("downloaded_aweme_ids", []),
            downloaded_post_ids,
        )
        user["download_records"] = merge_download_records(
            user.get("download_records", []),
            downloaded_records,
        )
        return len(downloaded_post_ids)

    def _update_history_sync_progress(
        self,
        *,
        history_sync: Dict[str, Any],
        page: Dict[str, Any],
        scanned_count: int,
        downloaded_count: int,
        errors: List[str],
    ) -> None:
        update_history_sync_progress(
            history_sync=history_sync,
            page=page,
            scanned_count=scanned_count,
            downloaded_count=downloaded_count,
            errors=errors,
            now=cn_now(),
        )

    def _build_download_record(
        self,
        aweme_detail: Dict[str, Any],
        asset_result: Dict[str, Any],
        *,
        source: str,
    ) -> Dict[str, Any]:
        aweme_id = str(aweme_detail.get("aweme_id", "")).strip()
        media_type = str(asset_result.get("media_type", "")).strip() or "unknown"
        files = asset_result.get("files", [])
        if not isinstance(files, list):
            files = []
        record = {
            "aweme_id": aweme_id,
            "desc": str(aweme_detail.get("desc", "")).strip(),
            "publish_time": format_publish_time(aweme_detail.get("create_time")),
            "downloaded_at": cn_now(),
            "media_type": media_type,
            "source": source,
            "image_count": safe_int(asset_result.get("image_count")),
            "downloaded_file_count": safe_int(asset_result.get("downloaded_file_count")),
            "existing_file_count": safe_int(asset_result.get("existing_file_count")),
            "total_size_bytes": safe_int(asset_result.get("total_size_bytes")),
            "files": [str(file_item) for file_item in files if str(file_item).strip()],
        }
        # 网页展示用的角色文件：封面、快播版本。老记录没有这些键，展示层会回落。
        for key in ("video_file", "fast_play_file", "cover_file"):
            value = str(asset_result.get(key) or "").strip()
            if value:
                record[key] = value
        fast_play_source = asset_result.get("fast_play_source")
        if isinstance(fast_play_source, dict):
            record["fast_play_source"] = dict(fast_play_source)
        if media_type == "video":
            video_info = aweme_detail.get("video", {})
            duration = video_info.get("duration") if isinstance(video_info, dict) else 0
            record["video_duration_ms"] = safe_int(duration)
            video_source = asset_result.get("video_source")
            if isinstance(video_source, dict):
                record["video_source"] = dict(video_source)
        return record
