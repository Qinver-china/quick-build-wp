from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.core.database import SessionLocal
from app.models.deploy import DeployPhase, DeployStatus, DeployTask
from app.models.log import DeployLog

router = APIRouter(prefix="/api/deploy", tags=["logs"])

TERMINAL_STATUSES = {DeployStatus.SUCCESS.value, DeployStatus.FAILED.value}

PHASE_LABELS = {
    DeployPhase.STEP1_BAOTA: "安装宝塔",
    DeployPhase.STEP2_NGINX: "安装 Nginx",
    DeployPhase.STEP3_PHP: "安装 PHP",
    DeployPhase.STEP2_PHP: "安装 PHP",
    DeployPhase.STEP3_MYSQL: "安装 MySQL",
    DeployPhase.STEP4_REDIS: "安装 Redis",
    DeployPhase.STEP5_PHP_EXT: "安装 PHP 组件与扩展",
    DeployPhase.STEP6_OPTIMIZE: "参数调优",
    DeployPhase.STEP7_SITE: "创建网站并安装 WordPress",
    DeployPhase.STEP8_SSL: "申请 SSL 证书",
    DeployPhase.DONE: "完成",
}


class LogEntryItem(BaseModel):
    id: int
    phase: str
    message: str


class LogTailResponse(BaseModel):
    """增量日志 + 任务进度（前端轮询唯一接口）。"""

    token: str
    logs: list[LogEntryItem]
    done: bool
    status: str
    current_phase: str
    user_step: int
    user_step_label: str
    error_message: str | None = None
    result: dict[str, Any] | None = None
    expired: bool = False
    truncated: bool = False
    skipped: int = 0


@router.get("/{token}/logs/tail", response_model=LogTailResponse)
def get_log_tail(
    token: str,
    after_id: int = Query(default=0, ge=0),
    limit: int = Query(default=300, ge=1, le=1000),
):
    db = SessionLocal()
    try:
        task = db.query(DeployTask).filter(DeployTask.token == token).first()
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")

        base = db.query(DeployLog).filter(DeployLog.task_id == task.id)
        truncated = False
        skipped = 0

        if after_id > 0:
            rows = (
                base.filter(DeployLog.id > after_id)
                .order_by(DeployLog.id.asc())
                .limit(limit)
                .all()
            )
        else:
            total = base.count()
            rows = base.order_by(DeployLog.id.desc()).limit(limit).all()
            rows.reverse()
            if total > len(rows):
                truncated = True
                skipped = total - len(rows)

        done = task.status.value in TERMINAL_STATUSES
        expired = False
        if task.expires_at:
            exp = task.expires_at.replace(tzinfo=None) if task.expires_at.tzinfo else task.expires_at
            expired = exp < datetime.utcnow()

        return LogTailResponse(
            token=task.token,
            logs=[LogEntryItem(id=log.id, phase=log.phase, message=log.message) for log in rows],
            done=done,
            status=task.status.value,
            current_phase=task.current_phase.value,
            user_step=task.user_step,
            user_step_label=PHASE_LABELS.get(task.current_phase, ""),
            error_message=task.error_message,
            result=task.result,
            expired=expired,
            truncated=truncated,
            skipped=skipped,
        )
    finally:
        db.close()
