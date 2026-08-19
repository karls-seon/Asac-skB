from __future__ import annotations

from app.repositories.plan_repository import PlanRepository
from app.schemas.recommendation import RecommendationItem, ValidationIssue, ValidationResult
from app.schemas.user_profile import UserProfile


class ConstraintValidator:
    """
    추천 결과가 사용자 Hard Constraint 및 실제 DB와 일치하는지 검증.
    데이터 최신성 자체는 검증하지 않는다.
    """

    def validate(
        self,
        profile: UserProfile,
        recommendations: list[RecommendationItem],
        repository: PlanRepository,
    ) -> ValidationResult:
        errors: list[ValidationIssue] = []
        warnings: list[ValidationIssue] = []

        if not recommendations:
            errors.append(
                ValidationIssue(
                    field="recommendations",
                    code="NO_RECOMMENDATION",
                    message="추천 가능한 요금제가 없습니다.",
                )
            )
            return ValidationResult(passed=False, errors=errors)

        for item in recommendations:
            plan = item.plan

            if not repository.exists(plan.plan_id):
                errors.append(
                    ValidationIssue(
                        plan_id=plan.plan_id,
                        field="plan_id",
                        code="PLAN_NOT_FOUND",
                        message="추천 요금제가 실제 요금제 DB에 존재하지 않습니다.",
                    )
                )
                continue

            # DB truth를 다시 조회해 agent hallucination / stale object를 방지.
            db_plan = repository.get_by_plan_id(plan.plan_id)
            if db_plan is None:
                continue

            fee = (
                db_plan.discounted_fee
                if db_plan.discounted_fee is not None
                else db_plan.monthly_fee
            )

            if profile.budget_krw is not None:
                if fee is None or fee > profile.budget_krw:
                    errors.append(
                        ValidationIssue(
                            plan_id=plan.plan_id,
                            field="budget_krw",
                            code="BUDGET_EXCEEDED",
                            message=f"사용자 최대 예산 {profile.budget_krw:,}원을 초과합니다.",
                        )
                    )

            if profile.preferred_carrier is not None:
                if db_plan.host_mno != profile.preferred_carrier:
                    errors.append(
                        ValidationIssue(
                            plan_id=plan.plan_id,
                            field="preferred_carrier",
                            code="CARRIER_MISMATCH",
                            message="사용자가 요청한 통신망과 일치하지 않습니다.",
                        )
                    )

            if profile.carrier_type is not None:
                if db_plan.carrier_type != profile.carrier_type:
                    errors.append(
                        ValidationIssue(
                            plan_id=plan.plan_id,
                            field="carrier_type",
                            code="CARRIER_TYPE_MISMATCH",
                            message="사용자가 요청한 MNO/MVNO 유형과 일치하지 않습니다.",
                        )
                    )

            if profile.network_gen is not None:
                if db_plan.network_gen != profile.network_gen:
                    errors.append(
                        ValidationIssue(
                            plan_id=plan.plan_id,
                            field="network_gen",
                            code="NETWORK_GEN_MISMATCH",
                            message="사용자가 요청한 LTE/5G 조건과 일치하지 않습니다.",
                        )
                    )

            if profile.monthly_data_gb is not None:
                data_ok = db_plan.data_unlimited or (
                    db_plan.data_gb is not None
                    and db_plan.data_gb >= profile.monthly_data_gb
                )
                if not data_ok:
                    errors.append(
                        ValidationIssue(
                            plan_id=plan.plan_id,
                            field="monthly_data_gb",
                            code="DATA_SHORTAGE",
                            message="사용자의 월 데이터 요구량을 충족하지 못합니다.",
                        )
                    )

            if profile.min_qos_mbps is not None:
                # 완전 무제한 또는 요구 QoS 이상이면 통과
                qos_ok = db_plan.data_unlimited or (
                    db_plan.qos_mbps is not None
                    and db_plan.qos_mbps >= profile.min_qos_mbps
                )
                if not qos_ok:
                    errors.append(
                        ValidationIssue(
                            plan_id=plan.plan_id,
                            field="min_qos_mbps",
                            code="QOS_TOO_LOW",
                            message="요청한 최소 QoS 속도를 충족하지 못합니다.",
                        )
                    )

            if profile.voice_unlimited is True and not db_plan.voice_unlimited:
                errors.append(
                    ValidationIssue(
                        plan_id=plan.plan_id,
                        field="voice_unlimited",
                        code="VOICE_NOT_UNLIMITED",
                        message="통화 무제한 조건을 충족하지 못합니다.",
                    )
                )

            if profile.sms_unlimited is True and not db_plan.sms_unlimited:
                errors.append(
                    ValidationIssue(
                        plan_id=plan.plan_id,
                        field="sms_unlimited",
                        code="SMS_NOT_UNLIMITED",
                        message="문자 무제한 조건을 충족하지 못합니다.",
                    )
                )

        return ValidationResult(
            passed=(len(errors) == 0),
            errors=errors,
            warnings=warnings,
        )
