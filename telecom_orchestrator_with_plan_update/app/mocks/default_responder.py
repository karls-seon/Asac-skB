from __future__ import annotations

from app.schemas.recommendation import RecommendationItem, ValidationResult
from app.schemas.user_profile import UserProfile


class TemplateResponder:
    """LLM Response Agent 교체 전까지 사용하는 결정론적 응답기."""

    def respond(
        self,
        *,
        user_query: str,
        profile: UserProfile | None,
        recommendations: list[RecommendationItem],
        validation: ValidationResult | None,
        followup_question: str | None = None,
    ) -> str:
        if followup_question:
            return followup_question

        if validation is not None and not validation.passed:
            return (
                "조건을 만족하는 추천 결과를 확정하지 못했습니다. "
                "예산이나 데이터 사용량 등 조건을 조금 완화해서 다시 요청해 주세요."
            )

        if not recommendations:
            return "조건에 맞는 요금제를 찾지 못했습니다."

        lines = ["추천 결과입니다."]

        for item in recommendations:
            p = item.plan
            fee = p.discounted_fee if p.discounted_fee is not None else p.monthly_fee
            fee_text = f"{fee:,}원" if fee is not None else "요금 정보 없음"

            if p.data_unlimited:
                data_text = "데이터 무제한"
            elif p.data_gb is not None:
                data_text = f"데이터 {p.data_gb:g}GB"
            else:
                data_text = "데이터 제공량 확인 필요"

            brand = p.mvno_brand or p.host_mno
            reasons = "; ".join(item.recommendation_reasons) or "사용자 조건 적합"

            lines.append(
                f"\n{item.rank}위. {p.plan_name} ({brand}/{p.host_mno}망)\n"
                f"- 요금: {fee_text}\n"
                f"- {data_text}\n"
                f"- 추천 점수: {item.score:.1f}\n"
                f"- 이유: {reasons}"
            )

            if p.discounted_fee is not None and p.monthly_fee is not None:
                if p.discounted_fee != p.monthly_fee:
                    period = (
                        f", 할인기간 {p.discount_period_months}개월"
                        if p.discount_period_months is not None
                        else ""
                    )
                    lines.append(
                        f"- 정상요금 {p.monthly_fee:,}원 / 할인요금 "
                        f"{p.discounted_fee:,}원{period}"
                    )

        return "\n".join(lines)
