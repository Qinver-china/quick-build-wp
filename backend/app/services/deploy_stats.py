from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.deploy import DeployStatus, DeployTask
from app.models.stats import DeployStat, DeployStatStatus
from app.services.site_config import resolve_sites_from_task

ERROR_SUMMARY_MAX = 500


def sanitize_sites_for_stats(task: DeployTask) -> list[dict]:
    sites: list[dict] = []
    for entry in resolve_sites_from_task(task):
        domains = entry.get("domains") or []
        primary = entry.get("primary_domain") or (domains[0] if domains else "")
        sites.append(
            {
                "site_name": entry.get("site_name") or "",
                "primary_domain": primary,
                "domains": list(domains),
            }
        )
    return sites


def _task_created_at(task: DeployTask) -> datetime:
    created_at = task.created_at or datetime.now(timezone.utc)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return created_at


def _truncate_error(message: str | None) -> str | None:
    error_text = (message or "").strip()
    if not error_text:
        return None
    if len(error_text) > ERROR_SUMMARY_MAX:
        return error_text[: ERROR_SUMMARY_MAX - 3] + "..."
    return error_text


def _resolve_stat_status(task: DeployTask, status: str | None) -> str | None:
    if status:
        return status
    if task.status == DeployStatus.SUCCESS:
        return DeployStatStatus.SUCCESS.value
    if task.status == DeployStatus.FAILED:
        return DeployStatStatus.FAILED.value
    return None


def start_deploy_stat(db: Session, task: DeployTask) -> None:
    """任务创建时写入进行中统计（幂等）。"""
    existing = db.query(DeployStat).filter(DeployStat.task_id == task.id).first()
    if existing:
        return

    stat = DeployStat(
        task_id=task.id,
        client_ip=task.client_ip,
        sites=sanitize_sites_for_stats(task),
        status=DeployStatStatus.RUNNING.value,
        failed_phase=None,
        error_summary=None,
        created_at=_task_created_at(task),
        finished_at=None,
    )
    db.add(stat)
    db.commit()


def restart_deploy_stat(db: Session, task: DeployTask) -> None:
    """失败任务重试时恢复为进行中。"""
    existing = db.query(DeployStat).filter(DeployStat.task_id == task.id).first()
    if not existing:
        start_deploy_stat(db, task)
        return

    existing.client_ip = task.client_ip
    existing.sites = sanitize_sites_for_stats(task)
    existing.status = DeployStatStatus.RUNNING.value
    existing.failed_phase = None
    existing.error_summary = None
    existing.finished_at = None
    db.commit()


def record_deploy_stat(db: Session, task: DeployTask, *, status: str | None = None) -> None:
    """任务终态时更新统计记录（若创建时未写入则补写终态快照）。"""
    stat_status = _resolve_stat_status(task, status)
    if not stat_status:
        return

    error_text = _truncate_error(task.error_message)
    finished_at = datetime.now(timezone.utc)
    existing = db.query(DeployStat).filter(DeployStat.task_id == task.id).first()

    if existing:
        existing.client_ip = task.client_ip
        existing.sites = sanitize_sites_for_stats(task)
        existing.status = stat_status
        existing.failed_phase = task.current_phase.value if stat_status != "success" else None
        existing.error_summary = error_text
        existing.finished_at = finished_at
        db.commit()
        return

    stat = DeployStat(
        task_id=task.id,
        client_ip=task.client_ip,
        sites=sanitize_sites_for_stats(task),
        status=stat_status,
        failed_phase=task.current_phase.value if stat_status != "success" else None,
        error_summary=error_text,
        created_at=_task_created_at(task),
        finished_at=finished_at,
    )
    db.add(stat)
    db.commit()
