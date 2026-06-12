from __future__ import annotations

import re

from sqlalchemy.orm import Session

from app.models.deploy import DeployTask
from app.services.log_publisher import publish_log
from app.services.preflight import (
    BAOTA_PANEL_INSTALL_URL,
    detect_server_os,
    normalize_server_os,
    run_preflight_for_task,
    server_os_label,
)
from app.services.baota_panel import _PANEL_PYTHON_BOOTSTRAP, _run_panel_python
from app.services.remote_probe import find_baota_install_log, is_baota_install_running, is_baota_panel_ready
from app.services.remote_state import RemoteDeployState
from app.services.ssh import SSHClient

OS_INSTALL_LABELS = {
    "ubuntu": "Ubuntu / Deepin 官方",
    "debian": "Debian 官方",
    "centos": "CentOS / 阿里云等 官方",
    "generic": "通用",
}


def _ssh_from_task(task: DeployTask, password: str, db: Session) -> SSHClient:
    return SSHClient(
        host=task.ssh_host,
        port=task.ssh_port,
        username=task.ssh_user,
        password=password,
        task_id=task.id,
        log_phase="step1_baota",
        db=db,
    )


def _install_flags(task: DeployTask) -> str:
    return (
        f"-u {task.bt_user} -p {task.bt_password} -P {task.bt_port} "
        f"--safe-path {task.bt_safe_path} --ssl-disable -y"
    )


def build_baota_install_command(server_os: str, task: DeployTask) -> str:
    """按宝塔官方文档生成 install_panel.sh 安装命令。"""
    flags = _install_flags(task)
    os_key = normalize_server_os(server_os)
    url = BAOTA_PANEL_INSTALL_URL

    if os_key == "centos":
        return (
            f"url={url};"
            "if [ -f /usr/bin/curl ]; then curl -sSO $url; else wget -O install_panel.sh $url; fi && "
            f"bash install_panel.sh {flags}"
        )
    if os_key == "debian":
        return f"wget -O install_panel.sh {url} && bash install_panel.sh {flags}"
    if os_key == "ubuntu":
        return f"wget -O install_panel.sh {url} && sudo bash install_panel.sh {flags}"

    return (
        f"url={url};"
        "if [ -f /usr/bin/curl ]; then curl -sSO $url; else wget -O install_panel.sh $url; fi && "
        f"bash install_panel.sh {flags}"
    )


def _install_methods(server_os: str, detected_os: str | None = None) -> list[tuple[str, str]]:
    os_key = normalize_server_os(server_os)
    if os_key != "generic":
        return [
            (os_key, OS_INSTALL_LABELS[os_key]),
            ("generic", OS_INSTALL_LABELS["generic"]),
        ]

    if detected_os and detected_os != "generic":
        return [
            (detected_os, f"自动检测 · {OS_INSTALL_LABELS.get(detected_os, detected_os)}"),
            ("generic", OS_INSTALL_LABELS["generic"]),
        ]
    return [("generic", OS_INSTALL_LABELS["generic"])]


def preflight_check(task: DeployTask, password: str, db: Session) -> None:
    publish_log(task.id, "step1_baota", "正在执行部署前环境检测...", db)

    result = run_preflight_for_task(task, password)
    if result.blocked or result.domain_conflict:
        raise RuntimeError(result.message)
    if not result.ok:
        raise RuntimeError(result.message)
    if result.requires_confirmation and not task.confirm_non_fresh:
        raise RuntimeError(
            "服务器不是全新环境，且未获得二次确认。请在前端完成环境检测并确认后再部署。"
        )

    publish_log(task.id, "step1_baota", "SSH 连通: 成功", db)
    publish_log(
        task.id,
        "step1_baota",
        f"系统版本: {result.os_pretty or result.os_detected} {result.os_version}".strip(),
    )

    if result.is_fresh:
        publish_log(task.id, "step1_baota", "环境检测: 全新服务器，未发现宝塔与 Web 环境", db)
    else:
        publish_log(task.id, "step1_baota", "环境检测: 非全新环境（用户已二次确认）", db)
        for w in result.warnings:
            publish_log(task.id, "step1_baota", f"警告: {w}", db)

    if result.baota_installed:
        publish_log(task.id, "step1_baota", "宝塔面板已安装，将跳过面板安装步骤", db)
    else:
        selected = server_os_label(task.server_os)
        if normalize_server_os(task.server_os) == "generic" and result.os_detected != "generic":
            publish_log(
                task.id,
                "step1_baota",
                f"未手动选择系统，预检识别为 {server_os_label(result.os_detected)}，将优先使用对应官方安装命令",
                db,
            )
        else:
            publish_log(task.id, "step1_baota", f"未检测到宝塔面板，准备安装（{selected}）", db)


def _run_install_attempt(
    ssh: SSHClient,
    install_cmd: str,
    method_label: str,
    secrets: list[str],
    panel_ready,
    task: DeployTask,
    db: Session,
    attempt_index: int,
) -> bool:
    publish_log(
        task.id,
        "step1_baota",
        f"使用 {method_label} 安装脚本...",
        db,
    )
    log_file = f"/tmp/qbw_install_panel_{attempt_index}.log"

    try:
        ssh.run_background_tail(
            install_cmd,
            log_file=log_file,
            poll_interval=15,
            max_wait=1800,
            ready_check=panel_ready,
            secrets=secrets,
        )
    except TimeoutError:
        if panel_ready():
            publish_log(task.id, "step1_baota", "安装超时但面板似乎已就绪，继续...", db)
            return True
        publish_log(task.id, "step1_baota", f"{method_label} 安装超时且面板未就绪", db)
        return False

    if panel_ready():
        return True

    publish_log(task.id, "step1_baota", f"{method_label} 安装结束但面板未就绪", db)
    return False


def install_baota_panel(
    task: DeployTask,
    password: str,
    db: Session,
    remote_state: RemoteDeployState | None = None,
) -> dict:
    baota_status = remote_state.baota if remote_state else None
    skip_ready = baota_status == "complete"

    if not skip_ready:
        publish_log(task.id, "step1_baota", "开始安装宝塔面板（预计 5-10 分钟）...", db)

    with _ssh_from_task(task, password, db) as ssh:
        secrets = [password, task.bt_password]

        def panel_ready() -> bool:
            return is_baota_panel_ready(ssh, secrets)

        if skip_ready or panel_ready():
            panel_info = _finish_panel_setup(ssh, task, password, db, verbose=not skip_ready)
            publish_log(task.id, "step1_baota", "宝塔面板已就绪，跳过安装", db)
            return panel_info

        if baota_status == "in_progress" or is_baota_install_running(ssh, secrets):
            publish_log(
                task.id,
                "step1_baota",
                "检测到宝塔正在安装中，等待现有安装完成（不重复执行）...",
                db,
            )
            log_file = find_baota_install_log(ssh, secrets)
            try:
                ssh.tail_until_ready(
                    log_file=log_file,
                    poll_interval=15,
                    max_wait=1800,
                    ready_check=panel_ready,
                    secrets=secrets,
                )
            except TimeoutError:
                if panel_ready():
                    publish_log(task.id, "step1_baota", "等待超时但面板似乎已就绪，继续...", db)
                else:
                    publish_log(
                        task.id,
                        "step1_baota",
                        "等待超时且面板未就绪，将重新尝试安装...",
                        db,
                    )
            if panel_ready():
                panel_info = _finish_panel_setup(ssh, task, password, db)
                publish_log(task.id, "step1_baota", f"宝塔面板安装完成: {panel_info['panel_url']}", db)
                return panel_info

        code, out, _ = ssh.run("test -d /www/server/panel && echo EXISTS || echo FRESH", secrets=[password])
        if "EXISTS" in out and panel_ready():
            panel_info = _finish_panel_setup(ssh, task, password, db)
            publish_log(task.id, "step1_baota", "宝塔面板已存在，使用现有面板", db)
            return panel_info

        detected_os: str | None = None
        if normalize_server_os(task.server_os) == "generic":
            detected_os = detect_server_os(ssh, secrets)
            if detected_os != "generic":
                publish_log(
                    task.id,
                    "step1_baota",
                    f"自动检测操作系统: {server_os_label(detected_os)}，选用官方推荐安装命令",
                    db,
                )
            else:
                publish_log(
                    task.id,
                    "step1_baota",
                    "未能明确识别系统类型，将使用通用安装命令",
                    db,
                )

        methods = _install_methods(task.server_os, detected_os)
        installed = False

        for index, (os_key, method_label) in enumerate(methods):
            if index > 0:
                publish_log(
                    task.id,
                    "step1_baota",
                    f"{methods[0][1]} 安装未成功，降级使用通用安装脚本重试...",
                    db,
                )
            install_cmd = build_baota_install_command(os_key, task)
            if _run_install_attempt(
                ssh,
                install_cmd,
                method_label,
                secrets,
                panel_ready,
                task,
                db,
                index,
            ):
                installed = True
                break

        if not installed:
            raise RuntimeError("宝塔面板安装失败，已尝试专用脚本与通用脚本")

        panel_info = _finish_panel_setup(ssh, task, password, db)
        publish_log(task.id, "step1_baota", f"宝塔面板安装完成: {panel_info['panel_url']}", db)
        return panel_info


PANEL_DIR = "/www/server/panel"
PANEL_ADMIN_PATH_PL = f"{PANEL_DIR}/data/admin_path.pl"

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9?!]*[ -/]*[@-~]")


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE.sub("", text)


def _build_configure_panel_credentials_script(username: str, password: str) -> str:
    return f"""import os
{_PANEL_PYTHON_BOOTSTRAP}
import tools

tools.panel({password!r})
tools.set_panel_username({username!r})
default_pl = os.path.join({PANEL_DIR!r}, "default.pl")
with open(default_pl, "w", encoding="utf-8") as fh:
    fh.write({password!r})
os.chmod(default_pl, 0o600)
print("PANEL_CREDS_OK")
"""


def configure_panel_credentials(
    task: DeployTask,
    ssh: SSHClient,
    secrets: list[str],
    db: Session,
) -> None:
    """将宝塔面板登录账号/密码设为任务记录值，并同步写入 default.pl。"""
    publish_log(
        task.id,
        "step1_baota",
        f"正在设置宝塔面板登录账号: {task.bt_user}",
        db,
    )
    script = _build_configure_panel_credentials_script(task.bt_user, task.bt_password)
    code, out, err = _run_panel_python(ssh, script, timeout=120, secrets=secrets)
    combined = (out + err).strip()
    if code != 0 or "PANEL_CREDS_OK" not in combined:
        snippet = combined.replace("\n", " ")[:300]
        raise RuntimeError(f"宝塔面板账号设置失败: {snippet or '无输出'}")
    publish_log(task.id, "step1_baota", "宝塔面板登录账号与密码已设置（以任务记录为准）", db)


def _finish_panel_setup(
    ssh: SSHClient,
    task: DeployTask,
    password: str,
    db: Session,
    *,
    verbose: bool = True,
) -> dict:
    secrets = [password, task.bt_password]
    configure_panel_credentials(task, ssh, secrets, db)
    return get_panel_info(ssh, task, password, verbose=verbose)


def _read_panel_safe_path(ssh: SSHClient, secrets: list[str]) -> str | None:
    _, out, _ = ssh.run(f"cat {PANEL_ADMIN_PATH_PL} 2>/dev/null", timeout=15, secrets=secrets)
    path = (out or "").strip().strip("/")
    if path and re.match(r"^[a-zA-Z0-9_\-]+$", path):
        return path
    return None


def _parse_bt_default_output(out: str) -> dict[str, str]:
    """解析 bt default 输出；需先剥离 ANSI，且逐行匹配避免误抓 Warning。"""
    parsed: dict[str, str] = {}
    for line in _strip_ansi(out).splitlines():
        text = line.strip()
        if not text:
            continue
        url_match = re.search(r"https?://[^\s\]]+", text)
        if url_match and "panel_url" not in parsed:
            parsed["panel_url"] = url_match.group(0).rstrip("/")
    return parsed


def get_panel_info(
    ssh: SSHClient,
    task: DeployTask,
    password: str,
    *,
    verbose: bool = True,
) -> dict:
    """解析面板访问地址；登录账号与密码始终以任务数据库记录为准。"""
    secrets = [password, task.bt_password]

    safe_path = _read_panel_safe_path(ssh, secrets) or task.bt_safe_path
    panel_url = f"http://{task.ssh_host}:{task.bt_port}/{safe_path}"

    if verbose:
        _, out, _ = ssh.run("timeout 45 bt default 2>/dev/null || true", timeout=60, secrets=secrets)
        if out:
            parsed = _parse_bt_default_output(out)
            if parsed.get("panel_url"):
                panel_url = parsed["panel_url"]

    return {
        "panel_url": panel_url,
        "panel_user": task.bt_user,
        "panel_password": task.bt_password,
    }
