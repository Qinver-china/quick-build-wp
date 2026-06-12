#!/bin/bash
# 下载部署所需的本地备用资源（WordPress 安装包、WP-CLI、Redis 插件）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> 下载 WP-CLI"
bash "$ROOT/scripts/download_wp_cli.sh"

echo "==> 下载 WordPress 中文包"
bash "$ROOT/scripts/download_wordpress.sh" zh_CN

echo "==> 下载 Redis Object Cache 插件"
bash "$ROOT/scripts/download_redis_cache_plugin.sh"

echo ""
echo "全部资源已就绪：backend/assets/"
