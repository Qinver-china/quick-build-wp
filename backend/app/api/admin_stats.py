from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.models.stats import DeployStat
from app.schemas.stats import (
    StatsListItem,
    StatsListResponse,
    StatsPeriodSummary,
    StatsSiteItem,
    StatsSummaryResponse,
)

router = APIRouter(prefix="/api/admin/stats", tags=["admin-stats"])


def _require_admin_token(
    authorization: str | None = Header(default=None),
    x_admin_stats_token: str | None = Header(default=None, alias="X-Admin-Stats-Token"),
) -> None:
    expected = settings.admin_stats_token.strip()
    if not expected:
        raise HTTPException(status_code=503, detail="管理统计功能未配置 ADMIN_STATS_TOKEN")

    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    elif x_admin_stats_token:
        token = x_admin_stats_token.strip()

    if not token or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="无效的管理统计 Token")


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.stats_timezone)
    except Exception:
        return ZoneInfo("Asia/Shanghai")


def _period_bounds(now_local: datetime) -> dict[str, datetime]:
    today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=today_start.weekday())
    month_start = today_start.replace(day=1)
    return {"today": today_start, "week": week_start, "month": month_start}


def _to_utc(dt_local: datetime) -> datetime:
    return dt_local.astimezone(ZoneInfo("UTC"))


def _build_period_summary(db: Session, since_utc: datetime) -> StatsPeriodSummary:
    row = (
        db.query(
            func.count(DeployStat.id).label("total"),
            func.sum(case((DeployStat.status == "success", 1), else_=0)).label("success"),
            func.sum(
                case((DeployStat.status.in_(["failed", "cancelled"]), 1), else_=0)
            ).label("failed"),
        )
        .filter(DeployStat.finished_at >= since_utc)
        .one()
    )
    return StatsPeriodSummary(
        total=int(row.total or 0),
        success=int(row.success or 0),
        failed=int(row.failed or 0),
    )


@router.get("/summary", response_model=StatsSummaryResponse, dependencies=[Depends(_require_admin_token)])
def get_stats_summary(db: Session = Depends(get_db)):
    tz = _tz()
    now_local = datetime.now(tz)
    bounds = _period_bounds(now_local)

    return StatsSummaryResponse(
        today=_build_period_summary(db, _to_utc(bounds["today"])),
        week=_build_period_summary(db, _to_utc(bounds["week"])),
        month=_build_period_summary(db, _to_utc(bounds["month"])),
        timezone=settings.stats_timezone,
    )


@router.get("", response_model=StatsListResponse, dependencies=[Depends(_require_admin_token)])
def list_stats(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    query = db.query(DeployStat)
    if status:
        query = query.filter(DeployStat.status == status)

    total = query.count()
    pages = max(1, (total + page_size - 1) // page_size)
    offset = (page - 1) * page_size

    rows = (
        query.order_by(DeployStat.finished_at.desc())
        .offset(offset)
        .limit(page_size)
        .all()
    )

    items = [
        StatsListItem(
            id=row.id,
            task_id=row.task_id,
            client_ip=row.client_ip,
            sites=[StatsSiteItem(**site) for site in (row.sites or [])],
            status=row.status,
            failed_phase=row.failed_phase,
            error_summary=row.error_summary,
            created_at=row.created_at,
            finished_at=row.finished_at,
        )
        for row in rows
    ]

    return StatsListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        pages=pages,
    )
