"""
Plan Matching & Ranking Node

담당: (배정 예정)
입력: state['profile'], state['candidates']
출력: state['result'] = RecommendationResult(top_n, expected_monthly_cost)

TODO:
- Hard Filter: profile 조건(budget, voice/sms unlimited, 나이 제한 등)으로 후보 축소
- Cost Engine: Expected Effective Monthly Cost 계산
  (LogNormal(mu, sigma) 기반 E[bill] = base_fee + E[max(0, U - allowance) * overage_rate])
- Preference Score: preferred_benefits 매칭 가중치
- 위 셋을 결합해 top_n 산출

원문이 지시한 대로 **규칙 필터 + ML 스코어링 하이브리드**다. 둘 중 택일이 아니다.

비교는 `monthly_fee_normalized`(24개월 가중 평균)로 한다. 정가끼리 비교하면
"6개월 0원" 요금제가 실제보다 비싸 보인다.

추천 로직 본체는 그래프와 분리된 **순수 함수**로 두고 이 노드는 얇게 감싸기만 한다.
노트북 비교(3개 방식)와 웹이 같은 함수를 써야 결과가 갈리지 않는다.
"""

from ..schemas import GraphState


def matching_ranking_node(state: GraphState) -> GraphState:
    raise NotImplementedError("Hard Filter -> Cost Engine -> Preference Score 미구현")
