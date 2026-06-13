"""MySQL 版本与服务器内存兼容性（基于宝塔 mysql.sh 安装要求）。"""

from __future__ import annotations

MYSQL_FALLBACK_VERSION = "5.7"
MYSQL_80_SOURCE_VERSIONS = frozenset({"8.0"})
MYSQL_80_MIN_TOTAL_RAM_MB = 5120  # 5GB


def can_auto_fallback_mysql(mysql_version: str) -> bool:
    return (mysql_version or "").strip() in MYSQL_80_SOURCE_VERSIONS


def resolve_mysql_version_for_ram(
    requested: str,
    ram_mb: int,
) -> tuple[str, str | None]:
    """总内存不足 5GB 时，MySQL 8.0 预检降级为 5.7。"""
    req = (requested or "").strip()
    if not req:
        return req, None
    if req in MYSQL_80_SOURCE_VERSIONS and ram_mb < MYSQL_80_MIN_TOTAL_RAM_MB:
        return MYSQL_FALLBACK_VERSION, (
            f"检测到服务器总内存为 {ram_mb}MB（不足 5GB），"
            f"MySQL 8.0 编译安装对内存要求较高。"
            f"将自动降级为 MySQL {MYSQL_FALLBACK_VERSION} 进行部署"
        )
    return req, None


def mysql_install_fallback_message(from_version: str, to_version: str, reason: str) -> str:
    detail = reason.strip().rstrip("。")
    return (
        f"MySQL {from_version} 安装失败（{detail}），"
        f"自动降级为 MySQL {to_version} 重新安装"
    )
