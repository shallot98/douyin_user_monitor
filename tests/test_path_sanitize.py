"""Python 与 shell 的目录名去 emoji 必须逐字一致。

主力盘（wopan）拒绝含 emoji 的目录名，所以 mover 写入时会去掉 emoji，而备份盘和
download_records 保留原名。网页播放要用同一套变换才能在主力盘上找到文件——两边
一旦不一致，就会出现「文件在盘上但页面 404」，且非常难查。

这里除了单元用例，还拿仓库里真实的账号名/标题名跑全量对比。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path
from typing import List

from douyin_user_monitor.monitor.path_sanitize import (
    FALLBACK_DIR_PREFIX,
    fallback_dir_for,
    sanitize_component,
    sanitize_relative_dir,
    sanitize_relative_path,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = REPO_ROOT / "data" / "monitor_users.json"


def shell_sanitize_component(text: str) -> str:
    """调用 scripts/openlist-auto-move/paths.sh 里真正在用的那段实现。"""
    script = f"""
    set -euo pipefail
    SCRIPT_DIR="{REPO_ROOT}/scripts"
    SCRIPT_BASENAME="openlist-auto-move-mp4.sh"
    DEFAULT_CONFIG_FILE="${{SCRIPT_DIR}}/openlist-auto-move.conf"
    OPENLIST_MOVER_PROJECT_ROOT="{REPO_ROOT}"
    source "${{SCRIPT_DIR}}/openlist-auto-move/common.sh"
    source "${{SCRIPT_DIR}}/openlist-auto-move/paths.sh"
    sanitize_component "$1"
    """
    result = subprocess.run(
        ["bash", "-c", script, "bash", text],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout


class SanitizeComponentTests(unittest.TestCase):
    def test_strips_trailing_and_leading_emoji(self):
        self.assertEqual(sanitize_component("鱼幼薇🐱"), "鱼幼薇")
        self.assertEqual(sanitize_component("💗是奶桑呀💗"), "是奶桑呀")

    def test_strips_variation_selector_and_zwj(self):
        # 柒柒🐿️ 里的 U+FE0F 必须一起去掉，否则残留不可见字符
        self.assertEqual(sanitize_component("柒柒🐿️"), "柒柒")
        self.assertEqual(sanitize_component("a‍b"), "ab")

    def test_keeps_plain_text_untouched(self):
        self.assertEqual(sanitize_component("纯中文昵称"), "纯中文昵称")
        self.assertEqual(sanitize_component("plain_name-1"), "plain_name-1")

    def test_collapses_whitespace_runs(self):
        self.assertEqual(sanitize_component("a   b"), "a b")
        self.assertEqual(sanitize_component("  边缘空格  "), "边缘空格")

    def test_emoji_only_name_becomes_empty(self):
        self.assertEqual(sanitize_component("🐱🍬"), "")

    def test_strips_skin_tone_modifier(self):
        # 肤色修饰符 U+1F3FB-1F3FF 曾在码点表里漏掉，剥完会留 🏻 残渣被 wopan 拒收
        self.assertEqual(sanitize_component("佳佳Joy👩🏻‍❤️‍👨🏻#旅行"), "佳佳Joy#旅行")
        self.assertEqual(sanitize_component("👩🏻‍❤️‍👨🏻"), "")

    def test_strips_combining_keycap(self):
        # U+20E3 组合键帽符（"2⃣" 的 ⃣）不是 Extended_Pictographic，必须与 VS16 一样显式剥掉
        self.assertEqual(sanitize_component("小水老师💧2⃣️（夏日版）"), "小水老师2（夏日版）")
        self.assertEqual(sanitize_component("1️⃣"), "1")

    def test_strips_wopan_rejected_exotic_classes(self):
        # 逐类探针证实 wopan 拒收：数学字母、象形文种、区域指示符（国旗）
        self.assertEqual(sanitize_component("𝒔𝒆𝒙𝒚"), "")
        self.assertEqual(sanitize_component("𝒔ummer"), "ummer")
        self.assertEqual(sanitize_component("𓇼"), "")
        self.assertEqual(sanitize_component("🇲🇴澳门"), "澳门")

    def test_keeps_wopan_accepted_exotic_chars(self):
        # 同样探针证实可落地：汉字扩展、组合符、全角（♡ 属 emoji 表，本来就剥）
        self.assertEqual(sanitize_component("𠮷野家"), "𠮷野家")
        self.assertEqual(sanitize_component("宝贝♡"), "宝贝")
        self.assertEqual(sanitize_component("a̷b"), "a̷b")


class SanitizeRelativeDirTests(unittest.TestCase):
    def test_each_segment_is_sanitised(self):
        self.assertEqual(
            sanitize_relative_dir("柒柒🐿️/图片/标题🍬"),
            "柒柒/图片/标题",
        )

    def test_dot_is_passthrough(self):
        self.assertEqual(sanitize_relative_dir("."), ".")
        self.assertEqual(sanitize_relative_dir(""), ".")

    def test_segment_that_becomes_empty_is_rejected(self):
        # 纯 emoji 目录名去掉后为空，无法在主力盘落地，必须显式失败而不是静默产出坏路径
        with self.assertRaises(ValueError):
            sanitize_relative_dir("🐱/图片")


class FallbackDirTests(unittest.TestCase):
    def test_prefixes_sec_user_id(self):
        self.assertEqual(fallback_dir_for("MS4wLjABC"), "sec_MS4wLjABC")

    def test_strips_surrounding_whitespace(self):
        self.assertEqual(fallback_dir_for("  sid-1  "), "sec_sid-1")

    def test_empty_yields_bare_prefix(self):
        # 调用方应先判 sec_user_id 非空；这里只保证不抛、产出可辨认的占位
        self.assertEqual(fallback_dir_for(""), "sec_")


class SanitizeRelativePathTests(unittest.TestCase):
    def test_strips_emoji_in_dirs_and_filename(self):
        self.assertEqual(
            sanitize_relative_path("鱼幼薇🐱/标题🍬/1️⃣.jpg"),
            "鱼幼薇/标题/1.jpg",
        )

    def test_filename_emoji_is_sanitised(self):
        # 以前 _primary_relative 只清洗目录段、文件名原样；现在文件名也去 emoji
        self.assertEqual(
            sanitize_relative_path("用户/带🍬表情的文件名.mp4"),
            "用户/带表情的文件名.mp4",
        )

    def test_directory_only_preserves_filename_and_internal_spaces(self):
        self.assertEqual(
            sanitize_relative_path("鱼幼薇🐱/标题🍬/带  🍬.mp4", sanitize_filename=False),
            "鱼幼薇/标题/带  🍬.mp4",
        )
        self.assertEqual(sanitize_relative_path("带🍬.mp4", sanitize_filename=False), "带🍬.mp4")

    def test_directory_only_keeps_sec_fallback_and_rejects_empty_middle_dir(self):
        self.assertEqual(
            sanitize_relative_path("🐱/图片/作品🍬.mp4", sec_user_id="sid-1", sanitize_filename=False),
            "sec_sid-1/图片/作品🍬.mp4",
        )
        with self.assertRaises(ValueError):
            sanitize_relative_path("用户/🐱/作品🍬.mp4", sanitize_filename=False)

    def test_first_segment_all_emoji_uses_sec_fallback(self):
        self.assertEqual(
            sanitize_relative_path("🐱🍬/图片/1.jpg", sec_user_id="sid-1"),
            "sec_sid-1/图片/1.jpg",
        )

    def test_first_segment_all_emoji_without_sec_returns_none(self):
        self.assertIsNone(sanitize_relative_path("🐱🍬/图片/1.jpg"))

    def test_middle_segment_all_emoji_raises(self):
        with self.assertRaises(ValueError):
            sanitize_relative_path("用户/🐱/1.jpg")

    def test_filename_all_emoji_returns_none(self):
        # 纯 emoji 文件名（实测下载器恒带时间戳前缀，此分支不可达，占位防 panic）
        self.assertIsNone(sanitize_relative_path("用户/图片/🐱🍬"))

    def test_single_segment_all_emoji_returns_none(self):
        self.assertIsNone(sanitize_relative_path("🐱🍬"))

    def test_single_segment_filename_is_sanitised(self):
        self.assertEqual(sanitize_relative_path("带🍬.mp4"), "带.mp4")

    def test_plain_path_unchanged(self):
        self.assertEqual(
            sanitize_relative_path("用户/图片/标题/1.jpg"),
            "用户/图片/标题/1.jpg",
        )

    def test_sec_fallback_prefix_matches_helper(self):
        path = sanitize_relative_path("☁️ ☁️/x.mp4", sec_user_id="ABC")
        self.assertEqual(path, f"{FALLBACK_DIR_PREFIX}ABC/x.mp4")


def _real_corpus() -> List[str]:
    """仓库真实数据里的账号目录名与图文标题目录名。"""
    if not STATE_FILE.is_file():
        return []
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    names = set()
    for user in state.get("users", []):
        nickname = str(user.get("nickname") or "").strip()
        if nickname:
            names.add(nickname)
        for record in user.get("download_records", []) or []:
            for file_path in record.get("files") or []:
                parts = str(file_path).split("/")
                if parts:
                    names.add(parts[0])
                if len(parts) >= 3 and parts[1] == "图片":
                    names.add(parts[2])
    return sorted(n for n in names if n)


@unittest.skipUnless(shutil.which("bash") and shutil.which("perl"), "需要 bash 与 perl")
class ShellParityTests(unittest.TestCase):
    """与 shell 实现的一致性：先跑固定样本，再跑真实语料全量。"""

    SAMPLES = (
        "鱼幼薇🐱",
        "💗是奶桑呀💗",
        "柒柒🐿️",
        "跳跳糖🍬",
        "🍍凤梨丫头（舞播全国版）",
        "芝芝很酷💫",
        "纯中文昵称",
        "plain-name_1",
        "a   b",
        "  两边空格  ",
        "𝘼𝙜𝙤𝙣𝙮",
        "▖▜▖▗",
        "🐱🍬",
        "小水老师💧2⃣️（夏日版）",
        "👩🏻‍❤️‍👨🏻#旅行碎片",
        "佳佳Joy👩🏻",
        "𝒔𝒆𝒙𝒚",
        "𓇼",
        "🇲🇴澳门",
        "𠮷野家",
    )

    def test_matches_shell_on_samples(self):
        for text in self.SAMPLES:
            with self.subTest(text=text):
                self.assertEqual(sanitize_component(text), shell_sanitize_component(text))

    def test_matches_shell_on_real_corpus(self):
        corpus = _real_corpus()
        if not corpus:
            self.skipTest("本机没有 data/monitor_users.json 语料")
        # 逐条 bash 调用很慢，抽样覆盖 + 全部含非 ASCII 的名字
        interesting = [name for name in corpus if any(ord(ch) > 0x2000 for ch in name)]
        sample = interesting[:120]
        self.assertGreater(len(sample), 0, "语料里应当有含特殊字符的名字")
        mismatches = []
        for name in sample:
            if sanitize_component(name) != shell_sanitize_component(name):
                mismatches.append(name)
        self.assertEqual(mismatches, [], f"与 shell 输出不一致: {mismatches[:5]}")


if __name__ == "__main__":
    unittest.main()
