"""Merge independently verified cloud derivatives without rewriting download history."""

from __future__ import annotations

from copy import deepcopy

from douyin_user_monitor.monitor.playback_media import PlaybackMedia

MEDIA_FIELDS = (
    "width", "height", "bit_rate", "duration", "video_codec", "pixel_format",
    "audio_codecs", "is_mp4", "faststart",
)


class PublicationError(ValueError):
    """The evidence or current source record does not permit publication."""


def _check_publication(publication):
    if publication.get("remote_verified") is not True:
        raise PublicationError("Cloud artifacts have not passed remote verification")
    if not publication.get("playback_file") and not publication.get("cover_file"):
        raise PublicationError("Publication contains no verified derivative")
    if publication.get("original_file") and (
        publication["evidence"].get("original", {}).get("content_verified") is not True
        or not publication["original_media"].get("full_decode_verified")
    ):
        raise PublicationError("Recovered original must pass complete byte and decode verification")
    if not publication.get("playback_file"):
        return
    media = publication["playback_media"]
    inspected = PlaybackMedia(**{name: media[name] for name in MEDIA_FIELDS})
    if not inspected.ready or inspected.duration <= 0 or not media.get("full_decode_verified"):
        raise PublicationError("Playback does not satisfy H.264/AAC/faststart and full-decode verification")


def _check_current_source(user, record, publication):
    if user.get("sec_user_id") != publication["sec_user_id"]:
        raise PublicationError("Source user identity changed")
    if record.get("downloaded_at") != publication["downloaded_at"]:
        raise PublicationError("Source download revision changed")
    if publication["source_relative_path"] not in record.get("files", []):
        raise PublicationError("Source file is no longer in the download record")
    for source, field in (("playback_file", "fast_play_file"), ("cover_file", "cover_file"), ("original_file", "video_file")):
        target = publication.get(source)
        if not target:
            continue
        previous = publication.get("previous_" + field)
        if (record.get(field) or None) not in (previous, target):
            raise PublicationError(f"Concurrent {field} change; publication must be reviewed")


def _playback_source(publication):
    media = publication["playback_media"]
    return {
        **{name: deepcopy(media[name]) for name in MEDIA_FIELDS},
        "gear_name": "h264_cloud_" + publication["operation"],
        "quality_source": "cloud_repair",
        "verified_at": publication["verified_at"],
    }


def _merge_record(user, record, publication):
    _check_current_source(user, record, publication)
    result = deepcopy(record)
    repair = result.setdefault("cloud_playback_repair", {})
    repair.setdefault("previous_references", {
        key: deepcopy(record.get(key)) for key in ("fast_play_file", "fast_play_source", "cover_file")
    })
    if publication.get("original_file"):
        for key in ("video_file", "video_source"):
            repair["previous_references"].setdefault(key, deepcopy(record.get(key)))
    repair.update(deepcopy(publication["evidence"]))
    repair["verified_at"] = publication["verified_at"]
    for source, target in (("original_file", "video_file"), ("playback_file", "fast_play_file"), ("cover_file", "cover_file")):
        path = publication.get(source)
        if path:
            if path == publication["source_relative_path"]:
                raise PublicationError("Derivative cannot replace the source reference")
            if path not in result["files"]:
                result["files"].append(path)
            result[target] = path
    if publication.get("playback_file"):
        result["fast_play_source"] = _playback_source(publication)
    if publication.get("original_file"):
        result["video_source"] = {**{name: deepcopy(publication["original_media"][name]) for name in MEDIA_FIELDS},
                                  "gear_name": "recovered_highest_available", "quality_source": "upstream_recovery"}
    return result


def _merge_user(user, records, publications):
    pending = dict(publications)
    merged = []
    for record in records:
        publication = pending.pop(str(record.get("aweme_id")), None)
        merged.append(_merge_record(user, record, publication) if publication else record)
    if pending:
        raise PublicationError("Source work is no longer in the user record")
    return merged


def publish_verified(storage, publications):
    """Apply a verified batch in the storage lock; existing files retain their indices."""
    grouped = {}
    for publication in publications:
        _check_publication(publication)
        per_user = grouped.setdefault(publication["user_id"], {})
        if publication["aweme_id"] in per_user:
            raise PublicationError("Duplicate work in publication batch")
        per_user[publication["aweme_id"]] = deepcopy(publication)

    def merge(state):
        # 记录在 RecordStore；update_state 只为拿状态文件锁，与监控进程的记录写入串行
        pending = dict(grouped)
        merged = {}
        for user in state["users"]:
            updates = pending.pop(user["id"], None)
            if updates:
                merged[user["id"]] = _merge_user(user, storage.records.get(user["id"]), updates)
        if pending:
            raise PublicationError("Source user is no longer in monitor state")
        storage.records.put_many(merged)

    state = storage.update_state(merge)
    return {**state, "users": storage.records.attach_all(state["users"])}
