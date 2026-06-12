"""PHP 版本与操作系统兼容性（基于宝塔官方 install 脚本限制）。"""

from __future__ import annotations

PHP_FALLBACK_VERSION = "8.2"
PHP_FALLBACK_SOURCE_VERSIONS = frozenset({"8.4", "8.5"})


def can_auto_fallback_php(php_version: str) -> bool:
    return (php_version or "").strip() in PHP_FALLBACK_SOURCE_VERSIONS


def php_version_os_error(php_version: str, os_detected: str, os_version: str) -> str | None:
    """不兼容时返回可读错误，否则返回 None。"""
    ver = (php_version or "").strip()
    if ver not in PHP_FALLBACK_SOURCE_VERSIONS:
        return None

    os_id = (os_detected or "").lower()
    major = (os_version or "").split(".")[0]

    if os_id == "centos" and major == "7":
        return (
            f"当前系统（CentOS 7）不支持 PHP {ver}。"
            f"宝塔安装脚本会直接拒绝"
        )

    return None


def resolve_php_version_for_os(
    requested: str,
    os_detected: str,
    os_version: str,
) -> tuple[str, str | None]:
    """预检降级：不兼容时返回 (PHP_FALLBACK_VERSION, 警告文案)。"""
    req = (requested or "").strip()
    if not req:
        return req, None
    err = php_version_os_error(req, os_detected, os_version)
    if err and can_auto_fallback_php(req):
        return PHP_FALLBACK_VERSION, (
            f"您选择了 PHP {req}，但{err}。"
            f"将自动降级为 PHP {PHP_FALLBACK_VERSION} 进行部署"
        )
    return req, None


def php_install_fallback_message(from_version: str, to_version: str, reason: str) -> str:
    detail = reason.strip().rstrip("。")
    return (
        f"PHP {from_version} 安装失败（{detail}），"
        f"自动降级为 PHP {to_version} 重新安装"
    )
