"""通过宝塔官方 install 脚本（nginx.sh / mysql.sh / php.sh）安装 LNMP 组件。"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session

from app.models.deploy import DeployTask
from app.services.log_publisher import publish_log
from app.services.php_compat import (
    PHP_FALLBACK_VERSION,
    can_auto_fallback_php,
    php_install_fallback_message,
    resolve_php_version_for_os,
)
from app.services.mysql_compat import (
    MYSQL_FALLBACK_VERSION,
    can_auto_fallback_mysql,
    mysql_install_fallback_message,
    resolve_mysql_version_for_ram,
)
from app.services.server_memory import detect_ram_mb
from app.services.remote_probe import (
    component_ready,
    is_component_install_running,
    probe_component_status,
)
from app.services.remote_state import RemoteDeployState, StepStatus
from app.services.ssh import SSHClient

PANEL_INSTALL_DIR = "/www/server/panel/install"
DOWNLOAD_URL_DEFAULT = "https://download.bt.cn"
INSTALL_LIB_MARKER = "/tmp/qbw_baota_install_lib.done"

# 与 install_panel_quick.sh 一致：组件脚本名与安装完成后的可执行文件探测路径
BAOTA_COMPONENTS: dict[str, tuple[str, str | None]] = {
    "nginx": ("nginx.sh", "/www/server/nginx/sbin/nginx"),
    "mysql": ("mysql.sh", "/www/server/mysql/bin/mysql"),
    "php": ("php.sh", None),
}


def _ssh(task: DeployTask, password: str, log_phase: str, db: Session) -> SSHClient:
    return SSHClient(
        host=task.ssh_host,
        port=task.ssh_port,
        username=task.ssh_user,
        password=password,
        task_id=task.id,
        log_phase=log_phase,
        db=db,
    )


def _resolve_download_url(ssh: SSHClient, secrets: list[str]) -> str:
    _, out, _ = ssh.run(
        "cat /www/server/panel/install/d_node.pl 2>/dev/null; "
        "cat /www/node.pl 2>/dev/null",
        timeout=15,
        secrets=secrets,
    )
    for line in (out or "").splitlines():
        url = line.strip()
        if url.startswith("http://") or url.startswith("https://"):
            return url.rstrip("/")
    return DOWNLOAD_URL_DEFAULT


def _resolve_bash_path(ssh: SSHClient, secrets: list[str]) -> str:
    """宝塔 quick 安装脚本中 BASH_FILI_PATH：yum 系为 1，apt 系为 4。"""
    _, out, _ = ssh.run(
        "if command -v apt-get >/dev/null 2>&1; then echo 4; "
        "elif command -v yum >/dev/null 2>&1; then echo 1; "
        "else echo 1; fi",
        timeout=15,
        secrets=secrets,
    )
    path = (out or "").strip()
    return path if path in ("1", "4") else "1"


def _ensure_panel_install_lib(
    task: DeployTask,
    ssh: SSHClient,
    secrets: list[str],
    db: Session,
    log_phase: str,
) -> None:
    """执行 lib.sh 准备编译依赖（与 install_panel_quick.sh 相同）。"""
    code, out, _ = ssh.run(
        f"test -f {INSTALL_LIB_MARKER} && echo OK",
        timeout=15,
        secrets=secrets,
    )
    if code == 0 and "OK" in out:
        return

    download_url = _resolve_download_url(ssh, secrets)
    bash_path = _resolve_bash_path(ssh, secrets)
    publish_log(task.id, log_phase, "正在准备宝塔安装前置依赖 (lib.sh)...", db)
    script = f"""set -e
mkdir -p {PANEL_INSTALL_DIR}
cd {PANEL_INSTALL_DIR}
echo "/www" > /var/bt_setupPath.conf
wget -O lib.sh {download_url}/install/{bash_path}/lib.sh
bash lib.sh
touch {INSTALL_LIB_MARKER}
"""
    code, out, err = ssh.run_script(script, timeout=900, secrets=secrets)
    if code != 0:
        publish_log(
            task.id,
            log_phase,
            f"警告: lib.sh 执行异常（将继续尝试安装组件）: {(err or out).strip()[:200]}",
            db,
        )
        ssh.run(f"touch {INSTALL_LIB_MARKER}", secrets=secrets)


def _ready_check(ssh: SSHClient, component: str, version: str, secrets: list[str]) -> Callable[[], bool]:
    def check() -> bool:
        return component_ready(ssh, component, version, secrets)

    return check


def _install_log_failure(ssh: SSHClient, log_file: str, secrets: list[str]) -> str | None:
    """从安装日志尾部提取失败原因（宝塔脚本常快速退出且 exit 0）。"""
    _, out, _ = ssh.run(
        f"cat {log_file} 2>/dev/null | tail -50",
        timeout=30,
        secrets=secrets,
    )
    markers = (
        "当前系统暂不支持",
        "安装失败",
        "install failed",
        "Install failed",
        "ERROR:",
        "error:",
        "至少需要",
        "空闲内存",
        "释放内存",
    )
    for line in out.splitlines():
        text = line.strip()
        if not text:
            continue
        for marker in markers:
            if marker in text:
                return text
    return None


def _fail_component_install(
    task: DeployTask,
    db: Session,
    log_phase: str,
    name: str,
    version: str,
    reason: str,
) -> None:
    publish_log(
        task.id,
        log_phase,
        f"错误: {name} {version} 安装失败: {reason}",
        db,
    )
    raise RuntimeError(f"{name} {version} 安装失败: {reason}")


def _read_remote_os(ssh: SSHClient, secrets: list[str]) -> tuple[str, str]:
    _, os_out, _ = ssh.run(
        '. /etc/os-release 2>/dev/null; echo "${ID:-}:${VERSION_ID:-}"',
        timeout=15,
        secrets=secrets,
    )
    os_id, _, os_ver = (os_out.strip() + "::").partition(":")
    return os_id, os_ver.rstrip(":")


def _apply_php_fallback(
    task: DeployTask,
    db: Session,
    from_version: str,
    to_version: str,
    reason: str,
    log_phase: str,
) -> None:
    if not task.php_version_requested:
        task.php_version_requested = from_version
    task.php_version = to_version
    db.commit()
    db.refresh(task)
    publish_log(
        task.id,
        log_phase,
        php_install_fallback_message(from_version, to_version, reason),
        db,
    )


def _maybe_downgrade_php_for_os(
    task: DeployTask,
    ssh: SSHClient,
    secrets: list[str],
    db: Session,
    log_phase: str,
) -> None:
    """策略 1：step3 开始前按 OS 预检降级。"""
    os_id, os_ver = _read_remote_os(ssh, secrets)
    effective, warn = resolve_php_version_for_os(task.php_version, os_id, os_ver)
    if effective != task.php_version:
        _apply_php_fallback(
            task,
            db,
            task.php_version,
            effective,
            warn or "当前系统不支持所选 PHP 版本",
            log_phase,
        )


def _apply_mysql_fallback(
    task: DeployTask,
    db: Session,
    from_version: str,
    to_version: str,
    reason: str,
    log_phase: str,
) -> None:
    if not task.mysql_version_requested:
        task.mysql_version_requested = from_version
    task.mysql_version = to_version
    db.commit()
    db.refresh(task)
    publish_log(
        task.id,
        log_phase,
        mysql_install_fallback_message(from_version, to_version, reason),
        db,
    )


def _maybe_downgrade_mysql_for_ram(
    task: DeployTask,
    ssh: SSHClient,
    secrets: list[str],
    db: Session,
    log_phase: str,
) -> None:
    """策略 1：step3_mysql 开始前按总内存预检降级。"""
    ram_mb = detect_ram_mb(ssh, secrets)
    effective, warn = resolve_mysql_version_for_ram(task.mysql_version, ram_mb)
    if effective == task.mysql_version or not warn:
        return
    if not task.mysql_version_requested:
        task.mysql_version_requested = task.mysql_version
    task.mysql_version = effective
    db.commit()
    db.refresh(task)
    publish_log(task.id, log_phase, warn, db)


def _install_mysql_with_fallback(
    task: DeployTask,
    ssh: SSHClient,
    secrets: list[str],
    db: Session,
    log_phase: str,
    step_status: StepStatus | None,
) -> None:
    """策略 2：MySQL 8.0 安装失败时降级到 5.7 再试一次。"""
    requested = task.mysql_version
    try:
        _install_baota_component(
            task, ssh, secrets, db, "mysql", task.mysql_version, log_phase, step_status
        )
    except RuntimeError as exc:
        if (
            can_auto_fallback_mysql(requested)
            and task.mysql_version == requested
            and MYSQL_FALLBACK_VERSION != requested
        ):
            _apply_mysql_fallback(
                task, db, requested, MYSQL_FALLBACK_VERSION, str(exc), log_phase
            )
            _install_baota_component(
                task,
                ssh,
                secrets,
                db,
                "mysql",
                MYSQL_FALLBACK_VERSION,
                log_phase,
                None,
            )
        else:
            raise


def _install_php_with_fallback(
    task: DeployTask,
    ssh: SSHClient,
    secrets: list[str],
    db: Session,
    log_phase: str,
    step_status: StepStatus | None,
) -> None:
    """策略 2：安装失败时降级到 PHP_FALLBACK_VERSION 再试一次。"""
    requested = task.php_version
    try:
        _install_baota_component(
            task, ssh, secrets, db, "php", task.php_version, log_phase, step_status
        )
    except RuntimeError as exc:
        if (
            can_auto_fallback_php(requested)
            and task.php_version == requested
            and PHP_FALLBACK_VERSION != requested
        ):
            _apply_php_fallback(task, db, requested, PHP_FALLBACK_VERSION, str(exc), log_phase)
            _install_baota_component(
                task, ssh, secrets, db, "php", PHP_FALLBACK_VERSION, log_phase, None
            )
        else:
            raise


def _install_baota_component(
    task: DeployTask,
    ssh: SSHClient,
    secrets: list[str],
    db: Session,
    name: str,
    version: str,
    log_phase: str,
    step_status: StepStatus | None = None,
) -> None:
    """使用宝塔官方 nginx.sh / mysql.sh / php.sh 安装（同 install_panel_quick.sh）。"""
    script_name, _ = BAOTA_COMPONENTS[name]
    ready = _ready_check(ssh, name, version, secrets)
    live_status = probe_component_status(ssh, name, version, secrets)
    if live_status == "in_progress":
        status = "in_progress"
    elif live_status == "complete" or step_status == "complete":
        status = "complete"
    else:
        status = "missing"

    panel_ready = True
    if name == "php":
        from app.services.baota_panel import list_panel_php_versions, resolve_panel_php_version

        available = list_panel_php_versions(ssh, secrets)
        panel_ready = resolve_panel_php_version(version, available) is not None

    if (status == "complete" or ready()) and panel_ready:
        publish_log(task.id, log_phase, f"{name} 已安装，跳过", db)
        return

    if (status == "complete" or ready()) and not panel_ready:
        publish_log(
            task.id,
            log_phase,
            f"{name} 二进制已存在但宝塔面板未识别，将重新执行安装 {version}...",
            db,
        )

    if status == "in_progress" or is_component_install_running(ssh, name, secrets):
        publish_log(
            task.id,
            log_phase,
            f"检测到 {name} 正在安装中，等待现有安装完成（不重复执行）...",
            db,
        )
        log_file = f"/tmp/qbw_install_{name}.log"
        try:
            ssh.tail_until_ready(
                log_file=log_file,
                poll_interval=20,
                max_wait=2400,
                ready_check=ready,
                secrets=secrets,
            )
        except TimeoutError:
            if ready():
                publish_log(task.id, log_phase, f"{name} 等待超时但检测到已就绪", db)
            else:
                log_file = f"/tmp/qbw_install_{name}.log"
                reason = _install_log_failure(ssh, log_file, secrets) or "安装等待超时且组件未就绪"
                _fail_component_install(task, db, log_phase, name, version, reason)
        if ready():
            publish_log(task.id, log_phase, f"{name} {version} 安装完成", db)
            return
        log_file = f"/tmp/qbw_install_{name}.log"
        reason = _install_log_failure(ssh, log_file, secrets) or "安装进程已结束但组件未就绪"
        _fail_component_install(task, db, log_phase, name, version, reason)

    download_url = _resolve_download_url(ssh, secrets)
    bash_path = _resolve_bash_path(ssh, secrets)
    publish_log(
        task.id,
        log_phase,
        f"正在通过宝塔官方脚本安装 {name} {version}（{script_name}）...",
        db,
    )
    log_file = f"/tmp/qbw_install_{name}.log"
    install_cmd = (
        f"cd {PANEL_INSTALL_DIR} && "
        f"wget -O {script_name} {download_url}/install/{bash_path}/{script_name} && "
        f"bash {script_name} install {version}"
    )
    try:
        ssh.run_background_tail(
            install_cmd,
            log_file=log_file,
            poll_interval=20,
            max_wait=3600,
            ready_check=ready,
            secrets=secrets,
        )
    except TimeoutError:
        if ready():
            publish_log(task.id, log_phase, f"{name} 安装超时但检测到已就绪", db)
        else:
            reason = _install_log_failure(ssh, log_file, secrets) or "安装超时且组件未就绪"
            _fail_component_install(task, db, log_phase, name, version, reason)

    if ready():
        publish_log(task.id, log_phase, f"{name} {version} 安装完成", db)
    else:
        reason = _install_log_failure(ssh, log_file, secrets) or "安装脚本已结束但组件未就绪"
        _fail_component_install(task, db, log_phase, name, version, reason)


def _require_panel_php(
    task: DeployTask,
    ssh: SSHClient,
    secrets: list[str],
    db: Session,
    log_phase: str,
) -> str:
    """确认 PHP 已在宝塔面板可用；不可用则抛出明确错误。"""
    from app.services.baota_panel import list_panel_php_versions, resolve_panel_php_version

    available = list_panel_php_versions(ssh, secrets)
    resolved = resolve_panel_php_version(task.php_version, available)
    if resolved:
        return resolved

    binary_ready = component_ready(ssh, "php", task.php_version, secrets)
    detail = f"面板可用版本: {', '.join(available) if available else '无'}"
    if binary_ready:
        detail += "（检测到 PHP 二进制但面板未识别，请尝试在面板首页修复或重新安装 PHP）"
    else:
        detail += "（PHP 二进制亦未就绪）"
    raise RuntimeError(
        f"PHP {task.php_version} 未在宝塔面板中就绪，无法创建网站。{detail}"
    )


def ensure_baota_php_version(
    task: DeployTask,
    ssh: SSHClient,
    secrets: list[str],
    db: Session,
    log_phase: str = "step7_site",
) -> str:
    """建站前确保 PHP 在宝塔面板可用；缺失时尝试补装一次。"""
    from app.services.baota_panel import list_panel_php_versions, resolve_panel_php_version

    available = list_panel_php_versions(ssh, secrets)
    resolved = resolve_panel_php_version(task.php_version, available)
    if resolved:
        return resolved

    publish_log(
        task.id,
        log_phase,
        f"宝塔面板未识别 PHP {task.php_version}（当前: {', '.join(available) if available else '无'}），正在补装...",
        db,
    )
    _ensure_panel_install_lib(task, ssh, secrets, db, log_phase)
    _install_php_with_fallback(task, ssh, secrets, db, log_phase, None)
    return _require_panel_php(task, ssh, secrets, db, log_phase)


def install_nginx(
    task: DeployTask,
    password: str,
    db: Session,
    remote_state: RemoteDeployState | None = None,
) -> dict:
    """安装 Nginx（Web 服务器，PHP 运行前置依赖）。"""
    publish_log(
        task.id,
        "step2_nginx",
        f"开始安装 Nginx {task.nginx_version}...",
        db,
    )
    secrets = [password, task.bt_password]
    log_phase = "step2_nginx"

    with _ssh(task, password, log_phase, db) as ssh:
        _ensure_panel_install_lib(task, ssh, secrets, db, log_phase)
        nginx_status = remote_state.nginx if remote_state else None
        _install_baota_component(
            task, ssh, secrets, db, "nginx", task.nginx_version, log_phase, nginx_status
        )
        ssh.run("/etc/init.d/nginx start 2>/dev/null; true", secrets=secrets)
        publish_log(task.id, log_phase, f"Nginx {task.nginx_version} 安装完成", db)

    return {"nginx": task.nginx_version}


def install_php_stack(
    task: DeployTask,
    password: str,
    db: Session,
    remote_state: RemoteDeployState | None = None,
) -> dict:
    """安装 PHP（需在 Nginx 就绪后执行）。"""
    publish_log(
        task.id,
        "step3_php",
        f"开始安装 PHP {task.php_version}...",
        db,
    )
    secrets = [password, task.bt_password]
    log_phase = "step3_php"

    with _ssh(task, password, log_phase, db) as ssh:
        _maybe_downgrade_php_for_os(task, ssh, secrets, db, log_phase)

        _ensure_panel_install_lib(task, ssh, secrets, db, log_phase)
        php_status = remote_state.php if remote_state else None
        _install_php_with_fallback(
            task, ssh, secrets, db, log_phase, php_status
        )
        panel_php = _require_panel_php(task, ssh, secrets, db, log_phase)
        publish_log(
            task.id,
            log_phase,
            f"PHP 环境安装完成（面板版本 {panel_php}）",
            db,
        )

    return {"php": task.php_version}


def install_mysql(
    task: DeployTask,
    password: str,
    db: Session,
    remote_state: RemoteDeployState | None = None,
) -> dict:
    publish_log(task.id, "step3_mysql", f"开始安装 MySQL {task.mysql_version}...", db)
    secrets = [password, task.bt_password]
    log_phase = "step3_mysql"
    mysql_status = remote_state.mysql if remote_state else None

    with _ssh(task, password, log_phase, db) as ssh:
        _ensure_panel_install_lib(task, ssh, secrets, db, log_phase)
        _maybe_downgrade_mysql_for_ram(task, ssh, secrets, db, log_phase)
        _install_mysql_with_fallback(
            task, ssh, secrets, db, log_phase, mysql_status
        )
        ssh.run(
            "/etc/init.d/mysqld start 2>/dev/null || /etc/init.d/mysql start 2>/dev/null; true",
            secrets=secrets,
        )
        publish_log(task.id, log_phase, "MySQL 安装完成", db)

    return {"mysql": task.mysql_version}


def install_lnmp(task: DeployTask, password: str, db: Session) -> dict:
    """兼容旧调用：依次安装 Nginx、PHP 与 MySQL。"""
    nginx_info = install_nginx(task, password, db)
    php_info = install_php_stack(task, password, db)
    mysql_info = install_mysql(task, password, db)
    return {**nginx_info, **php_info, **mysql_info}
