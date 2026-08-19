from __future__ import annotations

from app.schemas.state import Intent


class RuleBasedIntentClassifier:
    """LLM Intent Agent가 준비되기 전까지 사용하는 교체 가능한 기본 구현."""

    def classify(self, user_query: str) -> Intent:
        q = user_query.lower()

        if any(k in q for k in ["추천", "골라", "어떤 요금제", "맞는 요금제"]):
            return "PLAN_RECOMMENDATION"

        if any(k in q for k in ["비교", "차이"]):
            return "PLAN_COMPARE"

        if any(k in q for k in ["찾아", "조회", "얼마", "요금제 있어"]):
            return "PLAN_SEARCH"

        return "GENERAL_QA"
