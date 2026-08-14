"""
Plan Matching & Ranking Node

담당: (배정 예정)
입력: state['profile'], state['candidates']
출력: state['result'] (top_n, expected_monthly_cost) 채워서 리턴

TODO:
- Hard Filter: profile 조건(budget, voice/sms unlimited 등)으로 후보군 걸러내기
- Cost Engine: Expected Effective Monthly Cost 계산
  (LogNormal(mu, sigma) 기반 E[bill] = base_fee + E[max(0, U - allowance) * overage_rate])
- Preference Score: preferred_benefits 매칭 가중치 반영
- 위 세 단계 결합해서 top_n 산출
"""

from schemas import GraphState


def matching_ranking_node(state: GraphState) -> GraphState:
    # TODO: Hard Filter -> Cost Engine -> Preference Score 로직으로 교체
    raise NotImplementedError("매칭/랭킹 로직 미구현")

    state["result"] = result
    return state
