"""LangGraph 멀티에이전트 파이프라인의 공유 State.

기획서(7개 에이전트: 오케스트레이터/사용자분석/데이터수집/사용량예측/매칭/
리포트/평가)가 그래프를 타고 지나가며 이 dict 하나를 읽고 채워나간다.
각 노드는 바뀐 필드만 돌려주면 LangGraph가 기존 State 위에 덮어써 준다.

지금은 Data Retrieval Agent가 쓰는 필드만 있다. 다음 에이전트를 만들 때마다
그 에이전트가 새로 채우는 필드를 여기 추가하면 된다.
"""
from typing import TypedDict


class AppState(TypedDict, total=False):
    # --- Data Retrieval Agent ---
    plans: list[dict]           # 최종 요금제 행 (schema.PLAN_COLUMNS 형태)
    benefits: list[dict]        # 최종 혜택 행 (schema.BENEFIT_COLUMNS 형태)
    data_refreshed: bool        # 이번 실행에서 실제로 재수집을 돌렸는지
    data_stale_aborted: bool    # 재수집을 돌렸으나 가드 위반으로 중단됐는지
    data_as_of: str             # 반환한 데이터의 기준 날짜 (YYYY-MM-DD)
    data_validation_errors: list[str]  # 구조 검증 실패 사유. 비어 있으면 통과
