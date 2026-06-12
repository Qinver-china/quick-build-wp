from __future__ import annotations

from dataclasses import dataclass

from app.models.deploy import DeployPhase, DeployTask
from app.services.remote_state import RemoteDeployState

PHASE_LABELS = {
    DeployPhase.STEP1_BAOTA: "宝塔面板",
    DeployPhase.STEP2_NGINX: "Nginx",
    DeployPhase.STEP3_PHP: "PHP",
    DeployPhase.STEP2_PHP: "PHP",
    DeployPhase.STEP3_MYSQL: "MySQL",
    DeployPhase.STEP4_REDIS: "Redis",
    DeployPhase.STEP5_PHP_EXT: "PHP 组件与扩展",
    DeployPhase.STEP6_OPTIMIZE: "环境参数调优",
    DeployPhase.STEP7_SITE: "网站与 WordPress",
}

STEP_CHECKS: list[tuple[str, str, str]] = [
    ("baota", "baota", "宝塔面板"),
    ("nginx", "nginx", "Nginx"),
    ("php", "php", "PHP"),
    ("mysql", "mysql", "MySQL"),
    ("redis", "redis_server", "Redis 服务"),
    ("php_ext", "php_extensions", "PHP 组件与扩展"),
    ("optimize", "optimize", "环境参数调优"),
    ("site", "site_prepared", "站点目录与 Nginx"),
    ("wordpress", "wordpress", "WordPress"),
]


@dataclass
class DeployProgress:
    panel_info: dict | None = None
    lnmp_info: dict | None = None
    redis_info: dict | None = None
    optimize_info: dict | None = None
    site_info: dict | None = None
    wp_info: dict | None = None
    remote_state: RemoteDeployState | None = None


def _is_complete(state: RemoteDeployState | None, attr: str) -> bool:
    if not state:
        return False
    return getattr(state, attr, "missing") == "complete"


def _build_manual_hint(completed_labels: list[str], failed_label: str) -> str:
    if not completed_labels:
        return f"在「{failed_label}」步骤失败，请查看日志排查问题后重试，或参考宝塔面板手动完成后续安装。"
    if len(completed_labels) == 1:
        return f"{completed_labels[0]}已经安装好，您可以手动继续接下来的操作。"
    return f"已完成：{'、'.join(completed_labels)}。您可以登录宝塔面板或 SSH 手动继续后续步骤。"


def build_partial_result(
    task: DeployTask,
    progress: DeployProgress,
    *,
    failed_phase: DeployPhase | None = None,
) -> dict:
    state = progress.remote_state
    failed_phase = failed_phase or task.current_phase
    failed_label = PHASE_LABELS.get(failed_phase, failed_phase.value)

    completed_steps: list[dict[str, str]] = []
    completed_labels: list[str] = []

    for key, attr, label in STEP_CHECKS:
        if _is_complete(state, attr):
            completed_steps.append({"key": key, "label": label, "status": "complete"})
            if label not in completed_labels:
                completed_labels.append(label)

    manual_hint = _build_manual_hint(completed_labels, failed_label)

    panel_url = f"http://{task.ssh_host}:{task.bt_port}/{task.bt_safe_path}"
    if progress.panel_info:
        panel_url = progress.panel_info.get("panel_url") or panel_url

    result: dict = {
        "partial": True,
        "failed_phase": failed_phase.value,
        "failed_label": failed_label,
        "manual_hint": manual_hint,
        "completed_steps": completed_steps,
        "completed_labels": completed_labels,
    }

    if _is_complete(state, "baota"):
        result["panel_url"] = panel_url
        result["panel_user"] = task.bt_user
        result["panel_password"] = task.bt_password

    env: dict[str, str] = {}
    if _is_complete(state, "nginx"):
        env["nginx"] = (progress.lnmp_info or {}).get("nginx") or task.nginx_version
    if _is_complete(state, "php"):
        env["php"] = (progress.lnmp_info or {}).get("php") or task.php_version
    if _is_complete(state, "mysql"):
        env["mysql"] = (progress.lnmp_info or {}).get("mysql") or task.mysql_version
    if env:
        result["environment"] = env

    if progress.redis_info and (_is_complete(state, "redis_server") or progress.redis_info.get("redis_server")):
        result["redis"] = {
            "host": progress.redis_info.get("redis_host", "127.0.0.1"),
            "port": progress.redis_info.get("redis_port", 6379),
            "php_extension": progress.redis_info.get("redis_php_extension"),
        }

    if progress.optimize_info:
        result["optimize"] = progress.optimize_info

    site_info = progress.site_info or {}
    if site_info.get("db_name"):
        result["database"] = {
            "name": site_info.get("db_name"),
            "user": site_info.get("db_user"),
            "password": site_info.get("db_pass"),
            "prefix": site_info.get("db_prefix"),
        }
    if _is_complete(state, "site_prepared") or site_info.get("site_path"):
        result["site"] = {
            "site_name": site_info.get("site_name") or task.site_name,
            "site_domain": site_info.get("site_domain") or task.site_domain,
            "site_path": site_info.get("site_path"),
            "site_url": site_info.get("site_url") or f"http://{task.site_domain}",
        }

    if _is_complete(state, "wordpress") or progress.wp_info:
        wp = progress.wp_info or {}
        result["site_url"] = wp.get("site_url") or result.get("site", {}).get("site_url")
        result["admin_url"] = wp.get("admin_url")
        result["admin_user"] = wp.get("admin_user") or task.wp_admin_user
        result["admin_password"] = task.wp_admin_password
        result["password_auto_generated"] = task.wp_password_auto_generated
        result["site_name"] = task.site_name

    return result
