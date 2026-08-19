import pandas as pd
import pytest

from app.utils.normalization import normalized_fee


def test_short_promo_does_not_beat_permanent_discount():
    # 3개월만 9,900원이고 이후 39,900원 -> 24개월 평균은 정가에 가깝다
    promo = pd.Series(
        {"monthly_fee": 39900, "discounted_fee": 9900, "discount_period_months": 3}
    )
    # 기간 표기가 없는 할인은 영구 할인으로 본다
    permanent = pd.Series(
        {"monthly_fee": 39900, "discounted_fee": 19900, "discount_period_months": None}
    )

    assert normalized_fee(promo) == pytest.approx(36150.0)
    assert normalized_fee(permanent) == 19900.0
    assert normalized_fee(promo) > normalized_fee(permanent)


def test_falls_back_to_monthly_fee_when_no_discount():
    row = pd.Series(
        {"monthly_fee": 55000, "discounted_fee": None, "discount_period_months": None}
    )
    assert normalized_fee(row) == 55000.0

    empty = pd.Series({"monthly_fee": None, "discounted_fee": None})
    assert normalized_fee(empty) is None


def test_discount_longer_than_horizon_is_just_the_discounted_fee():
    row = pd.Series(
        {"monthly_fee": 39900, "discounted_fee": 29900, "discount_period_months": 36}
    )
    assert normalized_fee(row) == 29900.0

