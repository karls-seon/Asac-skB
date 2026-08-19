from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


CarrierType = Literal["MNO", "MVNO"]
HostMNO = Literal["SKT", "KT", "LGU+"]
NetworkGen = Literal["LTE", "5G"]


class UserProfile(BaseModel):
    """User Analysis Agent가 생성하는 표준 사용자 요구사항."""

    model_config = ConfigDict(extra="forbid")

    budget_krw: int | None = Field(default=None, ge=0)
    monthly_data_gb: float | None = Field(default=None, ge=0)
    min_qos_mbps: float | None = Field(default=None, ge=0)

    voice_minutes: int | None = Field(default=None, ge=0)
    voice_unlimited: bool | None = None

    sms_count: int | None = Field(default=None, ge=0)
    sms_unlimited: bool | None = None

    age: int | None = Field(default=None, ge=0, le=120)

    carrier_type: CarrierType | None = None
    preferred_carrier: HostMNO | None = None
    network_gen: NetworkGen | None = None

    preferred_benefits: list[str] = Field(default_factory=list)
    required_benefits: list[str] = Field(default_factory=list)

    current_plan_fee: int | None = Field(default=None, ge=0)

    @field_validator("preferred_benefits", "required_benefits", mode="before")
    @classmethod
    def normalize_benefit_list(cls, value):
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        return [str(v).strip() for v in value if str(v).strip()]
