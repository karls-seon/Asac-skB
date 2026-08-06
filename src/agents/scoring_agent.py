"""요금제 매칭 스코어링 프로토타입.

2026-08-06 설계 논의 결론: 카탈로그(2,799행)가 이미 구조화돼 있고 학습에 쓸
정답 레이블(실제 클릭/전환 피드백)이 없어서 ML이 아니라 규칙 기반으로 간다.
필터(자격·하한선 미달 제거) -> 가중합 점수, 2단계. 사용자는 보상적으로
결정하지 않는다 - 데이터가 모자라면 아무리 싸도 안 산다. 그래서 하한선
미달은 점수를 깎는 게 아니라 후보에서 아예 뺀다.

가중치는 이 파일의 DEFAULT_WEIGHTS가 기본값이고, 대화 중 사용자가 자연어로
"가격이 더 중요해요" 같은 신호를 주면 그 축의 weight를 올리는 override가
나중에 붙는다(현재는 자리만 있고 실제 자연어 추론은 미구현 - user agent
쪽에서 override dict를 만들어 넘겨주면 이 함수는 그냥 받아서 쓴다).

합성 고객 데이터(data/synthetic/)는 안 쓴다 - 실서비스엔 없는 실측치라
프로토타입에서도 의존하면 안 된다. 데이터 사용량은 사용자에게 직접 물어봐서
받는다는 전제로, 여기서는 프로필 dict에 값을 바로 채워 넣는 방식으로
시뮬레이션한다.
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from schema import final_path  # noqa: E402

# EDA(notebooks/eda_plans.ipynb 섹션18) 근거: discounted_fee가 시장에서
# 가장 크게 갈리는 축(2만원 이하에 56%가 몰림)이라 price를 가장 무겁게 둔다.
# 나머지는 임시값 - 실사용 피드백 없이는 이게 유일한 근거라, 나중에
# 클릭/전환 데이터가 쌓이면 다시 잡아야 한다(지금은 "자리"만 확정).
DEFAULT_WEIGHTS = {
    "price": 0.40,
    "data": 0.30,
    "qos": 0.15,
    "tethering": 0.10,
    "ott": 0.05,
}

# signup_notice가 "이미 OO 요금제를 쓰고 있다면 이 요금제를 가입할 수
# 없어요" 형태일 때, 사용자의 현재 통신사와 겹치는지 보려면 이 표기가
# 필요하다. host_mno 컬럼값("LGU+")과 원문 표기("LG U+")가 다르다.
_HOST_DISPLAY = {"KT": "KT", "SKT": "SKT", "LGU+": "LG U+"}


def _current_carrier_blocks(row, current_carrier: str | None) -> bool:
    """signup_notice에 "이미 {현재 통신사} 쓰면 가입 불가"라고 적혀 있는지.

    문구 전체를 분류하려면 NLU가 필요하지만(6가지 유형 중 하나일 뿐이라),
    지금 필터링에 필요한 건 "동일 통신사 중복가입 불가" 하나뿐이라 그
    문구만 부분일치로 본다. 나머지 5개 유형(모요 비제휴/KB계좌 필요 등)은
    아직 슬롯이 없어서 거르지 않는다 - 있는 척하지 않는다.
    """
    if not current_carrier or pd.isna(row.get("signup_notice")):
        return False
    display = _HOST_DISPLAY.get(current_carrier, current_carrier)
    return f"이미 {display} 요금제를 쓰고 있다면" in row["signup_notice"]


def filter_eligible(plans: pd.DataFrame, profile: dict) -> pd.DataFrame:
    """자격/하한선 필터. 통과 못 하면 점수 아무리 높아도 후보가 아니다.

    가격 기준은 discounted_fee(지금 내는 월 납부액) 하나다. 처음엔 24개월
    총비용(TCO)으로 잡았다가 걷어냈다 - 알뜰폰 이용자는 프로모션 기간
    (중앙값 7개월)마다 갈아타는 게 일반적이라 24개월을 눌러앉는다는 전제
    자체가 틀렸다. MNO 선택약정 할인은 기간 제한 없이 유지되는 할인이라
    discounted_fee가 곧 안정적인 월 납부액이고, MVNO는 갈아타므로 역시
    discounted_fee가 실제로 내는 돈이다 - 양쪽 다 같은 컬럼이 정답이 된다.

    프로모션 종료 후 인상(중앙값 137%)은 안 갈아타는 사용자에겐 실재하는
    위험이라 버리지 않고 promo_ends_after/price_after_promo로 남긴다.
    점수에는 섞지 않는다 - 갈아탈 사람에겐 없는 비용이라 감점 사유가 아니다.
    """
    df = plans.copy()
    df["monthly_cost"] = df["discounted_fee"].fillna(df["monthly_fee"])
    df["promo_ends_after"] = df["discount_period_months"]
    df["price_after_promo"] = df["monthly_fee"].where(df["discount_period_months"].notna())

    budget = profile.get("budget_krw")
    if budget is not None:
        df = df[df["monthly_cost"] <= budget]

    carrier_type = profile.get("preferred_carrier_type")
    if carrier_type:
        df = df[df["carrier_type"] == carrier_type]

    network = profile.get("preferred_network")
    if network:
        df = df[df["network_gen"] == network]

    if profile.get("data_unlimited_required"):
        df = df[df["data_unlimited"].fillna(False)]
    elif profile.get("data_usage_gb") is not None:
        usage = profile["data_usage_gb"]
        df = df[df["data_unlimited"].fillna(False) | (df["data_gb"].fillna(0) >= usage)]

    if profile.get("voice_unlimited_required"):
        df = df[df["voice_unlimited"].fillna(False)]

    current_carrier = profile.get("current_carrier")
    if current_carrier:
        blocked = df.apply(lambda r: _current_carrier_blocks(r, current_carrier), axis=1)
        df = df[~blocked]

    return df


def _minmax(series: pd.Series, higher_is_better: bool) -> pd.Series:
    """결측은 0.5(중립)로 - 정보 미공개를 감점 사유로 쓰지 않는다
    (EDA에서 tethering_gb 71%, data_throttle_speed 31%가 결측이라, 이걸
    0점 처리하면 정보를 안 준 요금제가 자동으로 다 밀려난다)."""
    valid = series.dropna()
    if valid.empty or valid.min() == valid.max():
        return series.apply(lambda v: 0.5 if pd.isna(v) else 1.0)
    lo, hi = valid.min(), valid.max()
    norm = (series - lo) / (hi - lo)
    if not higher_is_better:
        norm = 1 - norm
    return norm.fillna(0.5)


def score(candidates: pd.DataFrame, profile: dict, weights: dict | None = None) -> pd.DataFrame:
    """필터를 통과한 후보만 가중합으로 정렬. weights는 DEFAULT_WEIGHTS를
    기본으로 하고 profile/override로 넘어온 값만 덮어쓴다."""
    if candidates.empty:
        return candidates

    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    df = candidates.copy()

    df["_price_score"] = _minmax(df["monthly_cost"], higher_is_better=False)

    qos_speed = df["data_throttle_speed"].str.extract(r"([\d.]+)")[0].astype(float)
    data_score = df["data_gb"].copy()
    data_score[df["data_unlimited"].fillna(False)] = data_score.max() if data_score.notna().any() else 999
    df["_data_score"] = _minmax(data_score, higher_is_better=True)
    df["_qos_score"] = _minmax(qos_speed, higher_is_better=True)
    df["_tethering_score"] = _minmax(df["tethering_gb"], higher_is_better=True)

    ott_pref = profile.get("ott_preference") or []
    if ott_pref:
        df["_ott_score"] = df["ott_options"].fillna("").apply(
            lambda opts: 1.0 if any(o in opts for o in ott_pref) else 0.0
        )
    else:
        df["_ott_score"] = 0.5  # 선호 없으면 중립 - 있는 요금제를 부당하게 깎지 않음

    df["match_score"] = (
        w["price"] * df["_price_score"]
        + w["data"] * df["_data_score"]
        + w["qos"] * df["_qos_score"]
        + w["tethering"] * df["_tethering_score"]
        + w["ott"] * df["_ott_score"]
    )

    # 동률 다수(EDA: 스펙 완전동일 그룹 392개, 1,541행) 대비 tie-breaker.
    # benefit_count가 많은 쪽을 우선 - "혜택이 더 많이 딸려 있다"가 실사용자
    # 입장에서 가장 직관적인 2차 기준.
    return df.sort_values(
        ["match_score", "benefit_count"], ascending=[False, False]
    ).reset_index(drop=True)


def recommend(profile: dict, weights: dict | None = None, top_n: int = 5) -> pd.DataFrame:
    plans = pd.read_csv(final_path("통신요금제_통합데이터_최종.csv"), encoding="utf-8-sig")
    eligible = filter_eligible(plans, profile)
    ranked = score(eligible, profile, weights)
    cols = ["plan_id", "plan_name", "carrier_type", "host_mno", "monthly_cost",
            "promo_ends_after", "price_after_promo", "data_gb", "data_unlimited",
            "match_score"]
    return ranked[cols].head(top_n)


def demo():
    """assert 기반 자체 점검 + 예시 프로필 2개 실행. python -m src.agents.scoring_agent"""
    profile_budget = {
        "budget_krw": 30000,
        "preferred_network": "5G",
        "data_usage_gb": 10,
    }
    result = recommend(profile_budget)
    assert not result.empty, "예산 3만원/5G/10GB 조건인데 후보가 하나도 없음 - 필터가 너무 빡빡함"
    assert (result["monthly_cost"] <= 30000).all(), "예산 초과 요금제가 필터를 안 걸러짐"
    print("=== 프로필 1: 예산 3만원 / 5G / 10GB ===")
    print(result.to_string(index=False))

    profile_mvno_lock = {
        "preferred_carrier_type": "MNO",
        "current_carrier": "KT",
    }
    plans = pd.read_csv(final_path("통신요금제_통합데이터_최종.csv"), encoding="utf-8-sig")
    eligible2 = filter_eligible(plans, profile_mvno_lock)
    still_blocked = eligible2[
        eligible2["signup_notice"].fillna("").str.contains("이미 KT 요금제를 쓰고 있다면")
    ]
    assert still_blocked.empty, (
        f"KT 가입중 사용자한테 KT 중복가입 제한 요금제 {len(still_blocked)}건이 안 걸러짐"
    )
    result2 = recommend(profile_mvno_lock)
    print("\n=== 프로필 2: MNO만 / 현재 KT 사용중(요고 모요채널 등 중복가입 제한 제외 확인) ===")
    print(result2.to_string(index=False))

    print("\n자체 점검 통과.")


if __name__ == "__main__":
    demo()
