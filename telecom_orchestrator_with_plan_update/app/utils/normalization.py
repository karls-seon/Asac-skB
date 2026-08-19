from __future__ import annotations

import math
import re
from typing import Any

import pandas as pd


def none_if_na(value: Any):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def to_int_or_none(value: Any) -> int | None:
    value = none_if_na(value)
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def to_float_or_none(value: Any) -> float | None:
    value = none_if_na(value)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_bool(value: Any) -> bool:
    value = none_if_na(value)
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    return text in {"true", "1", "yes", "y", "t"}


def parse_qos_mbps(value: Any) -> float | None:
    """'3Mbps', '400Kbps', '100Kbps' 등을 Mbps 숫자로 정규화."""
    value = none_if_na(value)
    if value is None:
        return None

    text = str(value).replace(" ", "")
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(Mbps|Kbps|Gbps)", text, flags=re.I)
    if not match:
        return None

    amount = float(match.group(1))
    unit = match.group(2).lower()

    if unit == "gbps":
        return amount * 1000
    if unit == "kbps":
        return amount / 1000
    return amount


def effective_fee(row: pd.Series) -> int | None:
    """추천 필터링용 대표 요금. 할인요금이 존재하면 우선 사용."""
    discounted = to_int_or_none(row.get("discounted_fee"))
    monthly = to_int_or_none(row.get("monthly_fee"))
    return discounted if discounted is not None else monthly


def normalized_fee(row: pd.Series, horizon_months: int = 24) -> float | None:
    """약정 기간을 반영한 24개월 평균 월 요금.

    discounted_fee는 discount_period_months 동안만 유효하다. 3개월 프로모션가와
    영구 할인가를 같은 숫자로 비교하면 단기 미끼 요금제가 항상 이긴다.

    discount_period_months가 없으면(파싱 실패 또는 영구 할인) 할인가가 끝까지
    유지된다고 본다 - 크롤링에서 기간을 못 뽑은 경우와 구분되지 않으므로
    낙관적으로 잡는다.
    """
    monthly = to_int_or_none(row.get("monthly_fee"))
    discounted = to_int_or_none(row.get("discounted_fee"))

    if discounted is None:
        return float(monthly) if monthly is not None else None
    if monthly is None:
        return float(discounted)

    period = to_int_or_none(row.get("discount_period_months"))
    if period is None or period >= horizon_months:
        return float(discounted)
    period = max(period, 0)

    total = discounted * period + monthly * (horizon_months - period)
    return total / horizon_months
