from __future__ import annotations

import logging

from app.core.database import SessionLocal
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="deploy.purge_expired")
def purge_expired_deploy_tasks() -> int:
    """定期清理超过保留期的部署任务。"""
    db = SessionLocal()
    try:
        from app.services.task_cleanup import purge_expired_tasks

        count = purge_expired_tasks(db)
        if count:
            logger.info("Purged %s expired deploy task(s)", count)
        return count
    finally:
        db.close()
