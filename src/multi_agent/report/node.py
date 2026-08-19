"""
Explanation & Report Node

담당: (배정 예정)
입력: state['result']
출력: state['report_text']

TODO:
- LLM 프롬프트로 자연어 리포트 생성
- 숫자(가격, 절감액 등)는 반드시 state['result']에서 그대로 주입할 것 -> LLM이 숫자를 새로 만들면 안 됨
- LLM은 "왜 이 요금제인지" 설명 문장 생성에만 사용
"""

from schemas import GraphState


def report_node(state: GraphState) -> GraphState:
    # TODO: LLM 프롬프트 기반 리포트 생성 로직으로 교체
    raise NotImplementedError("리포트 생성 미구현")

    state["report_text"] = report_text
    return state
