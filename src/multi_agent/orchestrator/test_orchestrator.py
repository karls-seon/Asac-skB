"""배선 + CSV -> PlanCandidate 매핑 점검.

    cd src && python -m multi_agent.orchestrator.test_orchestrator

노드가 전부 스텁이라 end-to-end 결과는 아직 없다. 대신 **그래프가 첫 노드까지
닿는지**와 **담당자 없는 retrieval_step이 실제 CSV를 계약대로 옮기는지**를 본다.
"""
import sys

from ..schemas import PlanCandidate
from .graph import app
from .retrieval_step import _qos_mbps, retrieval_step, to_candidate


def _row(**over) -> dict:
    base = {
        "plan_id": "x", "host_mno": "KT", "mvno_brand": "테스트", "monthly_fee": "30000",
        "discounted_fee": "30000", "discount_period_months": "", "data_gb": "10",
        "data_unlimited": "False", "data_throttle_speed": "1Mbps", "voice_minutes": "",
        "voice_unlimited": "True", "sms_count": "", "sms_unlimited": "True",
        "age_condition": "", "is_online_only": "False", "ott_option_count": "0",
        "membership_grade": "", "tethering_support": "unsupported",
    }
    return {**base, **over}


def main() -> None:
    assert _qos_mbps("400Kbps") == 0.4 and _qos_mbps("3Mbps") == 3.0
    assert _qos_mbps("") is None, "소진 후 차단이면 None이어야 한다"

    # 24개월 가중 평균: 6개월 0원 + 18개월 30,000원 = 22,500원
    promo = to_candidate(_row(discounted_fee="0", discount_period_months="6"))
    assert promo.monthly_fee_normalized == 22500.0, promo.monthly_fee_normalized
    # 할인 기간이 24개월을 넘어도 24개월로 자른다(그 이상은 비교 기준이 아니다)
    long_promo = to_candidate(_row(discounted_fee="0", discount_period_months="36"))
    assert long_promo.monthly_fee_normalized == 0.0

    plain = to_candidate(_row())
    assert plain.monthly_fee_promo is None, "할인가와 정가가 같으면 프로모션이 아니다"
    assert plain.benefits == []
    assert to_candidate(_row(ott_option_count="3", membership_grade="VIP",
                             tethering_support="quota")).benefits == ["OTT", "membership", "tethering"]

    # 실제 최종 CSV 전체를 옮겨 본다 - 컬럼이 바뀌면 여기서 터진다
    state = retrieval_step({"input_type": "form", "raw_input": None, "form_data": None,
                            "profile": None, "candidates": [], "result": None,
                            "report_text": None})
    cands = state["candidates"]
    assert len(cands) > 2000, f"후보가 너무 적다: {len(cands)}건"
    assert all(isinstance(c, PlanCandidate) for c in cands)
    assert all(c.monthly_fee_normalized > 0 for c in cands), "요금 0원은 파싱 실패 신호"
    print(f"retrieval_step OK - {len(cands)}건 매핑")

    # 그래프가 컴파일되고 첫 노드까지 닿는지. 스텁이라 거기서 멈추는 게 정상이다.
    try:
        app.invoke({"input_type": "chat", "raw_input": "테스트", "form_data": None,
                    "profile": None, "candidates": [], "result": None, "report_text": None})
        raise AssertionError("노드가 전부 스텁인데 그래프가 끝까지 돌았다")
    except NotImplementedError as e:
        print(f"배선 OK - profiling 노드까지 도달({e})")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
