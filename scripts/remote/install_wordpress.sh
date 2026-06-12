#!/bin/bash
# Reference script for WordPress install on Baota LNMP
set -euo pipefail

SITE_PATH="{{ site_path }}"
SITE_URL="{{ site_url }}"
SITE_TITLE="{{ site_title }}"
DB_NAME="{{ db_name }}"
DB_USER="{{ db_user }}"
DB_PASS="{{ db_pass }}"
WP_USER="{{ wp_admin_user }}"
WP_PASS="{{ wp_admin_password }}"
WP_EMAIL="{{ wp_admin_email }}"
WP_LOCALE="{{ wp_locale }}"

# WP-CLI：部署程序会先在目标机 curl 下载（30s 超时），失败再从本地上传 backend/assets/wp-cli.phar
command -v wp >/dev/null || {
  cd /tmp
  curl -sO --connect-timeout 10 --max-time 30 \
    https://raw.githubusercontent.com/wp-cli/builds/gh-pages/phar/wp-cli.phar \
    && chmod +x wp-cli.phar && mv wp-cli.phar /usr/local/bin/wp
}
command -v wp >/dev/null || {
  echo "请先通过 Quick Build WP 部署流程安装 WP-CLI" >&2
  exit 1
}

cd "$SITE_PATH"
# 核心文件由部署程序根据 GitHub 可达性选择 WP-CLI 下载或本地上传安装包
if [ ! -f wp-includes/version.php ]; then
  echo "请先通过 Quick Build WP 部署流程安装 WordPress 核心文件" >&2
  exit 1
fi
wp config create --dbname="$DB_NAME" --dbuser="$DB_USER" --dbpass="$DB_PASS" --dbhost=localhost --allow-root
wp core install --url="$SITE_URL" --title="$SITE_TITLE" --admin_user="$WP_USER" --admin_password="$WP_PASS" --admin_email="$WP_EMAIL" --skip-email --allow-root
wp option update permalink_structure '/%postname%/' --allow-root
chown -R www:www "$SITE_PATH"
