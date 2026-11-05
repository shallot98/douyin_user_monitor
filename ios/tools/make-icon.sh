#!/usr/bin/env bash
# 生成 App 图标（改设计就改这里，重跑一遍）。
# 依赖 ImageMagick（Debian/Ubuntu: apt install imagemagick）。
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="DyMon/Assets.xcassets/AppIcon.appiconset"
mkdir -p "$OUT"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

convert -size 1024x1024 gradient:'#ff2f6d-#00d5ff' "$TMP/grad.png"
convert -size 1024x1024 xc:black -fill white -draw 'polygon 396,268 396,756 736,512' "$TMP/mask.png"
convert "$TMP/grad.png" "$TMP/mask.png" -alpha off -compose CopyOpacity -composite "$TMP/tri.png"
convert -size 1024x1024 xc:'#0b0b0f' -fill '#15151f' -draw 'roundrectangle 160,160 864,864 200,200' \
  "$TMP/tri.png" -gravity center -composite "$TMP/icon.png"

# App 图标不允许带透明通道，统一压成 8bit 不透明 PNG
convert "$TMP/icon.png" -background '#0b0b0f' -alpha remove -alpha off -depth 8 "$OUT/icon-1024.png"
for spec in 40:icon-40 60:icon-60 58:icon-58 87:icon-87 80:icon-80 120:icon-120 180:icon-180 120:icon-120b; do
  size="${spec%%:*}"
  name="${spec##*:}"
  convert "$OUT/icon-1024.png" -resize "${size}x${size}" \
    -background '#0b0b0f' -alpha remove -alpha off -depth 8 "$OUT/$name.png"
done
echo "icons -> $OUT"
