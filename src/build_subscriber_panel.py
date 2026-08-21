"""요금제 × 날짜 일별 가입자 패널을 만든다.

    python src/build_subscriber_panel.py                 # 최근 30일, 롱 포맷
    python src/build_subscriber_panel.py --days 60
    python src/build_subscriber_panel.py --wide           # 요금제 1행 × 날짜 컬럼
    python src/build_subscriber_panel.py --value subscriber_count --wide

출력: data/final/통신요금제_가입자일별_최종.csv
      (plan_id, plan_name, carrier_type, date, subscriber_count,
       new_subscribers, subscriber_count_source)
`plan_id`로 통합데이터 CSV와 조인한다. 통합데이터 본체에 날짜 컬럼을 붙이지 않은
건 날짜가 하루에 하나씩 늘어나는 컬럼이라 매일 스키마가 바뀌고 refresh_plans.py의
컬럼 비교가 깨지기 때문이다.

**대부분의 칸은 추정값이다.** 실측 스냅샷은 5일치(08-11/13/14/18/19)뿐이라
나머지 날짜는 이렇게 만들고, 칸마다 `subscriber_count_source`로 구분한다.

  ① 스냅샷 사이의 날 - 양 끝 실측을 선형 보간.
  ② 첫 실측보다 이전 날 - 관측된 하루당 증가율 r로 거꾸로 되감는다
     (count(d-1) = count(d) / (1+r)). 1명 밑으로 내려가면 아직 안 나온
     요금제로 보고 0으로 둔다.
  ③ 관측 구간 중간에 가입자 100명 이하로 처음 등장한 요금제 - 첫 등장 전날까지
     0. 그 사이에 실제로 출시된 것이므로 되감지 않는다. 처음부터 수만 명인 채로
     등장한 건 모요 목록에 늦게 올라온 것이라 ②로 되감는다.

**3사(MNO) 요금제는 아예 넣지 않는다.** 되감을 기준점이 난수뿐이고, 그 값은 모요
MVNO 분포에서 뽑은 것이라 실제 3사 가입자(요금제당 수십만~수백만)와 자릿수가
다르다. 채워 두면 "MNO 대 MVNO 추이"처럼 성립하지 않는 비교를 부른다.

한계: 되감기는 "지금 증가율이 과거에도 같았다"고 가정한다. 실제로는 출시 직후
빠르게 늘고 뒤로 갈수록 완만해지므로 과거 값이 실제보다 높게 잡힌다.
"""
import argparse

import numpy as np
import pandas as pd

from schema import final_path
from subscriber_trend import load as load_trend

PLAN_CSV = final_path("통신요금제_통합데이터_최종.csv")
OUT_CSV = final_path("통신요금제_가입자일별_최종.csv")
WIDE_CSV = final_path("통신요금제_가입자일별_가로_최종.csv")
SEED = 42
# 되감기용 증가율 상한(관측된 요금제별 중앙값의 95분위). 최대 0.199까지 나오는데
# 그대로 30일을 되감으면 "한 달 만에 237배로 늘었다"는 과거가 만들어진다.
MAX_BACKCAST_RATE = 0.05
# 관측 구간 중간에 처음 보인 요금제 중 "정말 그때 나온 것"으로 볼 상한.
NEW_PLAN_MAX = 100


def plan_rates(trend: pd.DataFrame) -> pd.Series:
    """요금제별 하루당 증가율(신규/누적)의 중앙값. 되감기 상한까지 잘라 준다."""
    rate = (trend["new_subscribers"] / trend["subscriber_count"]).replace([np.inf, -np.inf], np.nan)
    rate = rate.where((rate >= 0) & (rate < 1))
    return rate.groupby(trend["plan_id"]).median().clip(upper=MAX_BACKCAST_RATE)


def build(days: int, rng: np.random.Generator) -> pd.DataFrame:
    trend = load_trend()
    trend["plan_id"] = trend["plan_id"].astype(str)
    plans = pd.read_csv(PLAN_CSV, dtype={"plan_id": str}, encoding="utf-8-sig")
    plans = plans[plans["carrier_type"] == "MVNO"]  # 3사는 실측 가입자가 없어 제외

    end = pd.Timestamp(trend["date"].max())
    dates = pd.date_range(end=end, periods=days)

    observed = trend.pivot_table(index="plan_id", columns="date", values="subscriber_count")
    observed.columns = pd.to_datetime(observed.columns)
    observed = observed.reindex(index=plans["plan_id"], columns=dates)
    is_crawled = observed.notna().to_numpy()
    # 스냅샷에 한 번도 안 잡힌 요금제는 되감을 기준점이 없다. 통합데이터의 난수를
    # 끌어다 심으면 그게 실측처럼 30일치로 퍼지므로, 그냥 패널에서 뺀다.
    observed = observed[is_crawled.any(axis=1)]
    is_crawled = is_crawled[is_crawled.any(axis=1)]

    filled = observed.interpolate(axis=1, limit_area="inside").to_numpy()

    rates = plan_rates(trend).reindex(observed.index)
    pool = rates.dropna().to_numpy()
    rates = rates.fillna(pd.Series(rng.choice(pool, size=len(rates)), index=rates.index))

    # "중간 등장" 기준은 패널 첫날이 아니라 **가장 오래된 스냅샷 날짜**다. 패널
    # 첫날로 재면 모든 요금제가 중간 등장으로 잡혀 그 전 날짜가 전부 0이 된다.
    first_seen = pd.to_datetime(trend.groupby("plan_id")["date"].min()).reindex(observed.index)
    first_count = trend.sort_values("date").groupby("plan_id")["subscriber_count"].first().reindex(observed.index)
    launched_midway = (
        (first_seen > pd.Timestamp(trend["date"].min())) & (first_count <= NEW_PLAN_MAX)
    ).to_numpy()

    for i in range(len(filled)):
        known = np.flatnonzero(~np.isnan(filled[i]))
        if len(known) == 0:
            continue
        first, last = known[0], known[-1]
        filled[i, last + 1:] = filled[i, last]  # 최신 스냅샷에서 빠진 요금제
        if launched_midway[i]:
            filled[i, :first] = 0  # 관측 구간 안에서 출시된 요금제
            continue
        back = filled[i, first] / (1 + rates.iloc[i]) ** np.arange(first, 0, -1)
        filled[i, :first] = np.where(back < 1, 0, np.round(back))

    counts = pd.DataFrame(filled, index=observed.index, columns=dates).round()
    new = counts.diff(axis=1).clip(lower=0)  # 카운터 리셋으로 생기는 음수는 0으로
    new.iloc[:, 0] = 0

    counts.index.name, counts.columns.name = "plan_id", "date"
    panel = counts.reset_index().melt(id_vars="plan_id", value_name="subscriber_count")
    panel["new_subscribers"] = new.reset_index().melt(id_vars="plan_id")["value"].to_numpy()
    # melt는 날짜(컬럼) 순으로 펼치므로 요금제×날짜 행렬도 열 우선으로 편다.
    panel["subscriber_count_source"] = np.where(is_crawled.ravel(order="F"), "crawled", "estimated")
    panel["date"] = pd.to_datetime(panel["date"]).dt.strftime("%Y-%m-%d")
    meta = plans[["plan_id", "plan_name", "carrier_type"]].drop_duplicates("plan_id")
    panel = meta.merge(panel, on="plan_id")
    panel[["subscriber_count", "new_subscribers"]] = panel[["subscriber_count", "new_subscribers"]].astype(int)

    assert panel["subscriber_count"].min() >= 0
    assert panel["new_subscribers"].min() >= 0
    assert len(panel) == len(observed) * days, "요금제 × 날짜 격자가 안 맞음"
    assert (panel["carrier_type"] == "MVNO").all(), "MNO 행이 섞였음"
    return panel


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30, help="마지막 스냅샷부터 거슬러 며칠치")
    ap.add_argument("--wide", action="store_true", help="요금제 1행 × 날짜 컬럼 표도 쓴다")
    ap.add_argument("--value", default="new_subscribers",
                    choices=["new_subscribers", "subscriber_count"], help="--wide에 넣을 값")
    args = ap.parse_args()

    panel = build(args.days, np.random.default_rng(SEED))
    panel.to_csv(OUT_CSV, index=False, encoding="utf-8-sig", lineterminator="\r\n")
    print(f"wrote {OUT_CSV} ({len(panel):,} rows, {panel['date'].nunique()}일)")
    print(panel.groupby("subscriber_count_source").size().to_string())

    if args.wide:
        wide = panel.pivot(index=["plan_id", "plan_name", "carrier_type"],
                           columns="date", values=args.value).reset_index()
        wide.to_csv(WIDE_CSV, index=False, encoding="utf-8-sig", lineterminator="\r\n")
        print(f"wrote {WIDE_CSV} ({args.value}, {wide.shape[0]:,}행 × {wide.shape[1]}컬럼)")


if __name__ == "__main__":
    main()
