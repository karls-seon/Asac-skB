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
    # 사이트 구조가 바뀌어 파싱이 조용히 실패한 흔적 (schema_drift.py).
    # 비어 있으면 정상 - 뒤쪽 에이전트는 이 값이 차 있을 때만 신경 쓰면 된다.
    drift_report_path: str      # 진단 리포트 경로. 회귀가 없으면 빈 문자열
    drift_regressed: dict       # {사이트: [파싱 안 된 소스 키]}

    # --- 실시간 추천 그래프 (live_graph.py) ---
    # 배치 파이프라인과 State 스키마는 공유하지만 그래프는 분리한다.
    # 트리거가 다르기 때문이다(스케줄러 vs 사용자 요청).
    user_input_raw: str         # 사용자가 입력한 자연어 그대로
    user_profile: dict          # User Profiling이 뽑은 슬롯
    profiling_complete: bool    # 추천을 시작할 만큼 슬롯이 찼는지
    profiling_questions: list[str]  # 아직 못 받은 슬롯을 묻는 문장
    match_result: dict          # Plan Matching 산출물(scoring_agent.match)
    matching_retry_count: int   # 조건 완화 재시도 횟수
    relaxed_slots: list[str]    # 자동으로 푼 조건. 리포트에 반드시 밝힌다
    report_text: str            # 최종 답변
