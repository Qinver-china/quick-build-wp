"""用户输入的 SQL / Shell 安全处理（标识符白名单、MySQL 字符串转义、Shell 引用）。"""

from __future__ import annotations

import re
import shlex

SQL_IDENT_RE = re.compile(r"^[a-zA-Z0-9_]+$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def validate_sql_identifier(
    value: str | None,
    *,
    field: str = "数据库标识符",
    max_len: int = 64,
) -> str | None:
    if value is None or value == "":
        return None
    text = value.strip()
    if len(text) > max_len:
        raise ValueError(f"{field}长度不能超过 {max_len} 个字符")
    if not SQL_IDENT_RE.match(text):
        raise ValueError(f"{field}只能包含字母、数字和下划线")
    return text


def sanitize_sql_identifier(value: str | None, *, max_len: int = 64, default: str = "") -> str:
    """剥离非法字符，用于运行时二次防护（不可代替入口校验）。"""
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "", (value or "").strip())
    if max_len:
        cleaned = cleaned[:max_len]
    return cleaned or default


def sanitize_db_prefix(value: str | None, *, max_len: int = 20) -> str:
    prefix = sanitize_sql_identifier(value, max_len=max_len, default="")
    if prefix and not prefix.endswith("_"):
        prefix += "_"
    return prefix or "wp_"


def escape_mysql_string_literal(value: str) -> str:
    """转义 MySQL 单引号字符串字面量内的特殊字符。"""
    return value.replace("\\", "\\\\").replace("'", "''")


def quote_shell(value: str) -> str:
    """远程 SSH 命令参数 Shell 引用，防止命令注入。"""
    return shlex.quote(value)


def validate_email(value: str | None, *, field: str = "邮箱") -> str | None:
    if value is None or value == "":
        return None
    text = value.strip()
    if len(text) > 255:
        raise ValueError(f"{field}长度不能超过 255 个字符")
    if not EMAIL_RE.match(text):
        raise ValueError(f"无效的{field}地址")
    return text


def sanitize_db_credentials(
    db_name: str,
    db_user: str,
    db_pass: str,
    db_prefix: str,
    *,
    user_max_len: int = 16,
    name_max_len: int = 64,
) -> tuple[str, str, str, str]:
    """部署前统一清洗库名/用户/前缀（密码仅做 Shell/SQL 转义，不改写内容）。"""
    safe_name = sanitize_sql_identifier(db_name, max_len=name_max_len, default="wp_default")
    safe_user = sanitize_sql_identifier(db_user, max_len=user_max_len, default=safe_name[:user_max_len])
    safe_prefix = sanitize_db_prefix(db_prefix)
    return safe_name, safe_user, db_pass, safe_prefix
