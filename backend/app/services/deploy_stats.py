from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.deploy import DeployStatus, DeployTask
from app.models.stats import DeployStat
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


def _resolve_stat_status(task: DeployTask, status: str | None) -> str | None:
    if status:
        return status
    if task.status == DeployStatus.SUCCESS:
        return "success"
    if task.status == DeployStatus.FAILED:
        return "failed"
    return None


def record_deploy_stat(db: Session, task: DeployTask, *, status: str | None = None) -> None:
    """任务终态时写入统计快照（幂等，不含敏感字段）。"""
    stat_status = _resolve_stat_status(task, status)
    if not stat_status:
        return

    existing = db.query(DeployStat).filter(DeployStat.task_id == task.id).first()
    if existing:
        return

    error_text = (task.error_message or "").strip()
    if error_text and len(error_text) > ERROR_SUMMARY_MAX:
        error_text = error_text[: ERROR_SUMMARY_MAX - 3] + "..."

    created_at = task.created_at
    if created_at and created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)

    stat = DeployStat(
        task_id=task.id,
        client_ip=task.client_ip,
        sites=sanitize_sites_for_stats(task),
        status=stat_status,
        failed_phase=task.current_phase.value if stat_status != "success" else None,
        error_summary=error_text or None,
        created_at=created_at,
        finished_at=datetime.now(timezone.utc),
    )
    db.add(stat)
    db.commit()
