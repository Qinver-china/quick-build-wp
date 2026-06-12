"""根据服务器配置自动优化 PHP / PHP-FPM / MySQL。"""

from __future__ import annotations

import math
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.deploy import DeployTask
from app.services.log_publisher import publish_log
from app.services.remote_probe import is_optimize_done
from app.services.remote_state import RemoteDeployState
from app.services.ssh import SSHClient

DETECT_SCRIPT = r"""
echo "RAM_MB=$(free -m | awk '/^Mem:/{print $2}')"
echo "CPU_CORES=$(nproc 2>/dev/null || echo 1)"
"""


@dataclass
class OptimizeProfile:
    ram_mb: int
    cpu_cores: int
    memory_limit: str
    upload_max_filesize: str
    post_max_size: str
    max_execution_time: str
    max_input_vars: str
    pm_max_children: int
    pm_start_servers: int
    pm_min_spare_servers: int
    pm_max_spare_servers: int
    opcache_memory: int
    innodb_buffer_pool_size: str
    max_connections: int

    def to_dict(self) -> dict:
        return {
            "ram_mb": self.ram_mb,
            "cpu_cores": self.cpu_cores,
            "memory_limit": self.memory_limit,
            "upload_max_filesize": self.upload_max_filesize,
            "post_max_size": self.post_max_size,
            "max_execution_time": self.max_execution_time,
            "max_input_vars": self.max_input_vars,
            "php_fpm": {
                "pm.max_children": self.pm_max_children,
                "pm.start_servers": self.pm_start_servers,
                "pm.min_spare_servers": self.pm_min_spare_servers,
                "pm.max_spare_servers": self.pm_max_spare_servers,
            },
            "opcache_memory_mb": self.opcache_memory,
            "innodb_buffer_pool_size": self.innodb_buffer_pool_size,
            "max_connections": self.max_connections,
        }


def php_ver_short(version: str) -> str:
    return version.replace(".", "")


def compute_profile(ram_mb: int, cpu_cores: int) -> OptimizeProfile:
    cpu_cores = max(1, cpu_cores)
    ram_mb = max(512, ram_mb)

    if ram_mb < 1024:
        memory_limit = "256M"
        innodb_mb = max(128, ram_mb // 4)
        max_children = max(5, min(12, ram_mb // 80))
    elif ram_mb < 2048:
        memory_limit = "256M"
        innodb_mb = max(256, ram_mb // 4)
        max_children = max(8, min(20, ram_mb // 64))
    elif ram_mb < 4096:
        memory_limit = "512M"
        innodb_mb = max(512, ram_mb // 3)
        max_children = max(15, min(35, ram_mb // 64))
    elif ram_mb < 8192:
        memory_limit = "512M"
        innodb_mb = max(1024, ram_mb // 3)
        max_children = max(20, min(50, ram_mb // 64))
    else:
        memory_limit = "512M"
        innodb_mb = min(4096, ram_mb // 2)
        max_children = max(30, min(80, ram_mb // 64))

    max_children = min(max_children, cpu_cores * 10)
    max_children = max(5, max_children)

    return OptimizeProfile(
        ram_mb=ram_mb,
        cpu_cores=cpu_cores,
        memory_limit=memory_limit,
        upload_max_filesize="64M",
        post_max_size="64M",
        max_execution_time="300",
        max_input_vars="3000",
        pm_max_children=max_children,
        pm_start_servers=max(2, math.ceil(max_children / 4)),
        pm_min_spare_servers=max(2, math.ceil(max_children / 8)),
        pm_max_spare_servers=max(4, math.ceil(max_children / 2)),
        opcache_memory=min(256, max(64, ram_mb // 16)),
        innodb_buffer_pool_size=f"{innodb_mb}M",
        max_connections=min(300, max(100, max_children * 4)),
    )


def _detect_server(ssh: SSHClient, secrets: list[str]) -> tuple[int, int]:
    _, out, _ = ssh.run_script(DETECT_SCRIPT, timeout=30, secrets=secrets)
    ram_mb, cpu_cores = 1024, 1
    for line in out.splitlines():
        if line.startswith("RAM_MB="):
            try:
                ram_mb = int(line.split("=", 1)[1])
            except ValueError:
                pass
        if line.startswith("CPU_CORES="):
            try:
                cpu_cores = int(line.split("=", 1)[1])
            except ValueError:
                pass
    return ram_mb, cpu_cores


def _build_apply_script(task: DeployTask, profile: OptimizeProfile) -> str:
    php_short = php_ver_short(task.php_version)
    p = profile
    return f"""set -e
PHP_SHORT="{php_short}"
PHP_ROOT="/www/server/php/${{PHP_SHORT}}"
PHP_INI="${{PHP_ROOT}}/etc/php.ini"
FPM_POOL="${{PHP_ROOT}}/etc/php-fpm.d/www.conf"
SOFT="/www/server/panel/install/install_soft.sh"

echo "[optimize] PHP 路径: $PHP_ROOT"

# --- PHP ini 优化 ---
if [ -f "$PHP_INI" ]; then
  sed -i 's/^;\\?memory_limit.*/memory_limit = {p.memory_limit}/' "$PHP_INI"
  sed -i 's/^;\\?upload_max_filesize.*/upload_max_filesize = {p.upload_max_filesize}/' "$PHP_INI"
  sed -i 's/^;\\?post_max_size.*/post_max_size = {p.post_max_size}/' "$PHP_INI"
  sed -i 's/^;\\?max_execution_time.*/max_execution_time = {p.max_execution_time}/' "$PHP_INI"
  sed -i 's/^;\\?max_input_vars.*/max_input_vars = {p.max_input_vars}/' "$PHP_INI"
  grep -q '^memory_limit' "$PHP_INI" || echo 'memory_limit = {p.memory_limit}' >> "$PHP_INI"
  grep -q '^upload_max_filesize' "$PHP_INI" || echo 'upload_max_filesize = {p.upload_max_filesize}' >> "$PHP_INI"
  grep -q '^post_max_size' "$PHP_INI" || echo 'post_max_size = {p.post_max_size}' >> "$PHP_INI"
  grep -q '^max_execution_time' "$PHP_INI" || echo 'max_execution_time = {p.max_execution_time}' >> "$PHP_INI"
  grep -q '^max_input_vars' "$PHP_INI" || echo 'max_input_vars = {p.max_input_vars}' >> "$PHP_INI"

  # Opcache
  sed -i 's/^;\\?opcache.enable.*/opcache.enable=1/' "$PHP_INI"
  sed -i 's/^;\\?opcache.enable_cli.*/opcache.enable_cli=1/' "$PHP_INI"
  sed -i 's/^;\\?opcache.memory_consumption.*/opcache.memory_consumption={p.opcache_memory}/' "$PHP_INI"
  sed -i 's/^;\\?opcache.max_accelerated_files.*/opcache.max_accelerated_files=10000/' "$PHP_INI"
  sed -i 's/^;\\?opcache.revalidate_freq.*/opcache.revalidate_freq=60/' "$PHP_INI"
  grep -q '^opcache.enable' "$PHP_INI" || cat >> "$PHP_INI" <<OPCACHE

[opcache]
opcache.enable=1
opcache.enable_cli=1
opcache.memory_consumption={p.opcache_memory}
opcache.max_accelerated_files=10000
opcache.revalidate_freq=60
OPCACHE
  echo "[optimize] php.ini 已更新"
else
  echo "[optimize] 警告: 未找到 php.ini"
fi

# --- PHP-FPM 进程参数 ---
if [ -f "$FPM_POOL" ]; then
  sed -i 's/^pm = .*/pm = dynamic/' "$FPM_POOL"
  sed -i 's/^pm.max_children.*/pm.max_children = {p.pm_max_children}/' "$FPM_POOL"
  sed -i 's/^pm.start_servers.*/pm.start_servers = {p.pm_start_servers}/' "$FPM_POOL"
  sed -i 's/^pm.min_spare_servers.*/pm.min_spare_servers = {p.pm_min_spare_servers}/' "$FPM_POOL"
  sed -i 's/^pm.max_spare_servers.*/pm.max_spare_servers = {p.pm_max_spare_servers}/' "$FPM_POOL"
  grep -q '^pm.max_children' "$FPM_POOL" || echo 'pm.max_children = {p.pm_max_children}' >> "$FPM_POOL"
  grep -q '^pm.start_servers' "$FPM_POOL" || echo 'pm.start_servers = {p.pm_start_servers}' >> "$FPM_POOL"
  grep -q '^pm.min_spare_servers' "$FPM_POOL" || echo 'pm.min_spare_servers = {p.pm_min_spare_servers}' >> "$FPM_POOL"
  grep -q '^pm.max_spare_servers' "$FPM_POOL" || echo 'pm.max_spare_servers = {p.pm_max_spare_servers}' >> "$FPM_POOL"
  echo "[optimize] PHP-FPM 已更新"
fi

# --- MySQL 优化 ---
MYSQL_CNF="/etc/my.cnf"
QBW_CNF="/etc/my.cnf.d/qbw-optimize.cnf"
mkdir -p /etc/my.cnf.d 2>/dev/null || true
cat > "$QBW_CNF" <<MYSQL
[mysqld]
innodb_buffer_pool_size = {p.innodb_buffer_pool_size}
max_connections = {p.max_connections}
innodb_flush_log_at_trx_commit = 2
innodb_log_file_size = 256M
table_open_cache = 400
thread_cache_size = 64
tmp_table_size = 64M
max_heap_table_size = 64M
MYSQL

if [ -f "$MYSQL_CNF" ] && ! grep -q 'qbw-optimize.cnf' "$MYSQL_CNF" 2>/dev/null; then
  grep -q '!includedir /etc/my.cnf.d' "$MYSQL_CNF" || echo '!includedir /etc/my.cnf.d' >> "$MYSQL_CNF"
fi
echo "[optimize] MySQL 配置已写入 $QBW_CNF"

# --- 重启服务 ---
/etc/init.d/php-fpm-${{PHP_SHORT}} restart 2>/dev/null || /etc/init.d/php-fpm restart 2>/dev/null || true
/etc/init.d/mysqld restart 2>/dev/null || /etc/init.d/mysql restart 2>/dev/null || true

# --- 验证 ---
"$PHP_ROOT/bin/php" -m 2>/dev/null | grep -i opcache && echo "[optimize] Opcache: 已加载" || echo "[optimize] Opcache: 未检测到"
echo "[optimize] 完成"
"""


def optimize_environment(
    task: DeployTask,
    password: str,
    db: Session,
    remote_state: RemoteDeployState | None = None,
) -> dict:
    publish_log(task.id, "step6_optimize", "正在检测服务器配置以生成优化方案...", db)
    secrets = [password, task.bt_password]

    with SSHClient(
        host=task.ssh_host,
        port=task.ssh_port,
        username=task.ssh_user,
        password=password,
        task_id=task.id,
        log_phase="step6_optimize",
        db=db,
    ) as ssh:
        if remote_state and remote_state.optimize == "complete":
            publish_log(task.id, "step6_optimize", "环境优化已完成，跳过", db)
            ram_mb, cpu_cores = _detect_server(ssh, secrets)
            profile = compute_profile(ram_mb, cpu_cores)
            result = profile.to_dict()
            result["opcache_enabled"] = True
            result["skipped"] = True
            return result
        if is_optimize_done(ssh, secrets):
            publish_log(task.id, "step6_optimize", "环境优化已完成，跳过", db)
            ram_mb, cpu_cores = _detect_server(ssh, secrets)
            profile = compute_profile(ram_mb, cpu_cores)
            result = profile.to_dict()
            result["opcache_enabled"] = True
            result["skipped"] = True
            return result

        ram_mb, cpu_cores = _detect_server(ssh, secrets)
        profile = compute_profile(ram_mb, cpu_cores)

        publish_log(
            task.id,
            "step6_optimize",
            f"服务器配置: {ram_mb}MB 内存 / {cpu_cores} CPU 核心",
            db,
        )
        publish_log(
            task.id,
            "step6_optimize",
            f"推荐 PHP memory_limit={profile.memory_limit}, upload={profile.upload_max_filesize}, "
            f"max_children={profile.pm_max_children}",
            db,
        )
        publish_log(
            task.id,
            "step6_optimize",
            f"推荐 MySQL innodb_buffer_pool_size={profile.innodb_buffer_pool_size}, "
            f"max_connections={profile.max_connections}",
            db,
        )

        publish_log(task.id, "step6_optimize", "开始应用 PHP / PHP-FPM / MySQL 优化...", db)
        script = _build_apply_script(task, profile)
        code, out, err = ssh.run_script(script, timeout=900, secrets=secrets)
        if code != 0:
            raise RuntimeError(f"环境优化失败: {err or out}")

        opcache_ok = "Opcache: 已加载" in out

        publish_log(task.id, "step6_optimize", "基础环境优化完成", db)

        result = profile.to_dict()
        result["opcache_enabled"] = opcache_ok
        return result
