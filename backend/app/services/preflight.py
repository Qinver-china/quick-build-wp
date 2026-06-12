"""服务器部署前环境检测。"""

from __future__ import annotations

import concurrent.futures
import re
from dataclasses import dataclass, field

from app.services.php_compat import resolve_php_version_for_os
from app.services.ssh import SSHClient

PREFLIGHT_TIMEOUT = 90
SSH_CONNECT_TIMEOUT = 20

OS_ALIASES = {
    "ubuntu": ("ubuntu", "deepin"),
    "debian": ("debian",),
    "centos": (
        "centos",
        "rhel",
        "rocky",
        "almalinux",
        "alma",
        "ol",
        "alinux",
        "anolis",
        "opencloud",
        "opencloudos",
        "tencentos",
        "huawei",
    ),
    "generic": (),
}

BAOTA_PANEL_INSTALL_URL = "https://download.bt.cn/install/install_panel.sh"

OS_LABELS = {
    "ubuntu": "Ubuntu",
    "debian": "Debian",
    "centos": "CentOS / RHEL 系",
    "generic": "通用",
}


def normalize_server_os(value: str | None) -> str:
    if value in (None, "", "other"):
        return "generic"
    if value in OS_ALIASES:
        return value
    return "generic"


def server_os_label(value: str | None) -> str:
    return OS_LABELS.get(normalize_server_os(value), "通用")

PREFLIGHT_SCRIPT = r"""
echo "===QBW_PREFLIGHT_START==="
if [ -f /etc/os-release ]; then
  . /etc/os-release
  echo "OS_ID=${ID:-unknown}"
  echo "OS_ID_LIKE=${ID_LIKE:-}"
  echo "OS_VERSION=${VERSION_ID:-unknown}"
  echo "OS_PRETTY=${PRETTY_NAME:-unknown}"
else
  echo "OS_ID=unknown"
  echo "OS_VERSION=unknown"
  echo "OS_PRETTY=$(uname -srm 2>/dev/null || echo unknown)"
fi
uname -a 2>/dev/null || true

if [ -d /www/server/panel ]; then echo "BAOTA=installed"; else echo "BAOTA=none"; fi

WEB_FLAGS=""
[ -d /www/server/nginx ] || [ -x /www/server/nginx/sbin/nginx ] 2>/dev/null && WEB_FLAGS="${WEB_FLAGS}nginx,"
[ -d /www/server/apache ] || [ -x /www/server/apache/bin/httpd ] 2>/dev/null && WEB_FLAGS="${WEB_FLAGS}apache,"
[ -d /www/server/php ] && WEB_FLAGS="${WEB_FLAGS}php,"
[ -d /www/server/mysql ] || [ -d /www/server/mariadb ] && WEB_FLAGS="${WEB_FLAGS}mysql,"
command -v nginx >/dev/null 2>&1 && WEB_FLAGS="${WEB_FLAGS}nginx-bin,"
command -v httpd >/dev/null 2>&1 && WEB_FLAGS="${WEB_FLAGS}httpd-bin,"
command -v apache2 >/dev/null 2>&1 && WEB_FLAGS="${WEB_FLAGS}apache2-bin,"
[ -d /www/wwwroot ] && site_count=$(find /www/wwwroot -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l) && echo "SITE_DIRS=${site_count}"
echo "WEB_ENV=${WEB_FLAGS%,}"

if [ -d /www/wwwroot ]; then
  find /www/wwwroot -mindepth 1 -maxdepth 1 -type d 2>/dev/null | head -5 | while read d; do
    echo "SITE_SAMPLE=$(basename "$d")"
  done
fi
echo "===QBW_PREFLIGHT_END==="
"""


@dataclass
class PreflightResult:
    ok: bool
    ssh_ok: bool
    is_fresh: bool
    requires_confirmation: bool
    os_detected: str
    os_version: str
    os_pretty: str
    os_match: bool
    baota_installed: bool
    web_environment: list[str] = field(default_factory=list)
    site_dirs: int = 0
    site_samples: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    message: str = ""
    uname: str | None = None
    blocked: bool = False
    domain_conflict: bool = False
    target_domain: str = ""
    target_domains: list[str] = field(default_factory=list)
    conflicting_domains: list[str] = field(default_factory=list)
    existing_site_for_domain: bool = False
    php_version_requested: str | None = None
    php_version_effective: str | None = None
    php_version_fallback: bool = False

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "ssh_ok": self.ssh_ok,
            "is_fresh": self.is_fresh,
            "requires_confirmation": self.requires_confirmation,
            "os_detected": self.os_detected,
            "os_version": self.os_version,
            "os_pretty": self.os_pretty,
            "os_match": self.os_match,
            "baota_installed": self.baota_installed,
            "web_environment": self.web_environment,
            "site_dirs": self.site_dirs,
            "site_samples": self.site_samples,
            "warnings": self.warnings,
            "message": self.message,
            "uname": self.uname,
            "blocked": self.blocked,
            "domain_conflict": self.domain_conflict,
            "target_domain": self.target_domain,
            "target_domains": self.target_domains,
            "conflicting_domains": self.conflicting_domains,
            "existing_site_for_domain": self.existing_site_for_domain,
            "php_version_requested": self.php_version_requested,
            "php_version_effective": self.php_version_effective,
            "php_version_fallback": self.php_version_fallback,
        }


def _domain_dir_name(domain: str) -> str:
    return domain.strip().lower().replace(".", "_")


def _web_has_component(web_environment: list[str], *keys: str) -> bool:
    return any(key in web_environment for key in keys)


def _lnmp_stack_ready(web_environment: list[str]) -> bool:
    return (
        _web_has_component(web_environment, "nginx", "nginx-bin")
        and _web_has_component(web_environment, "php")
        and _web_has_component(web_environment, "mysql")
    )


def _target_domain_site_exists(
    ssh: SSHClient,
    domain: str,
    secrets: list[str],
    site_samples: list[str],
) -> bool:
    domain = domain.strip().lower()
    if not domain:
        return False

    dir_name = _domain_dir_name(domain)
    if dir_name in site_samples:
        return True

    site_path = f"/www/wwwroot/{dir_name}"
    conf_path = f"/www/server/panel/vhost/nginx/{dir_name}.conf"
    _, out, _ = ssh.run(
        f'test -d "{site_path}" && echo PATH_OK; '
        f'if [ -f "{conf_path}" ]; then '
        f'grep -E "server_name[[:space:]].*{domain}" "{conf_path}" >/dev/null 2>&1 && echo VHOST_OK; '
        f"fi",
        timeout=20,
        secrets=secrets,
    )
    return "PATH_OK" in out or "VHOST_OK" in out


def detect_server_os(ssh: SSHClient, secrets: list[str]) -> str:
    """通过 /etc/os-release 识别远程系统，用于通用模式下的安装脚本选择。"""
    code, out, _ = ssh.run(
        '. /etc/os-release 2>/dev/null; echo "ID=${ID:-unknown}"; echo "ID_LIKE=${ID_LIKE:-}"',
        timeout=20,
        secrets=secrets,
    )
    if code != 0 or not out.strip():
        return "generic"
    lines = out.strip().splitlines()
    os_id = lines[0].split("=", 1)[-1] if lines else "unknown"
    os_id_like = lines[1].split("=", 1)[-1] if len(lines) > 1 else ""
    return _normalize_os_id(os_id, os_id_like)


def _normalize_os_id(os_id: str, os_id_like: str = "") -> str:
    combined = f"{os_id} {os_id_like}".lower()
    for label, aliases in OS_ALIASES.items():
        if label == "generic":
            continue
        for alias in aliases:
            if alias in combined or os_id.lower() == alias:
                return label
    return "generic"


def _os_matches_selected(detected: str, selected: str) -> bool:
    selected = normalize_server_os(selected)
    if selected == "generic" or detected == "generic":
        return True
    return detected == selected


def _parse_preflight_output(output: str) -> dict[str, str | list[str]]:
    data: dict[str, str | list[str]] = {"site_samples": []}
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("==="):
            continue
        if line.startswith("SITE_SAMPLE="):
            samples = data.get("site_samples", [])
            if isinstance(samples, list):
                samples.append(line.split("=", 1)[1])
            continue
        if "=" in line:
            key, val = line.split("=", 1)
            data[key] = val
    return data


def _ssh_failure(message: str) -> PreflightResult:
    return PreflightResult(
        ok=False,
        ssh_ok=False,
        is_fresh=False,
        requires_confirmation=False,
        os_detected="unknown",
        os_version="",
        os_pretty="",
        os_match=False,
        baota_installed=False,
        warnings=[],
        message=message,
    )


def run_preflight(
    host: str,
    port: int,
    username: str,
    password: str,
    server_os: str = "generic",
    site_domain: str | None = None,
    site_domains: list[str] | None = None,
    php_version: str | None = None,
) -> PreflightResult:
    ssh = SSHClient(host, port, username, password, log_phase="preflight")
    warnings: list[str] = []

    try:
        ssh.connect(timeout=SSH_CONNECT_TIMEOUT)
    except Exception as e:
        return _ssh_failure(f"SSH 连接失败: {e}")

    try:
        code, out, err = ssh.run("uname -a && id", timeout=15, secrets=[password])
        if code != 0:
            return _ssh_failure(f"SSH 连接失败: {err or out or '命令执行失败'}")
        if "uid=0" not in out and "root" not in username:
            return _ssh_failure("需要 root 权限或具备 sudo 的账号")
        uname_info = out.strip()

        code, out, err = ssh.run_script(PREFLIGHT_SCRIPT, timeout=60, secrets=[password])
        if code != 0:
            return PreflightResult(
                ok=False,
                ssh_ok=True,
                is_fresh=False,
                requires_confirmation=False,
                os_detected="unknown",
                os_version="",
                os_pretty="",
                os_match=False,
                baota_installed=False,
                warnings=[],
                message=f"环境检测命令失败: {err or out}",
                uname=uname_info,
            )

        parsed = _parse_preflight_output(out)
        os_id = str(parsed.get("OS_ID", "unknown"))
        os_id_like = str(parsed.get("OS_ID_LIKE", ""))
        os_detected = _normalize_os_id(os_id, os_id_like)
        os_version = str(parsed.get("OS_VERSION", ""))
        os_pretty = str(parsed.get("OS_PRETTY", ""))
        os_match = _os_matches_selected(os_detected, server_os)

        baota_installed = parsed.get("BAOTA") == "installed"
        web_raw = str(parsed.get("WEB_ENV", ""))
        web_environment = [w for w in web_raw.split(",") if w] if web_raw else []

        site_dirs = 0
        try:
            site_dirs = int(str(parsed.get("SITE_DIRS", "0")))
        except ValueError:
            site_dirs = 0

        site_samples = parsed.get("site_samples", [])
        if not isinstance(site_samples, list):
            site_samples = []

        # Windows / 非 Linux 粗检
        if re.search(r"windows|mingw|mswin", out, re.I):
            return PreflightResult(
                ok=False,
                ssh_ok=True,
                is_fresh=False,
                requires_confirmation=False,
                os_detected=os_detected,
                os_version=os_version,
                os_pretty=os_pretty,
                os_match=False,
                baota_installed=baota_installed,
                web_environment=web_environment,
                site_dirs=site_dirs,
                site_samples=site_samples,
                warnings=["检测到 Windows 系统，不支持自动部署"],
                message="不支持 Windows 系统，请使用 Ubuntu、Debian 或 CentOS 等 Linux 服务器",
                uname=uname_info,
            )

        if not os_match:
            warnings.append(
                f"检测到系统为 {os_pretty or os_detected}，与您选择的「{server_os_label(server_os)}」不一致，请确认"
            )

        if baota_installed:
            warnings.append("检测到已安装宝塔面板")

        web_labels = {
            "nginx": "Nginx",
            "apache": "Apache",
            "php": "PHP",
            "mysql": "MySQL/MariaDB",
            "nginx-bin": "系统 Nginx",
            "httpd-bin": "系统 Httpd",
            "apache2-bin": "系统 Apache2",
        }
        seen: set[str] = set()
        for w in web_environment:
            label = web_labels.get(w, w)
            if label not in seen:
                seen.add(label)
                warnings.append(f"检测到已有 Web 环境: {label}")

        if site_dirs > 0:
            sample = "、".join(site_samples[:3]) if site_samples else ""
            extra = f"（如 {sample}）" if sample else ""
            warnings.append(f"检测到已有网站目录 {site_dirs} 个{extra}")

        php_version_requested: str | None = None
        php_version_effective: str | None = None
        php_version_fallback = False

        if php_version:
            effective, fallback_warn = resolve_php_version_for_os(
                php_version, os_detected, os_version
            )
            php_version_effective = effective
            if effective != php_version.strip():
                php_version_requested = php_version.strip()
                php_version_fallback = True
                if fallback_warn:
                    warnings.append(fallback_warn)

        is_fresh = not baota_installed and not web_environment and site_dirs == 0
        requires_confirmation = not is_fresh

        check_domains: list[str] = []
        if site_domains:
            seen: set[str] = set()
            for d in site_domains:
                dd = d.strip().lower()
                if dd and dd not in seen:
                    seen.add(dd)
                    check_domains.append(dd)
        elif site_domain:
            check_domains = [site_domain.strip().lower()]

        target_domain = check_domains[0] if check_domains else ""
        target_domains = check_domains
        conflicting_domains: list[str] = []
        existing_site_for_domain = False
        domain_conflict = False
        blocked = False

        if check_domains:
            for d in check_domains:
                if _target_domain_site_exists(ssh, d, [password], site_samples):
                    conflicting_domains.append(d)
            existing_site_for_domain = bool(conflicting_domains)
            if (
                baota_installed
                and _lnmp_stack_ready(web_environment)
                and conflicting_domains
            ):
                domain_conflict = True
                blocked = True
                names = "、".join(f"「{d}」" for d in conflicting_domains)
                message = (
                    f"服务器已安装宝塔、Nginx、PHP、MySQL，且已存在域名 {names} 的网站，"
                    "无法在同一域名上重复安装。请更换绑定域名，或在全新服务器上部署。"
                )
                return PreflightResult(
                    ok=False,
                    ssh_ok=True,
                    is_fresh=False,
                    requires_confirmation=True,
                    os_detected=os_detected,
                    os_version=os_version,
                    os_pretty=os_pretty,
                    os_match=os_match,
                    baota_installed=baota_installed,
                    web_environment=web_environment,
                    site_dirs=site_dirs,
                    site_samples=site_samples,
                    warnings=warnings,
                    message=message,
                    uname=uname_info,
                    blocked=True,
                    domain_conflict=True,
                    target_domain=target_domain,
                    target_domains=target_domains,
                    conflicting_domains=conflicting_domains,
                    existing_site_for_domain=True,
                )

        if is_fresh:
            message = f"SSH 连接成功。系统: {os_pretty or os_detected} {os_version}。服务器为全新环境，可以安装。"
        else:
            message = (
                "SSH 连接成功，但服务器并非全新环境。"
                "强烈建议在全新服务器上安装；若继续，将自动跳过已安装组件并从缺失步骤开始。"
            )

        return PreflightResult(
            ok=True,
            ssh_ok=True,
            is_fresh=is_fresh,
            requires_confirmation=requires_confirmation,
            os_detected=os_detected,
            os_version=os_version,
            os_pretty=os_pretty,
            os_match=os_match,
            baota_installed=baota_installed,
            web_environment=web_environment,
            site_dirs=site_dirs,
            site_samples=site_samples,
            warnings=warnings,
            message=message,
            uname=uname_info,
            blocked=blocked,
            domain_conflict=domain_conflict,
            target_domain=target_domain,
            target_domains=target_domains,
            conflicting_domains=conflicting_domains,
            existing_site_for_domain=existing_site_for_domain,
            php_version_requested=php_version_requested,
            php_version_effective=php_version_effective or php_version,
            php_version_fallback=php_version_fallback,
        )
    finally:
        ssh.close()


def run_preflight_for_task(task, password: str) -> PreflightResult:
    from app.services.site_config import collect_all_domains, resolve_sites_from_task

    sites = resolve_sites_from_task(task)
    all_domains = collect_all_domains(sites)
    return run_preflight_with_timeout(
        host=task.ssh_host,
        port=task.ssh_port,
        username=task.ssh_user,
        password=password,
        server_os=normalize_server_os(getattr(task, "server_os", "generic")),
        site_domain=all_domains[0] if all_domains else getattr(task, "site_domain", None),
        site_domains=all_domains or None,
        php_version=getattr(task, "php_version", None),
    )


def run_preflight_with_timeout(
    host: str,
    port: int,
    username: str,
    password: str,
    server_os: str = "generic",
    site_domain: str | None = None,
    site_domains: list[str] | None = None,
    php_version: str | None = None,
    timeout: int = PREFLIGHT_TIMEOUT,
) -> PreflightResult:
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            run_preflight,
            host,
            port,
            username,
            password,
            server_os,
            site_domain,
            site_domains,
            php_version,
        )
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            return _ssh_failure("环境检测超时，请检查 SSH 地址、端口和网络连通性")
        except Exception as e:
            return _ssh_failure(f"SSH 连接失败: {e}")
