"""모요 가입자 수(subscriber_count)의 일별 추이.

    python src/subscriber_trend.py               # 최근 하루 증가 상위 20개
    python src/subscriber_trend.py --top 50
    python src/subscriber_trend.py --plan 10271  # 특정 요금제 전체 시계열
    python src/subscriber_trend.py --csv data/review/subscriber_trend.csv

모요 카드의 "N명이 선택"은 누적값이라 일별 신규 가입은 스냅샷 간 차분으로 낸다.
스냅샷은 data/final/history/*/ 와 현재 최종본을 쓴다. 날짜는 디렉터리명이 아니라
파일 안의 crawled_at에서 뽑는다 - history/D 에 들어 있는 건 D일에 백업한 **직전**
데이터라 디렉터리명과 한 회차씩 어긋난다. 갱신을 거른 날이 있어(08-15~17) 인접한
두 스냅샷이 하루 간격이 아닐 수 있으므로, 그 간격(`days_elapsed`)으로 나눠
**하루당 증가 수**로 맞춘다.
"""
import argparse

import pandas as pd

from schema import BASE_DIR, final_path

HISTORY_DIR = BASE_DIR / "data" / "final" / "history"
FILENAME = "통신요금제_통합데이터_최종.csv"
COLS = ["plan_id", "plan_name", "subscriber_count", "crawled_at"]
# 누적값이 실측인지 난수인지 표시하는 컬럼. 두 번째는 예전 이름이라 이미 쌓인
# history 스냅샷에 남아 있다 - 하나만 보면 예전 파일의 난수가 실측으로 둔갑한다.
SOURCE_COLS = ("subscriber_count_source", "subscriber_data_source")


def load() -> pd.DataFrame:
    """(date, plan_id, plan_name, subscriber_count, days_elapsed, new_subscribers) 롱 포맷.

    new_subscribers는 스냅샷 간 증가분을 그 간격(일)으로 나눈 **하루당** 값이다.
    """
    paths = [d / FILENAME for d in sorted(HISTORY_DIR.iterdir()) if (d / FILENAME).exists()]
    paths.append(final_path(FILENAME))

    frames = []
    for path in paths:
        # fill_subscriber_daily가 채운 난수를 다시 차분에 넣으면 안 되므로 실측만
        # 남긴다. 판단 기준은 **누적값**의 출처다 - 하루 증가가 추정이어도 누적이
        # 실측이면 다음 날 차분의 기준점으로 써야 한다.
        df = pd.read_csv(path, usecols=lambda c: c in COLS or c in SOURCE_COLS)
        source = next((c for c in SOURCE_COLS if c in df.columns), None)
        if source:
            df = df[df[source] == "crawled"].drop(columns=[source])
        df = df[df["subscriber_count"].notna()]
        df["date"] = df["crawled_at"].str[:10].max()
        frames.append(df.drop(columns=["crawled_at"]))

    out = pd.concat(frames, ignore_index=True).drop_duplicates(["date", "plan_id"])
    out = out.sort_values(["plan_id", "date"])
    by_plan = out.groupby("plan_id")
    out["days_elapsed"] = by_plan["date"].transform(lambda s: pd.to_datetime(s).diff().dt.days)
    out["new_subscribers"] = by_plan["subscriber_count"].diff() / out["days_elapsed"]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", help="plan_id 하나의 전체 시계열만 본다")
    ap.add_argument("--top", type=int, default=20, help="마지막 날 증가 상위 N개")
    ap.add_argument("--csv", help="전체 롱 포맷을 이 경로에 쓴다")
    args = ap.parse_args()

    df = load()
    if args.csv:
        df.to_csv(args.csv, index=False, encoding="utf-8-sig")
        print(f"wrote {args.csv} ({len(df):,} rows)")

    if args.plan:
        one = df[df["plan_id"].astype(str) == args.plan]
        cols = ["date", "plan_name", "subscriber_count", "days_elapsed", "new_subscribers"]
        print(one[cols].to_string(index=False, float_format="%.1f"))
        return

    last = df["date"].max()
    top = df[(df["date"] == last) & df["new_subscribers"].notna()]
    top = top.nlargest(args.top, "new_subscribers")
    gap = top["days_elapsed"].max()
    print(f"[{last}] 하루당 증가 상위 {args.top} (직전 스냅샷과 {gap:.0f}일 간격)")
    cols = ["plan_id", "plan_name", "subscriber_count", "new_subscribers"]
    print(top[cols].to_string(index=False, float_format="%.1f"))
    # 모요는 개편·재출시되면 "N명이 선택"을 0부터 다시 센다(하루 -5만도 나온다).
    # 감소분까지 더하면 총계가 무의미해져서 증가분만 합산한다.
    diffs = df[df["date"] == last]["new_subscribers"].dropna()
    print(f"\n하루당 증가 합계: {diffs[diffs > 0].sum():,.0f}명"
          f" / 감소 {int((diffs < 0).sum())}개(카운터 리셋 추정)")


if __name__ == "__main__":
    main()
