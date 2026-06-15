from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class StatsSiteItem(BaseModel):
    site_name: str = ""
    primary_domain: str = ""
    domains: list[str] = Field(default_factory=list)


class StatsPeriodSummary(BaseModel):
    total: int = 0
    success: int = 0
    failed: int = 0


class StatsSummaryResponse(BaseModel):
    today: StatsPeriodSummary
    week: StatsPeriodSummary
    month: StatsPeriodSummary
    all_time: StatsPeriodSummary
    timezone: str


class StatsListItem(BaseModel):
    id: str
    task_id: str
    client_ip: str | None
    sites: list[StatsSiteItem]
    status: Literal["running", "success", "failed", "cancelled"]
    failed_phase: str | None
    error_summary: str | None
    created_at: datetime
    finished_at: datetime | None


class StatsListResponse(BaseModel):
    items: list[StatsListItem]
    total: int
    page: int
    page_size: int
    pages: int
