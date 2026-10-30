"""只读统计：还有多少作品没打 AI 分（content_filter 缺失/版本过旧/检测失败）。"""
from collections import Counter

from douyin_user_monitor.monitor.content_filter import RESULT_VERSION
from douyin_user_monitor.monitor.storage import MonitorStorage
from douyin_user_monitor.settings_loaders import load_settings

settings = load_settings()
storage = MonitorStorage(settings.monitor.state_path)
users = storage.records.attach_all(storage.load_state()["users"])

total = 0
done = 0
stale = 0
missing = 0
err = 0
err_versions = Counter()
cats = Counter()
per_user_pending = Counter()

for user in users:
    uid = str(user.get("id") or "")
    nickname = user.get("nickname") or uid
    records = storage.records.get(uid)
    for record in records:
        if not isinstance(record, dict) or not record.get("aweme_id"):
            continue
        total += 1
        result = record.get("content_filter")
        if not isinstance(result, dict):
            missing += 1
            per_user_pending[nickname] += 1
            continue
        version = int(result.get("version") or 0)
        status = result.get("status")
        if status == "error":
            err += 1
            err_versions[version] += 1
            per_user_pending[nickname] += 1
            continue
        if version < RESULT_VERSION:
            stale += 1
            per_user_pending[nickname] += 1
            continue
        done += 1
        cats[result.get("category")] += 1

print(f"RESULT_VERSION = {RESULT_VERSION}")
print(f"作品总数            : {total}")
print(f"已有有效打分        : {done}")
print(f"  分类分布          : {dict(cats)}")
print(f"从未打过（无字段）  : {missing}")
print(f"版本过旧需重打      : {stale}")
print(f"检测失败(error)     : {err}  按版本 {dict(err_versions)}")
print(f"待补打合计(不含retry): {missing + stale}")
print(f"待补打合计(含retry)  : {missing + stale + err}")
print("\nTop 15 待补打用户（含 error）:")
for nickname, count in per_user_pending.most_common(15):
    print(f"  {count:6d}  {nickname}")
print(f"\n涉及用户数: {len(per_user_pending)} / {len(users)}")
