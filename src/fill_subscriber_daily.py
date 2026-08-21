"""최종 요금제 CSV에 일별 신규 가입자(`new_subscribers`) 컬럼을 붙이고,
값을 알 수 없는 칸은 현재 분포에서 뽑은 난수로 채운다.

    python src/fill_subscriber_daily.py            # 최종 CSV 갱신
    python src/fill_subscriber_daily.py --dry-run  # 안 쓰고 요약만

**채운 값은 실측이 아니다.** 어떤 칸이 실측인지는 `subscriber_count_source` /
`new_subscribers_source`(crawled / estimated)로 구분한다. 두 컬럼을 따로 두는
이유는 누적은 실측인데 하루 증가만 모르는 행이 많아서다(카운터 리셋 773행).
한 컬럼으로 뭉치면 크롤링해 온 누적값이 다음 날 차분에서 통째로 빠진다.

난수를 어디에, 왜 넣는지:
  ① MNO 548개 - 3사 사이트에는 "N명이 선택" 표시 자체가 없다. 모요 MVNO
     2,211개의 subscriber_count 경험분포에서 부트스트랩으로 뽑는다. 요금·
     데이터량과 인기의 상관이 사실상 0이라(로그 상관 -0.02 / 0.13) 요금 구간별로
     나눠 뽑아도 얻는 게 없다.
  ② 일별 신규 가입 - 직전 스냅샷이 없는 신규 요금제와 차분이 음수인 행(모요가
     개편 때 카운터를 0부터 다시 센다)은 실제 신규 가입을 알 수 없다. 관측된
     하루당 증가율 분포에서 뽑아 그 행의 subscriber_count에 곱한다.

`new_subscribers`는 **하루당** 증가 수다. 갱신을 거른 날이 있으면 스냅샷 간격이
하루가 아니므로 subscriber_trend.load()가 간격으로 나눠 준 값을 쓴다.

refresh_plans.py가 최종 CSV를 새로 쓰면 이 두 컬럼은 사라진다. 갱신 뒤 다시
돌리면 된다(시드 고정이라 같은 값이 나온다).
"""
import argparse

import numpy as np
import pandas as pd

from schema import final_path
from subscriber_trend import SOURCE_COLS, load as load_trend

PLAN_CSV = final_path("통신요금제_통합데이터_최종.csv")
SEED = 42


def fill(df: pd.DataFrame, trend: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    last = trend["date"].max()
    diffs = trend[trend["date"] == last].set_index("plan_id")["new_subscribers"]

    # 지난번 난수를 지우지 않으면 그게 실측으로 둔갑해 다음 표본에 섞인다.
    df = df.copy()
    df["subscriber_count"] = pd.to_numeric(df["subscriber_count"], errors="coerce")
    source = next((c for c in SOURCE_COLS if c in df.columns), None)
    if source:
        df.loc[df[source] == "estimated", "subscriber_count"] = np.nan
        df = df.drop(columns=[c for c in (*SOURCE_COLS, "new_subscribers_source", "new_subscribers")
                              if c in df.columns])

    df["new_subscribers"] = df["plan_id"].map(diffs)  # 하루당 증가
    known_count = df["subscriber_count"].notna()
    # 카운터 리셋으로 음수가 된 차분은 "모름"으로 되돌린다.
    df.loc[df["new_subscribers"] < 0, "new_subscribers"] = np.nan

    pool = df.loc[known_count, "subscriber_count"].to_numpy()
    rate = (df["new_subscribers"] / df["subscriber_count"]).replace([np.inf, -np.inf], np.nan)
    rate_pool = rate[(rate >= 0) & (rate < 1)].dropna().to_numpy()

    df["subscriber_count_source"] = np.where(known_count, "crawled", "estimated")
    df["new_subscribers_source"] = np.where(df["new_subscribers"].notna(), "crawled", "estimated")

    n = int((~known_count).sum())
    df.loc[~known_count, "subscriber_count"] = rng.choice(pool, size=n)

    need = df["new_subscribers"].isna()
    df.loc[need, "new_subscribers"] = np.round(
        df.loc[need, "subscriber_count"] * rng.choice(rate_pool, size=int(need.sum()))
    )

    df["subscriber_count"] = df["subscriber_count"].astype(int)
    df["new_subscribers"] = df["new_subscribers"].astype(int)

    assert df["subscriber_count"].min() > 0, "가입자 수 0 이하"
    assert df["new_subscribers"].min() >= 0, "신규 가입 음수 남음"
    assert (df["new_subscribers"] <= df["subscriber_count"]).all(), "신규가 누적보다 큼"

    cols = list(df.columns)
    for name in ("new_subscribers_source", "new_subscribers", "subscriber_count_source"):
        cols.remove(name)
        cols.insert(cols.index("subscriber_count") + 1, name)
    return df[cols]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    # 전 컬럼을 문자열로 읽는다. 숫자로 읽으면 결측이 있는 정수 컬럼이 float가 돼
    # "7"이 "7.0"으로 바뀌고 refresh_plans가 그걸 전부 "변경"으로 잡는다
    # (2026-08-20 갱신에서 허위 변경 4,771건).
    df = pd.read_csv(PLAN_CSV, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    trend = load_trend()
    trend["plan_id"] = trend["plan_id"].astype(str)

    out = fill(df, trend, np.random.default_rng(SEED))
    summary = out.groupby(["carrier_type", "subscriber_count_source", "new_subscribers_source"]).size()
    print(summary.to_string())
    print("\n일별 신규 가입 합계: {:,}명 (estimated 포함)".format(int(out["new_subscribers"].sum())))

    if args.dry_run:
        return
    # 파이프라인이 쓰는 형식(utf-8-sig / CRLF) 유지.
    out.to_csv(PLAN_CSV, index=False, encoding="utf-8-sig", lineterminator="\r\n")
    print(f"\nwrote {PLAN_CSV}")


if __name__ == "__main__":
    main()
