"""
Data Retrieval Node

담당: (배정 예정)
입력: state['profile']
출력: state['candidates']에 PlanCandidate 리스트 채워서 리턴

TODO:
- SQLite DB에서 요금제 조회 (크롤링된 2,826개 요금제)
- profile.preferred_carrier, age 등으로 1차 필터링해서 후보군 축소
- host_mno / age_condition / is_online_only 그룹 키 활용 가능
"""

from schemas import GraphState


def data_retrieval_node(state: GraphState) -> GraphState:
    # TODO: DB 조회 로직으로 교체
    raise NotImplementedError("DB 조회 미구현")

    state["candidates"] = candidates
    return state
