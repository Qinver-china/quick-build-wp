"""通过宝塔面板内部 API（btpython）创建/同步网站与数据库，确保面板列表可见。"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.core.sql_safety import sanitize_db_credentials
from app.models.deploy import DeployTask
from app.services.log_publisher import publish_log
from app.services.ssh import SSHClient

PANEL_DIR = "/www/server/panel"
PANEL_CLASS_DIR = f"{PANEL_DIR}/class"

# 宝塔 public/panelSite/database 等模块位于 class/ 目录
_PANEL_PYTHON_BOOTSTRAP = f"""import os
import sys
os.chdir({PANEL_DIR!r})
for _p in ({PANEL_DIR!r}, {PANEL_CLASS_DIR!r}):
    if _p not in sys.path:
        sys.path.insert(0, _p)
"""


def _php_version_short(php_version: str) -> str:
    return php_version.replace(".", "")


def resolve_panel_php_version(requested: str, available: list[str]) -> str | None:
    """将用户请求的 PHP 版本映射为宝塔面板 GetPHPVersion 中的 version 字段。"""
    want = _php_version_short(requested.strip())
    if want in available:
        return want
    for ver in available:
        if ver.replace(".", "") == want:
            return ver
    return None


def _build_list_panel_php_script() -> str:
    return f"""import json
import sys
{_PANEL_PYTHON_BOOTSTRAP}
import public
import panelSite
import os

versions = []
try:
    site_obj = panelSite.panelSite()
    get = public.dict_obj()
    raw = site_obj.GetPHPVersion(get)
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            ver = str(item.get("version", "")).replace(" ", "").strip()
            if ver and ver != "00":
                versions.append(ver)
except Exception:
    pass

if not versions:
    php_root = "/www/server/php"
    if os.path.isdir(php_root):
        for name in sorted(os.listdir(php_root)):
            php_bin = os.path.join(php_root, name, "bin", "php")
            if os.path.isfile(php_bin):
                versions.append(str(name).replace(".", ""))

print("PANEL_PHP:" + json.dumps(sorted(set(versions)), ensure_ascii=False))
"""


def list_panel_php_versions(ssh: SSHClient, secrets: list[str]) -> list[str]:
    """读取宝塔面板已识别、可用于建站的 PHP 版本列表。"""
    code, out, err = _run_panel_python(
        ssh, _build_list_panel_php_script(), timeout=120, secrets=secrets
    )
    combined = (out + err).strip()
    for line in combined.splitlines():
        if line.startswith("PANEL_PHP:"):
            try:
                data = json.loads(line.split(":", 1)[1])
            except json.JSONDecodeError:
                continue
            if isinstance(data, list):
                return [str(v) for v in data]
    return []


def _panel_output_message(combined: str) -> str:
    """从面板脚本输出中提取可读错误，忽略 cryptography 等弃用警告。"""
    lines = []
    for line in combined.splitlines():
        text = line.strip()
        if not text:
            continue
        if "DeprecationWarning" in text or text.startswith("from cryptography"):
            continue
        lines.append(text)
    return "\n".join(lines)


def _run_panel_python(ssh: SSHClient, script: str, timeout: int, secrets: list[str]) -> tuple[int, str, str]:
    wrapped = f"""set -euo pipefail
PY=""
for candidate in btpython {PANEL_DIR}/pyenv/bin/python3 {PANEL_DIR}/pyenv/bin/python; do
  if command -v "$candidate" >/dev/null 2>&1 || [ -x "$candidate" ]; then
    PY="$candidate"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "PANEL_PY_MISSING" >&2
  exit 1
fi
SCRIPT="/tmp/qbw_panel_{ssh.task_id or 'adhoc'}.py"
cat > "$SCRIPT" <<'QBWPANELPY'
{script}
QBWPANELPY
cd {PANEL_DIR}
export PYTHONPATH="{PANEL_DIR}:{PANEL_CLASS_DIR}:${{PYTHONPATH:-}}"
"$PY" "$SCRIPT"
rm -f "$SCRIPT"
"""
    return ssh.run_script(wrapped, timeout=timeout, secrets=secrets)


def _build_ensure_site_script(
    domain: str,
    site_path: str,
    panel_php_version: str,
    site_ps: str,
    extra_domains: list[str] | None = None,
) -> str:
    domain = domain.strip().lower()
    extras = [d for d in (extra_domains or []) if d and d != domain]
    webname = json.dumps(
        {"domain": domain, "domainlist": extras, "count": len(extras)},
        ensure_ascii=False,
    )
    php_ver = _php_version_short(panel_php_version)
    return f"""import json
import sys
{_PANEL_PYTHON_BOOTSTRAP}
import public
import panelSite

domain = {domain!r}
site_path = {site_path!r}
php_ver = {php_ver!r}
site_ps = {site_ps!r}

site_obj = panelSite.panelSite()
get_php = public.dict_obj()
available = []
try:
    raw = site_obj.GetPHPVersion(get_php)
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                ver = str(item.get("version", "")).replace(" ", "").strip()
                if ver and ver != "00":
                    available.append(ver)
except Exception:
    pass

if php_ver not in available:
    matched = None
    for ver in available:
        if ver.replace(".", "") == php_ver:
            matched = ver
            break
    if matched:
        php_ver = matched
    else:
        msg = "指定PHP版本不存在! 请求 " + php_ver
        if available:
            msg += "，面板已安装: " + ",".join(available)
        else:
            msg += "，面板未识别任何 PHP 版本"
        print("SITE_PANEL_FAIL:" + msg)
        sys.exit(1)

sites = public.M('sites')
if sites.where('name=?', (domain,)).count():
    print('SITE_PANEL_SKIP')
    sys.exit(0)

get = public.dict_obj()
get.webname = {webname!r}
get.path = site_path
get.type_id = 0
get.type = 'PHP'
get.version = php_ver
get.port = '80'
get.ps = site_ps or domain
get.ftp = 'false'
get.sql = 'false'
get.codeing = 'utf8mb4'
get.project_type = 'PHP'

result = panelSite.panelSite().AddSite(get)
if isinstance(result, dict) and result.get('siteStatus'):
    print('SITE_PANEL_OK')
    sys.exit(0)

msg = ''
if isinstance(result, dict):
    msg = result.get('msg') or result.get('error') or str(result)
else:
    msg = str(result)
print('SITE_PANEL_FAIL:' + msg[:500])
sys.exit(1)
"""


def _build_ensure_database_script(
    db_name: str,
    db_user: str,
    db_pass: str,
    site_ps: str,
) -> str:
    db_name, db_user, db_pass, _ = sanitize_db_credentials(
        db_name,
        db_user,
        db_pass,
        "wp_",
        user_max_len=16,
        name_max_len=64,
    )
    return f"""import sys
import time
{_PANEL_PYTHON_BOOTSTRAP}
import public
import database

db_name = {db_name!r}
db_user = {db_user!r}
db_pass = {db_pass!r}
site_ps = {site_ps!r}

sql = public.M('databases')
if sql.where('name=?', (db_name,)).count():
    print('DB_PANEL_SKIP')
    sys.exit(0)

mysql_obj = public.get_mysql_obj_by_sid(0)
mysql_exists = False
if mysql_obj:
    rows = mysql_obj.query('show databases')
    if isinstance(rows, list):
        mysql_exists = any(row[0] == db_name for row in rows)

if mysql_exists:
    add_time = time.strftime('%Y-%m-%d %X', time.localtime())
    sql.add(
        'pid,sid,db_type,name,username,password,accept,ps,addtime',
        (0, 0, 0, db_name, db_user, db_pass, '127.0.0.1', site_ps or db_name, add_time),
    )
    print('DB_PANEL_SYNC_OK')
    sys.exit(0)

get = public.dict_obj()
get.name = db_name
get.db_user = db_user
get.password = db_pass
get.address = '127.0.0.1'
get.codeing = 'utf8mb4'
get.ps = site_ps or db_name
get.sid = 0

result = database.database().AddDatabase(get)
if isinstance(result, dict) and result.get('status'):
    print('DB_PANEL_OK')
    sys.exit(0)

msg = ''
if isinstance(result, dict):
    msg = result.get('msg') or str(result)
else:
    msg = str(result)
print('DB_PANEL_FAIL:' + msg[:500])
sys.exit(1)
"""


def ensure_baota_site(
    task: DeployTask,
    ssh: SSHClient,
    site_info: dict,
    secrets: list[str],
    db: Session,
) -> bool:
    """在宝塔面板中创建网站（若已存在则跳过）。返回 True 表示面板中已有该站点。"""
    domain = site_info["site_domain"]
    site_path = site_info["site_path"]
    site_ps = site_info.get("site_name") or domain
    extra_domains = site_info.get("extra_domains") or []

    domain_label = domain
    if extra_domains:
        domain_label = f"{domain}（含 {len(extra_domains)} 个附加域名）"
    publish_log(task.id, "step7_site", f"正在同步网站到宝塔面板: {domain_label}", db)
    from app.services.lnmp_install import ensure_baota_php_version

    panel_php = ensure_baota_php_version(task, ssh, secrets, db, log_phase="step7_site")
    script = _build_ensure_site_script(
        domain, site_path, panel_php, site_ps, extra_domains
    )
    code, out, err = _run_panel_python(ssh, script, timeout=180, secrets=secrets)

    combined = (out + err).strip()
    if "SITE_PANEL_SKIP" in combined:
        publish_log(task.id, "step7_site", f"宝塔网站列表中已存在: {domain}", db)
        return True
    if code == 0 and "SITE_PANEL_OK" in combined:
        publish_log(task.id, "step7_site", f"已添加到宝塔网站列表: {domain}", db)
        return True

    fail_msg = _panel_output_message(combined)
    for line in combined.splitlines():
        if line.startswith("SITE_PANEL_FAIL:"):
            fail_msg = line.split(":", 1)[1].strip()
            break
    raise RuntimeError(f"同步网站到宝塔面板失败: {fail_msg or '未知错误'}")


def ensure_baota_database(
    task: DeployTask,
    ssh: SSHClient,
    site_info: dict,
    secrets: list[str],
    db: Session,
) -> None:
    """在宝塔面板中创建数据库；MySQL 已存在时仅写入面板记录。"""
    db_name = site_info["db_name"]
    db_user = site_info["db_user"]
    db_pass = site_info["db_pass"]
    db_prefix = site_info["db_prefix"]
    site_ps = site_info.get("site_name") or site_info["site_domain"]

    publish_log(task.id, "step7_site", f"正在同步数据库到宝塔面板: {db_name}", db)
    script = _build_ensure_database_script(db_name, db_user, db_pass, site_ps)
    code, out, err = _run_panel_python(
        ssh, script, timeout=180, secrets=secrets + [db_pass]
    )

    combined = (out + err).strip()
    if "DB_PANEL_SKIP" in combined:
        publish_log(task.id, "step7_site", f"宝塔数据库列表中已存在: {db_name}", db)
        return
    if code == 0 and ("DB_PANEL_OK" in combined or "DB_PANEL_SYNC_OK" in combined):
        custom = bool(task.db_name or task.db_user or task.db_password or task.db_prefix)
        src = "用户自定义" if custom else "自动生成"
        action = "已同步到" if "DB_PANEL_SYNC_OK" in combined else "已添加到"
        publish_log(
            task.id,
            "step7_site",
            f"数据库 {db_name} {action}宝塔列表（{src}，表前缀 {db_prefix}）",
            db,
        )
        return

    fail_msg = _panel_output_message(combined)
    for line in combined.splitlines():
        if line.startswith("DB_PANEL_FAIL:"):
            fail_msg = line.split(":", 1)[1].strip()
            break
    raise RuntimeError(f"同步数据库到宝塔面板失败: {fail_msg or '未知错误'}")
