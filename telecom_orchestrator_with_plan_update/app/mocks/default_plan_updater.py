from __future__ import annotations

from datetime import datetime, timezone

from app.repositories.plan_repository import PlanRepository
from app.schemas.plan_update import PlanUpdateResult


class ReloadOnlyPlanUpdater:
    """
    Orchestrator 통합 테스트용 Mock.

    실제 웹 크롤링/DB upsert를 수행하지 않는다.
    현재 data/plans.csv, data/benefits.csv를 다시 읽어서
    Plan Update Workflow 연결만 검증한다.

    최종 단계에서는 Plan Data 담당자의 실제 PlanUpdater 구현으로 교체한다.
    """

    def refresh(
        self,
        *,
        repository: PlanRepository,
        sources: list[str] | None = None,
    ) -> PlanUpdateResult:
        started_at = datetime.now(timezone.utc)

        repository.reload()

        finished_at = datetime.now(timezone.utc)

        return PlanUpdateResult(
            status="SUCCESS",
            sources=sources or ["LOCAL_SNAPSHOT_MOCK"],
            crawled_plan_count=len(repository.plans),
            crawled_benefit_count=len(repository.benefits),
            unchanged_count=len(repository.plans),
            started_at=started_at,
            finished_at=finished_at,
        )
