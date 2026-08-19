"""    cd src && python -m multi_agent.profiling.test_profiling

구현 전에는 "아직 스텁"만 확인하고, 구현하는 순간부터 계약을 검사한다.
"""
import sys

from ..schemas import GraphState, UserProfile
from .node import user_profiling_node


def _state(**over) -> GraphState:
    base: GraphState = {
        "input_type": "chat",
        "raw_input": "한달에 15기가 쓰고 통화는 거의 안 해요. 3만원 안쪽이면 좋겠어요.",
        "form_data": None,
        "profile": None,
        "candidates": [],
        "result": None,
        "report_text": None,
    }
    return {**base, **over}


def main() -> None:
    try:
        out = user_profiling_node(_state())
    except NotImplementedError as e:
        print(f"아직 스텁: {e}")
        return

    assert isinstance(out["profile"], UserProfile), "state['profile']에 UserProfile을 넣어야 한다"
    p = out["profile"]
    assert p.monthly_data_gb == 15, f"데이터 사용량 추출 실패: {p.monthly_data_gb}"
    assert p.budget_krw == 30000, f"예산 추출 실패: {p.budget_krw}"
    # 안 적힌 값을 지어내면 그 숫자가 그대로 추천 근거가 된다
    assert p.age is None, "입력에 없는 값은 None이어야 한다"
    print("profiling 계약 OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
