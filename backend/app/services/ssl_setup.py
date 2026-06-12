"""通过宝塔面板 ACME 为网站申请 Let's Encrypt 证书并启用 HTTPS。"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.models.deploy import DeployTask
from app.services.baota_panel import (
    _PANEL_PYTHON_BOOTSTRAP,
    _panel_output_message,
    _run_panel_python,
)
from app.services.log_publisher import publish_log
from app.services.ssh import SSHClient
from app.services.wordpress import _ssh, update_wordpress_https_urls

SSL_FAILURE_WARNING = "证书申请失败了，请在宝塔手动申请证书即可"


def _build_apply_ssl_script(domain: str) -> str:
    domains_json = json.dumps([domain], ensure_ascii=False)
    return f"""import json
import sys
{_PANEL_PYTHON_BOOTSTRAP}
try:
    import acme_v2
    import panelSite
except ImportError as exc:
    print('SSL_FAIL:当前宝塔环境不支持证书申请模块: ' + str(exc))
    sys.exit(1)

domain = {domain!r}
domains_json = {domains_json!r}

site = public.M('sites').where('name=?', (domain,)).find()
if not site:
    print('SSL_FAIL:网站未在宝塔列表中找到，无法申请证书')
    sys.exit(1)

site_id = site['id']
site_obj = panelSite.panelSite()

check = public.dict_obj()
check.siteName = domain
existing = site_obj.GetSSL(check)
if isinstance(existing, dict) and existing.get('status'):
    try:
        check.siteName = domain
        site_obj.HttpToHttps(check)
    except Exception:
        pass
    print('SSL_OK')
    sys.exit(0)

get = public.dict_obj()
get.id = str(site_id)
get.auth_type = 'http'
get.auth_to = str(site_id)
get.auto_wildcard = ''
get.domains = domains_json

result = acme_v2.acme_v2().apply_cert_api(get)
if not isinstance(result, dict) or not result.get('status'):
    msg = result.get('msg', result) if isinstance(result, dict) else result
    print('SSL_FAIL:' + str(msg)[:500])
    sys.exit(1)

get.type = '1'
get.siteName = domain
get.key = result.get('private_key', '')
get.csr = (result.get('cert') or '') + (result.get('root') or '')
if not get.key or 'KEY' not in get.key:
    print('SSL_FAIL:证书申请未返回有效私钥')
    sys.exit(1)

set_result = site_obj.SetSSL(get)
if isinstance(set_result, dict) and set_result.get('status') is False:
    print('SSL_FAIL:' + str(set_result.get('msg', set_result))[:500])
    sys.exit(1)

try:
    redirect = public.dict_obj()
    redirect.siteName = domain
    site_obj.HttpToHttps(redirect)
except Exception:
    pass

print('SSL_OK')
"""


def apply_baota_ssl(
    task: DeployTask,
    password: str,
    site_info: dict,
    wp_info: dict,
    db: Session,
) -> dict:
    """申请 SSL 证书；失败不中断部署，仅记录警告。"""
    domain = site_info["site_domain"]
    site_path = site_info["site_path"]
    https_url = f"https://{domain}"

    publish_log(task.id, "step8_ssl", f"正在为 {domain} 申请 SSL 证书（宝塔 Let's Encrypt）...", db)

    secrets = [password, task.wp_admin_password, task.bt_password]
    try:
        with _ssh(task, password, "step8_ssl", db) as ssh:
            script = _build_apply_ssl_script(domain)
            code, out, err = _run_panel_python(ssh, script, timeout=600, secrets=secrets)
            combined = (out + err).strip()

            if code == 0 and "SSL_OK" in combined:
                publish_log(task.id, "step8_ssl", "SSL 证书已申请并部署成功", db)
                result = {
                    "success": True,
                    "https_url": https_url,
                    "admin_url": f"{https_url}/wp-admin/",
                    "site_url": https_url,
                    "wp_https_updated": True,
                }
                try:
                    update_wordpress_https_urls(task, password, site_path, domain, db)
                except Exception as exc:
                    result["wp_https_updated"] = False
                    result["wp_https_error"] = str(exc)
                    publish_log(
                        task.id,
                        "step8_ssl",
                        f"SSL 已启用，但 WordPress 地址更新失败: {exc}（请在后台手动改为 HTTPS）",
                        db,
                    )
                return result

            fail_msg = _panel_output_message(combined)
            for line in combined.splitlines():
                if line.startswith("SSL_FAIL:"):
                    fail_msg = line.split(":", 1)[1].strip()
                    break
            publish_log(task.id, "step8_ssl", f"警告: {SSL_FAILURE_WARNING}", db)
            return {
                "success": False,
                "warning": SSL_FAILURE_WARNING,
                "error": fail_msg or "未知错误",
            }

    except Exception as exc:
        message = str(exc)
        publish_log(task.id, "step8_ssl", f"警告: {SSL_FAILURE_WARNING}", db)
        return {
            "success": False,
            "warning": SSL_FAILURE_WARNING,
            "error": message,
        }
