import asyncio
from pathlib import Path


class NewWorkCoverPublisher:
    def __init__(self, download_root: Path, cover_store):
        self._download_root = download_root.resolve()
        self._cover_store = cover_store

    async def publish(self, user: dict, record: dict) -> None:
        await asyncio.to_thread(self._publish, user, record)

    def _publish(self, user: dict, record: dict) -> None:
        relative_path = str(record.get("cover_file") or "").strip()
        if not relative_path:
            raise ValueError(f"作品 {record['aweme_id']} 缺少封面文件")
        source = (self._download_root / relative_path).resolve()
        if not source.is_relative_to(self._download_root):
            raise ValueError(f"作品 {record['aweme_id']} 的封面文件不在下载目录内")
        if not source.is_file():
            raise FileNotFoundError(f"作品 {record['aweme_id']} 的封面文件不存在：{relative_path}")
        self._cover_store.publish(str(user["id"]), record, source)
