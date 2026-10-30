"""Former nicknames of a monitored user.

Why this exists: download folders are named by the nickname **at download time**
(`downloader._build_user_folder_name`), so a renamed user's archive ends up split
across several folders on the netdisk. `download_records[].files` keeps the exact
historical path per record, so nothing becomes unreachable — but you still need
the old names to recognise whose folder is whose.

Two sources, unioned:

- persisted `nickname_history`: appended when a rename is detected during sync,
  so it survives `download_records` eviction (capped at 300/user) and keeps the
  unsanitized name.
- derived from `download_records[].files` top-level directories: the only source
  for users who were renamed before this field existed.

Note the derived names are folder names, so they went through
`downloader.sanitize_text` (emoji/length normalisation) and may differ slightly
from the nickname the user actually had. For locating files on the netdisk that
is the more useful form anyway.
"""

from __future__ import annotations

from typing import Any, Dict, List

MAX_NICKNAME_HISTORY = 20


def _clean(value: Any) -> str:
    return str(value or "").strip()


def normalize_nickname_history(user: Dict[str, Any]) -> List[str]:
    """Coerce the stored field to a clean, deduped, capped list (newest first)."""
    raw = user.get("nickname_history")
    if not isinstance(raw, list):
        raw = []
    current = _clean(user.get("nickname"))
    history: List[str] = []
    seen = set()
    for item in raw:
        name = _clean(item)
        if not name or name == current or name in seen:
            continue
        seen.add(name)
        history.append(name)
        if len(history) >= MAX_NICKNAME_HISTORY:
            break
    user["nickname_history"] = history
    return history


def record_nickname_change(user: Dict[str, Any], previous_nickname: Any) -> None:
    """Push `previous_nickname` onto the user's history when it really changed.

    Called on the on-disk user inside `update_state`, so the "previous" value is
    the authoritative one rather than a possibly-stale out-of-lock snapshot.
    """
    previous = _clean(previous_nickname)
    current = _clean(user.get("nickname"))
    if not previous or not current or previous == current:
        normalize_nickname_history(user)
        return
    existing = normalize_nickname_history(user)
    user["nickname_history"] = [previous, *[name for name in existing if name != previous]][
        :MAX_NICKNAME_HISTORY
    ]


def derive_archive_dir_names(user: Dict[str, Any]) -> List[str]:
    """Top-level archive folders seen in this user's download records (newest first)."""
    records = user.get("download_records")
    if not isinstance(records, list):
        return []
    names: List[str] = []
    seen = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        files = record.get("files")
        if not isinstance(files, list):
            continue
        for file_path in files:
            top = _clean(file_path).split("/")[0]
            if not top or top in seen:
                continue
            seen.add(top)
            names.append(top)
    return names


def build_former_nicknames(user: Dict[str, Any]) -> List[str]:
    """Persisted history ∪ archive folder names, minus the current nickname."""
    current = _clean(user.get("nickname"))
    result: List[str] = []
    seen = set()
    raw_history = user.get("nickname_history")
    candidates = list(raw_history) if isinstance(raw_history, list) else []
    candidates.extend(derive_archive_dir_names(user))
    for candidate in candidates:
        name = _clean(candidate)
        if not name or name == current or name in seen:
            continue
        seen.add(name)
        result.append(name)
        if len(result) >= MAX_NICKNAME_HISTORY:
            break
    return result
