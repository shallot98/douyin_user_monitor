# systemd 单元

线上 `/etc/systemd/system/` 的副本（2026-10-30 同步）。改这里后要手动拷回并 `systemctl daemon-reload`。

| 单元 | 线上状态 |
|------|----------|
| `douyin-user-monitor-mover.{service,timer}` + `timer.d/lower-frequency.conf` | enabled，每 10 分钟（drop-in 覆盖原每分钟） |
| `douyin-playback-repair-sync.{service,timer}` + `timer.d/lower-frequency.conf` | 2026-10-30 起 **disabled**（播放修复队列已结束） |

`douyin-monitor-8900.service` 及其 drop-in（内存上限、Telegram 环境）未收入：它引用 `/etc/douyin-monitor/auth.env` 等本机凭据，见 `docs/plantree/baseline/runtime-flows.md`。

安装示例：

```bash
cp scripts/systemd/douyin-user-monitor-mover.* /etc/systemd/system/
mkdir -p /etc/systemd/system/douyin-user-monitor-mover.timer.d
cp scripts/systemd/douyin-user-monitor-mover.timer.d/*.conf /etc/systemd/system/douyin-user-monitor-mover.timer.d/
systemctl daemon-reload
```
