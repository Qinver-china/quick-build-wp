import enum
import uuid
from datetime import datetime, timedelta

from sqlalchemy import DateTime, Enum, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import settings
from app.core.database import Base


class DeployStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class DeployPhase(str, enum.Enum):
    STEP1_BAOTA = "step1_baota"
    STEP2_NGINX = "step2_nginx"
    STEP3_PHP = "step3_php"
    STEP3_MYSQL = "step3_mysql"
    STEP4_REDIS = "step4_redis"
    STEP5_PHP_EXT = "step5_php_ext"
    STEP6_OPTIMIZE = "step6_optimize"
    STEP7_SITE = "step7_site"
    STEP8_SSL = "step8_ssl"
    DONE = "done"
    # 兼容旧任务阶段
    STEP2_PHP = "step2_php"
    STEP2_LNMP = "step2_lnmp"
    STEP2_OPTIMIZE = "step2_optimize"
    STEP2_REDIS = "step2_redis"
    STEP3_SITE = "step3_site"
    STEP4_WORDPRESS = "step4_wordpress"
    STEP5_VERIFY = "step5_verify"


# 用户可见的 9 步进度（0–8），完成时为 9
USER_STEPS = {
    DeployPhase.STEP1_BAOTA: 0,
    DeployPhase.STEP2_NGINX: 1,
    DeployPhase.STEP3_PHP: 2,
    DeployPhase.STEP3_MYSQL: 3,
    DeployPhase.STEP4_REDIS: 4,
    DeployPhase.STEP5_PHP_EXT: 5,
    DeployPhase.STEP6_OPTIMIZE: 6,
    DeployPhase.STEP7_SITE: 7,
    DeployPhase.STEP8_SSL: 8,
    DeployPhase.DONE: 9,
    # 旧阶段映射
    DeployPhase.STEP2_PHP: 2,
    DeployPhase.STEP2_LNMP: 2,
    DeployPhase.STEP2_OPTIMIZE: 6,
    DeployPhase.STEP2_REDIS: 4,
    DeployPhase.STEP3_SITE: 7,
    DeployPhase.STEP4_WORDPRESS: 7,
    DeployPhase.STEP5_VERIFY: 7,
}


class DeployTask(Base):
    __tablename__ = "deploy_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True, default=lambda: uuid.uuid4().hex)
    status: Mapped[DeployStatus] = mapped_column(Enum(DeployStatus), default=DeployStatus.PENDING)
    current_phase: Mapped[DeployPhase] = mapped_column(Enum(DeployPhase), default=DeployPhase.STEP1_BAOTA)
    client_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)

    # Encrypted SSH password (cleared after task ends)
    ssh_host: Mapped[str] = mapped_column(String(255))
    ssh_port: Mapped[int] = mapped_column(default=22)
    ssh_user: Mapped[str] = mapped_column(String(64), default="root")
    server_os: Mapped[str] = mapped_column(String(16), default="generic")
    confirm_non_fresh: Mapped[bool] = mapped_column(default=False)
    ssh_password_enc: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Baota panel config
    bt_user: Mapped[str] = mapped_column(String(64))
    bt_password: Mapped[str] = mapped_column(String(128))
    bt_port: Mapped[int] = mapped_column(default=8888)
    bt_safe_path: Mapped[str] = mapped_column(String(64))

    # LNMP versions
    nginx_version: Mapped[str] = mapped_column(String(16), default="1.24")
    php_version: Mapped[str] = mapped_column(String(16), default="8.2")
    php_version_requested: Mapped[str | None] = mapped_column(String(16), nullable=True)
    mysql_version: Mapped[str] = mapped_column(String(16), default="8.0")
    mysql_version_requested: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # WordPress config
    site_name: Mapped[str] = mapped_column(String(255))
    site_domain: Mapped[str] = mapped_column(String(255))
    site_input: Mapped[str | None] = mapped_column(String(255), nullable=True)
    wp_admin_user: Mapped[str] = mapped_column(String(64))
    wp_admin_password: Mapped[str] = mapped_column(String(128))
    wp_password_auto_generated: Mapped[bool] = mapped_column(default=False)
    wp_admin_email: Mapped[str] = mapped_column(String(255))
    wp_locale: Mapped[str] = mapped_column(String(16), default="zh_CN")
    db_prefix: Mapped[str | None] = mapped_column(String(20), nullable=True)
    db_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    db_user: Mapped[str | None] = mapped_column(String(32), nullable=True)
    db_password: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sites_config: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    # Results (JSON)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.utcnow() + timedelta(hours=settings.task_expire_hours),
    )

    @property
    def user_step(self) -> int:
        return USER_STEPS.get(self.current_phase, 0)
