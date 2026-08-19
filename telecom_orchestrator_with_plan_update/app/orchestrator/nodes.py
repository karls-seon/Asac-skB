from __future__ import annotations

from app.schemas.recommendation import ValidationResult
from app.schemas.state import TelecomState
from app.orchestrator.dependencies import AppDependencies


def make_nodes(deps: AppDependencies):
    """Dependencies를 closure로 주입해 테스트/교체가 쉬운 노드 세트를 생성."""

    def intent_node(state: TelecomState) -> dict:
        intent = deps.intent_classifier.classify(state["user_query"])
        return {"intent": intent}

    def user_analysis_node(state: TelecomState) -> dict:
        profile, missing_fields, followup = deps.user_analyzer.analyze(
            state["user_query"]
        )
        return {
            "user_profile": profile,
            "missing_fields": missing_fields,
            "needs_user_input": bool(missing_fields),
            "followup_question": followup,
        }

    def plan_search_node(state: TelecomState) -> dict:
        profile = state.get("user_profile")
        if profile is None:
            # PLAN_SEARCH 등 간단한 루트에서 아직 분석을 안 했다면 분석 수행.
            profile, missing_fields, followup = deps.user_analyzer.analyze(
                state["user_query"]
            )
        else:
            missing_fields = state.get("missing_fields", [])
            followup = state.get("followup_question")

        # 이전 validation에서 실패한 plan_id를 후보 검색에서 제외.
        invalid_ids = {
            issue.plan_id
            for issue in state.get("validation_errors", [])
            if issue.plan_id
        }

        candidates = deps.repository.find_candidates(
            profile,
            exclude_plan_ids=invalid_ids,
        )

        return {
            "user_profile": profile,
            "missing_fields": missing_fields,
            "followup_question": followup,
            "candidate_plans": candidates,
        }

    def recommendation_node(state: TelecomState) -> dict:
        validation_errors = [
            issue.model_dump()
            for issue in state.get("validation_errors", [])
        ]

        recommendations = deps.recommender.recommend(
            profile=state["user_profile"],
            candidates=state.get("candidate_plans", []),
            validation_errors=validation_errors,
            top_n=3,
        )

        # 첫 추천은 retry가 아니다. validation 실패 후 재진입할 때만 증가시키고 싶지만
        # 상태에 별도 flag를 늘리기보다 이전 validation error 존재 여부로 판단.
        retry_count = state.get("retry_count", 0)
        if validation_errors:
            retry_count += 1

        return {
            "recommendations": recommendations,
            "retry_count": retry_count,
        }

    def validation_node(state: TelecomState) -> dict:
        result = deps.validator.validate(
            profile=state["user_profile"],
            recommendations=state.get("recommendations", []),
            repository=deps.repository,
        )
        return {
            "validation_passed": result.passed,
            "validation_errors": result.errors,
            "validation_warnings": result.warnings,
        }

    def response_node(state: TelecomState) -> dict:
        validation = None
        if "validation_passed" in state:
            validation = ValidationResult(
                passed=state.get("validation_passed", False),
                errors=state.get("validation_errors", []),
                warnings=state.get("validation_warnings", []),
            )

        response = deps.responder.respond(
            user_query=state["user_query"],
            profile=state.get("user_profile"),
            recommendations=state.get("recommendations", []),
            validation=validation,
            followup_question=state.get("followup_question")
            if state.get("needs_user_input")
            else None,
        )
        return {"final_response": response}

    return {
        "intent": intent_node,
        "user_analysis": user_analysis_node,
        "plan_search": plan_search_node,
        "recommendation": recommendation_node,
        "validation": validation_node,
        "response": response_node,
    }
