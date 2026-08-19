from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.schemas.recommendation import CandidatePlan
from app.schemas.user_profile import UserProfile
from app.utils.normalization import (
    effective_fee,
    none_if_na,
    normalized_fee,
    parse_qos_mbps,
    to_bool,
    to_float_or_none,
    to_int_or_none,
)


class PlanRepository:
    """
    요금제 CSV/DB 접근 책임을 Agent에서 분리한 Repository.

    향후 CSV 대신 PostgreSQL, MySQL, API로 변경해도
    find_candidates() 인터페이스만 유지하면 Orchestrator는 수정할 필요가 없다.
    """

    def __init__(self, plans_path: str | Path, benefits_path: str | Path):
        self.plans_path = Path(plans_path)
        self.benefits_path = Path(benefits_path)

        self.plans = pd.read_csv(self.plans_path)
        self.benefits = pd.read_csv(self.benefits_path)

        self._prepare()

    def _prepare(self) -> None:
        self.plans = self.plans.copy()
        self.benefits = self.benefits.copy()

        self.plans["effective_fee"] = self.plans.apply(effective_fee, axis=1)
        self.plans["qos_mbps"] = self.plans["data_throttle_speed"].apply(parse_qos_mbps)
        self.plans["monthly_fee_normalized"] = self.plans.apply(normalized_fee, axis=1)

        # plan_id -> benefit services/categories
        benefit_group = (
            self.benefits.groupby("plan_id", dropna=False)
            .agg(
                benefit_services=(
                    "benefit_service",
                    lambda s: sorted({str(v).strip() for v in s.dropna() if str(v).strip()}),
                ),
                benefit_categories=(
                    "benefit_category",
                    lambda s: sorted({str(v).strip() for v in s.dropna() if str(v).strip()}),
                ),
                benefit_names=(
                    "benefit_name",
                    lambda s: sorted({str(v).strip() for v in s.dropna() if str(v).strip()}),
                ),
            )
            .reset_index()
        )

        self.plans = self.plans.merge(benefit_group, on="plan_id", how="left")

        for col in ["benefit_services", "benefit_categories", "benefit_names"]:
            self.plans[col] = self.plans[col].apply(
                lambda v: v if isinstance(v, list) else []
            )

    def find_candidates(
        self,
        profile: UserProfile,
        *,
        limit: int = 300,
        exclude_plan_ids: set[str] | None = None,
    ) -> list[CandidatePlan]:
        """
        Hard constraints 위주로 후보군을 축소한다.
        preferred_benefits는 Soft Preference이므로 여기서 강제 필터링하지 않는다.
        required_benefits만 Hard Filter로 사용한다.
        """
        df = self.plans.copy()
        exclude_plan_ids = exclude_plan_ids or set()

        if exclude_plan_ids:
            df = df[~df["plan_id"].astype(str).isin(exclude_plan_ids)]

        if profile.carrier_type is not None:
            df = df[df["carrier_type"].astype(str) == profile.carrier_type]

        if profile.preferred_carrier is not None:
            df = df[df["host_mno"].astype(str) == profile.preferred_carrier]

        if profile.network_gen is not None:
            df = df[df["network_gen"].astype(str) == profile.network_gen]

        if profile.budget_krw is not None:
            df = df[
                df["effective_fee"].notna()
                & (df["effective_fee"] <= profile.budget_krw)
            ]

        if profile.monthly_data_gb is not None:
            unlimited = df["data_unlimited"].apply(to_bool)
            enough_data = (
                pd.to_numeric(df["data_gb"], errors="coerce")
                >= profile.monthly_data_gb
            )
            df = df[unlimited | enough_data]

        if profile.min_qos_mbps is not None:
            unlimited = df["data_unlimited"].apply(to_bool)
            qos_ok = (
                pd.to_numeric(df["qos_mbps"], errors="coerce")
                >= profile.min_qos_mbps
            )
            # QoS가 없는 완전 무제한 상품도 허용할 수 있도록 unlimited 포함.
            df = df[unlimited | qos_ok]

        if profile.voice_unlimited is True:
            df = df[df["voice_unlimited"].apply(to_bool)]
        elif profile.voice_minutes is not None:
            unlimited = df["voice_unlimited"].apply(to_bool)
            voice_minutes = pd.to_numeric(df["voice_minutes"], errors="coerce")
            df = df[unlimited | (voice_minutes >= profile.voice_minutes)]

        if profile.sms_unlimited is True:
            df = df[df["sms_unlimited"].apply(to_bool)]
        elif profile.sms_count is not None:
            unlimited = df["sms_unlimited"].apply(to_bool)
            sms_count = pd.to_numeric(df["sms_count"], errors="coerce")
            df = df[unlimited | (sms_count >= profile.sms_count)]

        if profile.required_benefits:
            normalized_required = {x.lower() for x in profile.required_benefits}

            def has_required(row) -> bool:
                searchable = {
                    *(str(x).lower() for x in row.get("benefit_services", [])),
                    *(str(x).lower() for x in row.get("benefit_categories", [])),
                    *(str(x).lower() for x in row.get("benefit_names", [])),
                    str(row.get("ott_options", "")).lower(),
                    str(row.get("smart_device_benefit", "")).lower(),
                    str(row.get("extra_data_benefit", "")).lower(),
                    str(row.get("gift_benefit", "")).lower(),
                }
                blob = " | ".join(searchable)
                return all(req in blob for req in normalized_required)

            df = df[df.apply(has_required, axis=1)]

        # 후보가 너무 많으면 저가 + 데이터 중심으로 안정적으로 제한.
        df = df.sort_values(
            by=["effective_fee", "data_gb"],
            ascending=[True, False],
            na_position="last",
        ).head(limit)

        return [self._row_to_candidate(row) for _, row in df.iterrows()]

    def get_by_plan_id(self, plan_id: str) -> CandidatePlan | None:
        rows = self.plans[self.plans["plan_id"].astype(str) == str(plan_id)]
        if rows.empty:
            return None
        return self._row_to_candidate(rows.iloc[0])

    def exists(self, plan_id: str) -> bool:
        return bool((self.plans["plan_id"].astype(str) == str(plan_id)).any())

    def _row_to_candidate(self, row: pd.Series) -> CandidatePlan:
        return CandidatePlan(
            plan_id=str(row["plan_id"]),
            plan_name=str(row["plan_name"]),
            carrier_type=str(row["carrier_type"]),
            host_mno=str(row["host_mno"]),
            mvno_brand=none_if_na(row.get("mvno_brand")),
            network_gen=none_if_na(row.get("network_gen")),
            monthly_fee=to_int_or_none(row.get("monthly_fee")),
            discounted_fee=to_int_or_none(row.get("discounted_fee")),
            discount_type=none_if_na(row.get("discount_type")),
            discount_period_months=to_int_or_none(row.get("discount_period_months")),
            monthly_fee_normalized=to_float_or_none(row.get("monthly_fee_normalized")),
            data_gb=to_float_or_none(row.get("data_gb")),
            data_unlimited=to_bool(row.get("data_unlimited")),
            qos_mbps=to_float_or_none(row.get("qos_mbps")),
            voice_minutes=to_int_or_none(row.get("voice_minutes")),
            voice_unlimited=to_bool(row.get("voice_unlimited")),
            sms_count=to_int_or_none(row.get("sms_count")),
            sms_unlimited=to_bool(row.get("sms_unlimited")),
            tethering_gb=to_float_or_none(row.get("tethering_gb")),
            age_condition=none_if_na(row.get("age_condition")),
            benefit_services=row.get("benefit_services", []),
            benefit_categories=row.get("benefit_categories", []),
            source_url=none_if_na(row.get("source_url")),
        )

    def reload(self) -> None:
        """외부 Plan Update 작업이 CSV/DB를 갱신한 뒤 최신 데이터를 다시 로드한다."""
        self.plans = pd.read_csv(self.plans_path)
        self.benefits = pd.read_csv(self.benefits_path)
        self._prepare()
