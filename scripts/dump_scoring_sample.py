"""抽样导出：把待复核作品的打分输入（帧拼图 + 元数据）落成文件，供人工/AI 复核。
只读，不写回任何记录。

    .venv/bin/python scripts/dump_scoring_sample.py --per-bucket 1
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
from pathlib import Path
from typing import Any, Dict, List

from PIL import Image

from douyin_user_monitor.monitor.archive_path_aliases import load_archive_path_aliases
from douyin_user_monitor.monitor.content_filter import extract_ad_signals, extract_hashtags
from douyin_user_monitor.monitor.content_filter_media import ArchiveMediaLoader
from douyin_user_monitor.monitor.cover_store import CoverStore
from douyin_user_monitor.monitor.openlist_client import OpenListClient
from douyin_user_monitor.monitor.storage import MonitorStorage
from douyin_user_monitor.settings_loaders import load_settings

FRAME_SHORT = 400


def montage(images: List[bytes], out: Path) -> None:
    tiles = []
    for data in images:
        im = Image.open(io.BytesIO(data)).convert("RGB")
        scale = FRAME_SHORT / min(im.size)
        tiles.append(im.resize((int(im.width * scale), int(im.height * scale))))
    cols = 3
    rows = (len(tiles) + cols - 1) // cols
    cell_w = max(t.width for t in tiles)
    cell_h = max(t.height for t in tiles)
    sheet = Image.new("RGB", (cols * cell_w, rows * cell_h), (12, 12, 20))
    for i, tile in enumerate(tiles):
        sheet.paste(tile, ((i % cols) * cell_w, (i // cols) * cell_h))
    sheet.save(out, quality=88)


def bucket_of(cf: Dict[str, Any]) -> str:
    category = cf.get("category")
    score = (cf.get("appearance") or {}).get("borderline") or {}
    total = int(score.get("score") or 0)
    if category in ("ad", "text_only", "screenshot"):
        return category
    if float(cf.get("confidence") or 1) < 0.7:
        return "low_confidence"
    if total >= 3:
        return "borderline_high"
    return "normal_plain"


WANTED = ("normal_plain", "borderline_high", "ad", "text_only", "screenshot", "low_confidence")


async def main(per_bucket: int, out_dir: Path) -> int:
    settings = load_settings()
    storage = MonitorStorage(settings.monitor.state_path)
    openlist = OpenListClient(settings.archive)
    loader = ArchiveMediaLoader(settings.monitor.download_root, openlist,
                               load_archive_path_aliases(settings.monitor.state_path),
                               CoverStore(settings.monitor.state_path.parent / "covers"))
    users = storage.records.attach_all(storage.load_state()["users"])
    picked: Dict[str, List[Dict[str, Any]]] = {key: [] for key in WANTED}

    for user in users:
        if all(len(v) >= per_bucket for v in picked.values()):
            break
        for record in storage.records.get(str(user.get("id") or "")):
            if not isinstance(record, dict):
                continue
            cf = record.get("content_filter")
            if not isinstance(cf, dict) or cf.get("status") != "ok":
                continue
            bucket = bucket_of(cf)
            if len(picked[bucket]) < per_bucket:
                picked[bucket].append({"user": user, "record": record, "model_result": cf})

    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    for bucket, items in picked.items():
        for idx, item in enumerate(items, 1):
            user, record = item["user"], item["record"]
            detail = {"desc": record.get("desc") or ""}
            try:
                media = await loader.prepare(user, record, settings.content_filter.max_frames)
            except Exception as exc:  # noqa: BLE001
                print(f"  ! {bucket}#{idx} 抽帧失败: {exc}")
                continue
            name = f"{bucket}_{idx}_{record['aweme_id']}"
            montage(media.images, out_dir / f"{name}.jpg")
            entry = {
                "id": name,
                "bucket": bucket,
                "nickname": user.get("nickname"),
                "aweme_id": record.get("aweme_id"),
                "media_type": record.get("media_type"),
                "desc": detail["desc"],
                "hashtags": extract_hashtags(detail),
                "signals": extract_ad_signals(detail),
                "motion": media.motion,
                "image_note": media.image_note,
                "frames": len(media.images),
                "media_source": media.source,
                "model_result": {k: item["model_result"].get(k) for k in
                                 ("category", "ad_type", "confidence", "reason", "tags",
                                  "appearance", "summary", "verdict")},
            }
            manifest.append(entry)
            print(f"  ✓ {name} frames={entry['frames']} src={entry['media_source']}")
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    await openlist.close()
    print(f"\n导出 {len(manifest)} 个样本 -> {out_dir}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--per-bucket", type=int, default=1)
    parser.add_argument("--out", default="/tmp/score_sample")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.per_bucket, Path(args.out))))
