import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DeployStatStatus(str, enum.Enum):
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DeployStat(Base):
    __tablename__ = "deploy_stats"
    __table_args__ = (
        Index("ix_deploy_stats_finished_at", "finished_at"),
        Index("ix_deploy_stats_status", "status"),
        Index("ix_deploy_stats_client_ip", "client_ip"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    client_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    sites: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    failed_phase: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
