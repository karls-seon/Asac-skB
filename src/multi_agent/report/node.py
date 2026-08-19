"""
Explanation & Report Node

담당: (배정 예정)
입력: state['result'] (필요하면 state['profile'])
출력: state['report_text']

TODO:
- LLM으로 "왜 이 요금제인지" 설명 문장 생성
- 프롬프트는 이 폴더에 prompt.py로 분리

**숫자는 LLM이 만들지 않는다.** 가격·절감액·데이터양은 state['result']에 있는 값을
그대로 문장에 주입한다. LLM이 숫자를 새로 쓰면 추천 근거가 통째로 거짓이 된다.
"""

from ..schemas import GraphState


def report_node(state: GraphState) -> GraphState:
    raise NotImplementedError("리포트 생성 미구현")
