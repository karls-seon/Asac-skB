"""
User Profiling Node

담당: (배정 예정)
입력: state['raw_input'] (chat) 또는 state['form_data'] (form)
출력: state['profile']에 UserProfile 채워서 리턴

TODO:
- input_type == "chat" 이면 LLM 슬롯 추출 (채팅 자유 텍스트 -> UserProfile)
- input_type == "form" 이면 form_data 딕셔너리를 그대로 UserProfile로 매핑
- 필수 필드 누락 시 profile을 None으로 두고 리턴 (그래프가 재질문으로 라우팅)
"""

from schemas import GraphState, UserProfile


def user_profiling_node(state: GraphState) -> GraphState:
    if state["input_type"] == "chat":
        # TODO: LLM 기반 슬롯 추출 로직으로 교체
        raise NotImplementedError("chat 슬롯 추출 미구현")
    else:
        # TODO: form_data -> UserProfile 매핑 로직으로 교체
        raise NotImplementedError("form 매핑 미구현")

    state["profile"] = profile
    return state
