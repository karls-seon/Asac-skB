from __future__ import annotations

from typing import Literal

from app.schemas.state import TelecomState


def route_after_intent(
    state: TelecomState,
) -> Literal["user_analysis", "response"]:
    """
    현재 프로젝트 핵심은 추천 흐름.
    Search/Compare/General QA는 향후 전용 노드가 준비되면 확장 가능하다.
    """
    if state["intent"] == "PLAN_RECOMMENDATION":
        return "user_analysis"

    # 아직 전용 QA/Search Agent가 없으므로 현재는 response로 종료.
    # 팀 기능 추가 시 이 router mapping만 확장하면 된다.
    return "response"


def route_after_analysis(
    state: TelecomState,
) -> Literal["plan_search", "response"]:
    if state.get("needs_user_input", False):
        return "response"
    return "plan_search"


def route_after_validation(
    state: TelecomState,
) -> Literal["success", "retry", "failed"]:
    if state.get("validation_passed", False):
        return "success"

    retry_count = state.get("retry_count", 0)
    max_retry = state.get("max_retry", 2)

    if retry_count < max_retry:
        return "retry"

    return "failed"
