import hashlib
import re
import secrets
import shlex
import string
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.sql_safety import (
    escape_mysql_string_literal,
    quote_shell,
    sanitize_db_credentials,
    sanitize_db_prefix,
    sanitize_sql_identifier,
)
from app.models.deploy import DeployTask
from app.services.baota_panel import ensure_baota_database, ensure_baota_site
from app.services.log_publisher import publish_log
from app.services.server_memory import REDIS_SKIP_MESSAGE, detect_ram_mb, should_install_redis
from app.services.remote_state import RemoteDeployState
from app.services.site_config import persist_site_db_credentials, resolve_sites_from_task
from app.services.ssh import SSHClient

ASSETS_DIR = Path(__file__).resolve().parents[2] / "assets"
WP_CLI_PHAR_PATH = ASSETS_DIR / "wp-cli.phar"
WP_CLI_REMOTE_PATH = "/usr/local/bin/wp"
WP_CLI_REMOTE_TMP = "/tmp/qbw_wp-cli.phar"
WP_CLI_DOWNLOAD_URL = (
    "https://raw.githubusercontent.com/wp-cli/builds/gh-pages/phar/wp-cli.phar"
)
WP_CLI_DOWNLOAD_TIMEOUT = 30
WP_CORE_DOWNLOAD_TIMEOUT = 30
GITHUB_CHECK_URL = "https://github.com"
GITHUB_CHECK_TIMEOUT = 10
WP_CORE_REMOTE_ZIP = "/tmp/qbw-wordpress.zip"
REDIS_CACHE_LOCAL_ZIP = ASSETS_DIR / "redis-cache.zip"
REDIS_CACHE_REMOTE_ZIP = "/tmp/qbw-redis-cache.zip"
REDIS_PLUGIN_DOWNLOAD_TIMEOUT = 30
WORDPRESS_ORG_CHECK_URL = "https://downloads.wordpress.org"
WORDPRESS_ORG_CHECK_TIMEOUT = 10
# 宝塔面板创建数据库时用户名长度上限（与 MySQL 版本无关）
BAOTA_DB_USER_MAX_LEN = 16


def _ssh(task: DeployTask, password: str, phase: str, db: Session) -> SSHClient:
    return SSHClient(
        host=task.ssh_host,
        port=task.ssh_port,
        username=task.ssh_user,
        password=password,
        task_id=task.id,
        log_phase=phase,
        db=db,
    )


def _domain_dir_name(domain: str) -> str:
    return domain.replace(".", "_")


def _chown_www_site(ssh: SSHClient, site_path: str, secrets: list[str]) -> None:
    """将站点目录属主设为 www；宝塔 .user.ini 可能被 chattr +i 锁定，需先解除。"""
    path = shlex.quote(site_path)
    script = f"""set +e
SITE_PATH={path}
USER_INI="$SITE_PATH/.user.ini"
if [ -f "$USER_INI" ]; then
  chattr -i "$USER_INI" 2>/dev/null
fi
chown -R www:www "$SITE_PATH"
CHOWN_RC=$?
if [ -f "$USER_INI" ]; then
  chown www:www "$USER_INI" 2>/dev/null
  chattr +i "$USER_INI" 2>/dev/null
fi
if [ "$CHOWN_RC" -ne 0 ]; then
  find "$SITE_PATH" -mindepth 1 ! -name '.user.ini' -exec chown www:www {{}} + 2>/dev/null
fi
exit 0
"""
    ssh.run_script(script, timeout=120, secrets=secrets)


def site_paths(task: DeployTask) -> tuple[str, str]:
    site_dir_name = _domain_dir_name(task.site_domain.strip().lower())
    site_path = f"/www/wwwroot/{site_dir_name}"
    conf_path = f"/www/server/panel/vhost/nginx/{site_dir_name}.conf"
    return site_path, conf_path


def build_site_info(task: DeployTask, site_spec: dict | None = None) -> dict:
    spec = site_spec or (resolve_sites_from_task(task)[0] if resolve_sites_from_task(task) else {})
    return build_site_info_from_spec(task, spec)


def build_site_info_from_spec(task: DeployTask, site_spec: dict) -> dict:
    domains = site_spec.get("domains") or []
    site_domain = (site_spec.get("primary_domain") or domains[0]).strip().lower()
    extra_domains = [d for d in domains if d != site_domain]
    site_dir_name = _domain_dir_name(site_domain)
    site_path = f"/www/wwwroot/{site_dir_name}"
    db_name, db_user, db_pass = _resolve_db_credentials_for_spec(site_spec)
    return {
        "site_path": site_path,
        "site_domain": site_domain,
        "domains": domains,
        "extra_domains": extra_domains,
        "site_dir_name": site_dir_name,
        "site_name": (site_spec.get("site_name") or "示例博客").strip(),
        "site_url": f"http://{site_domain}",
        "db_name": db_name,
        "db_user": db_user,
        "db_pass": db_pass,
        "db_prefix": _resolve_db_prefix_for_spec(site_spec),
        "is_domain": True,
        "wp_admin_user": site_spec.get("wp_admin_user") or "admin",
        "wp_admin_password": site_spec.get("wp_admin_password") or task.wp_admin_password,
        "wp_admin_email": site_spec.get("wp_admin_email") or task.wp_admin_email,
        "wp_locale": site_spec.get("wp_locale") or task.wp_locale,
    }


def _php_bin(task: DeployTask) -> str:
    ver = task.php_version.replace(".", "")
    return f"/www/server/php/{ver}/bin/php"


def _auto_db_identifiers(primary: str) -> tuple[str, str]:
    """生成符合宝塔用户名长度限制的库名/用户名。"""
    slug = _domain_dir_name(primary)
    candidate = f"wp_{slug}"
    if len(candidate) <= BAOTA_DB_USER_MAX_LEN:
        return candidate[:64], candidate[:BAOTA_DB_USER_MAX_LEN]
    digest = hashlib.md5(primary.strip().lower().encode()).hexdigest()[:8]
    base = f"wp_{digest}"
    return base[:64], base[:BAOTA_DB_USER_MAX_LEN]


def _resolve_db_credentials_for_spec(site_spec: dict) -> tuple[str, str, str]:
    primary = (site_spec.get("primary_domain") or "").strip().lower()
    if not primary and site_spec.get("domains"):
        primary = site_spec["domains"][0]

    auto_name, auto_user = _auto_db_identifiers(primary)
    raw_user = site_spec.get("db_user")
    cleaned_user = sanitize_sql_identifier(raw_user, max_len=BAOTA_DB_USER_MAX_LEN) if raw_user else ""

    if site_spec.get("db_name"):
        db_name = sanitize_sql_identifier(site_spec["db_name"], max_len=64, default=auto_name)
    else:
        db_name = auto_name

    if cleaned_user:
        if len(cleaned_user) > BAOTA_DB_USER_MAX_LEN:
            db_user = auto_user
        else:
            db_user = cleaned_user
    elif len(db_name) <= BAOTA_DB_USER_MAX_LEN:
        db_user = db_name
    else:
        db_user = auto_user

    db_pass = site_spec.get("db_password") or _gen_db_password()
    db_name, db_user, db_pass, _ = sanitize_db_credentials(
        db_name,
        db_user,
        db_pass,
        _resolve_db_prefix_for_spec(site_spec),
        user_max_len=BAOTA_DB_USER_MAX_LEN,
        name_max_len=64,
    )
    return db_name, db_user, db_pass


def _wp_cli_option(flag: str, value: str) -> str:
    return f"{flag}={quote_shell(value)}"


def _build_create_database_script(db_name: str, db_user: str, db_pass: str) -> str:
    """纯 shell + mysql 建库；root 密码仅通过 btpython/面板内置 Python 读取（避免系统 python 缺 psutil）。"""
    safe_name, safe_user, safe_pass, _ = sanitize_db_credentials(
        db_name,
        db_user,
        db_pass,
        "wp_",
        user_max_len=BAOTA_DB_USER_MAX_LEN,
        name_max_len=64,
    )
    user_sql = escape_mysql_string_literal(safe_user)
    pass_sql = escape_mysql_string_literal(safe_pass)
    return f"""set -euo pipefail
export QBW_DB_NAME={quote_shell(safe_name)}
export QBW_DB_USER_SQL={quote_shell(user_sql)}
export QBW_DB_PASS_SQL={quote_shell(pass_sql)}
MYSQL_BIN="/www/server/mysql/bin/mysql"
[ -x "$MYSQL_BIN" ] || MYSQL_BIN="mysql"

_read_mysql_root() {{
  local root="" py=""
  for py in btpython /www/server/panel/pyenv/bin/python3 /www/server/panel/pyenv/bin/python; do
    if command -v "$py" >/dev/null 2>&1 || [ -x "$py" ]; then
      root=$("$py" -c "import sys; sys.path.insert(0,'/www/server/panel/class'); import public; print(public.M('config').where('id=?',(1,)).getField('mysql_root') or '')" 2>/dev/null || true)
      [ -n "$root" ] && break
    fi
  done
  echo "$root"
}}

_mysql_ping() {{
  local root="$1"
  if [ -n "$root" ]; then
    MYSQL_PWD="$root" "$MYSQL_BIN" -uroot -h127.0.0.1 -e "SELECT 1" >/dev/null 2>&1
    return $?
  fi
  "$MYSQL_BIN" -uroot -e "SELECT 1" >/dev/null 2>&1
}}

MYSQL_ROOT=$(_read_mysql_root)
echo "[db] 正在连接 MySQL..."
if ! _mysql_ping "$MYSQL_ROOT"; then
  echo "[db] 面板记录的 root 密码无效，尝试本地 socket 无密码连接..."
  MYSQL_ROOT=""
  if ! _mysql_ping ""; then
    echo "ERROR: 无法连接 MySQL，请检查 MySQL 服务或 root 密码" >&2
    exit 1
  fi
fi

SQL_FILE="/tmp/qbw_create_db_${{QBW_DB_NAME}}.sql"
cat > "$SQL_FILE" <<EOSQL
CREATE DATABASE IF NOT EXISTS \`$QBW_DB_NAME\` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
DROP USER IF EXISTS '$QBW_DB_USER_SQL'@'localhost';
CREATE USER '$QBW_DB_USER_SQL'@'localhost' IDENTIFIED BY '$QBW_DB_PASS_SQL';
GRANT ALL PRIVILEGES ON \`$QBW_DB_NAME\`.* TO '$QBW_DB_USER_SQL'@'localhost';
FLUSH PRIVILEGES;
EOSQL

echo "[db] 正在创建数据库 $QBW_DB_NAME ..."
if [ -n "$MYSQL_ROOT" ]; then
  export MYSQL_PWD="$MYSQL_ROOT"
  "$MYSQL_BIN" -uroot -h127.0.0.1 < "$SQL_FILE"
  unset MYSQL_PWD
else
  "$MYSQL_BIN" -uroot < "$SQL_FILE"
fi
rm -f "$SQL_FILE"
echo "DATABASE_OK"
"""


WORDPRESS_NGINX_REWRITE = """location /
{
    try_files $uri $uri/ /index.php?$args;
}
"""


def _build_nginx_vhost(task: DeployTask, site_domain: str, site_path: str, site_dir_name: str) -> str:
    """宝塔风格 Nginx 站点配置（引用 enable-php-XX.conf）。"""
    php_short = task.php_version.replace(".", "")
    return f"""server {{
    listen 80;
    server_name {site_domain};
    index index.php index.html index.htm default.php default.htm default.html;
    root {site_path};

    include /www/server/nginx/conf/enable-php-{php_short}.conf;
    include /www/server/panel/vhost/rewrite/{site_dir_name}.conf;

    location ~ ^/(\\.user\\.ini|\\.htaccess|\\.git|\\.svn|\\.project|LICENSE|README\\.md) {{
        return 404;
    }}

    location ~ /\\.well-known {{
        allow all;
    }}

    access_log /www/wwwlogs/{site_dir_name}.log;
    error_log /www/wwwlogs/{site_dir_name}.error.log;
}}
"""


def _ensure_wordpress_nginx_rewrite(
    ssh: SSHClient,
    site_vhost_name: str,
    secrets: list[str],
) -> None:
    """写入 WordPress 伪静态（Nginx try_files），供固定链接使用。"""
    rewrite_path = f"/www/server/panel/vhost/rewrite/{site_vhost_name}.conf"
    nginx_conf = f"/www/server/panel/vhost/nginx/{site_vhost_name}.conf"
    rewrite_escaped = WORDPRESS_NGINX_REWRITE.replace("'", "'\\''")
    ssh.run(
        f"mkdir -p /www/server/panel/vhost/rewrite && "
        f"printf '%s' '{rewrite_escaped}' > {rewrite_path}",
        secrets=secrets,
    )
    ssh.run(
        f"grep -q 'vhost/rewrite/{site_vhost_name}.conf' {nginx_conf} 2>/dev/null || "
        f"sed -i '/enable-php-/a \\    include /www/server/panel/vhost/rewrite/{site_vhost_name}.conf;' "
        f"{nginx_conf}",
        secrets=secrets,
    )
    ssh.run("nginx -t 2>&1 && /etc/init.d/nginx reload", secrets=secrets)


def _set_wordpress_permalink(
    task: DeployTask,
    ssh: SSHClient,
    site_path: str,
    secrets: list[str],
    db: Session,
) -> None:
    """设置固定链接为自定义结构 /%post_id%.html。Nginx 环境勿用 wp rewrite --hard。"""
    code, out, err = ssh.run(
        f"cd {quote_shell(site_path)} && wp option update permalink_structure '/%post_id%.html' --allow-root",
        timeout=120,
        secrets=secrets,
    )
    if code == 0:
        publish_log(task.id, "step7_site", "固定链接已设置为自定义结构 /%post_id%.html", db)
        return
    publish_log(
        task.id,
        "step7_site",
        f"固定链接设置警告: {(err or out).strip()}（可在后台「设置-固定链接」手动保存）",
        db,
    )


def _try_remote_download_wp_cli(ssh: SSHClient, secrets: list[str]) -> bool:
    """在目标机用 curl 下载 wp-cli.phar，30 秒内未成功则返回 False。"""
    cmd = (
        "cd /tmp && rm -f wp-cli.phar qbw_wp-cli.phar && "
        f"curl -sO --connect-timeout 10 --max-time {WP_CLI_DOWNLOAD_TIMEOUT} "
        f"{WP_CLI_DOWNLOAD_URL} "
        "&& test -s wp-cli.phar && mv -f wp-cli.phar qbw_wp-cli.phar && echo DOWNLOAD_OK"
    )
    code, out, _ = ssh.run(cmd, timeout=WP_CLI_DOWNLOAD_TIMEOUT + 15, secrets=secrets)
    return code == 0 and "DOWNLOAD_OK" in out


def _install_wp_cli_binary(ssh: SSHClient, secrets: list[str]) -> tuple[int, str, str]:
    return ssh.run(
        f"install -m 755 {WP_CLI_REMOTE_TMP} {WP_CLI_REMOTE_PATH} "
        f"&& rm -f {WP_CLI_REMOTE_TMP} /tmp/wp-cli.phar "
        f"&& {WP_CLI_REMOTE_PATH} --info 2>&1 | head -3",
        timeout=60,
        secrets=secrets,
    )


def ensure_wp_cli(
    task: DeployTask,
    ssh: SSHClient,
    secrets: list[str],
    db: Session,
) -> None:
    """安装 WP-CLI：优先目标机 curl 下载，30 秒内失败则改由本地上传。"""
    code, out, _ = ssh.run(
        f"test -x {WP_CLI_REMOTE_PATH} && {WP_CLI_REMOTE_PATH} --info >/dev/null 2>&1 && echo OK",
        timeout=30,
        secrets=secrets,
    )
    if code == 0 and "OK" in out:
        publish_log(task.id, "step7_site", "WP-CLI 已安装，跳过", db)
        return

    source = "remote"
    publish_log(
        task.id,
        "step7_site",
        f"正在尝试从 GitHub 下载 WP-CLI（{WP_CLI_DOWNLOAD_TIMEOUT} 秒超时）...",
        db,
    )
    if not _try_remote_download_wp_cli(ssh, secrets):
        source = "local"
        publish_log(
            task.id,
            "step7_site",
            "远程下载超时或失败，改用本地上传 WP-CLI...",
            db,
        )
        if not WP_CLI_PHAR_PATH.is_file():
            raise RuntimeError(
                f"远程下载失败且本地缺少 WP-CLI 程序包: {WP_CLI_PHAR_PATH}。"
                "请运行 scripts/download_wp_cli.sh 下载后重试。"
            )
        size_mb = WP_CLI_PHAR_PATH.stat().st_size / (1024 * 1024)
        publish_log(
            task.id,
            "step7_site",
            f"正在上传 WP-CLI 到目标服务器（{size_mb:.1f} MB）...",
            db,
        )
        ssh.upload_file(str(WP_CLI_PHAR_PATH), WP_CLI_REMOTE_TMP, mode=0o755)
    else:
        publish_log(task.id, "step7_site", "WP-CLI 远程下载成功", db)

    code, out, err = _install_wp_cli_binary(ssh, secrets)
    if code != 0:
        raise RuntimeError(f"WP-CLI 安装失败: {err or out}")

    if source == "remote":
        publish_log(task.id, "step7_site", "WP-CLI 已通过远程下载安装完成", db)
    else:
        publish_log(task.id, "step7_site", "WP-CLI 已通过本地上传安装完成", db)


def _wordpress_local_zip(locale: str) -> Path:
    loc = (locale or "zh_CN").strip()
    return ASSETS_DIR / f"wordpress-{loc}.zip"


def can_target_access_github(ssh: SSHClient, secrets: list[str]) -> bool:
    """探测目标服务器是否能在超时内访问 GitHub。"""
    code, out, _ = ssh.run(
        f"curl -fsS --connect-timeout 5 --max-time {GITHUB_CHECK_TIMEOUT} "
        f"-o /dev/null {GITHUB_CHECK_URL} && echo GITHUB_OK",
        timeout=GITHUB_CHECK_TIMEOUT + 10,
        secrets=secrets,
    )
    return code == 0 and "GITHUB_OK" in out


def _wordpress_core_ready(ssh: SSHClient, site_path: str, secrets: list[str]) -> bool:
    code, out, _ = ssh.run(
        f"test -f {quote_shell(site_path + '/wp-includes/version.php')} && echo OK",
        timeout=15,
        secrets=secrets,
    )
    return code == 0 and "OK" in out


def _try_remote_wp_core_download(
    ssh: SSHClient,
    site_path: str,
    locale: str,
    secrets: list[str],
) -> bool:
    """在目标机用 WP-CLI 下载核心，30 秒内未成功则返回 False。"""
    locale_flag = f" --locale={quote_shell(locale)}" if locale else ""
    cmd = (
        f"cd {quote_shell(site_path)} && "
        f"timeout {WP_CORE_DOWNLOAD_TIMEOUT} "
        f"{WP_CLI_REMOTE_PATH} core download{locale_flag} --allow-root"
    )
    try:
        code, _, _ = ssh.run(
            cmd,
            timeout=WP_CORE_DOWNLOAD_TIMEOUT + 15,
            secrets=secrets,
        )
    except Exception:
        return False
    # timeout 命令超时时退出码为 124
    if code == 124:
        return False
    return code == 0 and _wordpress_core_ready(ssh, site_path, secrets)


def _deploy_wp_core_from_local_zip(
    task: DeployTask,
    ssh: SSHClient,
    site_path: str,
    locale: str,
    secrets: list[str],
    db: Session,
) -> None:
    zip_path = _wordpress_local_zip(locale)
    if not zip_path.is_file():
        raise RuntimeError(
            f"目标服务器无法访问 GitHub，且本地缺少 WordPress 安装包: {zip_path}。"
            f"请运行 scripts/download_wordpress.sh {locale} 下载后重试。"
        )

    size_mb = zip_path.stat().st_size / (1024 * 1024)
    publish_log(
        task.id,
        "step7_site",
        f"正在上传 WordPress 安装包到目标服务器（{locale}，{size_mb:.1f} MB）...",
        db,
    )
    ssh.upload_file(str(zip_path), WP_CORE_REMOTE_ZIP, mode=0o644)

    task_tag = re.sub(r"[^a-zA-Z0-9_-]", "_", task.id or "adhoc")
    extract_dir = f"/tmp/qbw_wp_extract_{task_tag}"
    q_extract = quote_shell(extract_dir)
    q_site = quote_shell(site_path)
    q_zip = quote_shell(WP_CORE_REMOTE_ZIP)
    code, out, err = ssh.run(
        f"rm -rf {q_extract} && mkdir -p {q_extract} {q_site} && "
        f"unzip -oq {q_zip} -d {q_extract} && "
        f"if [ -d {q_extract}/wordpress ]; then "
        f"cp -a {q_extract}/wordpress/. {q_site}/; "
        f"else cp -a {q_extract}/. {q_site}/; fi && "
        f"rm -rf {q_extract} {q_zip} && "
        f"test -f {q_site}/wp-includes/version.php && echo EXTRACT_OK",
        timeout=300,
        secrets=secrets,
    )
    if code != 0 or "EXTRACT_OK" not in out:
        raise RuntimeError(f"WordPress 安装包解压失败: {err or out}")
    publish_log(task.id, "step7_site", "WordPress 核心文件已通过本地上传解压完成", db)


def deploy_wordpress_core(
    task: DeployTask,
    ssh: SSHClient,
    site_path: str,
    secrets: list[str],
    db: Session,
) -> None:
    """部署 WordPress 核心：可访问 GitHub 时用 WP-CLI 下载，否则上传本地安装包。"""
    locale = (task.wp_locale or "zh_CN").strip()

    if _wordpress_core_ready(ssh, site_path, secrets):
        publish_log(task.id, "step7_site", "WordPress 核心文件已存在，跳过下载", db)
        return

    if can_target_access_github(ssh, secrets):
        publish_log(
            task.id,
            "step7_site",
            f"目标服务器可访问 GitHub，正在通过 WP-CLI 下载 WordPress（{locale}，"
            f"{WP_CORE_DOWNLOAD_TIMEOUT} 秒超时）...",
            db,
        )
        if _try_remote_wp_core_download(ssh, site_path, locale, secrets):
            publish_log(task.id, "step7_site", "WordPress 核心已通过 WP-CLI 下载完成", db)
            return
        publish_log(
            task.id,
            "step7_site",
            "WP-CLI 远程下载超时或失败，改用本地上传 WordPress 安装包...",
            db,
        )
    else:
        publish_log(
            task.id,
            "step7_site",
            "目标服务器无法访问 GitHub，改用本地上传 WordPress 安装包...",
            db,
        )

    _deploy_wp_core_from_local_zip(task, ssh, site_path, locale, secrets, db)


def _resolve_db_prefix_for_spec(site_spec: dict) -> str:
    return sanitize_db_prefix(site_spec.get("db_prefix"))


def setup_site_and_db(
    task: DeployTask,
    password: str,
    db: Session,
    remote_state: RemoteDeployState | None = None,
    site_spec: dict | None = None,
    site_index: int = 0,
) -> dict:
    spec = site_spec or resolve_sites_from_task(task)[site_index]
    label = spec.get("primary_domain") or spec.get("domains", [""])[0]
    publish_log(task.id, "step7_site", f"开始部署网站 {label}（先数据库、后站点）", db)

    site_info = build_site_info_from_spec(task, spec)
    persist_site_db_credentials(task, site_index, site_info, db)
    publish_log(
        task.id,
        "step7_site",
        f"数据库凭据已记录: 库名 {site_info['db_name']}，用户 {site_info['db_user']}",
        db,
    )

    site_domain = site_info["site_domain"]
    site_dir_name = site_info["site_dir_name"]
    site_path = site_info["site_path"]
    db_pass = site_info["db_pass"]
    extra_domains = site_info.get("extra_domains") or []
    site_label = site_domain
    if extra_domains:
        site_label = f"{site_domain}（含 {len(extra_domains)} 个附加域名）"

    wp_pass = site_info.get("wp_admin_password") or task.wp_admin_password
    secrets = [password, wp_pass, task.bt_password, db_pass]

    with _ssh(task, password, "step7_site", db) as ssh:
        publish_log(task.id, "step7_site", f"正在创建数据库: {site_info['db_name']}", db)
        ensure_baota_database(task, ssh, site_info, secrets, db)

        # 清理旧版手动写入的 Nginx 配置（下划线目录名），避免与宝塔站点记录冲突
        legacy_conf = f"/www/server/panel/vhost/nginx/{site_dir_name}.conf"
        ssh.run(f"rm -f {quote_shell(legacy_conf)}", secrets=secrets)

        publish_log(task.id, "step7_site", f"正在创建网站: {site_label}", db)
        ensure_baota_site(task, ssh, site_info, secrets, db)
        _ensure_wordpress_nginx_rewrite(ssh, site_domain, secrets)

        ssh.run(f"mkdir -p {quote_shell(site_path)}", secrets=secrets)
        _chown_www_site(ssh, site_path, secrets)

    return site_info


def install_wordpress(
    task: DeployTask,
    password: str,
    site_info: dict,
    db: Session,
    remote_state: RemoteDeployState | None = None,
) -> dict:
    domain = site_info.get("site_domain", "")
    publish_log(task.id, "step7_site", f"正在安装 WordPress: {domain}", db)
    wp_admin_user = site_info.get("wp_admin_user") or task.wp_admin_user
    wp_admin_password = site_info.get("wp_admin_password") or task.wp_admin_password
    wp_admin_email = site_info.get("wp_admin_email") or task.wp_admin_email
    wp_locale = site_info.get("wp_locale") or task.wp_locale
    secrets = [password, wp_admin_password, site_info["db_pass"], task.bt_password]
    site_path = site_info["site_path"]

    with _ssh(task, password, "step7_site", db) as ssh:
        _, wp_out, _ = ssh.run(
            f"cd {quote_shell(site_path)} && {WP_CLI_REMOTE_PATH} core is-installed --allow-root >/dev/null 2>&1 && echo OK",
            timeout=30,
            secrets=secrets,
        )
        wp_installed = (remote_state and remote_state.wordpress == "complete") or "OK" in wp_out
        if wp_installed:
            publish_log(task.id, "step7_site", "WordPress 已安装，跳过核心安装", db)
            _ensure_wordpress_nginx_rewrite(ssh, site_info["site_domain"], secrets)
            _set_wordpress_permalink(task, ssh, site_path, secrets, db)
            redis_plugin = setup_wordpress_redis_plugin(
                task, password, site_path, ssh, secrets, db
            )
            return {
                "site_url": site_info["site_url"],
                "admin_url": f"{site_info['site_url']}/wp-admin/",
                "admin_user": wp_admin_user,
                "redis_plugin": redis_plugin,
            }

        ensure_wp_cli(task, ssh, secrets, db)
        deploy_wordpress_core_for_locale(task, ssh, site_path, wp_locale, secrets, db)
        _ensure_wordpress_nginx_rewrite(ssh, site_info["site_domain"], secrets)

        commands = [
            (
                f"cd {quote_shell(site_path)} && wp config create "
                f"{_wp_cli_option('--dbname', site_info['db_name'])} "
                f"{_wp_cli_option('--dbuser', site_info['db_user'])} "
                f"{_wp_cli_option('--dbpass', site_info['db_pass'])} "
                f"--dbhost=localhost "
                f"{_wp_cli_option('--dbprefix', site_info['db_prefix'])} "
                f"--force --allow-root"
            ),
            (
                f"cd {quote_shell(site_path)} && wp core install "
                f"{_wp_cli_option('--url', site_info['site_url'])} "
                f"{_wp_cli_option('--title', site_info['site_name'])} "
                f"{_wp_cli_option('--admin_user', wp_admin_user)} "
                f"{_wp_cli_option('--admin_password', wp_admin_password)} "
                f"{_wp_cli_option('--admin_email', wp_admin_email)} "
                f"--skip-email --allow-root"
            ),
            f"cd {quote_shell(site_path)} && wp option update blog_public 1 --allow-root",
        ]

        for cmd in commands:
            code, out, err = ssh.run(cmd, timeout=600, secrets=secrets)
            if code != 0:
                raise RuntimeError(f"WordPress 安装失败: {err or out}")

        _chown_www_site(ssh, site_path, secrets)
        _set_wordpress_permalink(task, ssh, site_path, secrets, db)
        _chown_www_site(ssh, site_path, secrets)

        publish_log(task.id, "step7_site", "WordPress 安装完成", db)

        redis_plugin = setup_wordpress_redis_plugin(
            task, password, site_path, ssh, secrets, db
        )

    return {
        "site_url": site_info["site_url"],
        "admin_url": f"{site_info['site_url']}/wp-admin/",
        "admin_user": wp_admin_user,
        "redis_plugin": redis_plugin,
    }


def deploy_wordpress_core_for_locale(
    task: DeployTask,
    ssh: SSHClient,
    site_path: str,
    locale: str,
    secrets: list[str],
    db: Session,
) -> None:
    """按指定语言部署 WordPress 核心（多站时各站语言可不同）。"""
    locale = (locale or "zh_CN").strip()

    if _wordpress_core_ready(ssh, site_path, secrets):
        publish_log(task.id, "step7_site", "WordPress 核心文件已存在，跳过下载", db)
        return

    if can_target_access_github(ssh, secrets):
        publish_log(
            task.id,
            "step7_site",
            f"目标服务器可访问 GitHub，正在通过 WP-CLI 下载 WordPress（{locale}，"
            f"{WP_CORE_DOWNLOAD_TIMEOUT} 秒超时）...",
            db,
        )
        if _try_remote_wp_core_download(ssh, site_path, locale, secrets):
            publish_log(task.id, "step7_site", "WordPress 核心已通过 WP-CLI 下载完成", db)
            return
        publish_log(
            task.id,
            "step7_site",
            "WP-CLI 远程下载超时或失败，改用本地上传 WordPress 安装包...",
            db,
        )
    else:
        publish_log(
            task.id,
            "step7_site",
            "目标服务器无法访问 GitHub，改用本地上传 WordPress 安装包...",
            db,
        )

    _deploy_wp_core_from_local_zip(task, ssh, site_path, locale, secrets, db)


def can_target_access_wordpress_org(ssh: SSHClient, secrets: list[str]) -> bool:
    """探测目标服务器是否能在超时内访问 WordPress.org 下载站。"""
    code, out, _ = ssh.run(
        f"curl -fsS --connect-timeout 5 --max-time {WORDPRESS_ORG_CHECK_TIMEOUT} "
        f"-o /dev/null {WORDPRESS_ORG_CHECK_URL} && echo WPORG_OK",
        timeout=WORDPRESS_ORG_CHECK_TIMEOUT + 10,
        secrets=secrets,
    )
    return code == 0 and "WPORG_OK" in out


def _redis_plugin_active(ssh: SSHClient, site_path: str, secrets: list[str]) -> bool:
    quoted_path = quote_shell(site_path)
    _, out, _ = ssh.run(
        f"cd {quoted_path} && wp plugin is-active redis-cache --allow-root && echo PLUGIN_ACTIVE=1",
        timeout=60,
        secrets=secrets,
    )
    return "PLUGIN_ACTIVE=1" in out


def _try_remote_redis_plugin_install(
    ssh: SSHClient,
    site_path: str,
    secrets: list[str],
) -> bool:
    quoted_path = quote_shell(site_path)
    cmd = (
        f"cd {quoted_path} && timeout {REDIS_PLUGIN_DOWNLOAD_TIMEOUT} "
        f"wp plugin install redis-cache --activate --allow-root"
    )
    try:
        code, _, _ = ssh.run(cmd, timeout=REDIS_PLUGIN_DOWNLOAD_TIMEOUT + 15, secrets=secrets)
    except Exception:
        return False
    if code == 124:
        return False
    return code == 0 and _redis_plugin_active(ssh, site_path, secrets)


def _deploy_redis_plugin_from_local_zip(
    task: DeployTask,
    ssh: SSHClient,
    site_path: str,
    secrets: list[str],
    db: Session,
) -> bool:
    zip_path = REDIS_CACHE_LOCAL_ZIP
    if not zip_path.is_file():
        publish_log(
            task.id,
            "step7_site",
            f"本地缺少 Redis 插件包: {zip_path}，请运行 scripts/download_redis_cache_plugin.sh 下载",
            db,
        )
        return False

    size_kb = zip_path.stat().st_size / 1024
    publish_log(
        task.id,
        "step7_site",
        f"正在上传 Redis 插件包到目标服务器（{size_kb:.0f} KB）...",
        db,
    )
    ssh.upload_file(str(zip_path), REDIS_CACHE_REMOTE_ZIP, mode=0o644)
    quoted_path = quote_shell(site_path)
    q_zip = quote_shell(REDIS_CACHE_REMOTE_ZIP)
    code, out, err = ssh.run(
        f"cd {quoted_path} && wp plugin install {q_zip} --activate --allow-root",
        timeout=300,
        secrets=secrets,
    )
    ssh.run(f"rm -f {q_zip}", timeout=30, secrets=secrets)
    if code != 0:
        publish_log(
            task.id,
            "step7_site",
            f"本地上传安装 Redis 插件失败: {(err or out).strip()[:300]}",
            db,
        )
        return False
    return _redis_plugin_active(ssh, site_path, secrets)


def _install_redis_cache_plugin(
    task: DeployTask,
    ssh: SSHClient,
    site_path: str,
    secrets: list[str],
    db: Session,
) -> bool:
    if _redis_plugin_active(ssh, site_path, secrets):
        publish_log(task.id, "step7_site", "Redis 缓存插件已激活，跳过安装", db)
        return True

    if can_target_access_wordpress_org(ssh, secrets):
        publish_log(
            task.id,
            "step7_site",
            f"目标服务器可访问 WordPress.org，正在下载 redis-cache 插件（"
            f"{REDIS_PLUGIN_DOWNLOAD_TIMEOUT} 秒超时）...",
            db,
        )
        if _try_remote_redis_plugin_install(ssh, site_path, secrets):
            publish_log(task.id, "step7_site", "Redis 缓存插件已通过 WP-CLI 远程下载并激活", db)
            return True
        publish_log(
            task.id,
            "step7_site",
            "WP-CLI 远程下载 Redis 插件超时或失败，改用本地上传...",
            db,
        )
    else:
        publish_log(
            task.id,
            "step7_site",
            "目标服务器无法访问 WordPress.org，改用本地上传 Redis 插件...",
            db,
        )

    return _deploy_redis_plugin_from_local_zip(task, ssh, site_path, secrets, db)


def _build_redis_prefix() -> str:
    """生成 Redis 键前缀：WP_ + 随机字符串（仅 ASCII，避免中文等问题）。"""
    suffix = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(12))
    return f"WP_{suffix}"


def setup_wordpress_redis_plugin(
    task: DeployTask,
    password: str,
    site_path: str,
    ssh: SSHClient,
    secrets: list[str],
    db: Session,
) -> dict:
    """安装并启用 WordPress Redis Object Cache 插件。"""
    ram_mb = detect_ram_mb(ssh, secrets)
    if not should_install_redis(ram_mb):
        publish_log(task.id, "step7_site", REDIS_SKIP_MESSAGE, db)
        return {
            "installed": False,
            "active": False,
            "object_cache_enabled": False,
            "skipped_low_memory": True,
        }

    publish_log(task.id, "step7_site", "正在安装 WordPress Redis 缓存插件...", db)

    if not _install_redis_cache_plugin(task, ssh, site_path, secrets, db):
        publish_log(
            task.id,
            "step7_site",
            "Redis 插件安装失败（可稍后在后台手动安装 redis-cache）",
            db,
        )
        return {"installed": False, "active": False, "object_cache_enabled": False}

    redis_prefix = _build_redis_prefix()

    quoted_path = quote_shell(site_path)
    commands = [
        f"cd {quoted_path} && wp config set WP_REDIS_HOST '127.0.0.1' --allow-root",
        f"cd {quoted_path} && wp config set WP_REDIS_PORT 6379 --raw --allow-root",
        f"cd {quoted_path} && wp config set WP_REDIS_TIMEOUT 1 --raw --allow-root",
        f"cd {quoted_path} && wp config set WP_REDIS_READ_TIMEOUT 1 --raw --allow-root",
        f"cd {quoted_path} && wp config set WP_REDIS_DATABASE 0 --raw --allow-root",
        f"cd {quoted_path} && wp config set WP_REDIS_PREFIX {quote_shell(redis_prefix)} --allow-root",
        f"cd {quoted_path} && wp config set WP_REDIS_GRACEFUL true --raw --allow-root",
        f"cd {quoted_path} && wp redis enable --allow-root 2>/dev/null || true",
        f"cd {quoted_path} && wp plugin is-active redis-cache --allow-root && echo PLUGIN_ACTIVE=1 || echo PLUGIN_ACTIVE=0",
    ]

    plugin_active = False
    object_cache_enabled = False

    plugin_active = _redis_plugin_active(ssh, site_path, secrets)

    for cmd in commands:
        code, out, err = ssh.run(cmd, timeout=300, secrets=secrets)
        if code != 0 and "wp config set" in cmd:
            publish_log(
                task.id,
                "step7_site",
                f"Redis 配置写入警告: {(err or out).strip()[:200]}",
                db,
            )
        if "PLUGIN_ACTIVE=1" in out:
            plugin_active = True

    _chown_www_site(ssh, site_path, secrets)

    _, status_out, _ = ssh.run(
        f"cd {quoted_path} && wp redis status --allow-root 2>/dev/null || true",
        timeout=60,
        secrets=secrets,
    )
    object_cache_enabled = "Status: Connected" in status_out or "Enabled" in status_out

    if plugin_active:
        publish_log(
            task.id,
            "step7_site",
            f"Redis Object Cache 插件已安装并激活（前缀: {redis_prefix}）",
            db,
        )
    if object_cache_enabled:
        publish_log(task.id, "step7_site", "WordPress 对象缓存已通过 Redis 启用", db)
    elif plugin_active:
        publish_log(
            task.id,
            "step7_site",
            "插件已激活；若对象缓存未启用，请在后台「设置 → Redis」点击启用",
            db,
        )

    return {
        "installed": plugin_active,
        "active": plugin_active,
        "object_cache_enabled": object_cache_enabled,
        "plugin_slug": "redis-cache",
        "redis_prefix": redis_prefix,
    }


def update_wordpress_https_urls(
    task: DeployTask,
    password: str,
    site_path: str,
    site_domain: str,
    db: Session,
) -> None:
    """将 WordPress 站点地址与首页地址更新为 HTTPS。"""
    https_url = f"https://{site_domain.strip().lower()}"
    secrets = [password, task.wp_admin_password]
    publish_log(task.id, "step8_ssl", f"正在将 WordPress 访问地址更新为 {https_url} ...", db)

    with _ssh(task, password, "step8_ssl", db) as ssh:
        commands = [
            f"cd {quote_shell(site_path)} && wp option update siteurl {quote_shell(https_url)} --allow-root",
            f"cd {quote_shell(site_path)} && wp option update home {quote_shell(https_url)} --allow-root",
        ]
        for cmd in commands:
            code, out, err = ssh.run(cmd, timeout=120, secrets=secrets)
            if code != 0 and "option update" in cmd:
                raise RuntimeError(f"更新 WordPress HTTPS 地址失败: {err or out}")
        _chown_www_site(ssh, site_path, secrets)

    publish_log(task.id, "step8_ssl", "WordPress 后台访问地址已更新为 HTTPS", db)


def verify_site(task: DeployTask, password: str, wp_info: dict, db: Session) -> dict:
    publish_log(task.id, "step7_site", "正在验证网站可访问性...", db)
    secrets = [password]

    with _ssh(task, password, "step7_site", db) as ssh:
        for path in ["", "/wp-admin/"]:
            url = f"{wp_info['site_url']}{path}"
            code, out, _ = ssh.run(
                f"curl -s -o /dev/null -w '%{{http_code}}' --connect-timeout 10 {quote_shell(url)}",
                timeout=30,
                secrets=secrets,
            )
            status = out.strip()
            publish_log(task.id, "step7_site", f"{url} -> HTTP {status}", db)
            if status not in ("200", "301", "302"):
                publish_log(task.id, "step7_site", f"警告: {url} 返回 {status}，请检查安全组是否放行 80 端口", db)

    publish_log(task.id, "step7_site", "部署验证完成", db)
    return wp_info


def _gen_db_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))
