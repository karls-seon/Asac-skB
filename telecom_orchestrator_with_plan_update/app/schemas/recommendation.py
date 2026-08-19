from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CandidatePlan(BaseModel):
    """PlanRepository가 반환하는 추천 후보 표준 스키마."""

    model_config = ConfigDict(extra="allow")

    plan_id: str
    plan_name: str

    carrier_type: str
    host_mno: str
    mvno_brand: str | None = None
    network_gen: str | None = None

    monthly_fee: int | None = None
    discounted_fee: int | None = None
    discount_type: str | None = None
    discount_period_months: int | None = None
    # 약정 기간 반영 24개월 평균 월 요금. 단기 프로모션가와 영구 할인가를
    # 같은 값으로 비교하지 않기 위한 대표 요금.
    monthly_fee_normalized: float | None = None

    data_gb: float | None = None
    data_unlimited: bool = False
    qos_mbps: float | None = None

    voice_minutes: int | None = None
    voice_unlimited: bool = False

    sms_count: int | None = None
    sms_unlimited: bool = False

    tethering_gb: float | None = None
    age_condition: str | None = None

    benefit_services: list[str] = Field(default_factory=list)
    benefit_categories: list[str] = Field(default_factory=list)

    source_url: str | None = None


class RecommendationItem(BaseModel):
    plan: CandidatePlan
    score: float = Field(ge=0)
    rank: int = Field(ge=1)
    matched_benefits: list[str] = Field(default_factory=list)
    recommendation_reasons: list[str] = Field(default_factory=list)


class ValidationIssue(BaseModel):
    plan_id: str | None = None
    field: str
    code: str
    message: str


class ValidationResult(BaseModel):
    passed: bool
    errors: list[ValidationIssue] = Field(default_factory=list)
    warnings: list[ValidationIssue] = Field(default_factory=list)
