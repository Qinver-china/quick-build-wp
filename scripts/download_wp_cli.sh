#!/bin/bash
# 下载 WP-CLI 到 backend/assets，供部署时通过 SSH 上传到目标服务器。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/backend/assets/wp-cli.phar"
URL="https://raw.githubusercontent.com/wp-cli/builds/gh-pages/phar/wp-cli.phar"

mkdir -p "$(dirname "$DEST")"

echo "正在下载 WP-CLI -> $DEST"
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
echo "完成。部署时将自动上传此文件到目标服务器。"
