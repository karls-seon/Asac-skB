from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.orchestrator.nodes import make_nodes
from app.orchestrator.routers import (
    route_after_analysis,
    route_after_intent,
    route_after_validation,
)
from app.schemas.state import TelecomState
from app.orchestrator.dependencies import AppDependencies


def build_workflow(deps: AppDependencies):
    """
    TelecomState 기반 Orchestrator Graph.

    추천 핵심 경로:
    START
      -> intent
      -> user_analysis
      -> plan_search
      -> recommendation
      -> validation
          PASS -> response -> END
          FAIL -> plan_search -> recommendation -> validation
          retry limit -> response -> END
    """
    nodes = make_nodes(deps)

    builder = StateGraph(TelecomState)

    for name, node in nodes.items():
        builder.add_node(name, node)

    builder.add_edge(START, "intent")

    builder.add_conditional_edges(
        "intent",
        route_after_intent,
        {
            "user_analysis": "user_analysis",
            "response": "response",
        },
    )

    builder.add_conditional_edges(
        "user_analysis",
        route_after_analysis,
        {
            "plan_search": "plan_search",
            "response": "response",
        },
    )

    builder.add_edge("plan_search", "recommendation")
    builder.add_edge("recommendation", "validation")

    builder.add_conditional_edges(
        "validation",
        route_after_validation,
        {
            "success": "response",
            # validation 실패 시 candidate 재검색부터 수행하여 실패 plan을 제외
            "retry": "plan_search",
            "failed": "response",
        },
    )

    builder.add_edge("response", END)

    return builder.compile()
