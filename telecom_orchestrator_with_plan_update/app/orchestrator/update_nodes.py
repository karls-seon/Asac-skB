from __future__ import annotations

from app.orchestrator.dependencies import PlanUpdateDependencies
from app.schemas.plan_update import PlanUpdateState


def make_update_nodes(deps: PlanUpdateDependencies):
    def plan_update_node(state: PlanUpdateState) -> dict:
        result = deps.plan_updater.refresh(
            repository=deps.repository,
            sources=state.get("requested_sources"),
        )

        return {
            "update_result": result,
            "update_succeeded": result.status == "SUCCESS",
            "update_errors": result.errors,
        }

    def update_response_node(state: PlanUpdateState) -> dict:
        result = state["update_result"]

        if result.status == "SUCCESS":
            message = (
                "요금제 데이터 갱신 Workflow가 완료되었습니다. "
                f"수집/확인 요금제 {result.crawled_plan_count}개, "
                f"신규 {result.inserted_count}개, 변경 {result.updated_count}개, "
                f"비활성화 {result.deactivated_count}개입니다."
            )
        else:
            message = (
                f"요금제 데이터 갱신 결과: {result.status}. "
                + (" / ".join(result.errors) if result.errors else "세부 오류를 확인해 주세요.")
            )

        return {"final_update_message": message}

    return {
        "plan_update": plan_update_node,
        "update_response": update_response_node,
    }
