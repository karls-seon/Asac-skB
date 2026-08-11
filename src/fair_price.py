"""스펙 -> 적정가 회귀.

`(데이터·통화·문자·통신사·망 …)`에서 `discounted_fee`를 예측하고,
**예측가 - 실제가**를 가성비 점수로 쓴다. 점수가 크면 스펙에 비해 싸다는 뜻이다.

정답이 실제 시장 가격이라 우리가 라벨을 지어내지 않는다. 가격·스펙이 똑같아
순서를 매길 근거가 없던 동점 요금제를, 시장이 매긴 값으로 가른다.

    python src/fair_price.py        # 성능 출력 + 저평가 요금제 10개
"""

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.metrics import mean_absolute_error, r2_score
from sklearn.model_selection import KFold, cross_val_predict

from recommend import load_plans

# `monthly_fee`는 할인 전 가격이라 정답을 거의 그대로 알려준다. 넣으면 성능은
# 올라가지만 배운 게 없다. `discount_type`·`discount_period_months`도 같은 이유로 뺀다.
NUMERIC = [
    "data_gb", "tethering_gb", "voice_minutes", "sms_count",
    "benefit_count", "ott_option_count",
]
BOOLEAN = ["data_unlimited", "voice_unlimited", "sms_unlimited", "is_online_only"]
CATEGORICAL = ["carrier_type", "host_mno", "network_gen", "tethering_support", "plan_category"]

SEED = 20260811


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """모델에 넣을 표를 만든다. 결측은 그대로 둔다 - 트리가 알아서 가른다."""
    x = pd.DataFrame(index=df.index)
    for col in NUMERIC:
        x[col] = pd.to_numeric(df.get(col), errors="coerce")
    for col in BOOLEAN:
        # 빈값을 False로 바꾸면 "정보 없음"이 "아니다"로 둔갑한다. NaN으로 남긴다.
        x[col] = df.get(col).map({True: 1.0, False: 0.0}) if col in df else np.nan
    for col in CATEGORICAL:
        x[col] = df.get(col).astype("category") if col in df else pd.Series(dtype="category")
    return x


def fit(plans: pd.DataFrame | None = None):
    """전체 요금제로 학습하고, 교차검증 예측으로 성능과 가성비 점수를 함께 낸다.

    자기 자신을 학습에 쓴 모델로 자기 가격을 예측하면 잔차가 0에 가까워져 점수가
    무의미해진다. 그래서 점수는 5-fold 교차검증 예측(자기 fold를 못 본 모델의 예측)
    으로 만든다.
    """
    df = load_plans() if plans is None else plans
    df = df[df["discounted_fee"].notna()].copy()

    x, y = build_features(df), df["discounted_fee"].to_numpy()
    model = HistGradientBoostingRegressor(
        categorical_features="from_dtype", random_state=SEED
    )
    pred = cross_val_predict(model, x, y, cv=KFold(5, shuffle=True, random_state=SEED))
    # 트리 회귀에 하한이 없어 예측이 음수로 내려가는 행이 나온다(4건, 최소 -475원).
    # 요금이 음수인 요금제는 존재하지 않는다.
    pred = pred.clip(min=0)

    df["fair_price"] = pred.round()
    df["value_score"] = (pred - y).round()   # +면 스펙 대비 싸다

    metrics = {
        "n": len(df),
        "MAE": round(mean_absolute_error(y, pred)),
        "R2": round(r2_score(y, pred), 3),
        "실제가 표준편차": round(float(np.std(y))),
    }
    return model.fit(x, y), df, metrics


def attach_value_score(plans: pd.DataFrame | None = None) -> pd.DataFrame:
    """요금제 테이블에 `fair_price`·`value_score`를 붙여 돌려준다.

    학습이 몇 초 걸리니 요청마다 부르지 말고, 앱이 뜰 때 한 번 불러서 그 결과를
    `recommend()`에 넘긴다.
    """
    _, scored, _ = fit(plans)
    return scored


def check(scored: pd.DataFrame, metrics: dict) -> None:
    # 정답을 그대로 알려주는 컬럼이 새어 들어가지 않았는가.
    assert "monthly_fee" not in build_features(scored).columns

    # 스펙으로 가격이 설명되긴 하는가. 안 되면 동점을 가를 근거로 못 쓴다.
    assert metrics["R2"] > 0.7, metrics
    assert metrics["MAE"] < metrics["실제가 표준편차"] / 2, metrics

    # 점수는 잔차라 전체 평균이 0 근처여야 한다. 한쪽으로 쏠리면 편향된 모델이다.
    assert abs(scored["value_score"].mean()) < metrics["MAE"], scored["value_score"].mean()

    # 적정가는 음수가 될 수 없다.
    assert (scored["fair_price"] >= 0).all(), scored["fair_price"].min()

    # 정의 그대로: 점수 = 예측가 - 실제가.
    diff = scored["fair_price"] - scored["discounted_fee"] - scored["value_score"]
    assert diff.abs().max() <= 1, diff.abs().max()

    print(f"check ok: R2 {metrics['R2']}, MAE {metrics['MAE']:,}원")


if __name__ == "__main__":
    _, scored, metrics = fit()
    check(scored, metrics)
    print(metrics)

    cols = ["plan_name", "carrier_type", "data_gb", "discounted_fee", "fair_price", "value_score"]
    print("\n[스펙 대비 싼 요금제 10개]")
    print(scored.nlargest(10, "value_score")[cols].to_string(index=False))
    print("\n[스펙 대비 비싼 요금제 5개]")
    print(scored.nsmallest(5, "value_score")[cols].to_string(index=False))
