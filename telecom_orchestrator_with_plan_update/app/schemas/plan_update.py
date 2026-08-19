from __future__ import annotations

from datetime import datetime
from typing import Literal, TypedDict

from pydantic import BaseModel, Field


UpdateTrigger = Literal["SCHEDULED", "MANUAL"]
UpdateStatus = Literal["SUCCESS", "PARTIAL", "FAILED"]


class PlanUpdateResult(BaseModel):
    """Plan Update Agent가 Orchestrator에 반환해야 하는 표준 결과."""

    status: UpdateStatus
    sources: list[str] = Field(default_factory=list)

    crawled_plan_count: int = Field(default=0, ge=0)
    crawled_benefit_count: int = Field(default=0, ge=0)

    inserted_count: int = Field(default=0, ge=0)
    updated_count: int = Field(default=0, ge=0)
    deactivated_count: int = Field(default=0, ge=0)
    unchanged_count: int = Field(default=0, ge=0)

    errors: list[str] = Field(default_factory=list)
    started_at: datetime
    finished_at: datetime


class PlanUpdateState(TypedDict, total=False):
    """추천 Workflow와 분리된 요금제 갱신 Workflow 전용 State."""

    trigger: UpdateTrigger
    requested_sources: list[str]

    update_result: PlanUpdateResult
    update_succeeded: bool
    update_errors: list[str]

    final_update_message: str
