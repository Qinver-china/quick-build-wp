#!/bin/bash
# 下载 WordPress Redis Object Cache 插件到 backend/assets，供目标机无法访问 WordPress.org 时上传安装。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ASSETS="$ROOT/backend/assets"
URL="https://downloads.wordpress.org/plugin/redis-cache.latest-stable.zip"
DEST="$ASSETS/redis-cache.zip"

mkdir -p "$ASSETS"

echo "正在下载 redis-cache 插件 -> $DEST"
if command -v curl >/dev/null 2>&1; then
  curl -fsSL -o "$DEST" "$URL"
elif command -v wget >/dev/null 2>&1; then
  wget -qO "$DEST" "$URL"
else
  echo "需要 curl 或 wget" >&2
  exit 1
fi

chmod 644 "$DEST"
ls -lh "$DEST"
echo "完成。部署时若目标服务器无法访问 WordPress.org，将自动上传此插件包。"
