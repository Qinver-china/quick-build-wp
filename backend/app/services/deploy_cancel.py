from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.core.redis_client import get_redis
from app.models.deploy import DeployTask
from app.models.log import DeployLog
from app.services.deploy_lock import LOCK_PREFIX, clear_deploy_lock, release_host_lock
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

CANCEL_PREFIX = "deploy:cancel:"
LOG_PREFIX = "deploy:log:"
CELERY_RESULT_PREFIX = "celery-task-meta-"
CANCEL_TTL_SECONDS = 86400


class DeployCancelledError(Exception):
    pass


def mark_deploy_cancelled(task_id: str) -> None:
    get_redis().set(f"{CANCEL_PREFIX}{task_id}", "1", ex=CANCEL_TTL_SECONDS)


def is_deploy_cancelled(task_id: str) -> bool:
    return get_redis().get(f"{CANCEL_PREFIX}{task_id}") == "1"


def clear_deploy_cancelled(task_id: str) -> None:
    get_redis().delete(f"{CANCEL_PREFIX}{task_id}")


def revoke_deploy_worker(task_id: str) -> None:
    """尽力终止正在执行的 Celery 部署任务。"""
    try:
        celery_app.control.revoke(task_id, terminate=True, signal="SIGTERM")
    except Exception:
        logger.exception("Failed to revoke celery task %s", task_id)


def clear_task_redis_keys(task_id: str) -> None:
    """清除与任务相关的 Redis 状态（保留 cancel 标记供 Worker 感知）。"""
    redis_client = get_redis()
    keys = [
        f"{LOCK_PREFIX}{task_id}",
        f"{LOG_PREFIX}{task_id}",
        f"{CELERY_RESULT_PREFIX}{task_id}",
    ]
    redis_client.delete(*keys)

    for key in redis_client.scan_iter(match=f"deploy:*{task_id}*", count=200):
        if key.startswith(CANCEL_PREFIX):
            continue
        redis_client.delete(key)


def purge_deploy_task(db: Session, task: DeployTask) -> None:
    """终止并彻底清除任务：通知 Worker 停止、删除 DB 记录与日志、清理 Redis。"""
    task_id = task.id

    mark_deploy_cancelled(task_id)
    revoke_deploy_worker(task_id)
    clear_deploy_lock(task_id)
    release_host_lock(task.ssh_host, task.ssh_port, task_id)

    db.query(DeployLog).filter(DeployLog.task_id == task_id).delete(synchronize_session=False)
    db.delete(task)
    db.commit()

    clear_task_redis_keys(task_id)
