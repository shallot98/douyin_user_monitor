# 测试与发布门

Role: baseline
Updated: 2026-10-30

## 命令

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider   # 约 63 秒
.venv/bin/ruff check .
node --test tests/test_works_viewer.cjs tests/test_swipe_core.cjs
```

必须用 `.venv/bin/python`：系统 `python3` 缺 `aiofiles` 等依赖，`monitor/__init__.py` 会连带导入下载器而失败。`pytest-timeout` 未安装。

`tests/conftest.py` 把 `DYMON_STATE_PATH` 指向临时目录。2026-10-30 全量运行期间未发现测试写入真实 `data/`。

## 2026-10-30 实测（修复前，工作区含未提交改动）

| 门 | 结果 |
|----|------|
| pytest | 1071 passed、1 failed、201 subtests passed |
| ruff | 22 errors（17 I001 import 排序、2 E731、1 E402、1 F401、1 F841 在 `content_filter.py:1147`）；19 个可 `--fix` |
| node | `test_works_viewer.cjs` 20/20，`test_swipe_core.cjs` 8/8 |

唯一失败：`tests/test_dashboard_ui.py::test_statistics_summary_uses_dark_theme_surface` 断言 `common.css?v=theme-20260921b`，页面实际为 `theme-20261006a`。是写死的缓存版本号过期，不是功能回归。

修复后（同日，`4106ead` 之后的 5 个提交）：ruff 0 error；pytest 1072 passed（clean checkout 1071 passed + 1 skipped）；node 28/28。每个提交都单独在临时 worktree 按 CI 步骤验证通过。

## CI（`.github/workflows/ci.yml`）

push main / PR 触发，Python 3.10：安装 `requirements-dev.txt` → `ruff check .` → 复制两份 example 配置 → `pytest tests/ -q`。2026-10-30 起另装 ffmpeg（播放/封面测试真实调用）并跑 `node --test tests/*.cjs`。无覆盖率、无类型检查、无部署。功能分支 push 不触发，靠 PR #1 触发。首次 GitHub 运行（2026-10-30，PR #1）pytest 因 runner 无 ffmpeg 失败 22 项，已补装。

## 测试覆盖空白

2026-10-31 已补 `cover_image`、`media_urls`、`schedule`、`sync_utils`、`telegram_notifier`、`timeutils` 的直接测试。仍无直接测试：`__main__`、`api/models`、`cookie_pool_rotation_write`（可能被间接覆盖）。

## 发布方式

无制品。改代码 → 跑上述三项 → `restart_8900.sh` → 检查 `/api/monitor/status` 与关键页面。shell mover 改动在下一次 timer 触发时生效，无需重启。
