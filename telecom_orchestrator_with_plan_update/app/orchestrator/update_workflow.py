from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.orchestrator.dependencies import PlanUpdateDependencies
from app.orchestrator.update_nodes import make_update_nodes
from app.schemas.plan_update import PlanUpdateState


def build_plan_update_workflow(deps: PlanUpdateDependencies):
    """
    추천 요청과 독립적으로 실행되는 데이터 갱신 Workflow.

    Scheduler / 관리자 수동 실행
        -> Plan Update Agent
        -> Repository reload
        -> 결과 기록
        -> END

    실제 크롤링/정규화/DB upsert는 PlanUpdater 구현체 내부의 책임이다.
    """
    nodes = make_update_nodes(deps)

    builder = StateGraph(PlanUpdateState)
    builder.add_node("plan_update", nodes["plan_update"])
    builder.add_node("update_response", nodes["update_response"])

    builder.add_edge(START, "plan_update")
    builder.add_edge("plan_update", "update_response")
    builder.add_edge("update_response", END)

    return builder.compile()
