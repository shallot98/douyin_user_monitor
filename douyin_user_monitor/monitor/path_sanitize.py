r"""Path component sanitising shared with `scripts/openlist-auto-move`.

Why this exists: the primary archive drive (wopan) rejects directory names that
contain emoji — it answers with a misleading `8005 登录失败！`. The mover therefore
strips emoji from directory names when writing to the primary drive, while the
backup drive and `download_records[].files` keep the original names. Playback has
to apply the same transform to find files on the primary drive.

The emoji table below is generated from the very perl this repo's shell scripts
use (`perl -CSDA -Mutf8 -pe 's/\p{Extended_Pictographic}//g'`), so both sides
agree by construction. `tests/test_path_sanitize.py` re-checks the agreement
against real account/title names.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

# perl 的 \p{Extended_Pictographic} 码点区间（由该 perl 导出，勿手改）
EXTENDED_PICTOGRAPHIC_RANGES: Tuple[Tuple[int, int], ...] = (
    (0xA9, 0xA9), (0xAE, 0xAE), (0x203C, 0x203C), (0x2049, 0x2049),
    (0x2122, 0x2122), (0x2139, 0x2139), (0x2194, 0x2199), (0x21A9, 0x21AA),
    (0x231A, 0x231B), (0x2328, 0x2328), (0x2388, 0x2388), (0x23CF, 0x23CF),
    (0x23E9, 0x23F3), (0x23F8, 0x23FA), (0x24C2, 0x24C2), (0x25AA, 0x25AB),
    (0x25B6, 0x25B6), (0x25C0, 0x25C0), (0x25FB, 0x25FE), (0x2600, 0x2605),
    (0x2607, 0x2612), (0x2614, 0x2685), (0x2690, 0x2705), (0x2708, 0x2712),
    (0x2714, 0x2714), (0x2716, 0x2716), (0x271D, 0x271D), (0x2721, 0x2721),
    (0x2728, 0x2728), (0x2733, 0x2734), (0x2744, 0x2744), (0x2747, 0x2747),
    (0x274C, 0x274C), (0x274E, 0x274E), (0x2753, 0x2755), (0x2757, 0x2757),
    (0x2763, 0x2767), (0x2795, 0x2797), (0x27A1, 0x27A1), (0x27B0, 0x27B0),
    (0x27BF, 0x27BF), (0x2934, 0x2935), (0x2B05, 0x2B07), (0x2B1B, 0x2B1C),
    (0x2B50, 0x2B50), (0x2B55, 0x2B55), (0x3030, 0x3030), (0x303D, 0x303D),
    (0x3297, 0x3297), (0x3299, 0x3299), (0x1F000, 0x1F0FF), (0x1F10D, 0x1F10F),
    (0x1F12F, 0x1F12F), (0x1F16C, 0x1F171), (0x1F17E, 0x1F17F), (0x1F18E, 0x1F18E),
    (0x1F191, 0x1F19A), (0x1F1AD, 0x1F1E5), (0x1F201, 0x1F20F), (0x1F21A, 0x1F21A),
    (0x1F22F, 0x1F22F), (0x1F232, 0x1F23A), (0x1F23C, 0x1F23F), (0x1F249, 0x1F3FA),
    (0x1F3FB, 0x1F3FF), (0x1F400, 0x1F53D), (0x1F546, 0x1F64F), (0x1F680, 0x1F6FF), (0x1F774, 0x1F77F),
    (0x1F7D5, 0x1F7FF), (0x1F80C, 0x1F80F), (0x1F848, 0x1F84F), (0x1F85A, 0x1F85F),
    (0x1F888, 0x1F88F), (0x1F8AE, 0x1F8FF), (0x1F90C, 0x1F93A), (0x1F93C, 0x1F945),
    (0x1F947, 0x1FAFF), (0x1FC00, 0x1FFFD),
)

# 与 shell 同源：额外去掉 VS16(FE0F)、ZWJ(200D) 与组合键帽符(20E3)。
# 注意 perl 的 \p{Extended_Pictographic} 不含肤色修饰符(1F3FB-1F3FF)与键帽符(20E3)，
# 但 wopan 拒收剥完的残留，两边都显式补剥（paths.sh 的 perl 有同步的区间）。
ZERO_WIDTH_CODEPOINTS = (0xFE0F, 0x200D, 0x20E3)

# wopan 实测还会拒绝这些「装饰字符」家族（剥完 emoji 后的残留），逐类探针证实：
#   数学字母数字符号(𝒔𝒆𝒙𝒚 风格) 1D400-1D7FF、埃及象形等冷门文种、区域指示符(国旗) 1F1E6-1F1FF。
# 这类家族无穷无尽，直接把 **BMP 之外的星球面** 全剥，只保留 CJK 扩展 B-F 的真汉字；
# BMP 里的数学字母区单独剥。paths.sh 的 perl 有同步的区间。
WOPAN_STRIP_RANGES: Tuple[Tuple[int, int], ...] = (
    (0x1D400, 0x1D7FF),    # Mathematical Alphanumeric Symbols
    (0x10000, 0x1FFFF),    # 星球面 SIP（emoji 已由上表处理，这里兜底剩余）
    (0x323B0, 0x10FFFF),   # CJK 扩展 G 之后的一切（标签、变体选择符补充等）
)

def _build_emoji_pattern() -> "re.Pattern[str]":
    parts = []
    for start, end in EXTENDED_PICTOGRAPHIC_RANGES:
        if start == end:
            parts.append(re.escape(chr(start)))
        else:
            parts.append(f"{re.escape(chr(start))}-{re.escape(chr(end))}")
    parts.extend(re.escape(chr(cp)) for cp in ZERO_WIDTH_CODEPOINTS)
    return re.compile("[" + "".join(parts) + "]")


_EMOJI_PATTERN = _build_emoji_pattern()


def _build_wopan_pattern() -> "re.Pattern[str]":
    parts = []
    for start, end in WOPAN_STRIP_RANGES:
        if start == end:
            parts.append(re.escape(chr(start)))
        else:
            parts.append(f"{re.escape(chr(start))}-{re.escape(chr(end))}")
    return re.compile("[" + "".join(parts) + "]")


_WOPAN_PATTERN = _build_wopan_pattern()
_WHITESPACE_RUN = re.compile(r"\s+")


def sanitize_component(text: str) -> str:
    """Strip emoji/exotic chars, collapse whitespace runs, trim — mirrors paths.sh."""
    stripped = _EMOJI_PATTERN.sub("", str(text or ""))
    stripped = _WOPAN_PATTERN.sub("", stripped)
    collapsed = _WHITESPACE_RUN.sub(" ", stripped)
    return collapsed.strip()


def sanitize_relative_dir(relative_dir: str) -> str:
    """Sanitise each path segment. Empty result for a segment means unusable."""
    text = str(relative_dir or "")
    if text in ("", "."):
        return "."
    parts = []
    for segment in text.split("/"):
        cleaned = sanitize_component(segment)
        if not cleaned:
            raise ValueError(f"目录名去除 emoji 后为空: {segment!r}")
        parts.append(cleaned)
    return "/".join(parts)


FALLBACK_DIR_PREFIX = "sec_"


def fallback_dir_for(sec_user_id: str) -> str:
    """sec_user_id 兜底目录名。

    全 emoji / 被 wopan 拒绝的目录名无法用去 emoji 形态落地，改用这个 ID 名。
    前缀 ``sec_`` 便于人眼在网盘目录列表里一眼辨认「这是 ID 兜底目录、非真昵称」，
    且与任何真昵称碰撞概率≈0。迁移 rename pass 与播放侧 ``_primary_relative`` 都
    调它，保证两端算出同一个目录名。
    """
    sid = str(sec_user_id or "").strip()
    return f"{FALLBACK_DIR_PREFIX}{sid}"


def sanitize_relative_path(
    relative: str, sec_user_id: Optional[str] = None, *, sanitize_filename: bool = True
) -> Optional[str]:
    """整段相对路径（目录段 + 文件名）sanitize，供主力/备份盘查找改名后的文件。

    与 :func:`sanitize_relative_dir` 的区别：本函数一并清洗文件名末段，并对首段
    （用户目录）去 emoji 后为空的情况用 ``sec_user_id`` 兜底——rename pass 把全
    emoji 目录改名为 ``fallback_dir_for(sec_user_id)``，播放侧用同一函数反推才
    能命中。两端共用本函数保证算出同一路径。``sanitize_filename=False``
    保留末段原名，匹配 mover 只清洗目标目录的模式。

    - 首段（用户目录）空 → ``fallback_dir_for(sec_user_id)``；``sec_user_id`` 为
      ``None`` 则返 ``None``（调用方跳过该候选）。
    - 非首段目录空 → ``raise ValueError``（中段无法兜底，调用方跳过该候选）。
    - 文件名（末段）空 → 返 ``None``（实测下载器文件名恒带 ``时间戳_awemeid_`` 前缀，
      此分支不可达，占位防 panic）。
    """
    text = str(relative or "").strip().strip("/")
    if not text:
        return None
    parts = text.split("/")
    cleaned_parts: List[str] = []
    for idx, segment in enumerate(parts):
        is_last = idx == len(parts) - 1
        cleaned = segment if is_last and not sanitize_filename else sanitize_component(segment)
        if cleaned:
            cleaned_parts.append(cleaned)
            continue
        is_first = idx == 0
        if is_last:
            # 文件名（或单段纯 emoji）去 emoji 后为空，无法兜底
            return None
        if is_first:
            # 首段用户目录空 → sec_user_id 兜底
            if not sec_user_id:
                return None
            cleaned_parts.append(fallback_dir_for(sec_user_id))
        else:
            raise ValueError(f"目录名去除 emoji 后为空: {segment!r}")
    return "/".join(cleaned_parts)


def trim_relative_dirs(relative: str) -> Optional[str]:
    """去掉各目录段首尾空白，文件名保持原样；与 mover ``trim_relative_dir`` 同源。

    D2/夸克不接受尾空格目录名，mover 落地时去掉；记录里仍存原名。
    无需变换或某段变空时返回 ``None``（调用方跳过该候选）。
    """
    text = str(relative or "").strip().strip("/")
    parts = text.split("/")
    if len(parts) < 2:
        return None
    dirs = [segment.strip() for segment in parts[:-1]]
    if any(not segment for segment in dirs):
        return None
    trimmed = "/".join([*dirs, parts[-1]])
    return trimmed if trimmed != text else None


def sanitize_user_dir_only(relative: str, sec_user_id: Optional[str] = None) -> Optional[str]:
    """只清洗首段用户目录，其余目录与文件名保持原名。

    去 emoji 迁移里有一批作品只改了用户目录、作品目录没改成功（如
    ``小米糕/图片/20260717_..._黑武士和他的公主🪽/1.jpg``），全清洗和全原名两种
    候选都命中不了。首段空时同样用 ``sec_user_id`` 兜底；单段路径没有用户目录，返 ``None``。
    """
    text = str(relative or "").strip().strip("/")
    first, sep, rest = text.partition("/")
    if not sep or not rest:
        return None
    cleaned = sanitize_component(first)
    if not cleaned:
        if not sec_user_id:
            return None
        cleaned = fallback_dir_for(sec_user_id)
    return f"{cleaned}/{rest}"
