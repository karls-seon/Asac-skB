"""
User Profiling Node

담당: (배정 예정)
입력: state['raw_input'] (chat) 또는 state['form_data'] (form)
출력: state['profile'] = UserProfile

TODO:
- input_type == "chat" 이면 LLM 슬롯 추출 (자유 텍스트 -> UserProfile)
- input_type == "form" 이면 form_data 딕셔너리를 그대로 UserProfile로 매핑
- 필수 필드 누락 시 profile을 None으로 두고 리턴 (그래프가 재질문으로 라우팅)
- 프롬프트는 이 폴더에 prompt.py로 분리한다

주의: 예산은 **월 납부액 숫자**만 받는다(요금제명 선택 아님, 선택 입력).
"""

from ..schemas import GraphState


def user_profiling_node(state: GraphState) -> GraphState:
    if state["input_type"] == "chat":
        raise NotImplementedError("chat 슬롯 추출 미구현")
    raise NotImplementedError("form 매핑 미구현")
