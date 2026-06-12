"""远程服务器内存检测与 Redis 安装策略。"""

from __future__ import annotations

from app.services.ssh import SSHClient

REDIS_MIN_RAM_MB = 3072
REDIS_SKIP_MESSAGE = "当前服务器内存小于 3GB，不建议安装 Redis"

_RAM_DETECT_CMD = "free -m | awk '/^Mem:/{print $2}'"


def detect_ram_mb(ssh: SSHClient, secrets: list[str]) -> int:
    """读取目标机总内存（MB）。"""
    _, out, _ = ssh.run(_RAM_DETECT_CMD, timeout=15, secrets=secrets)
    text = (out or "").strip().splitlines()
    if not text:
        return 1024
    try:
        return max(0, int(text[0]))
    except ValueError:
        return 1024


def should_install_redis(ram_mb: int) -> bool:
    return ram_mb >= REDIS_MIN_RAM_MB


def redis_maxmemory_mb(ram_mb: int) -> int:
    """Redis maxmemory：总内存的 40%（至少 64MB）。"""
    return max(64, int(ram_mb * 0.4))
