from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator
import re
import secrets
import string

from app.core.sql_safety import validate_email, validate_sql_identifier
from app.core.ssh_host import validate_ssh_host
from app.services.site_config import (
    find_duplicate_domains,
    normalize_site_entry,
    parse_domains,
    validate_bind_host,
)

ServerOS = Literal["ubuntu", "debian", "centos", "generic"]


def _gen_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _gen_safe_path() -> str:
    return secrets.token_hex(4)


class SiteCreateConfig(BaseModel):
    site_name: str | None = Field(default=None, max_length=255)
    domains: list[str] = Field(..., min_length=1)
    wp_admin_user: str | None = Field(default=None, max_length=60)
    wp_admin_password: str | None = Field(default=None, max_length=128)
    wp_admin_email: str | None = Field(default=None, max_length=255)
    wp_locale: str = Field(default="zh_CN")
    db_prefix: str | None = Field(default=None, max_length=20)
    db_name: str | None = Field(default=None, max_length=64)
    db_user: str | None = Field(default=None, max_length=16)
    db_password: str | None = Field(default=None, max_length=128)

    @field_validator("site_name", "wp_admin_user", mode="before")
    @classmethod
    def empty_str_to_none(cls, v: str | None) -> str | None:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        return v.strip() if isinstance(v, str) else v

    @field_validator("wp_admin_password", mode="before")
    @classmethod
    def empty_password_to_none(cls, v: str | None) -> str | None:
        if v is None or (isinstance(v, str) and not v):
            return None
        return v

    @field_validator("domains", mode="before")
    @classmethod
    def normalize_domains(cls, v: list[str] | str) -> list[str]:
        return parse_domains(v)

    @field_validator("wp_admin_user")
    @classmethod
    def validate_admin_user(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not re.match(r"^[a-zA-Z0-9_\-]+$", v):
            raise ValueError("管理员账号只能包含字母、数字、下划线和连字符")
        return v

    @field_validator("wp_admin_password")
    @classmethod
    def validate_wp_admin_password(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if len(v) < 6:
            raise ValueError("管理员密码至少 6 位")
        return v

    @field_validator("db_prefix")
    @classmethod
    def validate_db_prefix_field(cls, v: str | None) -> str | None:
        return validate_sql_identifier(v, field="数据库表前缀", max_len=20)

    @field_validator("db_name")
    @classmethod
    def validate_db_name_field(cls, v: str | None) -> str | None:
        return validate_sql_identifier(v, field="数据库名称", max_len=64)

    @field_validator("db_user")
    @classmethod
    def validate_db_user_field(cls, v: str | None) -> str | None:
        return validate_sql_identifier(v, field="数据库用户名", max_len=16)

    @field_validator("db_password")
    @classmethod
    def validate_db_password(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if len(v) < 6:
            raise ValueError("数据库密码至少 6 位")
        return v

    @field_validator("wp_admin_email")
    @classmethod
    def validate_wp_admin_email(cls, v: str | None) -> str | None:
        return validate_email(v)

    def to_normalized(self) -> dict[str, Any]:
        return normalize_site_entry(
            self.model_dump(),
            auto_wp_password=not self.wp_admin_password,
        )


class DeployCreateRequest(BaseModel):
    # Step 1
    ssh_host: str = Field(..., min_length=1, max_length=255)
    ssh_password: str = Field(..., min_length=1)
    ssh_port: int = Field(default=22, ge=1, le=65535)
    ssh_user: str = Field(default="root", max_length=64)
    server_os: ServerOS = Field(default="generic")

    @field_validator("ssh_host")
    @classmethod
    def validate_ssh_host_field(cls, v: str) -> str:
        return validate_ssh_host(v)

    @field_validator("server_os", mode="before")
    @classmethod
    def normalize_server_os(cls, v: str | None) -> str:
        if v in (None, "", "other"):
            return "generic"
        return v
    confirm_non_fresh: bool = Field(default=False)
    bt_user: str | None = Field(default=None, max_length=64)
    bt_password: str | None = Field(default=None, max_length=128)
    bt_port: int = Field(default=8888, ge=1024, le=65535)

    # Step 2
    nginx_version: str = Field(default="1.30")
    php_version: str = Field(default="8.5")
    mysql_version: str = Field(default="8.0")

    # Step 3 — 多网站（推荐）或单站兼容字段
    sites: list[SiteCreateConfig] | None = Field(default=None, min_length=1)
    site_name: str | None = Field(default=None, max_length=255)
    site_domain: str | None = Field(default=None, min_length=3, max_length=255)
    wp_admin_user: str | None = Field(default=None, max_length=60)
    wp_admin_password: str | None = Field(default=None, max_length=128)
    wp_admin_email: str | None = Field(default=None, max_length=255)
    wp_locale: str = Field(default="zh_CN")
    db_prefix: str | None = Field(default=None, max_length=20)
    db_name: str | None = Field(default=None, max_length=64)
    db_user: str | None = Field(default=None, max_length=16)
    db_password: str | None = Field(default=None, max_length=128)

    @field_validator("site_domain")
    @classmethod
    def validate_site_domain(cls, v: str | None) -> str | None:
        if v is None or not str(v).strip():
            return None
        try:
            return validate_bind_host(str(v))
        except ValueError as e:
            raise ValueError("请输入有效的绑定域名或 IP 地址") from e

    @field_validator("site_name", "wp_admin_user", mode="before")
    @classmethod
    def empty_str_to_none(cls, v: str | None) -> str | None:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        return v.strip() if isinstance(v, str) else v

    @field_validator("wp_admin_password", mode="before")
    @classmethod
    def empty_password_to_none(cls, v: str | None) -> str | None:
        if v is None or (isinstance(v, str) and not v):
            return None
        return v

    @field_validator("wp_admin_user")
    @classmethod
    def validate_admin_user(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not re.match(r"^[a-zA-Z0-9_\-]+$", v):
            raise ValueError("管理员账号只能包含字母、数字、下划线和连字符")
        return v

    @field_validator("wp_admin_password")
    @classmethod
    def validate_wp_admin_password(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if len(v) < 6:
            raise ValueError("管理员密码至少 6 位")
        return v

    @field_validator("db_prefix")
    @classmethod
    def validate_db_prefix_field(cls, v: str | None) -> str | None:
        return validate_sql_identifier(v, field="数据库表前缀", max_len=20)

    @field_validator("db_name")
    @classmethod
    def validate_db_name_field(cls, v: str | None) -> str | None:
        return validate_sql_identifier(v, field="数据库名称", max_len=64)

    @field_validator("db_user")
    @classmethod
    def validate_db_user_field(cls, v: str | None) -> str | None:
        return validate_sql_identifier(v, field="数据库用户名", max_len=16)

    @field_validator("db_password")
    @classmethod
    def validate_db_password(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if len(v) < 6:
            raise ValueError("数据库密码至少 6 位")
        return v

    @field_validator("wp_admin_email")
    @classmethod
    def validate_wp_admin_email(cls, v: str | None) -> str | None:
        return validate_email(v)

    @model_validator(mode="after")
    def ensure_sites(self) -> DeployCreateRequest:
        if self.sites:
            dupes = find_duplicate_domains([s.to_normalized() for s in self.sites])
            if dupes:
                raise ValueError(f"以下域名在多个网站中重复：{', '.join(dupes)}")
            return self
        if not self.site_domain:
            raise ValueError("请至少添加一个网站并填写绑定域名")
        self.sites = [
            SiteCreateConfig(
                site_name=self.site_name,
                domains=[self.site_domain],
                wp_admin_user=self.wp_admin_user,
                wp_admin_password=self.wp_admin_password,
                wp_admin_email=self.wp_admin_email,
                wp_locale=self.wp_locale,
                db_prefix=self.db_prefix,
                db_name=self.db_name,
                db_user=self.db_user,
                db_password=self.db_password,
            )
        ]
        return self

    def normalized_sites(self) -> list[dict[str, Any]]:
        return [s.to_normalized() for s in (self.sites or [])]

    def resolved_bt_user(self) -> str:
        return self.bt_user or f"bt_{secrets.token_hex(4)}"

    def resolved_bt_password(self) -> str:
        return self.bt_password or _gen_password()

    def resolved_bt_safe_path(self) -> str:
        return _gen_safe_path()

    def primary_site(self) -> dict[str, Any]:
        return self.normalized_sites()[0]

    def resolved_site_name(self) -> str:
        return self.primary_site()["site_name"]

    def resolved_site_domain(self) -> str:
        return self.primary_site()["primary_domain"]

    def resolved_wp_admin_user(self) -> str:
        return self.primary_site()["wp_admin_user"]

    def resolved_wp_admin_password(self) -> str:
        return self.primary_site()["wp_admin_password"]

    def wp_password_was_auto_generated(self) -> bool:
        return bool(self.primary_site().get("wp_password_auto_generated"))

    def resolved_wp_email(self) -> str:
        return self.primary_site()["wp_admin_email"]

    def all_domains(self) -> list[str]:
        from app.services.site_config import collect_all_domains

        return collect_all_domains(self.normalized_sites())


class DeployCreateResponse(BaseModel):
    token: str
    progress_url: str


class DeployCancelResponse(BaseModel):
    ok: bool
    message: str


class DeployRetryResponse(BaseModel):
    ok: bool
    message: str
    current_phase: str
    user_step_label: str


class DeployStatusResponse(BaseModel):
    token: str
    status: str
    current_phase: str
    user_step: int
    user_step_label: str
    error_message: str | None
    result: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime
    expired: bool


class PreflightRequest(BaseModel):
    ssh_host: str
    ssh_password: str
    ssh_port: int = 22
    ssh_user: str = "root"
    server_os: ServerOS = "generic"
    php_version: str | None = Field(default=None, max_length=16)
    site_domain: str | None = Field(default=None, max_length=255)
    site_domains: list[str] | None = Field(default=None)

    @field_validator("ssh_host")
    @classmethod
    def validate_ssh_host_field(cls, v: str) -> str:
        return validate_ssh_host(v)

    @field_validator("site_domains", mode="before")
    @classmethod
    def normalize_site_domains(cls, v: list[str] | str | None) -> list[str] | None:
        if v is None:
            return None
        if isinstance(v, str):
            if not v.strip():
                return None
            return parse_domains(v)
        domains: list[str] = []
        for item in v:
            if item and str(item).strip():
                domains.extend(parse_domains(str(item)))
        return domains or None

    @field_validator("server_os", mode="before")
    @classmethod
    def normalize_server_os(cls, v: str | None) -> str:
        if v in (None, "", "other"):
            return "generic"
        return v


class PreflightResponse(BaseModel):
    ok: bool
    ssh_ok: bool
    is_fresh: bool
    requires_confirmation: bool
    os_detected: str
    os_version: str
    os_pretty: str
    os_match: bool
    baota_installed: bool
    web_environment: list[str]
    site_dirs: int
    site_samples: list[str]
    warnings: list[str]
    message: str
    uname: str | None = None
    blocked: bool = False
    domain_conflict: bool = False
    target_domain: str = ""
    target_domains: list[str] = Field(default_factory=list)
    conflicting_domains: list[str] = Field(default_factory=list)
    existing_site_for_domain: bool = False
    php_version_requested: str | None = None
    php_version_effective: str | None = None
    php_version_fallback: bool = False


class SSHTestRequest(BaseModel):
    ssh_host: str
    ssh_password: str
    ssh_port: int = 22
    ssh_user: str = "root"
    server_os: ServerOS = "generic"

    @field_validator("ssh_host")
    @classmethod
    def validate_ssh_host_field(cls, v: str) -> str:
        return validate_ssh_host(v)

    @field_validator("server_os", mode="before")
    @classmethod
    def normalize_server_os(cls, v: str | None) -> str:
        if v in (None, "", "other"):
            return "generic"
        return v


class SSHTestResponse(BaseModel):
    ok: bool
    message: str
    os_info: str | None = None
    preflight: PreflightResponse | None = None
