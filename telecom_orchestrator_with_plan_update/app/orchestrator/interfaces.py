from __future__ import annotations

from typing import Protocol

from app.repositories.plan_repository import PlanRepository
from app.schemas.plan_update import PlanUpdateResult
from app.schemas.recommendation import CandidatePlan, RecommendationItem, ValidationResult
from app.schemas.state import Intent
from app.schemas.user_profile import UserProfile


class IntentClassifier(Protocol):
    def classify(self, user_query: str) -> Intent: ...


class UserAnalyzer(Protocol):
    def analyze(self, user_query: str) -> tuple[UserProfile, list[str], str | None]: ...


class Recommender(Protocol):
    def recommend(
        self,
        profile: UserProfile,
        candidates: list[CandidatePlan],
        *,
        validation_errors: list[dict] | None = None,
        top_n: int = 3,
    ) -> list[RecommendationItem]: ...


class Validator(Protocol):
    def validate(
        self,
        profile: UserProfile,
        recommendations: list[RecommendationItem],
        repository: PlanRepository,
    ) -> ValidationResult: ...


class Responder(Protocol):
    def respond(
        self,
        *,
        user_query: str,
        profile: UserProfile | None,
        recommendations: list[RecommendationItem],
        validation: ValidationResult | None,
        followup_question: str | None = None,
    ) -> str: ...


class PlanUpdater(Protocol):
    """
    Plan Data 담당자가 구현해야 하는 요금제 갱신 계약.

    내부에서 Selenium/API/정적 HTML 등 어떤 수집 방식을 쓰는지는
    Orchestrator가 알 필요가 없다. 이 반환 형식만 지키면 된다.
    """

    def refresh(
        self,
        *,
        repository: PlanRepository,
        sources: list[str] | None = None,
    ) -> PlanUpdateResult: ...
