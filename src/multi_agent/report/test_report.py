"""    cd src && python -m multi_agent.report.test_report

리포트에 나온 금액이 result에 있는 값인지 본다(LLM이 숫자를 지어내는지 검사).
구현 전에는 스텁만 확인한다.
"""
import re
import sys

from ..schemas import GraphState, PlanCandidate, RecommendationResult
from .node import report_node


def _state() -> GraphState:
    plan = PlanCandidate(
        plan_id="37140", brand="KT엠모바일", legal_entity=None, host_mno="KT",
        monthly_fee_original=16400, monthly_fee_promo=None, discount_period_months=None,
        monthly_fee_normalized=16400.0,
        data_gb=20.0, data_unlimited=False, qos_speed_mbps=0.4,
        voice_minutes=None, voice_unlimited=True, sms_count=None, sms_unlimited=True,
        age_condition=None, is_online_only=False, contract_months=0,
        benefits=["OTT"], dominated_by=None,
    )
    result = RecommendationResult(
        top_n=[plan], expected_monthly_cost={"37140": 16400.0}, segment_label=None,
    )
    return {
        "input_type": "form", "raw_input": None, "form_data": None, "profile": None,
        "candidates": [plan], "result": result, "report_text": None,
    }


def main() -> None:
    state = _state()
    try:
        out = report_node(state)
    except NotImplementedError as e:
        print(f"아직 스텁: {e}")
        return

    text = out["report_text"]
    assert isinstance(text, str) and text.strip(), "state['report_text']가 비었다"

    allowed = {"16400", "16,400", "20", "37140"}
    found = set(re.findall(r"\d[\d,]*", text))
    invented = found - allowed
    assert not invented, f"result에 없는 숫자를 지어냈다: {sorted(invented)}"
    print("report 계약 OK (숫자 지어내기 없음)")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
