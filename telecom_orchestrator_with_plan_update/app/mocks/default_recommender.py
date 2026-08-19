from __future__ import annotations

from app.schemas.recommendation import CandidatePlan, RecommendationItem
from app.schemas.user_profile import UserProfile


class WeightedRuleRecommender:
    """
    통합 테스트용 baseline 추천기.
    팀 추천 알고리즘 담당자가 동일 인터페이스로 교체할 수 있다.
    """

    def recommend(
        self,
        profile: UserProfile,
        candidates: list[CandidatePlan],
        *,
        validation_errors: list[dict] | None = None,
        top_n: int = 3,
    ) -> list[RecommendationItem]:
        invalid_ids = {
            str(e.get("plan_id"))
            for e in (validation_errors or [])
            if e.get("plan_id")
        }

        scored: list[tuple[float, CandidatePlan, list[str], list[str]]] = []

        for plan in candidates:
            if plan.plan_id in invalid_ids:
                continue

            score = 0.0
            reasons: list[str] = []
            matched_benefits: list[str] = []

            # 24개월 정규화 요금 우선. 없으면 할인가 -> 정가 순.
            fee = plan.monthly_fee_normalized
            if fee is None:
                fee = plan.discounted_fee if plan.discounted_fee is not None else plan.monthly_fee

            # 1) Price fitness
            if fee is not None and profile.budget_krw:
                ratio = fee / max(profile.budget_krw, 1)
                price_score = max(0.0, 1.0 - ratio) * 35
                score += price_score
                reasons.append(f"예산 {profile.budget_krw:,}원 이내")

            # 2) Data fitness
            if profile.monthly_data_gb is not None:
                if plan.data_unlimited:
                    score += 35
                    reasons.append("데이터 무제한")
                elif plan.data_gb is not None:
                    ratio = min(plan.data_gb / max(profile.monthly_data_gb, 0.1), 2.0)
                    score += min(ratio, 1.0) * 30
                    # 지나친 overprovision은 살짝 감점
                    if ratio > 1.5:
                        score -= min((ratio - 1.5) * 4, 4)
                    reasons.append(f"월 데이터 {plan.data_gb:g}GB 제공")
            else:
                if plan.data_unlimited:
                    score += 12
                elif plan.data_gb is not None:
                    score += min(plan.data_gb, 100) / 100 * 12

            # 3) QoS
            if profile.min_qos_mbps is not None:
                if plan.qos_mbps is not None and plan.qos_mbps >= profile.min_qos_mbps:
                    score += 10
                    reasons.append(f"QoS {plan.qos_mbps:g}Mbps")

            # 4) Preferred benefits = soft score
            if profile.preferred_benefits:
                searchable = " | ".join(
                    [*plan.benefit_services, *plan.benefit_categories]
                ).lower()
                for benefit in profile.preferred_benefits:
                    if benefit.lower() in searchable:
                        matched_benefits.append(benefit)
                score += min(len(matched_benefits) * 8, 16)
                if matched_benefits:
                    reasons.append("선호 혜택: " + ", ".join(matched_benefits))

            # 5) Voice/SMS preference
            if profile.voice_unlimited is True and plan.voice_unlimited:
                score += 5
                reasons.append("통화 무제한")
            if profile.sms_unlimited is True and plan.sms_unlimited:
                score += 3
                reasons.append("문자 무제한")

            # 6) Small tie-breaker: lower fee
            if fee is not None:
                score += max(0, 2 - fee / 100000)

            scored.append((score, plan, matched_benefits, reasons))

        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:top_n]

        return [
            RecommendationItem(
                plan=plan,
                score=round(score, 3),
                rank=rank,
                matched_benefits=matched,
                recommendation_reasons=reasons[:5],
            )
            for rank, (score, plan, matched, reasons) in enumerate(top, start=1)
        ]
