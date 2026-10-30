"""离线重算擦边总分：把新的判定口径（caliber）套到已有结论上，一条请求都不发。

    python -m scripts.recalculate_borderline            # dry-run，只看差异
    python -m scripts.recalculate_borderline --apply    # 写回

violation 规则、胸部保底、滞回保守档 score_low / zone 都只是「已存分项」的纯函数，
所以历史 16,101 条不用重打就能换口径。

分项本身（模型给的 chest/hip/figure/overall/camera/motion）不动 —— 那要重新调模型，
是另一笔额度。写回在状态文件锁内逐条重算，不用批外快照，避免覆盖并发打分的结果。
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from typing import Any, Dict, List

from douyin_user_monitor.monitor.content_filter import CALIBER, derive_borderline
from douyin_user_monitor.monitor.storage import MonitorStorage
from douyin_user_monitor.settings_loaders import load_settings


def refresh(borderline: Dict[str, Any], *, hysteresis_delta: float) -> Dict[str, Any]:
    """按存储的分项重算派生字段；分项原样保留。"""
    return derive_borderline(borderline, hysteresis_delta=hysteresis_delta)


def _old(borderline: Dict[str, Any]) -> Dict[str, Any]:
    return {key: borderline.get(key) for key in ("weighted", "score", "violation")}


def recalculate_verdict(verdict: Dict[str, Any], *, hysteresis_delta: float) -> Dict[str, Any]:
    """重算派生字段并盖上口径章。

    没有擦边分段的结论（无人物 / 图文文字图）在新旧口径下分数映射完全相同，
    只需盖章 —— 不然下次换口径时分不清它们到底处理过没有。
    """
    appearance = verdict.get("appearance")
    borderline = appearance.get("borderline") if isinstance(appearance, dict) else None
    if not isinstance(borderline, dict):
        return {**verdict, "caliber": CALIBER}
    return {**verdict, "caliber": CALIBER,
            "appearance": {**appearance, "borderline": refresh(borderline, hysteresis_delta=hysteresis_delta)}}


def outdated(verdict: Any, *, retry_stale: bool) -> bool:
    """只处理已有成功结论的记录；口径已是当前值就跳过。"""
    if not isinstance(verdict, dict) or verdict.get("status") != "ok":
        return False
    return retry_stale or verdict.get("caliber") != CALIBER


def plan_for_user(records: List[Dict[str, Any]], *, hysteresis_delta: float,
                  retry_stale: bool) -> Dict[str, Dict[str, Any]]:
    """dry-run 用：返回 aweme_id → 新结论。"""
    return {str(record.get("aweme_id")): recalculate_verdict(record["content_filter"],
                                                             hysteresis_delta=hysteresis_delta)
            for record in records
            if isinstance(record, dict) and outdated(record.get("content_filter"), retry_stale=retry_stale)}


def apply_changes(storage: MonitorStorage, user_ids: List[str], *, hysteresis_delta: float,
                  retry_stale: bool) -> Counter:
    stats: Counter = Counter()

    def _apply(_state: Dict[str, Any]) -> None:
        for user_id in user_ids:
            # 锁内重读并就地重算：不拿批外快照，否则会把并发写入的新结论整行覆盖回去
            current = storage.records.get(user_id)
            updated_records = []
            touched = False
            for record in current:
                verdict = record.get("content_filter") if isinstance(record, dict) else None
                if not outdated(verdict, retry_stale=retry_stale):
                    if isinstance(verdict, dict) and verdict.get("status") == "ok":
                        stats["skipped_current_caliber"] += 1
                    updated_records.append(record)
                    continue
                updated = recalculate_verdict(verdict, hysteresis_delta=hysteresis_delta)
                new_borderline = (updated.get("appearance") or {}).get("borderline")
                stats["scanned"] += 1
                if not new_borderline:
                    stats["caliber_only"] += 1
                else:
                    old_borderline = (verdict.get("appearance") or {}).get("borderline") or {}
                    stats[f"to_{new_borderline['score']}"] += 1
                    if _old(old_borderline) != _old(new_borderline):
                        stats["score_changed"] += 1
                        if new_borderline["violation"] and not old_borderline.get("violation"):
                            stats["violation_added"] += 1
                updated_records.append({**record, "content_filter": updated})
                touched = True
            if touched:
                storage.records.put(user_id, updated_records)

    storage.update_state(_apply)
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="写回（默认只 dry-run）")
    parser.add_argument("--user-id", default=None, help="只处理某个用户")
    parser.add_argument("--retry-stale", action="store_true", help="已经是新口径的也重算一遍")
    args = parser.parse_args()

    settings = load_settings()
    delta = settings.content_filter.hysteresis_delta
    state_path = settings.monitor.state_path
    storage = MonitorStorage(state_path)
    users = [user for user in storage.records.attach_all(storage.load_state()["users"])
             if not args.user_id or str(user.get("id")) == args.user_id]

    if not args.apply:
        totals: Counter = Counter()
        for user in users:
            for candidate in plan_for_user(user.get("download_records") or [],
                                           hysteresis_delta=delta, retry_stale=args.retry_stale).values():
                totals["scanned"] += 1
                borderline = (candidate.get("appearance") or {}).get("borderline")
                if not borderline:
                    totals["caliber_only"] += 1
                    continue
                totals[f"to_{borderline['score']}"] += 1
                if borderline["zone"]:
                    totals["zone"] += 1
                if borderline["violation"]:
                    totals["violation"] += 1
        print(f"滞回 delta={delta}，caliber={CALIBER}，dry-run：")
        print(json.dumps(dict(sorted(totals.items())), ensure_ascii=False, indent=2))
        print("\n加 --apply 写回。")
        return 0

    stats = apply_changes(storage, [str(user.get("id")) for user in users],
                          hysteresis_delta=delta, retry_stale=args.retry_stale)
    print("写回完成：")
    print(json.dumps(dict(sorted(stats.items())), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
