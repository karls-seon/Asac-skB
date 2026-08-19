from __future__ import annotations

from dataclasses import dataclass

from app.orchestrator.interfaces import (
    IntentClassifier,
    PlanUpdater,
    Recommender,
    Responder,
    UserAnalyzer,
    Validator,
)
from app.repositories.plan_repository import PlanRepository


@dataclass(frozen=True)
class AppDependencies:
    """사용자 추천 Workflow의 의존성."""

    repository: PlanRepository
    intent_classifier: IntentClassifier
    user_analyzer: UserAnalyzer
    recommender: Recommender
    validator: Validator
    responder: Responder


@dataclass(frozen=True)
class PlanUpdateDependencies:
    """요금제 자동 갱신 Workflow의 의존성."""

    repository: PlanRepository
    plan_updater: PlanUpdater
