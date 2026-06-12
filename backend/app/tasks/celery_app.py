import logging

from celery import Celery
from celery.signals import worker_ready

from app.core.config import settings

logger = logging.getLogger(__name__)

celery_app = Celery(
    "quick_build_wp",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.tasks.deploy_pipeline", "app.tasks.cleanup"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_track_started=True,
    task_soft_time_limit=7200,
    task_time_limit=7500,
    worker_prefetch_multiplier=1,
    beat_schedule={
        "purge-expired-deploy-tasks": {
            "task": "deploy.purge_expired",
            "schedule": 3600.0,
        },
    },
)


@worker_ready.connect
def _recover_deploy_tasks_on_worker_start(**_kwargs) -> None:
    try:
        from app.core.schema import ensure_schema
        from app.services.task_recovery import recover_incomplete_tasks

        ensure_schema()
        count = recover_incomplete_tasks()
        if count:
            logger.info("Deploy recovery: queued %s task(s)", count)
    except Exception:
        logger.exception("Deploy recovery scan failed")
