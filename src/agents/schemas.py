"""
데이터 계약 (Data Contracts)

이 파일은 오케스트레이터(그래프 조립 담당)가 소유한다.
각 노드 담당자는 이 파일에 정의된 타입을 계약으로 보고 자기 노드를 구현한다.
필드 추가/이름 변경이 필요하면 팀 논의 후 이 파일만 수정한다.
"""

from typing import TypedDict, Literal
from pydantic import BaseModel


# --- 1. 사용자 입력 ---

class UserProfile(BaseModel):
    budget_krw: int | None
    monthly_data_gb: float | None
    min_qos_mbps: float | None
    voice_minutes: int | None
    voice_unlimited: bool
    sms_count: int | None
    sms_unlimited: bool
    age: int | None
    preferred_carrier: str | None
    preferred_benefits: list[str]  # ["OTT", "tethering", ...]
    current_plan_fee: int | None


# --- 2. 요금제 데이터 (크롤링 결과) ---

class PlanCandidate(BaseModel):
    plan_id: str
    brand: str                          # 소비자 노출용 브랜드명
    legal_entity: str | None            # 등록 법인명 (브랜드-법인 매핑 테이블 참조)
    host_mno: Literal["SKT", "KT", "LGU+"]

    monthly_fee_original: int           # 정가
    monthly_fee_promo: int | None       # 프로모션가 (있는 경우)
    discount_period_months: int | None  # null=파싱 실패 or 영구할인, 구분 필요
    monthly_fee_normalized: float       # 24개월 기준 정규화 값 (계산 결과)

    data_gb: float | None
    data_unlimited: bool
    qos_speed_mbps: float | None        # 소진 후 제한 속도

    voice_minutes: int | None
    voice_unlimited: bool
    sms_count: int | None
    sms_unlimited: bool

    age_condition: str | None           # 예: "만 65세 이상"
    is_online_only: bool
    contract_months: int | None         # 0=무약정

    benefits: list[str]                 # ["OTT", "tethering", "membership"]

    dominated_by: list[str] | None      # 이 요금제를 지배하는 plan_id들


# --- 3. 추천 결과 ---

class RecommendationResult(BaseModel):
    top_n: list[PlanCandidate]
    expected_monthly_cost: dict[str, float]  # plan_id -> E[bill]
    segment_label: str | None  # 세그먼트 컨텍스트용, 라우팅용 아님 (당장 안 씀, 필드만 유지)


# --- 4. 그래프 전체 State ---

class GraphState(TypedDict):
    input_type: Literal["chat", "form"]
    raw_input: str | None
    form_data: dict | None
    profile: UserProfile | None
    candidates: list[PlanCandidate]
    result: RecommendationResult | None
    report_text: str | None
