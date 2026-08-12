"""추천 방식 3개를 같은 격자 입력으로 돌려 비교한다.

    A. 세그먼트 분류  - 합성 인물을 군집화하고, 입력이 속한 세그먼트가 쓰는 요금제
    B. 요금제 군집    - 요금제를 스펙으로 군집화하고, 입력에 가까운 군집에서 싼 순
    C. 필터 + 랭킹    - recommend()

**필터를 공유하지 않는다.** 공유하면 셋이 같은 후보 안에서 놀아 차이가 안 나고,
"세그먼트 방식은 예산을 못 지킨다"가 드러나지 않는다.

정답 요금제가 없으므로 "어느 게 정확한가"는 재지 않는다. **어디서 무너지는가**를
잰다 - 규칙 위반율 / 커버리지 / 세 방식 결과 겹침 / 추천 요금 중앙값.

    python src/compare_methods.py
"""

import itertools

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from fair_price import attach_value_score
from make_synthetic import OUT_PATH as SYNTHETIC_PATH
from recommend import TOP_N, recommend
from schema import DATA_DIR

SEED = 20260811
K = 6

# 무제한을 거리 계산에 넣으려면 숫자여야 한다. 실제 최대 제공량(300GB)보다 큰 값.
# ponytail: 센티넬 하나로 때운다. 무제한을 별도 차원으로 두는 게 정석이지만,
# 군집이 무제한/유제한부터 가르는 건 이 값으로도 똑같이 된다.
UNLIMITED_GB = 500.0

# 격자 - 근거 있는 분포가 아니라 빈 구멍 없이 훑는 게 목적이다. 합성 인물을 쓰면
# 세그먼트 방식이 자기가 학습한 분포에서 시험보는 셈이라 비교가 기운다.
BUDGETS = [10_000, 20_000, 30_000, 50_000, 80_000]
DATA_NEEDS = [5.0, 20.0, 50.0, 100.0, None]   # None = 무제한 요구
MVNO_OK = [True, False]
VOICE = [True, False]
AGES = [25, 65]

# 사람 군집에는 나이를 넣고, 요금제 군집에는 넣지 않는다. 요금제 쪽에는 나이가
# 없다(`age_condition`이 2,797행 중 2,495행이 빈값이라 축으로 쓸 수 없다).
SEG_FEATURES = ["budget", "data_gb", "voice_unlimited", "age"]
PLAN_FEATURES = ["budget", "data_gb", "voice_unlimited"]

OUT_PATH = DATA_DIR / "review" / "method_comparison.csv"


def grid() -> list[dict]:
    rows = []
    for b, d, m, v, a in itertools.product(BUDGETS, DATA_NEEDS, MVNO_OK, VOICE, AGES):
        rows.append({
            "budget": b,
            "data_gb": d,
            "data_unlimited": d is None,
            "mvno_ok": m,
            "voice_unlimited": v,
            "age": a,
        })
    return rows


def _vec(features, *, budget, data_gb, data_unlimited, voice_unlimited, age=None, **_) -> pd.DataFrame:
    """군집 모델이 학습한 것과 같은 컬럼명·순서로 한 줄을 만든다."""
    values = {
        "budget": budget,
        "data_gb": UNLIMITED_GB if data_unlimited else data_gb,
        "voice_unlimited": float(voice_unlimited),
        "age": age,
    }
    return pd.DataFrame([[values[f] for f in features]], columns=features)


class SegmentMethod:
    """A. 합성 인물 군집 -> 그 세그먼트가 쓰는 요금제.

    멘토 방식. 세그먼트를 인물 속성으로 만들고 같은 속성으로 다시 분류하므로,
    별도 분류기를 학습해도 군집 경계를 재현할 뿐이다. `KMeans.predict`가 곧 그
    분류기라 따로 두지 않는다.
    """

    name = "A.세그먼트"

    def __init__(self, customers: pd.DataFrame):
        x = pd.DataFrame({
            "budget": customers["budget_krw"],
            "data_gb": customers["data_gb_month"].fillna(UNLIMITED_GB),
            "voice_unlimited": customers["voice_unlimited_need"].astype(float),
            "age": customers["age"],
        })[SEG_FEATURES]
        self.model = make_pipeline(StandardScaler(), KMeans(K, random_state=SEED, n_init=10))
        self.labels = self.model.fit_predict(x)
        # `plan_id`는 문자열인데 CSV에서 읽은 `source_plan_id`는 값이 전부 숫자면
        # int64로 추론된다. 그대로 두면 아래 `isin`이 한 건도 안 맞아 추천이 조용히
        # 0건이 된다 - 지금 customers.csv는 3사 요금제 코드에 글자가 섞여 있어
        # object로 읽히지만, 알뜰폰 전용 파일(customers_mvno.csv)은 전부 숫자라
        # 실제로 터진다.
        source = customers["source_plan_id"].astype(str)
        self.plans_by_segment = {
            seg: source[self.labels == seg].value_counts().head(TOP_N).index.tolist()
            for seg in range(K)
        }

    def __call__(self, plans, **cell):
        seg = self.model.predict(_vec(SEG_FEATURES, **cell))[0]
        return plans[plans["plan_id"].isin(self.plans_by_segment[seg])]


class PlanClusterMethod:
    """B. 요금제를 스펙으로 군집화 -> 입력에 가까운 군집에서 싼 순."""

    name = "B.요금제군집"

    def __init__(self, plans: pd.DataFrame):
        self.plans = plans[plans["discounted_fee"].notna()].copy()
        x = pd.DataFrame({
            "budget": self.plans["discounted_fee"],
            "data_gb": self.plans["data_gb"].where(
                ~self.plans["data_unlimited"].eq(True), UNLIMITED_GB
            ).fillna(0),
            "voice_unlimited": self.plans["voice_unlimited"].eq(True).astype(float),
        })[PLAN_FEATURES]
        self.model = make_pipeline(StandardScaler(), KMeans(K, random_state=SEED, n_init=10))
        self.plans["cluster"] = self.model.fit_predict(x)

    def __call__(self, plans, **cell):
        c = self.model.predict(_vec(PLAN_FEATURES, **cell))[0]
        hit = self.plans[self.plans["cluster"] == c]
        return hit.nsmallest(TOP_N, "discounted_fee")


class FilterMethod:
    """C. 필터 + 랭킹."""

    name = "C.필터랭킹"

    def __call__(self, plans, **kw):
        return recommend(plans, top_n=TOP_N, **kw)


def evaluate(methods, plans, cells) -> pd.DataFrame:
    rows = []
    for cell in cells:
        picks = {m.name: m(plans, **cell) for m in methods}
        for name, got in picks.items():
            fee = pd.to_numeric(got.get("discounted_fee"), errors="coerce")
            gb = pd.to_numeric(got.get("data_gb"), errors="coerce")
            unl = got["data_unlimited"].eq(True) if len(got) else pd.Series(dtype=bool)

            over_budget = int((fee > cell["budget"]).sum())
            if cell["data_unlimited"]:
                short_data = int((~unl).sum())
            else:
                short_data = int((~unl & ~gb.ge(cell["data_gb"])).sum())
            wrong_carrier = 0 if cell["mvno_ok"] else int(got["carrier_type"].ne("MNO").sum())

            rows.append({
                **cell,
                "method": name,
                "n": len(got),
                "over_budget": over_budget,
                "short_data": short_data,
                "wrong_carrier": wrong_carrier,
                "median_fee": float(fee.median()) if len(got) else np.nan,
                "plan_ids": "|".join(map(str, got["plan_id"])) if len(got) else "",
            })
    return pd.DataFrame(rows)


def summarize(res: pd.DataFrame) -> pd.DataFrame:
    g = res.groupby("method")
    return pd.DataFrame({
        "결과 0건 비율": (g["n"].apply(lambda s: (s == 0).mean()) * 100).round(1),
        "예산 초과율": (g.apply(lambda d: d.over_budget.sum() / max(d.n.sum(), 1), include_groups=False) * 100).round(1),
        "데이터 부족율": (g.apply(lambda d: d.short_data.sum() / max(d.n.sum(), 1), include_groups=False) * 100).round(1),
        "통신사 위반율": (g.apply(lambda d: d.wrong_carrier.sum() / max(d.n.sum(), 1), include_groups=False) * 100).round(1),
        "추천 요금 중앙값": g["median_fee"].median().round(0),
    })


def jaccard(res: pd.DataFrame) -> pd.DataFrame:
    names = sorted(res["method"].unique())
    # 격자 한 칸을 특정하는 키 전부. 하나라도 빠지면 unstack이 중복으로 깨진다.
    key = [c for c in grid()[0]]
    wide = res.set_index([*key, "method"])["plan_ids"].unstack()
    out = pd.DataFrame(index=names, columns=names, dtype=float)
    for a in names:
        for b in names:
            sims = []
            for x, y in zip(wide[a], wide[b]):
                sx, sy = set(str(x).split("|")) - {""}, set(str(y).split("|")) - {""}
                if sx or sy:
                    sims.append(len(sx & sy) / len(sx | sy))
            out.loc[a, b] = round(float(np.mean(sims)), 3) if sims else np.nan
    return out


def check(res: pd.DataFrame, summary: pd.DataFrame) -> None:
    cells = len(grid())
    assert cells == 200, cells
    assert len(res) == cells * 3, len(res)

    # 필터 방식은 정의상 규칙을 어기지 않는다. 어기면 recommend()가 깨진 것이다.
    c = summary.loc["C.필터랭킹"]
    assert c["예산 초과율"] == 0 and c["데이터 부족율"] == 0 and c["통신사 위반율"] == 0, c

    print("check ok")


if __name__ == "__main__":
    plans = attach_value_score()
    customers = pd.read_csv(SYNTHETIC_PATH, encoding="utf-8-sig")
    methods = [SegmentMethod(customers), PlanClusterMethod(plans), FilterMethod()]

    res = evaluate(methods, plans, grid())
    summary = summarize(res)
    check(res, summary)

    print(f"\n[격자 {len(grid())}칸 x 방식 3개]")
    print(summary.to_string())
    print("\n[세 방식 결과 겹침 - Jaccard 평균]")
    print(jaccard(res).to_string())

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    res.to_csv(OUT_PATH, index=False, encoding="utf-8-sig")
    print(f"\n칸별 원자료 -> {OUT_PATH}")
