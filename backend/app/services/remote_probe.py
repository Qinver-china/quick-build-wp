"""通过 SSH 探测目标服务器各部署步骤的完成/进行中/未安装状态。"""

from __future__ import annotations

import shlex

from sqlalchemy.orm import Session

from app.models.deploy import DeployPhase, DeployTask
from app.services.log_publisher import publish_log
from app.services.remote_state import STATUS_LABELS, RemoteDeployState, StepStatus
from app.services.ssh import SSHClient
from app.services.wordpress import _domain_dir_name


def _php_ver_short(version: str) -> str:
    return version.replace(".", "")


def php_binary_paths(php_version: str) -> list[str]:
    short = _php_ver_short(php_version)
    version = php_version.strip()
    paths = [
        f"/www/server/php/{short}/bin/php",
        f"/www/server/php/{version}/bin/php",
    ]
    # 去重并保持顺序
    seen: set[str] = set()
    ordered: list[str] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            ordered.append(path)
    return ordered


def _discover_php_binary_script(wanted: str) -> str:
    want = shlex.quote(wanted.strip())
    return f"""set +e
WANT={want}
WANT_SHORT="${{WANT//./}}"
for bin in /www/server/php/*/bin/php; do
  [ -e "$bin" ] || continue
  ver=$("$bin" -n -r 'echo PHP_MAJOR_VERSION.".".PHP_MINOR_VERSION;' 2>/dev/null)
  if [ -z "$ver" ]; then
    ver=$("$bin" -r 'echo PHP_MAJOR_VERSION.".".PHP_MINOR_VERSION;' 2>/dev/null)
  fi
  if [ "$ver" = "$WANT" ]; then
    echo "PHP_BIN=$bin"
    exit 0
  fi
done
for base in "/www/server/php/$WANT_SHORT/bin/php" "/www/server/php/$WANT/bin/php"; do
  if [ -e "$base" ]; then
    echo "PHP_BIN=$base"
    exit 0
  fi
done
exit 1
"""


def resolve_php_binary(ssh: SSHClient, php_version: str, secrets: list[str]) -> str | None:
    """解析与目标版本匹配的 PHP CLI 路径（兼容宝塔目录 84 / 8.4 等）。"""
    for path in php_binary_paths(php_version):
        q = shlex.quote(path)
        code, out, _ = ssh.run(
            f"if [ ! -e {q} ]; then exit 1; fi; "
            f"ver=$({q} -n -r 'echo PHP_MAJOR_VERSION.\".\".PHP_MINOR_VERSION;' 2>/dev/null || true); "
            f"if [ -z \"$ver\" ]; then "
            f"ver=$({q} -r 'echo PHP_MAJOR_VERSION.\".\".PHP_MINOR_VERSION;' 2>/dev/null || true); "
            f"fi; "
            f"if [ \"$ver\" = {shlex.quote(php_version.strip())} ]; then echo OK; "
            f"elif [ -e {q} ]; then echo OK; fi",
            timeout=25,
            secrets=secrets,
        )
        if code == 0 and "OK" in out:
            return path

    code, out, _ = ssh.run_script(
        _discover_php_binary_script(php_version),
        timeout=45,
        secrets=secrets,
    )
    if code == 0:
        for line in out.splitlines():
            if line.startswith("PHP_BIN="):
                path = line.split("=", 1)[1].strip()
                if path:
                    return path
    return None


def is_php_binary_ready(ssh: SSHClient, php_version: str, secrets: list[str]) -> bool:
    """检测宝塔 PHP 可执行文件是否已就绪（安装日志完成与二进制可用之间可能有延迟）。"""
    return resolve_php_binary(ssh, php_version, secrets) is not None


def _status(ready: bool, in_progress: bool) -> StepStatus:
    if ready:
        return "complete"
    if in_progress:
        return "in_progress"
    return "missing"


def probe_component_status(
    ssh: SSHClient,
    component: str,
    version: str,
    secrets: list[str],
) -> StepStatus:
    return _status(
        component_ready(ssh, component, version, secrets),
        is_soft_install_running(ssh, component, secrets),
    )


def component_ready(ssh: SSHClient, component: str, version: str, secrets: list[str]) -> bool:
    if component == "nginx":
        code, out, _ = ssh.run("test -x /www/server/nginx/sbin/nginx && echo OK", secrets=secrets)
        return code == 0 and "OK" in out
    if component == "php":
        return is_php_binary_ready(ssh, version, secrets)
    if component == "mysql":
        code, out, _ = ssh.run(
            "test -x /www/server/mysql/bin/mysql && echo OK",
            timeout=15,
            secrets=secrets,
        )
        return code == 0 and "OK" in out
    return False


def is_soft_install_running(ssh: SSHClient, name: str, secrets: list[str]) -> bool:
    return is_component_install_running(ssh, name, secrets)


def is_component_install_running(ssh: SSHClient, name: str, secrets: list[str]) -> bool:
    """检测宝塔官方组件脚本或 install_soft.sh 是否正在安装。"""
    script = f"{name}.sh"
    code, out, _ = ssh.run(
        f"pgrep -af '{script} install|install_soft.sh.*install {name}' "
        f"2>/dev/null | grep -v pgrep | head -1",
        timeout=15,
        secrets=secrets,
    )
    return bool(out.strip())


def is_baota_panel_ready(ssh: SSHClient, secrets: list[str]) -> bool:
    code, out, _ = ssh.run(
        "test -f /www/server/panel/data/admin_path.pl && echo OK",
        timeout=15,
        secrets=secrets,
    )
    return code == 0 and "OK" in out


def is_baota_install_running(ssh: SSHClient, secrets: list[str]) -> bool:
    code, out, _ = ssh.run(
        r"pgrep -af 'install_panel\.sh|install-ubuntu_6\.0\.sh|install_6\.0\.sh' "
        r"2>/dev/null | grep -v pgrep | head -1",
        timeout=15,
        secrets=secrets,
    )
    return bool(out.strip())


def find_baota_install_log(ssh: SSHClient, secrets: list[str]) -> str:
    _, out, _ = ssh.run(
        "ls -t /tmp/qbw_install_panel_*.log 2>/dev/null | head -1",
        timeout=15,
        secrets=secrets,
    )
    path = out.strip()
    return path or "/tmp/qbw_install_panel_0.log"


REDIS_PING_TIMEOUT_SEC = 5
REDIS_CONNECT_TIMEOUT_SEC = 2
# 强制 127.0.0.1 避免 IPv6 [::1] 挂起；外层 timeout 限制整命令时长
REDIS_PING_CMD = (
    f"timeout {REDIS_PING_TIMEOUT_SEC} redis-cli -h 127.0.0.1 "
    f"--connect-timeout {REDIS_CONNECT_TIMEOUT_SEC} ping 2>/dev/null"
)
REDIS_PING_SSH_TIMEOUT = REDIS_PING_TIMEOUT_SEC + 3


def is_redis_server_ready(ssh: SSHClient, secrets: list[str]) -> bool:
    code, out, _ = ssh.run(
        REDIS_PING_CMD,
        timeout=REDIS_PING_SSH_TIMEOUT,
        secrets=secrets,
        quiet=True,
    )
    return code == 0 and "PONG" in out.upper()


def _php_module_list(ssh: SSHClient, task: DeployTask, secrets: list[str]) -> str:
    php_bin = resolve_php_binary(ssh, task.php_version, secrets)
    if not php_bin:
        return ""
    _, out, _ = ssh.run(
        f"{shlex.quote(php_bin)} -m 2>/dev/null",
        timeout=15,
        secrets=secrets,
    )
    return out or ""


def is_php_redis_extension_ready(ssh: SSHClient, task: DeployTask, secrets: list[str]) -> bool:
    modules = _php_module_list(ssh, task, secrets).lower()
    return "redis" in {line.strip() for line in modules.splitlines()}


def is_php_opcache_extension_ready(ssh: SSHClient, task: DeployTask, secrets: list[str]) -> bool:
    modules = _php_module_list(ssh, task, secrets).lower()
    return any("opcache" in line for line in modules.splitlines())


def is_php_mbstring_extension_ready(ssh: SSHClient, task: DeployTask, secrets: list[str]) -> bool:
    modules = _php_module_list(ssh, task, secrets).lower()
    return "mbstring" in {line.strip() for line in modules.splitlines()}


def php_extensions_step_marker(task: DeployTask) -> str:
    """本任务 PHP 扩展步骤完成标记（扩展未全部加载也视为步骤已执行）。"""
    safe_id = (task.id or "adhoc").replace("/", "_")
    return f"/tmp/qbw_php_ext_step_{safe_id}.done"


def is_php_extensions_step_done(ssh: SSHClient, task: DeployTask, secrets: list[str]) -> bool:
    marker = php_extensions_step_marker(task)
    code, out, _ = ssh.run(
        f"test -f {shlex.quote(marker)} && echo OK",
        timeout=15,
        secrets=secrets,
    )
    return code == 0 and "OK" in out


def mark_php_extensions_step_done(ssh: SSHClient, task: DeployTask, secrets: list[str]) -> None:
    marker = php_extensions_step_marker(task)
    ssh.run(f"touch {shlex.quote(marker)}", timeout=15, secrets=secrets)


def is_php_extensions_ready(ssh: SSHClient, task: DeployTask, secrets: list[str]) -> bool:
    """mbstring / redis / opcache 是否均已加载（仅作状态展示，不用于阻塞后续步骤）。"""
    return (
        is_php_mbstring_extension_ready(ssh, task, secrets)
        and is_php_redis_extension_ready(ssh, task, secrets)
        and is_php_opcache_extension_ready(ssh, task, secrets)
    )


def is_optimize_done(ssh: SSHClient, secrets: list[str]) -> bool:
    code, out, _ = ssh.run(
        "test -f /etc/my.cnf.d/qbw-optimize.cnf && echo OK",
        timeout=15,
        secrets=secrets,
    )
    return code == 0 and "OK" in out


def site_paths(task: DeployTask) -> tuple[str, str]:
    site_dir_name = _domain_dir_name(task.site_domain.strip().lower())
    site_path = f"/www/wwwroot/{site_dir_name}"
    conf_path = f"/www/server/panel/vhost/nginx/{site_dir_name}.conf"
    return site_path, conf_path


def probe_remote_state(task: DeployTask, password: str, db: Session | None = None) -> RemoteDeployState:
    secrets = [password, task.bt_password]
    site_path, conf_path = site_paths(task)

    with SSHClient(
        host=task.ssh_host,
        port=task.ssh_port,
        username=task.ssh_user,
        password=password,
        task_id=task.id,
        log_phase="system",
        db=db,
    ) as ssh:
        baota_ready = is_baota_panel_ready(ssh, secrets)
        baota_running = is_baota_install_running(ssh, secrets)

        nginx_ready = component_ready(ssh, "nginx", task.nginx_version, secrets)
        php_ready = component_ready(ssh, "php", task.php_version, secrets)
        mysql_ready = component_ready(ssh, "mysql", task.mysql_version, secrets)

        _, site_out, _ = ssh.run(
            f"test -d {site_path} && test -f {conf_path} && echo OK",
            timeout=15,
            secrets=secrets,
        )
        site_ready = "OK" in site_out

        _, wp_out, _ = ssh.run(
            f"test -f {site_path}/wp-config.php && test -f {site_path}/wp-includes/version.php && echo OK",
            timeout=15,
            secrets=secrets,
        )
        wp_ready = "OK" in wp_out

        return RemoteDeployState(
            baota=_status(baota_ready, baota_running),
            nginx=_status(nginx_ready, is_soft_install_running(ssh, "nginx", secrets)),
            php=_status(php_ready, is_soft_install_running(ssh, "php", secrets)),
            mysql=_status(mysql_ready, is_soft_install_running(ssh, "mysql", secrets)),
            redis_server=_status(is_redis_server_ready(ssh, secrets), False),
            php_extensions=_status(is_php_extensions_step_done(ssh, task, secrets), False),
            optimize=_status(is_optimize_done(ssh, secrets), False),
            site_prepared=_status(site_ready, False),
            wordpress=_status(wp_ready, False),
        )


def log_remote_state(task: DeployTask, state: RemoteDeployState, db: Session) -> None:
    publish_log(task.id, "system", "远程状态探测结果：", db)
    rows = [
        ("宝塔面板", state.baota),
        (f"Nginx {task.nginx_version}", state.nginx),
        (f"PHP {task.php_version}", state.php),
        (f"MySQL {task.mysql_version}", state.mysql),
        ("Redis 服务", state.redis_server),
        ("PHP 扩展（mbstring/Redis/Opcache）", state.php_extensions),
        ("环境参数调优", state.optimize),
        ("站点与数据库", state.site_prepared),
        ("WordPress", state.wordpress),
    ]
    for label, status in rows:
        publish_log(
            task.id,
            "system",
            f"  · {label}: {STATUS_LABELS.get(status, status)}",
            db,
        )
    phase = state.resume_phase()
    phase_labels = {
        DeployPhase.STEP1_BAOTA: "安装宝塔",
        DeployPhase.STEP2_NGINX: "安装 Nginx",
        DeployPhase.STEP3_PHP: "安装 PHP",
        DeployPhase.STEP2_PHP: "安装 PHP",
        DeployPhase.STEP3_MYSQL: "安装 MySQL",
        DeployPhase.STEP4_REDIS: "安装 Redis",
        DeployPhase.STEP5_PHP_EXT: "安装 PHP 组件与扩展",
        DeployPhase.STEP6_OPTIMIZE: "参数调优",
        DeployPhase.STEP7_SITE: "创建网站并安装 WordPress",
    }
    if phase != DeployPhase.DONE:
        publish_log(
            task.id,
            "system",
            f"将从步骤「{phase_labels.get(phase, phase.value)}」继续执行",
            db,
        )
    else:
        publish_log(task.id, "system", "探测到所有步骤均已完成，将执行最终验证", db)
