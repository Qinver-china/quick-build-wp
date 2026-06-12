#!/bin/bash
# 下载 WordPress 安装包到 backend/assets，供目标机无法访问 GitHub 时本地上传部署。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ASSETS="$ROOT/backend/assets"
LOCALE="${1:-zh_CN}"

mkdir -p "$ASSETS"

case "$LOCALE" in
  zh_CN)
    URL="https://cn.wordpress.org/latest-zh_CN.zip"
    DEST="$ASSETS/wordpress-zh_CN.zip"
    ;;
  en_US)
    URL="https://wordpress.org/latest.zip"
    DEST="$ASSETS/wordpress-en_US.zip"
    ;;
  *)
    echo "不支持的语言包: $LOCALE（支持 zh_CN、en_US）" >&2
    exit 1
    ;;
esac

echo "正在下载 WordPress ($LOCALE) -> $DEST"
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
echo "完成。部署时若目标服务器无法访问 GitHub，将自动上传此安装包。"
