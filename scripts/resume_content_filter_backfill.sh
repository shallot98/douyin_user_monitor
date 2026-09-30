#!/usr/bin/env bash
# 定时恢复 AI 打分补打（Console 额度恢复后）：主任务并发 2，结束后自动重跑失败的
set -euo pipefail
cd /root/douyin_user_monitor
[ -f runtime/content_filter_backfill.json ] && cp runtime/content_filter_backfill.json "runtime/content_filter_backfill.before-resume-$(date +%Y%m%d%H%M).json"
COMMON=(-p WorkingDirectory=/root/douyin_user_monitor -p EnvironmentFile=/etc/douyin-monitor/auth.env
        -p CPUQuota=100% -p MemoryMax=1G -p Nice=19 -p IOSchedulingClass=idle
        -p StandardOutput=append:/root/douyin_user_monitor/logs/content_filter_backfill.log -p StandardError=inherit)
systemctl reset-failed dymon-ai-backfill dymon-ai-backfill-retry 2>/dev/null || true
systemd-run --unit=dymon-ai-backfill --collect "${COMMON[@]}" \
  /root/douyin_user_monitor/.venv/bin/python -m scripts.content_filter_backfill --concurrency 2
systemd-run --unit=dymon-ai-backfill-retry --collect "${COMMON[@]}" \
  /bin/bash -c 'sleep 30; while systemctl is-active --quiet dymon-ai-backfill; do sleep 60; done; exec /root/douyin_user_monitor/.venv/bin/python -m scripts.content_filter_backfill --concurrency 2 --retry-errors --progress runtime/content_filter_backfill_retry.json'
