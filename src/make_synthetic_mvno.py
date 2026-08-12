"""알뜰폰 전용 합성 고객 데이터 - 세그먼트 분류 ML이 실제로 필요한지 재는 실험용.

`make_synthetic.py`와 다른 점 세 가지.

1. **MVNO 요금제만 쓴다.** 가입자 수(`subscriber_count`)가 모요에만 있어서 MNO는
   균등 배분할 수밖에 없었고, 그 탓에 상품 수가 많은 KT가 48%까지 부풀었다.
   알뜰폰만 남기면 2,177행 전부에 가입자 수가 있어 그 문제가 통째로 사라진다.
2. **나이를 요금제와 독립으로 뽑지 않는다.** 독립으로 뽑으면 나이와 사용량의 상관이
   0이라, 인구통계로 세그먼트를 맞히는 ML은 구조적으로 AUC 0.5가 나온다. "ML이
   필요 없다"는 결론이 데이터에 신호를 안 넣은 탓에 나오는 셈이라 실험이 성립하지
   않는다. 요금대에 조건부로 뽑아 실측 기반 상관을 넣는다.
3. **사용량을 제공량의 80~100%로 본다** (기존 90~100%). 세그먼트 경계를 넓게 본다.

    python src/make_synthetic_mvno.py            # 4만 명
    python src/make_synthetic_mvno.py --check    # 자체 검증만

산출물: data/synthetic/customers_mvno.csv

**이 데이터로 답할 수 있는 것과 없는 것.** 신호를 우리가 넣는 구조라, 나이는 요금
그룹 3단계의 함수 + 노이즈다. ML이 찾아낼 수 있는 상한이 `AGE_GIVEN_FEE`에 이미
박혀 있다. 그래서 이 실험이 답하는 건 "현실에 세그먼트가 존재하는가"가 아니라
"이 정도 세기의 신호를 우리 파이프라인이 회수하는가"다. 검정력 분석이지 가설
검정이 아니다. 그래도 신호 세기가 실측이라 "세그먼트 ML을 붙여도 이 정도까지"라는
상한은 숫자로 나온다.
"""

import argparse

import numpy as np
import pandas as pd

from recommend import load_plans as read_plans
from schema import DATA_DIR

N_DEFAULT = 40_000
SEED = 20260812

# 제공량을 다 쓰지는 않는다고 보되, 요금제를 고른 이유가 사라질 만큼 적게 쓰지도
# 않는다고 본다. 세그먼트 경계를 넓게 본다.
USAGE_RATIO = (0.8, 1.0)

# 요금제에 OTT가 붙어 있어도 그걸 원해서 고른 사람만 있는 건 아니다. 딸려온 경우를
# 빼려고 확률을 낮춰 잡는다.
OTT_WANT_PROB = 0.7

# OTT를 원하는 사람 중 "그게 없으면 아예 안 보겠다"는 비율.
# ponytail: 근거 없는 확률이다. 요금제 스펙에서 역산할 수 없는 사람의 선호라
# 설문이나 로그가 생기면 그때 교체한다. 지금은 "한쪽으로 쏠리지 않는다"는 것
# 말고는 주장하지 않는다.
OTT_REQUIRED_PROB = 0.3

COLUMNS = [
    "customer_id", "age", "gender",
    "data_gb_month", "data_unlimited_need",
    "voice_unlimited_need", "sms_unlimited_need",
    "voice_minutes_need", "sms_count_need",
    "carrier_type", "current_carrier", "mvno_ok",
    "current_fee_krw", "budget_krw", "ott_want", "ott_required",
    "source_plan_id",
]

# 이미지에 20~60대만 있어서 그 밖은 만들지 않는다. 원자료의 70대+는 알뜰폰
# 응답자의 24.9%나 되는데, 패널이 2010년부터 같은 가구를 추적해 함께 늙은 결과라
# 실제 연령 구성으로 믿을 수 없다.
AGE_BANDS = {20: (20, 29), 30: (30, 39), 40: (40, 49), 50: (50, 59), 60: (60, 69)}

# 아래 세 상수는 2025년 조사 두 건에서 나온 고정값이다. 원자료 CSV(576컬럼)는
# git에 없어서 런타임 의존을 두면 다른 컴퓨터에서 못 돌아간다. 값을 박고 유도
# 과정을 여기 남긴다.
#
# (가) 요금 3분할 비중 - KISDI 한국미디어패널조사 2025 원자료, 알뜰폰 가입자
#      (`p__a03008 == 4`)의 월 납부액 구간(`p__c01002`) 311명 분포를 코드 경계에
#      맞춰 3등분. 저=코드1~2, 중=코드3, 고=코드4+.
FEE_GROUP_SHARE = {"저": 0.402, "중": 0.318, "고": 0.280}

# (나) 연령 x 요금그룹 - 위 원자료 알뜰폰 333명 교차표에서 P(요금그룹 | 연령)을
#      뽑고, 정보통신정책연구원(2025) <표 II-II-3-2> 알뜰폰 가입자 연령대 분포
#      (1,384명: 20대 5.4 / 30대 21.5 / 40대 26.8 / 50대 28.0 / 60대 18.3)를
#      사전분포로 곱해 뒤집었다.
#
#          P(연령 | 요금그룹) ∝ P(요금그룹 | 연령) x P(연령)
#                               [원자료 333명]      [표 1,384명]
#
#      원자료 교차표(건수). 60대+가 148명(45%)인 게 패널 고령화 왜곡이라, 표를
#      그대로 쓰지 않고 행 정규화한 조건부만 썼다. 표본 구성비는 그러면 상쇄된다.
#
#          연령    저   중   고   계
#          20대     5   18   14    37
#          30대    12   16   11    39
#          40대     8    6   14    28
#          50대    29   14   16    59
#          60대+   71   45   32   148
#
#      재현:
#          d = pd.read_csv("data/external/kisdi_panel_2025.csv", low_memory=False)
#          mv = d[d.p__a03008 == 4].dropna(subset=["p__age1", "p__c01002"])
#          band = pd.cut(mv.p__age1, [19, 29, 39, 49, 59, 200])
#          grp = pd.cut(mv.p__c01002, [0, 2, 3, 99])
#          ct = pd.crosstab(band, grp)
#          post = ct.div(ct.sum(1), axis=0).mul(IMAGE_MARGINAL, axis=0)
#          post = post.div(post.sum(0), axis=1)
#
# ponytail: 셀이 얇다. 20대 37명, 40대 28명이고 "40대 고 40.5%"는 14명에 걸려
# 있다. 결론을 쓸 때 n을 같이 적어야 한다. 알뜰폰 표본이 더 두꺼운 조사가 생기면
# 이 표만 갈아끼우면 된다.
AGE_GIVEN_FEE = {
    "저": {20: 0.019, 30: 0.176, 40: 0.204, 50: 0.367, 60: 0.234},
    "중": {20: 0.089, 30: 0.300, 40: 0.195, 50: 0.226, 60: 0.189},
    "고": {20: 0.062, 30: 0.183, 40: 0.405, 50: 0.230, 60: 0.120},
}

# (다) 성별 - 같은 원자료 알뜰폰 333명의 남성 비율. 이미지에 성별이 없어 요금대와
# 엮을 근거가 없다. 독립으로 뽑으므로 **성별을 쓰는 ML은 구조적으로 AUC 0.5다**.
MALE_PROB = 0.402

# 이 사람의 나이를 어느 칸에서 뽑았는지(`AGE_GIVEN_FEE`의 열 이름). 생성 과정의
# 기록이지 세그먼트가 아니다.
#
# 남겨 두는 이유는 `check()`가 이걸로 세 가지를 검증하기 때문이다 - 요금 3분할
# 비중이 조사값과 맞는지, 그룹별 평균 GB가 단조 증가하는지, 나이에 심은 신호가
# 살아 있는지. 셋 다 이 값 없이는 확인할 방법이 없다.
#
# **분석에 쓸 때 주의.** 요금을 분위로 자른 결과라 `budget_krw`와 사실상 같은
# 정보다. 이걸 정답으로 두고 입력으로 맞히는 실험을 하면 예산 하나로 100%가
# 나오는데, 배운 게 아니라 정답의 재료를 넣은 것이다(실제로 한 번 그랬다).
OUT_COLUMNS = COLUMNS + ["fee_group"]

OUT_PATH = DATA_DIR / "synthetic" / "customers_mvno.csv"


def _used_amount(quota: np.ndarray, unlimited: np.ndarray, rng, n: int) -> np.ndarray:
    """제공량에서 실사용량을 역산한다. 무제한이거나 제공량이 없으면 NaN.

    `data_gb`와 달리 내림을 쓰지 않는다 - 분·건은 정수 단위라 0.5분 같은 값이
    나와도 조건 비교(`>= 300분`)에 문제가 없고, 내림으로 0을 만들면 "통화를 전혀
    안 하는 사람"이 되어 버린다.
    """
    used = np.round(quota * rng.uniform(*USAGE_RATIO, size=n))
    return np.where(unlimited, np.nan, used)


def load_mvno_plans() -> pd.DataFrame:
    """알뜰폰 요금제 중 합성에 쓸 수 있는 행만 남긴다."""
    df = read_plans()
    df = df[df["carrier_type"].eq("MVNO")]
    # 가격을 모르면 현재 요금도 예산도 만들 수 없다.
    df = df[df["discounted_fee"].notna()]
    # 무제한이 아닌데 제공량이 비어 있으면 사용량을 역산할 근거가 없다.
    df = df[df["data_unlimited"].eq(True) | df["data_gb"].notna()]
    # 가입자 수가 없으면 뽑을 가중치가 없다. 지금은 전 행에 있지만, 모요 파싱이
    # 깨지면 조용히 균등 배분으로 돌아가는 대신 여기서 행이 줄어드는 편이 낫다.
    df = df[df["subscriber_count"].fillna(0) > 0]
    return df.reset_index(drop=True)


def assign_fee_group(df: pd.DataFrame) -> pd.Series:
    """요금제를 가입자 가중 분위로 잘라 저/중/고를 붙인다.

    금액이 아니라 **순위**로 자르는 게 핵심이다. 조사의 월 납부액과 우리 요금은
    단위가 안 맞는다 - `discounted_fee`는 프로모션가라 중앙 8,200원인데 조사는
    2~3만원이고, 정가(`monthly_fee`)로 바꾸면 31,900원으로 이번엔 너무 높다.
    분위로 자르면 이 어긋남을 타지 않고, 덤으로 `p__c01002` 코드가 실제로 몇 만원
    구간인지 몰라도 된다(구간 순서만 맞으면 된다).
    """
    w = df["subscriber_count"].to_numpy(dtype=float)
    w = w / w.sum()

    # 요금 오름차순 누적 가중치 = 그 요금제가 선 분위. 동점은 안정 정렬로 고정해
    # 시드가 같으면 항상 같은 그룹이 나오게 한다.
    order = np.argsort(df["discounted_fee"].to_numpy(), kind="stable")
    rank = np.empty(len(df))
    rank[order] = np.cumsum(w[order])

    lo, mid = FEE_GROUP_SHARE["저"], FEE_GROUP_SHARE["저"] + FEE_GROUP_SHARE["중"]
    return pd.Series(np.select([rank <= lo, rank <= mid], ["저", "중"], "고"), index=df.index)


def draw_ages(groups: np.ndarray, rng) -> tuple[np.ndarray, np.ndarray]:
    """요금그룹별로 연령대를 뽑고, 구간 안에서는 균등하게 나이를 준다.

    구간 안 분포까지 맞출 근거가 없다. 이미지가 주는 건 10년 단위 비율뿐이고,
    원자료의 구간 내 나이는 패널 고령화로 위쪽에 쏠려 있어 쓰면 오히려 나빠진다.
    """
    bands = np.empty(len(groups), dtype=int)
    for g, table in AGE_GIVEN_FEE.items():
        hit = groups == g
        if hit.any():
            keys = list(table)
            p = np.array([table[k] for k in keys], dtype=float)
            bands[hit] = rng.choice(keys, size=int(hit.sum()), p=p / p.sum())

    lo = np.array([AGE_BANDS[b][0] for b in bands])
    hi = np.array([AGE_BANDS[b][1] for b in bands])
    return rng.integers(lo, hi + 1), bands


def make(n: int = N_DEFAULT, seed: int = SEED) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    plans = load_mvno_plans()
    plans["fee_group"] = assign_fee_group(plans)

    w = plans["subscriber_count"].to_numpy(dtype=float)
    idx = rng.choice(len(plans), size=n, p=w / w.sum())
    src = plans.iloc[idx].reset_index(drop=True)

    unlimited = src["data_unlimited"].eq(True).to_numpy()
    ratio = rng.uniform(*USAGE_RATIO, size=n)
    # 무제한 가입자는 사용량으로 요금제를 고른 게 아니다. GB를 지어내면 그 숫자가
    # 그대로 추천 근거가 되므로, 무제한은 플래그만 주고 GB는 비운다.
    # 반올림하면 1GB 미만 요금제에서 제공량을 넘어선다(0.293 * 0.95 -> 0.3).
    # 내림으로 자르고, 그래도 0이 되는 소량 요금제는 제공량을 그대로 쓴다.
    used = np.floor(src["data_gb"].to_numpy() * ratio * 10) / 10
    used = np.where(used <= 0, src["data_gb"].to_numpy(), used)
    data_gb = np.where(unlimited, np.nan, used)

    voice_need = _used_amount(src["voice_minutes"].to_numpy(),
                              src["voice_unlimited"].eq(True).to_numpy(), rng, n)
    sms_need = _used_amount(src["sms_count"].to_numpy(),
                            src["sms_unlimited"].eq(True).to_numpy(), rng, n)

    ott = [
        rng.choice(str(o).split(" | ")) if isinstance(o, str) and o and rng.random() < OTT_WANT_PROB else ""
        for o in src["ott_options"]
    ]
    # OTT를 원한다고 한 사람에게만 묻는 값이다. 안 원하는 사람은 항상 False.
    ott_required = np.array(ott) != ""
    ott_required &= rng.random(n) < OTT_REQUIRED_PROB

    age, _ = draw_ages(src["fee_group"].to_numpy(), rng)
    fee = src["discounted_fee"].astype(int).to_numpy()

    return pd.DataFrame({
        "customer_id": [f"M{i:06d}" for i in range(n)],
        "age": age,
        "gender": np.where(rng.random(n) < MALE_PROB, "남", "여"),
        "data_gb_month": data_gb,
        "data_unlimited_need": unlimited,
        "voice_unlimited_need": src["voice_unlimited"].eq(True).to_numpy(),
        "sms_unlimited_need": src["sms_unlimited"].eq(True).to_numpy(),
        "voice_minutes_need": voice_need,
        "sms_count_need": sms_need,
        "carrier_type": "MVNO",
        # 알뜰폰은 어느 망이든 3사로 번호이동이라 제한이 없다. 하나로 묶는다
        # (`profile_input.CARRIERS`와 같은 값이어야 폼에 그대로 넣을 수 있다).
        "current_carrier": "알뜰폰",
        # 지금 알뜰폰을 쓰는 사람은 알뜰폰을 후보에서 빼지 않는다. 전원 True다.
        "mvno_ok": True,
        "current_fee_krw": fee,
        "budget_krw": fee,          # 갈아타기 전제 - 지금보다 비싼 건 추천하지 않는다
        "ott_want": ott,
        "ott_required": ott_required,
        "source_plan_id": src["plan_id"].to_numpy(),
        "fee_group": src["fee_group"].to_numpy(),
    })[OUT_COLUMNS]


def check() -> None:
    from profile_input import CARRIERS

    df = make(n=20_000, seed=1)

    assert len(df) == 20_000
    assert list(df.columns) == OUT_COLUMNS

    # 전원 알뜰폰이고, 알뜰폰을 후보에서 빼지 않는다.
    assert df["carrier_type"].eq("MVNO").all()
    assert df["mvno_ok"].all()
    assert set(df["current_carrier"]) <= set(CARRIERS), set(df["current_carrier"])

    # 나이는 이미지가 다루는 구간 안에만 있다.
    assert df["age"].between(20, 69).all(), (df["age"].min(), df["age"].max())

    # 무제한 인물에게는 GB가 없고, 아닌 인물에게는 있다.
    unl = df["data_unlimited_need"]
    assert df.loc[unl, "data_gb_month"].isna().all()
    assert df.loc[~unl, "data_gb_month"].notna().all()

    # 예산은 현재 요금과 같다.
    assert (df["budget_krw"] == df["current_fee_krw"]).all()

    # 사용량이 원본 요금제 제공량을 넘지 않는다. 넘으면 그 인물은 자기가 쓰는
    # 요금제로도 부족한 사람이 되어, 합성 전제가 깨진다.
    src = load_mvno_plans().set_index("plan_id")
    for need, col in (("data_gb_month", "data_gb"),
                      ("voice_minutes_need", "voice_minutes"),
                      ("sms_count_need", "sms_count")):
        cap = df["source_plan_id"].map(src[col])
        over = df[need].notna() & cap.notna() & (df[need] > cap)
        assert not over.any(), f"{need} 제공량 초과 {int(over.sum())}명"

    # 통화·문자 분수는 무제한이 아닌 사람에게만 있다. `recommend.usable_count()`가
    # 무제한을 inf로 보는 것과 짝이 맞아야 한다.
    for need, unlimited_col in (("voice_minutes_need", "voice_unlimited_need"),
                                ("sms_count_need", "sms_unlimited_need")):
        assert df.loc[df[unlimited_col], need].isna().all(), f"{need}: 무제한인데 분수가 있다"
        assert df.loc[~df[unlimited_col], need].notna().any(), f"{need}: 유제한인데 전부 비었다"

    # OTT를 원하는 사람이 있고, 전부는 아니다.
    want = df["ott_want"].ne("").mean()
    assert 0 < want < 0.5, want
    assert not df.loc[df["ott_want"].eq(""), "ott_required"].any()
    assert df["ott_required"].any(), "아무도 필수로 걸지 않았다"

    # 요금그룹 비중이 조사값 근처.
    got = df["fee_group"].value_counts(normalize=True)
    for g, want_share in FEE_GROUP_SHARE.items():
        assert abs(got[g] - want_share) < 0.03, f"{g} 그룹 {got[g]:.3f} != {want_share}"

    # 요금그룹이 사용량 대리변수로 작동한다. 이게 깨지면 나이에 넣은 신호가
    # 사용량과 무관해져서 실험이 성립하지 않는다.
    gb = df.groupby("fee_group")["data_gb_month"].mean()
    assert gb["저"] < gb["중"] < gb["고"], gb.to_dict()

    # 연령 주변분포가 이미지 표를 되살린다. 분위수로 잘라 그룹 비중이 양쪽 같으므로
    # 구조적으로 보존되어야 한다.
    want_marginal = {20: 5.4, 30: 21.5, 40: 26.8, 50: 28.0, 60: 18.3}
    got_marginal = (df["age"] // 10 * 10).value_counts(normalize=True).mul(100)
    for band, pct in want_marginal.items():
        assert abs(got_marginal[band] - pct) < 3.0, f"{band}대 {got_marginal[band]:.1f} != {pct}"

    # 나이와 요금대에 상관이 있다. 없으면 인구통계 ML이 잴 게 없어진다 - 이 파일이
    # 존재하는 이유가 통째로 사라지는 지점이라 느슨하게라도 걸어둔다.
    old = df["age"].ge(60)
    assert old[df["fee_group"].eq("저")].mean() > old[df["fee_group"].eq("고")].mean() * 1.5

    print(f"check ok: 연령 중앙 {df['age'].median():.0f}세, "
          f"요금그룹 {got.round(3).to_dict()}, "
          f"평균GB 저 {gb['저']:.1f} / 중 {gb['중']:.1f} / 고 {gb['고']:.1f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=N_DEFAULT)
    ap.add_argument("--check", action="store_true", help="자체 검증만 하고 파일은 쓰지 않는다")
    args = ap.parse_args()

    if args.check:
        check()
    else:
        df = make(args.n)
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(OUT_PATH, index=False, encoding="utf-8-sig")
        print(f"{len(df):,}명 -> {OUT_PATH}")
