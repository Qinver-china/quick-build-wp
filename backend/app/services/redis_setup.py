"""安装宝塔 Redis 服务、PHP Redis / Opcache 扩展。"""

from __future__ import annotations

import shlex
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.models.deploy import DeployTask
from app.services.log_publisher import publish_log
from app.services.server_memory import (
    REDIS_SKIP_MESSAGE,
    detect_ram_mb,
    redis_maxmemory_mb,
    should_install_redis,
)
from app.services.remote_probe import (
    is_php_extensions_step_done,
    is_php_mbstring_extension_ready,
    is_php_opcache_extension_ready,
    is_php_redis_extension_ready,
    is_redis_server_ready,
    is_soft_install_running,
    mark_php_extensions_step_done,
    REDIS_PING_CMD,
    resolve_php_binary,
)
from app.services.remote_state import RemoteDeployState
from app.services.server_optimize import php_ver_short
from app.services.ssh import SSHClient

SOFT_SH = "/www/server/panel/install/install_soft.sh"
REDIS_VERSION = "7.0"
EXT_INSTALL_MAX_WAIT = 1200
BASE_PHP_EXTENSIONS = ("mbstring", "opcache", "fileinfo")
REDIS_PHP_EXTENSION = "redis"
PHP_BINARY_WAIT_SECONDS = 180
PHP_BINARY_POLL_SECONDS = 10


def _wait_for_php_binary(
    task: DeployTask,
    ssh: SSHClient,
    secrets: list[str],
    db: Session,
    log_phase: str,
) -> bool:
    php_bin = resolve_php_binary(ssh, task.php_version, secrets)
    if php_bin:
        return True

    publish_log(
        task.id,
        log_phase,
        f"PHP {task.php_version} 二进制尚未就绪，等待安装收尾（最多 {PHP_BINARY_WAIT_SECONDS} 秒）...",
        db,
    )
    waited = 0
    while waited < PHP_BINARY_WAIT_SECONDS:
        ssh.run(f"sleep {PHP_BINARY_POLL_SECONDS}", timeout=PHP_BINARY_POLL_SECONDS + 5, secrets=secrets, quiet=True)
        waited += PHP_BINARY_POLL_SECONDS
        php_bin = resolve_php_binary(ssh, task.php_version, secrets)
        if php_bin:
            publish_log(
                task.id,
                log_phase,
                f"PHP {task.php_version} 已就绪（{php_bin}）",
                db,
            )
            return True

    php_bin = resolve_php_binary(ssh, task.php_version, secrets)
    if php_bin:
        return True

    _, diag, _ = ssh.run(
        "ls -d /www/server/php/*/bin/php 2>/dev/null | head -5",
        timeout=20,
        secrets=secrets,
    )
    if diag.strip():
        publish_log(
            task.id,
            log_phase,
            f"警告: 检测到 PHP 路径 {diag.strip().splitlines()[0]}，但版本自检未通过，仍将尝试安装扩展",
            db,
        )
        return True

    return False


def _extension_status(ssh: SSHClient, task: DeployTask, secrets: list[str]) -> dict:
    return {
        "mbstring_extension": is_php_mbstring_extension_ready(ssh, task, secrets),
        "redis_php_extension": is_php_redis_extension_ready(ssh, task, secrets),
        "opcache_enabled": is_php_opcache_extension_ready(ssh, task, secrets),
    }


def _redis_skip_result(ram_mb: int) -> dict:
    return {
        "redis_server": False,
        "redis_host": "127.0.0.1",
        "redis_port": 6379,
        "redis_skipped_low_memory": True,
        "ram_mb": ram_mb,
        "redis_php_extension": False,
    }


def _redis_maxmemory_script(maxmemory_mb: int) -> str:
    return f"""set -e
CONF="/www/server/redis/redis.conf"
if [ ! -f "$CONF" ]; then
  echo "[redis] 未找到 redis.conf，跳过 maxmemory 配置"
  exit 0
fi
if grep -q '^maxmemory ' "$CONF"; then
  sed -i 's/^maxmemory .*/maxmemory {maxmemory_mb}mb/' "$CONF"
else
  echo "maxmemory {maxmemory_mb}mb" >> "$CONF"
fi
grep -q '^maxmemory-policy ' "$CONF" || echo "maxmemory-policy allkeys-lru" >> "$CONF"
/etc/init.d/redis restart 2>/dev/null || /etc/init.d/redis start 2>/dev/null || true
{REDIS_PING_CMD} && echo "[redis] maxmemory 已设为 {maxmemory_mb}MB"
"""


def _apply_redis_maxmemory(
    task: DeployTask,
    ssh: SSHClient,
    secrets: list[str],
    db: Session,
    ram_mb: int,
    log_phase: str,
) -> None:
    max_mb = redis_maxmemory_mb(ram_mb)
    publish_log(
        task.id,
        log_phase,
        f"正在将 Redis maxmemory 设置为 {max_mb}MB（约为总内存 {ram_mb}MB 的 40%）...",
        db,
    )
    code, out, err = ssh.run_script(_redis_maxmemory_script(max_mb), timeout=120, secrets=secrets)
    if code == 0 and "maxmemory 已设为" in out:
        publish_log(task.id, log_phase, f"Redis maxmemory 已设为 {max_mb}MB", db)
    else:
        publish_log(
            task.id,
            log_phase,
            f"警告: Redis maxmemory 配置可能未生效: {(err or out).strip()[:200]}",
            db,
        )


def _redis_server_script() -> str:
    return f"""set -e
SOFT="{SOFT_SH}"
REDIS_VER="{REDIS_VERSION}"

echo "[redis] 安装宝塔 Redis 程序..."
if {REDIS_PING_CMD} | grep -q PONG; then
  echo "[redis] Redis 服务已在运行"
elif [ -x /www/server/redis/src/redis-server ]; then
  /etc/init.d/redis start 2>/dev/null || true
  echo "[redis] Redis 已安装，已尝试启动"
elif [ -f "$SOFT" ]; then
  bash "$SOFT" 1 install redis ${{REDIS_VER}} 2>/dev/null || bash "$SOFT" 1 install redis 2>/dev/null || true
  /etc/init.d/redis start 2>/dev/null || true
else
  echo "[redis] 警告: 未找到宝塔安装脚本"
fi

if [ -f /www/server/redis/redis.conf ]; then
  grep -q '^bind ' /www/server/redis/redis.conf || echo 'bind 127.0.0.1' >> /www/server/redis/redis.conf
  sed -i 's/^#\\?bind .*/bind 127.0.0.1/' /www/server/redis/redis.conf 2>/dev/null || true
fi
/etc/init.d/redis restart 2>/dev/null || /etc/init.d/redis start 2>/dev/null || true
{REDIS_PING_CMD} && echo "[redis] Redis 服务: 运行中" || echo "[redis] Redis 服务: 未响应"
"""


def _php_root(task: DeployTask) -> str:
    return f"/www/server/php/{php_ver_short(task.php_version)}"


def _ext_ready_check(
    ssh: SSHClient, task: DeployTask, ext_name: str, secrets: list[str]
) -> Callable[[], bool]:
    checks = {
        "mbstring": lambda: is_php_mbstring_extension_ready(ssh, task, secrets),
        "redis": lambda: is_php_redis_extension_ready(ssh, task, secrets),
        "opcache": lambda: is_php_opcache_extension_ready(ssh, task, secrets),
    }

    def check() -> bool:
        if ext_name in checks:
            return checks[ext_name]()
        php_bin = resolve_php_binary(ssh, task.php_version, secrets)
        if not php_bin:
            return False
        _, out, _ = ssh.run(
            f"{shlex.quote(php_bin)} -m 2>/dev/null | grep -i {ext_name}",
            timeout=15,
            secrets=secrets,
        )
        return bool(out.strip())

    return check


def _wait_or_install_extension(
    task: DeployTask,
    ssh: SSHClient,
    secrets: list[str],
    db: Session,
    ext_name: str,
    log_phase: str,
) -> bool:
    """安装单个 PHP 扩展；超时或未就绪时返回 False，不抛异常。"""
    php_short = php_ver_short(task.php_version)
    ready = _ext_ready_check(ssh, task, ext_name, secrets)

    if ready():
        publish_log(task.id, log_phase, f"PHP 扩展 {ext_name} 已加载，跳过安装", db)
        return True

    log_file = f"/tmp/qbw_install_phpext_{ext_name}.log"

    if is_soft_install_running(ssh, ext_name, secrets):
        publish_log(
            task.id,
            log_phase,
            f"检测到 {ext_name} 扩展正在安装中，等待完成（日志实时输出）...",
            db,
        )
        try:
            ssh.tail_until_ready(
                log_file=log_file,
                poll_interval=15,
                max_wait=EXT_INSTALL_MAX_WAIT,
                ready_check=ready,
                secrets=secrets,
            )
        except TimeoutError:
            if ready():
                return True
            publish_log(
                task.id,
                log_phase,
                f"警告: 等待 {ext_name} 扩展安装超时，将尝试继续后续步骤",
                db,
            )
        if ready():
            publish_log(task.id, log_phase, f"PHP 扩展 {ext_name} 安装完成", db)
            return True

    publish_log(task.id, log_phase, f"正在安装 PHP 扩展 {ext_name}（PHP {task.php_version}）...", db)
    cmd = f"bash {SOFT_SH} 1 install {ext_name} {php_short}"
    try:
        ssh.run_background_tail(
            cmd,
            log_file=log_file,
            poll_interval=15,
            max_wait=EXT_INSTALL_MAX_WAIT,
            ready_check=ready,
            secrets=secrets,
        )
    except TimeoutError:
        if not ready():
            publish_log(
                task.id,
                log_phase,
                f"警告: {ext_name} 扩展安装超时，尝试备用脚本...",
                db,
            )
            fallback = f"/www/server/panel/install/{ext_name}.sh"
            fb_log = f"/tmp/qbw_install_phpext_{ext_name}_fb.log"
            fb_cmd = f"test -f {fallback} && bash {fallback} install {php_short}"
            try:
                ssh.run_background_tail(
                    fb_cmd,
                    log_file=fb_log,
                    poll_interval=15,
                    max_wait=600,
                    ready_check=ready,
                    secrets=secrets,
                )
            except TimeoutError:
                pass

    if ready():
        publish_log(task.id, log_phase, f"PHP 扩展 {ext_name} 安装完成", db)
        return True
    return False


def _enable_opcache_ini(task: DeployTask, ssh: SSHClient, secrets: list[str]) -> None:
    php_root = _php_root(task)
    script = f"""set -e
PHP_INI="{php_root}/etc/php.ini"
if [ ! -f "$PHP_INI" ]; then exit 0; fi
sed -i 's/^;\\?opcache.enable.*/opcache.enable=1/' "$PHP_INI"
sed -i 's/^;\\?opcache.enable_cli.*/opcache.enable_cli=1/' "$PHP_INI"
if ! grep -q '^opcache.enable' "$PHP_INI"; then
  cat >> "$PHP_INI" <<'OPCACHE'

[opcache]
opcache.enable=1
opcache.enable_cli=1
opcache.memory_consumption=128
opcache.max_accelerated_files=10000
opcache.revalidate_freq=60
OPCACHE
fi
echo "[php-ext] php.ini Opcache 配置已更新"
"""
    ssh.run_script(script, timeout=60, secrets=secrets)


def _restart_php_fpm(task: DeployTask, ssh: SSHClient, secrets: list[str]) -> None:
    php_short = php_ver_short(task.php_version)
    ssh.run(
        f"/etc/init.d/php-fpm-{php_short} restart 2>/dev/null "
        f"|| /etc/init.d/php-fpm restart 2>/dev/null || true",
        timeout=60,
        secrets=secrets,
    )


def install_redis_server(
    task: DeployTask,
    password: str,
    db: Session,
    remote_state: RemoteDeployState | None = None,
) -> dict:
    publish_log(task.id, "step4_redis", "开始安装 Redis 服务...", db)
    secrets = [password, task.bt_password]
    log_phase = "step4_redis"

    with SSHClient(
        host=task.ssh_host,
        port=task.ssh_port,
        username=task.ssh_user,
        password=password,
        task_id=task.id,
        log_phase=log_phase,
        db=db,
    ) as ssh:
        ram_mb = detect_ram_mb(ssh, secrets)
        if not should_install_redis(ram_mb):
            publish_log(task.id, log_phase, REDIS_SKIP_MESSAGE, db)
            return _redis_skip_result(ram_mb)

        if remote_state and remote_state.redis_server == "complete":
            publish_log(task.id, log_phase, "Redis 服务已运行，跳过安装", db)
            _apply_redis_maxmemory(task, ssh, secrets, db, ram_mb, log_phase)
            return {
                "redis_server": True,
                "redis_host": "127.0.0.1",
                "redis_port": 6379,
                "ram_mb": ram_mb,
            }
        if is_redis_server_ready(ssh, secrets):
            publish_log(task.id, log_phase, "Redis 服务已运行，跳过安装", db)
            _apply_redis_maxmemory(task, ssh, secrets, db, ram_mb, log_phase)
            return {
                "redis_server": True,
                "redis_host": "127.0.0.1",
                "redis_port": 6379,
                "ram_mb": ram_mb,
            }

        code, out, err = ssh.run_script(_redis_server_script(), timeout=1800, secrets=secrets)
        if code != 0:
            raise RuntimeError(f"Redis 安装失败: {err or out}")

        server_ok = "Redis 服务: 运行中" in out
        if server_ok:
            publish_log(task.id, log_phase, "Redis 服务已安装并运行", db)
            _apply_redis_maxmemory(task, ssh, secrets, db, ram_mb, log_phase)
        else:
            publish_log(task.id, log_phase, "警告: Redis 服务未确认运行，请稍后在宝塔面板检查", db)

        return {
            "redis_server": server_ok,
            "redis_host": "127.0.0.1",
            "redis_port": 6379,
            "ram_mb": ram_mb,
        }


def install_php_extensions(
    task: DeployTask,
    password: str,
    db: Session,
    remote_state: RemoteDeployState | None = None,
) -> dict:
    publish_log(
        task.id,
        "step5_php_ext",
        "开始安装 PHP 组件与扩展（mbstring、Redis、Opcache）...",
        db,
    )
    secrets = [password, task.bt_password]
    log_phase = "step5_php_ext"

    with SSHClient(
        host=task.ssh_host,
        port=task.ssh_port,
        username=task.ssh_user,
        password=password,
        task_id=task.id,
        log_phase=log_phase,
        db=db,
    ) as ssh:
        if remote_state and remote_state.php_extensions == "complete":
            publish_log(task.id, log_phase, "PHP 组件步骤已执行，跳过安装", db)
            return _extension_status(ssh, task, secrets)
        if is_php_extensions_step_done(ssh, task, secrets):
            publish_log(task.id, log_phase, "PHP 组件步骤已执行，跳过安装", db)
            return _extension_status(ssh, task, secrets)

        ram_mb = detect_ram_mb(ssh, secrets)
        install_redis_ext = should_install_redis(ram_mb)
        if not install_redis_ext:
            publish_log(task.id, log_phase, REDIS_SKIP_MESSAGE, db)

        if not _wait_for_php_binary(task, ssh, secrets, db, log_phase):
            raise RuntimeError(
                f"PHP {task.php_version} 未安装或未就绪，无法继续安装扩展。"
                f"请检查 step3 安装日志，或在宝塔面板手动安装 PHP {task.php_version}"
            )

        extensions = list(BASE_PHP_EXTENSIONS)
        if install_redis_ext:
            extensions.insert(1, REDIS_PHP_EXTENSION)

        for ext_name in extensions:
            _wait_or_install_extension(task, ssh, secrets, db, ext_name, log_phase)

        publish_log(task.id, log_phase, "正在更新 Opcache 配置并重启 PHP-FPM...", db)
        _enable_opcache_ini(task, ssh, secrets)
        _restart_php_fpm(task, ssh, secrets)

        mbstring_ok = is_php_mbstring_extension_ready(ssh, task, secrets)
        redis_ok = is_php_redis_extension_ready(ssh, task, secrets)
        opcache_ok = is_php_opcache_extension_ready(ssh, task, secrets)

        if mbstring_ok:
            publish_log(task.id, log_phase, "PHP mbstring 扩展已加载", db)
        else:
            publish_log(
                task.id,
                log_phase,
                "警告: PHP mbstring 扩展未安装成功，WordPress 可能无法正常运行，请在宝塔面板手动安装",
                db,
            )
        if install_redis_ext:
            if redis_ok:
                publish_log(task.id, log_phase, "PHP Redis 扩展已加载", db)
            else:
                publish_log(
                    task.id,
                    log_phase,
                    "警告: PHP Redis 扩展未安装成功，可稍后在宝塔面板手动安装，不影响网站基本使用",
                    db,
                )
        if opcache_ok:
            publish_log(task.id, log_phase, "PHP Opcache 扩展已加载", db)
        else:
            publish_log(
                task.id,
                log_phase,
                "警告: PHP Opcache 扩展未安装成功，可稍后在宝塔面板手动安装，不影响网站基本使用",
                db,
            )

        mark_php_extensions_step_done(ssh, task, secrets)
        publish_log(task.id, log_phase, "PHP 组件与扩展步骤完成，继续后续部署", db)
        return {
            "mbstring_extension": mbstring_ok,
            "redis_php_extension": redis_ok if install_redis_ext else False,
            "opcache_enabled": opcache_ok,
            "redis_skipped_low_memory": not install_redis_ext,
            "ram_mb": ram_mb,
        }


def install_redis_stack(task: DeployTask, password: str, db: Session) -> dict:
    """兼容旧调用。"""
    server = install_redis_server(task, password, db)
    ext = install_php_extensions(task, password, db)
    return {**server, **ext}
