from __future__ import annotations

from pathlib import Path

from app.mocks.default_intent import RuleBasedIntentClassifier
from app.mocks.default_plan_updater import ReloadOnlyPlanUpdater
from app.mocks.default_recommender import WeightedRuleRecommender
from app.mocks.default_responder import TemplateResponder
from app.mocks.default_user_analysis import RuleBasedUserAnalyzer
from app.mocks.default_validator import ConstraintValidator
from app.orchestrator.dependencies import AppDependencies, PlanUpdateDependencies
from app.orchestrator.update_workflow import build_plan_update_workflow
from app.orchestrator.workflow import build_workflow
from app.repositories.plan_repository import PlanRepository


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def create_repository() -> PlanRepository:
    return PlanRepository(
        plans_path=PROJECT_ROOT / "data" / "plans.csv",
        benefits_path=PROJECT_ROOT / "data" / "benefits.csv",
    )


def create_default_dependencies(repository: PlanRepository | None = None) -> AppDependencies:
    repository = repository or create_repository()

    return AppDependencies(
        repository=repository,
        intent_classifier=RuleBasedIntentClassifier(),
        user_analyzer=RuleBasedUserAnalyzer(),
        recommender=WeightedRuleRecommender(),
        validator=ConstraintValidator(),
        responder=TemplateResponder(),
    )


def create_default_update_dependencies(
    repository: PlanRepository | None = None,
) -> PlanUpdateDependencies:
    repository = repository or create_repository()

    return PlanUpdateDependencies(
        repository=repository,
        plan_updater=ReloadOnlyPlanUpdater(),
    )


def create_app():
    """사용자 추천 Workflow."""
    return build_workflow(create_default_dependencies())


def create_plan_update_app():
    """Scheduler 또는 관리자 수동 실행용 Plan Update Workflow."""
    return build_plan_update_workflow(create_default_update_dependencies())
