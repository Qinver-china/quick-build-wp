from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.deploy import DeployTask
from app.services.deploy_cancel import purge_deploy_task

logger = logging.getLogger(__name__)

PURGE_BATCH_SIZE = 200


def expired_task_cutoff() -> datetime:
    return datetime.utcnow() - timedelta(hours=settings.task_expire_hours)


def purge_expired_tasks(db: Session) -> int:
    """删除超过保留期的部署任务及其日志、Redis 状态。"""
    cutoff = expired_task_cutoff()
    task_ids = [
        row[0]
        for row in db.query(DeployTask.id)
        .filter(DeployTask.created_at < cutoff)
        .order_by(DeployTask.created_at.asc())
        .limit(PURGE_BATCH_SIZE)
        .all()
    ]

    purged = 0
    for task_id in task_ids:
        task = db.get(DeployTask, task_id)
        if not task:
            continue
        try:
            purge_deploy_task(db, task)
            purged += 1
        except Exception:
            logger.exception("Failed to purge expired deploy task %s", task_id)
            db.rollback()

    return purged
