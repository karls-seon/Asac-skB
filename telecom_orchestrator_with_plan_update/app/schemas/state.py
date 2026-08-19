from __future__ import annotations

from typing import Literal, TypedDict

from app.schemas.recommendation import CandidatePlan, RecommendationItem, ValidationIssue
from app.schemas.user_profile import UserProfile


Intent = Literal[
    "PLAN_RECOMMENDATION",
    "PLAN_SEARCH",
    "PLAN_COMPARE",
    "GENERAL_QA",
]


class TelecomState(TypedDict, total=False):
    # Raw request
    user_query: str

    # Orchestrator routing
    intent: Intent

    # User Analysis Agent output
    user_profile: UserProfile
    missing_fields: list[str]
    needs_user_input: bool
    followup_question: str | None

    # Plan Data Search output
    candidate_plans: list[CandidatePlan]

    # Recommendation Agent output
    recommendations: list[RecommendationItem]

    # Validation Agent output
    validation_passed: bool
    validation_errors: list[ValidationIssue]
    validation_warnings: list[ValidationIssue]

    # Retry / workflow control
    retry_count: int
    max_retry: int

    # Response Agent output
    final_response: str
