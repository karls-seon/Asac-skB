"""    cd src && python -m multi_agent.matching.test_matching

예산 3만원 / 데이터 15GB 사용자에게 예산을 넘는 요금제를 1등으로 주지 않는지 본다.
구현 전에는 스텁만 확인한다.
"""
import sys

from ..schemas import GraphState, PlanCandidate, RecommendationResult, UserProfile
from .node import matching_ranking_node


def _plan(plan_id: str, fee: int, data_gb: float) -> PlanCandidate:
    return PlanCandidate(
        plan_id=plan_id, brand="테스트", legal_entity=None, host_mno="KT",
        monthly_fee_original=fee, monthly_fee_promo=None, discount_period_months=None,
        monthly_fee_normalized=float(fee),
        data_gb=data_gb, data_unlimited=False, qos_speed_mbps=1.0,
        voice_minutes=None, voice_unlimited=True, sms_count=None, sms_unlimited=True,
        age_condition=None, is_online_only=False, contract_months=0,
        benefits=[], dominated_by=None,
    )


def _state() -> GraphState:
    profile = UserProfile(
        budget_krw=30000, monthly_data_gb=15, min_qos_mbps=None,
        voice_minutes=None, voice_unlimited=True, sms_count=None, sms_unlimited=True,
        age=30, preferred_carrier=None, preferred_benefits=[], current_plan_fee=30000,
    )
    return {
        "input_type": "form", "raw_input": None, "form_data": None, "profile": profile,
        "candidates": [_plan("cheap", 22000, 20), _plan("over", 45000, 100),
                       _plan("too_small", 15000, 5)],
        "result": None, "report_text": None,
    }


def main() -> None:
    try:
        out = matching_ranking_node(_state())
    except NotImplementedError as e:
        print(f"아직 스텁: {e}")
        return

    result = out["result"]
    assert isinstance(result, RecommendationResult), "state['result']에 RecommendationResult"
    assert result.top_n, "후보가 있는데 결과가 비었다"
    top = result.top_n[0]
    assert top.monthly_fee_normalized <= 30000, f"예산 초과 요금제를 1등으로 줌: {top.plan_id}"
    assert (top.data_gb or 0) >= 15, f"필요 데이터보다 적은 요금제를 1등으로 줌: {top.plan_id}"
    assert set(result.expected_monthly_cost) >= {p.plan_id for p in result.top_n}, (
        "top_n에 있는 요금제는 expected_monthly_cost에도 있어야 한다"
    )
    print(f"matching 계약 OK (1등: {top.plan_id})")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
