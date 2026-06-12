import logging

from app.core.database import SessionLocal
from app.core.redis_client import get_redis
from app.models.deploy import DeployStatus, DeployTask
from app.services.deploy_lock import clear_deploy_lock, release_host_lock
from app.services.log_publisher import publish_log
from app.tasks.deploy_pipeline import run_deploy_pipeline

logger = logging.getLogger(__name__)

RECOVERY_SCAN_LOCK = "deploy:recovery_scan"
RECOVERY_SCAN_TTL = 120


def recover_incomplete_tasks() -> int:
    """Worker 启动时扫描未完成任务并重新入队（每台主机仅恢复最新一条）。"""
    redis_client = get_redis()
    if not redis_client.set(RECOVERY_SCAN_LOCK, "1", nx=True, ex=RECOVERY_SCAN_TTL):
        logger.info("Another worker is running deploy recovery scan, skipping")
        return 0

    db = SessionLocal()
    queued = 0
    try:
        tasks = (
            db.query(DeployTask)
            .filter(
                DeployTask.status.in_([DeployStatus.PENDING, DeployStatus.RUNNING]),
                DeployTask.ssh_password_enc.isnot(None),
            )
            .order_by(DeployTask.updated_at.desc())
            .all()
        )

        seen_hosts: set[str] = set()
        for task in tasks:
            host_key = f"{task.ssh_host}:{task.ssh_port}"
            if host_key in seen_hosts:
                if task.status == DeployStatus.RUNNING:
                    task.status = DeployStatus.FAILED
                    task.error_message = "同一服务器存在更新的恢复任务，此任务已取消"
                    db.commit()
                    release_host_lock(task.ssh_host, task.ssh_port, task.id)
                    publish_log(
                        task.id,
                        "system",
                        "检测到重复恢复任务，已取消（请使用最新部署任务）",
                        db,
                    )
                logger.info("Skip duplicate recovery for host %s task %s", host_key, task.token)
                continue

            seen_hosts.add(host_key)
            clear_deploy_lock(task.id)
            logger.info("Recovering deploy task %s (status=%s)", task.token, task.status.value)
            run_deploy_pipeline.apply_async(
                args=[task.id],
                kwargs={"recovery": True},
                task_id=task.id,
            )
            queued += 1

        if queued:
            logger.info("Queued %d deploy task(s) for recovery", queued)
    finally:
        db.close()

    return queued
