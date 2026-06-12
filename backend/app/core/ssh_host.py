"""SSH 目标地址安全校验：禁止本地/内网地址。"""

from __future__ import annotations

import ipaddress
import re

BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
    }
)

SSH_HOST_REJECT_MSG = (
    "不能使用本地或内网地址（如 127.0.0.1、localhost、192.168.x.x），"
    "请填写云服务器的公网 IP 或域名"
)


def _strip_brackets(host: str) -> str:
    text = host.strip()
    if text.startswith("[") and text.endswith("]"):
        return text[1:-1].strip()
    return text


def is_blocked_ssh_host(host: str) -> bool:
    """是否为应拒绝的本地/内网 SSH 地址。"""
    text = _strip_brackets(host)
    if not text:
        return True

    lower = text.lower().rstrip(".")
    if lower in BLOCKED_HOSTNAMES:
        return True

    if re.fullmatch(r"\d+", lower):
        return True

    try:
        ip = ipaddress.ip_address(lower)
    except ValueError:
        if lower.endswith(".local") or lower.endswith(".localhost"):
            return True
        return False

    return bool(
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_unspecified
        or ip.is_multicast
        or ip.is_reserved
    )


def validate_ssh_host(host: str) -> str:
    text = host.strip()
    if not text:
        raise ValueError("SSH 地址不能为空")
    if is_blocked_ssh_host(text):
        raise ValueError(SSH_HOST_REJECT_MSG)
    return text
