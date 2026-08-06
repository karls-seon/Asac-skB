"""LangGraph 그래프가 공유하는 State.

PoC 범위(2026-08-06)는 네 개다: Orchestrator / User Profiling /
Data Retrieval / Plan Matching. 각 노드는 자기가 바꾼 필드만 돌려주면
LangGraph가 기존 State 위에 덮어써 준다.

**제외된 에이전트의 필드는 두지 않는다.** 남겨 두면 "누가 채우는지 모르는
빈 칸"이 생기고, 나중에 그 칸을 보고 이미 구현된 줄 오해한다.
  - report_text        -> Explanation & Report (제외)
  - predicted_segment  -> Usage Prediction (제외)
  - eval_passed        -> Evaluation (제외)
  - drift_*            -> 수집 파이프라인 진단 (_archive/pipeline/)
  - data_refreshed / data_stale_aborted -> 재수집을 안 하므로 의미 없음
"""
from typing import TypedDict


class AppState(TypedDict, total=False):
    # --- Orchestrator가 넣는 입력 ---
    user_input_raw: str         # 사용자가 입력한 자연어 그대로

    # --- ① User Profiling ---
    user_profile: dict          # 뽑아낸 슬롯 (scoring_agent가 읽는 형태)
    profiling_complete: bool    # 추천을 시작할 만큼 슬롯이 찼는지
    profiling_questions: list[str]  # 아직 못 받은 슬롯을 묻는 문장

    # --- ② Data Retrieval (읽기 전용) ---
    plans: list[dict]           # 최종 요금제 행 (schema.PLAN_COLUMNS 형태)
    benefits: list[dict]        # 최종 혜택 행 (schema.BENEFIT_COLUMNS 형태)
    data_as_of: str             # 데이터 기준 날짜 (YYYY-MM-DD). 없으면 빈 문자열
    data_validation_errors: list[str]  # 구조 검증 실패 사유. 비어 있으면 통과

    # --- ③ Plan Matching & Ranking ---
    match_result: dict          # scoring_agent.match_from_rows 산출물
    matching_retry_count: int   # 조건 완화 재시도 횟수
    relaxed_slots: list[str]    # 자동으로 푼 조건. 결과에 반드시 밝힌다
