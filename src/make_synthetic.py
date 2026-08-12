"""합성 고객 데이터 생성.

요금제를 가입자 수(subscriber_count) 비율로 뽑고, 그 요금제의 스펙에서 사람의
속성을 역산한다. "이 요금제를 쓰는 사람은 이 정도 쓴다"고 가정하는 것이라
순환이 있지만, 분포만큼은 316만 건 실측에서 온다.

    python src/make_synthetic.py            # 4만 명 생성
    python src/make_synthetic.py --check    # 자체 검증만

산출물: data/synthetic/customers.csv
"""

import argparse
import sys

import numpy as np
import pandas as pd

from recommend import load_plans as read_plans
from schema import DATA_DIR

N_DEFAULT = 40_000
SEED = 20260811

# 조사상 알뜰폰 가입자 비중(한국미디어패널조사). 우리 요금제 테이블은 상품 수
# 기준으로 MVNO가 80%라, 그대로 뽑으면 합성 인구가 알뜰폰 일색이 된다.
MVNO_SHARE = 0.041

# 요금제에 OTT가 붙어 있어도 그걸 원해서 고른 사람만 있는 건 아니다. 딸려온 경우를
# 빼려고 확률을 낮춰 잡는다.
OTT_WANT_PROB = 0.7

# 제공량을 다 쓰지는 않는다고 보되, 요금제를 고른 이유가 사라질 만큼 적게 쓰지도
# 않는다고 본다. 데이터·통화·문자에 같이 쓴다.
USAGE_RATIO = (0.9, 1.0)

# 아래 둘은 요금제 스펙에서 역산할 수 없다. "알뜰폰도 볼 생각이 있는가", "OTT가
# 없으면 아예 안 보겠는가"는 사람의 선호지 요금제의 성질이 아니다.
# ponytail: 근거 없는 확률이다. 설문이나 로그가 생기면 그때 교체한다. 지금은
# 두 축 모두 "한쪽으로 쏠리지 않는다"는 것 말고는 주장하지 않는다.
MVNO_OPEN_PROB = 0.5       # 지금 3사를 쓰는 사람이 알뜰폰도 후보로 두는 비율
OTT_REQUIRED_PROB = 0.3    # OTT를 원하는 사람 중 "그게 없으면 안 본다"는 비율

OUT_PATH = DATA_DIR / "synthetic" / "customers.csv"

COLUMNS = [
    "customer_id", "age", "gender",
    "data_gb_month", "data_unlimited_need",
    "voice_unlimited_need", "sms_unlimited_need",
    "voice_minutes_need", "sms_count_need",
    "carrier_type", "current_carrier", "mvno_ok",
    "current_fee_krw", "budget_krw", "ott_want", "ott_required",
    "source_plan_id",
]


def load_plans() -> pd.DataFrame:
    """요금제 테이블에서 합성에 쓸 수 있는 행만 남긴다."""
    df = read_plans()
    # 가격을 모르면 현재 요금도 예산도 만들 수 없다.
    df = df[df["discounted_fee"].notna()]
    # 무제한이 아닌데 제공량이 비어 있으면 사용량을 역산할 근거가 없다.
    df = df[df["data_unlimited"].eq(True) | df["data_gb"].notna()]
    return df.reset_index(drop=True)


def sampling_weights(df: pd.DataFrame) -> np.ndarray:
    """MVNO는 가입자 수 비례, MNO는 균등. 둘의 합계 비중은 조사값에 맞춘다.

    가입자 수는 모요(알뜰폰 비교 사이트)에서만 나오므로 MNO 쪽에는 없다. MNO 안의
    분포를 알 방법이 없어 균등으로 둔다.
    """
    is_mvno = df["carrier_type"].eq("MVNO")
    w = np.zeros(len(df))

    mvno = df["subscriber_count"].where(is_mvno).fillna(0).to_numpy()
    if mvno.sum() > 0:
        w += MVNO_SHARE * mvno / mvno.sum()

    # ponytail: MNO 내부는 균등 배분. 통신사별 가입자 통계가 생기면 그걸로 교체.
    mno = (~is_mvno).to_numpy(dtype=float)
    if mno.sum() > 0:
        w += (1 - MVNO_SHARE) * mno / mno.sum()

    return w / w.sum()


def load_demographics() -> pd.DataFrame | None:
    """KISDI 원자료에서 나이·성별을 읽는다. 없으면 None.

    지금 받아둔 3개 파일에는 개인정보 섹션이 빠져 있어(통신비·단말·이용행태만
    받음) 대개 None이 된다. 나이·성별이 필요하면 개인정보 섹션을 추가로 받아
    data/external/ 아래에 두면 된다.
    """
    for path in sorted((DATA_DIR / "external").glob("*.csv")):
        head = pd.read_csv(path, nrows=1, low_memory=False)
        if {"p__gender", "p__age1"} <= set(head.columns):
            d = pd.read_csv(path, low_memory=False, usecols=["p__gender", "p__age1"])
            d = d.dropna()
            # 조사가 적어 둔 만나이를 그대로 쓴다. `p__byear`로 역산하면 조사 연도가
            # 아니라 **오늘** 기준이 되어 해가 바뀔 때마다 전원이 한 살씩 늙는다
            # (2025년 조사인데 2026년에 돌리면 +1). 전수 대조: `p__age1`은
            # `2025 - p__byear`와 8,411행 전부 일치한다.
            # `p__age`는 나이가 아니라 연령대 코드(1~8)다. 쓰지 않는다.
            d["age"] = d["p__age1"].astype(int)
            d["gender"] = d["p__gender"].map({1: "남", 2: "여"})
            return d[["age", "gender"]].query("14 <= age <= 90").reset_index(drop=True)
    return None


def _used_amount(quota: np.ndarray, unlimited: np.ndarray, rng, n: int,
                 ratio: tuple[float, float] = USAGE_RATIO) -> np.ndarray:
    """제공량에서 실사용량을 역산한다. 무제한이거나 제공량이 없으면 NaN.

    `data_gb`와 달리 내림을 쓰지 않는다 - 분·건은 정수 단위라 0.5분 같은 값이
    나와도 조건 비교(`>= 300분`)에 문제가 없고, 내림으로 0을 만들면 "통화를 전혀
    안 하는 사람"이 되어 버린다.

    `ratio`는 `make_synthetic_mvno.py`가 다른 폭(0.8~1.0)을 쓰려고 열어둔 것이다.
    """
    used = np.round(quota * rng.uniform(*ratio, size=n))
    return np.where(unlimited, np.nan, used)


def make(n: int = N_DEFAULT, seed: int = SEED) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    plans = load_plans()
    idx = rng.choice(len(plans), size=n, p=sampling_weights(plans))
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

    # 통화·문자도 데이터와 같은 규칙이다. 무제한 가입자에게는 분수를 주지 않는다 -
    # 요금제가 무제한이면 사이트가 제공량을 안 적어서(전수 확인: 겹침 0) 역산할
    # 근거가 없다. `recommend.usable_count()`가 무제한을 inf로 보는 것과 짝이 맞는다.
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

    is_mvno = src["carrier_type"].eq("MVNO").to_numpy()
    # 지금 알뜰폰을 쓰는 사람은 알뜰폰을 후보에서 빼지 않는다. 3사 사용자만 갈린다.
    mvno_ok = is_mvno | (rng.random(n) < MVNO_OPEN_PROB)
    # 알뜰폰은 어느 망이든 3사로 번호이동이라 제한이 없다. 하나로 묶는다
    # (`profile_input.CARRIERS`와 같은 값이어야 폼에 그대로 넣을 수 있다).
    current_carrier = np.where(is_mvno, "알뜰폰", src["host_mno"].to_numpy())

    fee = src["discounted_fee"].astype(int).to_numpy()
    out = pd.DataFrame({
        "customer_id": [f"C{i:06d}" for i in range(n)],
        "age": "",
        "gender": "",
        "data_gb_month": data_gb,
        "data_unlimited_need": unlimited,
        "voice_unlimited_need": src["voice_unlimited"].eq(True).to_numpy(),
        "sms_unlimited_need": src["sms_unlimited"].eq(True).to_numpy(),
        "voice_minutes_need": voice_need,
        "sms_count_need": sms_need,
        "carrier_type": src["carrier_type"].to_numpy(),
        "current_carrier": current_carrier,
        "mvno_ok": mvno_ok,
        "current_fee_krw": fee,
        "budget_krw": fee,          # 갈아타기 전제 — 지금보다 비싼 건 추천하지 않는다
        "ott_want": ott,
        "ott_required": ott_required,
        "source_plan_id": src["plan_id"].to_numpy(),
    })

    demo = load_demographics()
    if demo is None:
        print("경고: KISDI 개인정보(나이·성별)를 찾지 못해 두 컬럼을 비워 둔다.", file=sys.stderr)
    else:
        pick = demo.iloc[rng.choice(len(demo), size=n)]
        out["age"] = pick["age"].to_numpy()
        out["gender"] = pick["gender"].to_numpy()

    return out[COLUMNS]


def check() -> None:
    df = make(n=5_000, seed=1)

    assert len(df) == 5_000
    assert list(df.columns) == COLUMNS

    # 무제한 인물에게는 GB가 없고, 아닌 인물에게는 있다.
    unl = df["data_unlimited_need"]
    assert df.loc[unl, "data_gb_month"].isna().all()
    assert df.loc[~unl, "data_gb_month"].notna().all()

    # 예산은 현재 요금과 같다.
    assert (df["budget_krw"] == df["current_fee_krw"]).all()

    # 사용량이 원본 요금제 제공량을 넘지 않는다. 넘으면 그 인물은 자기가 쓰는
    # 요금제로도 부족한 사람이 되어, 합성 전제가 깨진다.
    src = load_plans().set_index("plan_id")["data_gb"]
    cap = df["source_plan_id"].map(src)
    over = (~unl) & (df["data_gb_month"] > cap)
    assert not over.any(), f"제공량 초과 {int(over.sum())}명"

    # MVNO 비중이 조사값 근처. 5천 명 표본이라 폭을 넉넉히 잡는다.
    share = df["carrier_type"].eq("MVNO").mean()
    assert 0.02 <= share <= 0.07, share

    # OTT를 원하는 사람이 있고, 전부는 아니다.
    want = df["ott_want"].ne("").mean()
    assert 0 < want < 0.5, want
    # "OTT 없으면 안 본다"는 OTT를 원한다고 한 사람에게만 붙는다.
    assert not df.loc[df["ott_want"].eq(""), "ott_required"].any()
    assert df["ott_required"].any(), "아무도 필수로 걸지 않았다"

    # 통화·문자 분수는 무제한이 아닌 사람에게만 있다. `recommend.usable_count()`가
    # 무제한을 inf로 보는 것과 짝이 맞아야 한다.
    for need, unlimited_col in (("voice_minutes_need", "voice_unlimited_need"),
                                ("sms_count_need", "sms_unlimited_need")):
        unlimited = df[unlimited_col]
        assert df.loc[unlimited, need].isna().all(), f"{need}: 무제한인데 분수가 있다"
        assert df.loc[~unlimited, need].notna().any(), f"{need}: 유제한인데 전부 비었다"

    # 사용량이 원본 요금제 제공량을 넘지 않는다 (데이터와 같은 규칙).
    src_plans = load_plans().set_index("plan_id")
    for need, col in (("voice_minutes_need", "voice_minutes"),
                      ("sms_count_need", "sms_count")):
        cap = df["source_plan_id"].map(src_plans[col])
        over = df[need].notna() & cap.notna() & (df[need] > cap)
        assert not over.any(), f"{need} 제공량 초과 {int(over.sum())}명"

    # 지금 쓰는 통신사는 폼이 받는 값(profile_input.CARRIERS)과 같아야 한다.
    from profile_input import CARRIERS

    assert set(df["current_carrier"]) <= set(CARRIERS), set(df["current_carrier"])
    # 알뜰폰 사용자는 알뜰폰을 후보에서 빼지 않는다.
    assert df.loc[df["carrier_type"].eq("MVNO"), "mvno_ok"].all()
    # 3사 사용자는 갈린다. 한쪽으로 쏠리면 축으로 못 쓴다.
    mno_open = df.loc[df["carrier_type"].eq("MNO"), "mvno_ok"].mean()
    assert 0.3 < mno_open < 0.7, mno_open

    print(f"check ok: MVNO {share:.1%}, OTT 희망 {want:.1%}, "
          f"통화 분수 보유 {df['voice_minutes_need'].notna().mean():.1%}")


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
