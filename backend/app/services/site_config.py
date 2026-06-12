"""多网站配置：解析、校验与从任务读取站点列表。"""

from __future__ import annotations

import ipaddress
import re
import secrets
import string
from typing import Any

from sqlalchemy.orm import Session

from app.core.sql_safety import sanitize_db_prefix, sanitize_sql_identifier, validate_email
from app.models.deploy import DeployTask

DOMAIN_RE = re.compile(
    r"^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z]{2,})+$"
)


def _gen_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def is_valid_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip())
        return True
    except ValueError:
        return False


def normalize_ip(host: str) -> str:
    return str(ipaddress.ip_address(host.strip()))


def validate_bind_host(host: str) -> str:
    """校验绑定地址：支持域名或 IP（IPv4 / IPv6）。"""
    text = host.strip()
    if not text:
        raise ValueError("绑定地址不能为空")
    if is_valid_ip(text):
        return normalize_ip(text)
    d = text.lower()
    if DOMAIN_RE.match(d):
        return d
    raise ValueError(f"无效域名或 IP: {host}")


def validate_domain(domain: str) -> str:
    return validate_bind_host(domain)


def default_admin_email_for_host(primary: str) -> str:
    if is_valid_ip(primary):
        return "admin@example.com"
    return f"admin@{primary}"


def parse_domains(domains: list[str] | str) -> list[str]:
    """将域名列表或换行分隔文本解析为去重后的域名列表（首项为主域名）。"""
    raw_lines: list[str]
    if isinstance(domains, str):
        raw_lines = domains.splitlines()
    else:
        raw_lines = list(domains)

    result: list[str] = []
    seen: set[str] = set()
    for line in raw_lines:
        text = line.strip()
        if not text:
            continue
        d = validate_domain(text)
        if d not in seen:
            seen.add(d)
            result.append(d)
    return result


def normalize_site_entry(
    entry: dict[str, Any],
    *,
    default_locale: str = "zh_CN",
    auto_wp_password: bool = False,
) -> dict[str, Any]:
    domains = parse_domains(entry.get("domains") or [])
    if not domains:
        raise ValueError("每个网站至少需要一个绑定域名")

    site_name = (entry.get("site_name") or "").strip() or "示例博客"
    primary = domains[0]
    wp_user = (entry.get("wp_admin_user") or "").strip() or "admin"
    wp_pass = entry.get("wp_admin_password")
    if not wp_pass:
        wp_pass = _gen_password()
        auto_wp = True
    else:
        auto_wp = auto_wp_password

    raw_email = (entry.get("wp_admin_email") or "").strip() or default_admin_email_for_host(primary)
    email = validate_email(raw_email) or raw_email
    locale = (entry.get("wp_locale") or default_locale).strip() or default_locale

    db_prefix = entry.get("db_prefix")
    db_name = entry.get("db_name")
    db_user = entry.get("db_user")

    return {
        "site_name": site_name,
        "domains": domains,
        "primary_domain": primary,
        "wp_admin_user": wp_user,
        "wp_admin_password": wp_pass,
        "wp_password_auto_generated": auto_wp,
        "wp_admin_email": email,
        "wp_locale": locale,
        "db_prefix": sanitize_db_prefix(db_prefix) if db_prefix else None,
        "db_name": sanitize_sql_identifier(db_name, max_len=64) if db_name else None,
        "db_user": sanitize_sql_identifier(db_user, max_len=16) if db_user else None,
        "db_password": entry.get("db_password"),
    }


def collect_all_domains(sites: list[dict[str, Any]]) -> list[str]:
    all_domains: list[str] = []
    seen: set[str] = set()
    for site in sites:
        for d in site.get("domains") or []:
            if d not in seen:
                seen.add(d)
                all_domains.append(d)
    return all_domains


def find_duplicate_domains(sites: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    dupes: list[str] = []
    for site in sites:
        for d in site.get("domains") or []:
            if d in seen and d not in dupes:
                dupes.append(d)
            seen.add(d)
    return dupes


def resolve_sites_from_task(task: DeployTask) -> list[dict[str, Any]]:
    if task.sites_config:
        return [dict(s) for s in task.sites_config]
    primary = (task.site_domain or "").strip().lower()
    return [
        {
            "site_name": task.site_name,
            "domains": [primary] if primary else [],
            "primary_domain": primary,
            "wp_admin_user": task.wp_admin_user,
            "wp_admin_password": task.wp_admin_password,
            "wp_password_auto_generated": task.wp_password_auto_generated,
            "wp_admin_email": task.wp_admin_email,
            "wp_locale": task.wp_locale,
            "db_prefix": task.db_prefix,
            "db_name": task.db_name,
            "db_user": task.db_user,
            "db_password": task.db_password,
        }
    ]


def persist_site_db_credentials(
    task: DeployTask,
    site_index: int,
    site_info: dict[str, Any],
    db: Session,
) -> None:
    """将自动生成的库凭据写回 sites_config 对应条目。"""
    sites = resolve_sites_from_task(task)
    if site_index >= len(sites):
        return

    entry = sites[site_index]
    changed = False
    for field, info_key in (
        ("db_name", "db_name"),
        ("db_user", "db_user"),
        ("db_password", "db_pass"),
    ):
        resolved = site_info[info_key]
        if entry.get(field) != resolved:
            entry[field] = resolved
            changed = True

    if changed:
        task.sites_config = sites
        if site_index == 0:
            task.db_name = site_info["db_name"]
            task.db_user = site_info["db_user"]
            task.db_password = site_info["db_pass"]
        db.commit()
