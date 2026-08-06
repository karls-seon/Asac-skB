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
import re
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


def _age_eligible(age_condition, user_age: int | None) -> bool:
    """이 행의 연령 조건을 사용자가 충족하는지.

    age_condition은 schema.normalize_age_condition이 "만 N세 이하/이상"으로
    맞춰 둔 값이라 그 패턴만 보면 된다. 비어 있으면 전연령 대상이고,
    "현역병사"처럼 나이가 아닌 자격은 여기서 판단할 수 없으므로 통과시킨다
    (거르려면 별도 슬롯이 필요한데 아직 없다 - 있는 척하지 않는다).
    나이를 안 물어봤으면(user_age=None) 전부 통과 - 모른다고 후보를
    미리 좁히면 연령 전용 요금제가 통째로 사라진다.
    """
    if user_age is None or pd.isna(age_condition) or not str(age_condition).strip():
        return True
    m = re.match(r"만 (\d+)세 (이하|이상)", str(age_condition))
    if not m:
        return True
    limit, direction = int(m.group(1)), m.group(2)
    return user_age <= limit if direction == "이하" else user_age >= limit


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

    user_age = profile.get("user_age")
    if user_age is not None:
        df = df[df["age_condition"].apply(lambda c: _age_eligible(c, user_age))]

    current_carrier = profile.get("current_carrier")
    if current_carrier:
        blocked = df.apply(lambda r: _current_carrier_blocks(r, current_carrier), axis=1)
        df = df[~blocked]

        # "지금 통신사 그대로" - 번호이동 자체가 번거로워서 안 옮기려는 니즈다.
        # 그래서 같은 망을 빌려 쓰는 알뜰폰(host_mno는 같지만 사업자가 다름)은
        # 조건을 충족하지 못한다. carrier_type까지 MNO로 좁혀야 진짜 "그대로"가
        # 된다. 반대로 현재 알뜰폰 사용자의 "쓰던 브랜드 유지"는 사용자의
        # mvno_brand를 알아야 하는데 슬롯이 없어서 아직 지원하지 않는다.
        if profile.get("keep_current_carrier"):
            df = df[(df["carrier_type"] == "MNO") & (df["host_mno"] == current_carrier)]

    return df


def _qos_mbps(speed: pd.Series) -> pd.Series:
    """"400Kbps"/"3Mbps" 같은 표기를 Mbps 숫자로 통일한다.

    숫자만 뽑으면 400Kbps(=0.4Mbps)가 10Mbps보다 40배 좋은 값이 된다.
    실제 데이터에 Kbps 표기가 275건(100/200/400Kbps) 있어서 그냥 지나칠 수
    없다 - 저속 구간이 통째로 최상위로 뒤집힌다.
    """
    num = speed.str.extract(r"([\d.]+)")[0].astype(float)
    is_kbps = speed.fillna("").str.contains("Kbps", case=False)
    return num.where(~is_kbps, num / 1000)


def _qos_score(df: pd.DataFrame) -> pd.Series:
    """소진 후 동작 점수. 결측의 의미가 무제한 여부에 따라 정반대다.

    | data_unlimited | QoS 표기 | 실제 의미                  | 점수 |
    |----------------|----------|----------------------------|------|
    | True           | 없음     | 완전 무제한(감속 자체 없음)| 최고 |
    | True           | 있음     | 일정량 후 감속 무제한      | 속도순 |
    | False          | 있음     | 소진 후 감속(추가요금 없음)| 속도순 |
    | False          | 없음     | 소진 후 **초과 과금**      | 최저 |

    스키마에서 초과요금 컬럼 5개를 뺄 때 "소진 후 동작은 data_throttle_speed로
    확인하라"고 정리해 뒀다(docs/컬럼_명세서.md). 그래서 정량제인데 이 값이
    비어 있으면 감속이 없다는 뜻이고, 감속이 없으면 남은 건 종량 과금이다.
    무제한(요고69·베스트Max 등 MNO 165건)은 반대로 감속 없이 계속 쓴다는
    뜻이라 만점이어야 한다. 둘을 같은 결측으로 묶으면 최고와 최악이 뭉갠다.
    """
    unlimited = df["data_unlimited"].fillna(False)
    speed = _qos_mbps(df["data_throttle_speed"])
    score = _minmax(speed, higher_is_better=True)
    score[speed.isna() & unlimited] = 1.0
    score[speed.isna() & ~unlimited] = 0.0
    return score


def _tethering_score(df: pd.DataFrame) -> pd.Series:
    """테더링 점수. tethering_gb가 비는 이유가 셋이고 의미가 정반대다
    (tethering_support 컬럼, docs/컬럼_명세서.md 참고).

    | tethering_support | 뜻                          | 점수         |
    |-------------------|-----------------------------|--------------|
    | quota             | 별도 한도 있음              | 한도 크기순  |
    | within_data       | 기본 데이터 전량 테더링 가능| 데이터 점수와 동일 |
    | unsupported       | 테더링 못 씀                | 0            |
    | undisclosed       | 사이트가 값을 안 알려줌     | 공개값 중앙값 |

    within_data를 0으로 두면 "데이터 전량을 테더링에 쓸 수 있는" 요금제가
    "테더링 자체가 안 되는" 요금제와 같아진다. 실제 데이터에서 unsupported가
    1,018건, within_data가 377건이라 둘을 뭉치면 오답이 대량으로 난다.
    """
    support = df["tethering_support"].fillna("undisclosed")
    score = _minmax(df["tethering_gb"], higher_is_better=True)
    score[support == "unsupported"] = 0.0
    # 별도 한도가 없을 뿐 기본 데이터를 그대로 쓸 수 있으니, 데이터 축에서
    # 받은 점수를 그대로 가져온다(데이터가 넉넉하면 테더링도 넉넉한 셈).
    score[support == "within_data"] = df.loc[support == "within_data", "_data_score"]
    return score


def _minmax(series: pd.Series, higher_is_better: bool) -> pd.Series:
    """min-max 정규화. 결측은 **공개된 값들의 중앙값**으로 채운다.

    0점 처리하면 정보를 안 준 요금제가 통째로 밀려나고(테더링 71%,
    QoS 31%가 결측이라 대량 학살이 난다), 반대로 0.5 고정으로 채우면
    이번엔 미공개가 과대평가된다 - 실측하면 테더링 중앙값(22.5GB)의
    정규화 점수가 0.27이라 0.5는 그보다 한참 후하다. 그러면 값을 정직하게
    공개한 중하위권 요금제가 숨긴 요금제보다 불리해져서, 정보를 감추는
    쪽이 이득인 구조가 된다. 중앙값으로 채우면 "모르면 평균적이라고
    가정"이라 어느 쪽으로도 치우치지 않는다.
    """
    valid = series.dropna()
    if valid.empty:
        return pd.Series(0.5, index=series.index)
    if valid.min() == valid.max():
        return series.notna().astype(float).where(series.notna(), 0.5)
    lo, hi = valid.min(), valid.max()
    norm = (series - lo) / (hi - lo)
    if not higher_is_better:
        norm = 1 - norm
    return norm.fillna(norm.median())


def score(candidates: pd.DataFrame, profile: dict, weights: dict | None = None) -> pd.DataFrame:
    """필터를 통과한 후보만 가중합으로 정렬. weights는 DEFAULT_WEIGHTS를
    기본으로 하고 profile/override로 넘어온 값만 덮어쓴다."""
    if candidates.empty:
        return candidates

    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    df = candidates.copy()

    # 가격은 예산이 있으면 **예산 대비**로 잰다. 후보군 min-max로 재면
    # 기준선이 후보 구성에 따라 흔들린다 - 10원짜리 하나가 최저값을 잡으면
    # 나머지 2~3만원대가 전부 0점 근처로 뭉개지고, 후보가 2~3개만 남으면
    # 극단값이 0과 1로 벌어진다. 예산 기준이면 "예산의 몇 %를 쓰는가"라
    # 후보 구성과 무관하게 안정적이고 사람이 읽어도 의미가 통한다.
    # (로그 스케일도 후보로 봤지만, 10원 vs 100원의 차이를 크게 벌리는 건
    #  월 90원 차이라 실질적으로 의미가 없다 - 저가 구간을 과대평가한다.)
    budget = profile.get("budget_krw")
    if budget:
        df["_price_score"] = (1 - df["monthly_cost"] / budget).clip(0, 1)
    else:
        df["_price_score"] = _minmax(df["monthly_cost"], higher_is_better=False)

    qos_speed = _qos_mbps(df["data_throttle_speed"])
    df["_qos_score"] = _qos_score(df)

    # 데이터는 "많을수록 좋다"가 아니라 "필요한 만큼 + 여유"다. 사용량을
    # 알면 그 대비로 재서, 필요량의 1.5배부터는 만점으로 포화시킨다.
    # 절대 최대값 기준으로 재면 월 10GB 쓰는 사람에게 200GB가 100GB보다
    # 2배 좋은 것처럼 계산되는데, 실제로는 둘 다 남아돌아 차이가 없다.
    # 무제한은 이 축에서 만점이고, 소진 후 속도 차이는 별도 qos 축이 잰다
    # (무제한 1Mbps와 200GB 풀속도의 우열은 여기가 아니라 거기서 갈린다).
    usage = profile.get("data_usage_gb")
    if usage:
        ratio = df["data_gb"] / (usage * 1.5)
        ratio[df["data_unlimited"].fillna(False)] = 1.0
        df["_data_score"] = ratio.clip(0, 1).fillna(0.5)
    else:
        data_score = df["data_gb"].copy()
        data_score[df["data_unlimited"].fillna(False)] = (
            data_score.max() if data_score.notna().any() else 999
        )
        df["_data_score"] = _minmax(data_score, higher_is_better=True)

    df["_tethering_score"] = _tethering_score(df)

    ott_pref = profile.get("ott_preference") or []
    if ott_pref:
        df["_ott_score"] = df["ott_options"].fillna("").apply(
            lambda opts: 1.0 if any(o in opts for o in ott_pref) else 0.0
        )
    else:
        df["_ott_score"] = 0.5  # 선호 없으면 중립 - 있는 요금제를 부당하게 깎지 않음

    # 후보 전체가 결측인 축은 가중치에서 빼고 나머지를 재정규화한다.
    # 결측을 중립 0.5로 채우면 그 축은 모든 후보에게 같은 값이라 순위에는
    # 영향이 없으면서 점수만 일정하게 밀어올린다 - "매치율 85%"의 15%가
    # 실은 "모르는 값"인 상태가 되어 점수를 사용자에게 설명할 수 없다.
    # (테더링 71%, QoS 31% 결측이라 후보군에 따라 실제로 자주 일어난다.)
    informative = {
        "price": True,
        "data": df["data_gb"].notna().any() or df["data_unlimited"].fillna(False).any(),
        # QoS는 결측도 정보다(무제한이면 완전 무제한, 정량제면 초과 과금).
        # 값이 하나도 없어도 후보들이 0/1로 갈리므로 항상 유효한 축이다.
        "qos": True,
        # 테더링도 결측이 정보다(unsupported=0, within_data=데이터 점수).
        # 후보가 전부 undisclosed일 때만 판별력이 없다.
        "tethering": (df["tethering_support"].fillna("undisclosed") != "undisclosed").any(),
        "ott": bool(ott_pref),
    }
    active = {k: v for k, v in w.items() if informative.get(k)}
    total = sum(active.values()) or 1.0
    df["match_score"] = sum(
        weight / total * df[f"_{axis}_score"] for axis, weight in active.items()
    )
    df.attrs["scored_axes"] = list(active)

    # 동률 다수(EDA: 스펙 완전동일 그룹 392개, 1,541행) 대비 tie-breaker.
    # benefit_count가 많은 쪽을 우선 - "혜택이 더 많이 딸려 있다"가 실사용자
    # 입장에서 가장 직관적인 2차 기준.
    df = df.sort_values(["match_score", "benefit_count"], ascending=[False, False])

    # 같은 요금제의 연령별 변형(base_plan_id 동일)은 한 줄만 남긴다.
    # 예: "음성 12.1"이 일반/만18세이하/만65세이상 3행으로 갈라져 있는데,
    # 사용자가 고를 수 있는 건 자기 나이에 맞는 하나뿐이라 3줄을 다 보여주면
    # 추천 목록만 낭비된다. 위에서 정렬을 마쳤으므로 first가 곧 최고점이다.
    df = df.drop_duplicates(subset="base_plan_id", keep="first")

    return df.reset_index(drop=True)


def recommend(profile: dict, weights: dict | None = None, top_n: int = 5) -> pd.DataFrame:
    plans = pd.read_csv(final_path("통신요금제_통합데이터_최종.csv"), encoding="utf-8-sig")
    eligible = filter_eligible(plans, profile)
    ranked = score(eligible, profile, weights)
    cols = ["plan_id", "plan_name", "carrier_type", "host_mno", "monthly_cost",
            "promo_ends_after", "price_after_promo", "data_gb", "data_unlimited",
            "match_score"]
    # 조건이 빡세면 후보가 0개일 수 있다. 이때 score()는 점수 컬럼을 못
    # 만들고 빈 프레임을 그대로 돌려주므로, 컬럼을 맞춰서 내보낸다.
    # (호출부가 "결과 없음"과 "에러"를 구분할 수 있어야 한다 - 조건을
    #  완화하라고 안내하려면 크래시가 아니라 빈 결과로 와야 한다.)
    if ranked.empty:
        return pd.DataFrame(columns=cols)
    return ranked[cols].head(top_n)


def demo():
    """assert 기반 자체 점검 + 예시 프로필 3개 실행. python src/agents/scoring_agent.py"""
    plans = pd.read_csv(final_path("통신요금제_통합데이터_최종.csv"), encoding="utf-8-sig")

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

    profile_keep = {
        "current_carrier": "KT",
        "keep_current_carrier": True,
        "user_age": 30,
    }
    eligible2 = filter_eligible(plans, profile_keep)
    assert (eligible2["host_mno"] == "KT").all(), "통신사 유지 조건인데 타사 요금제가 남음"
    assert (eligible2["carrier_type"] == "MNO").all(), "통신사 유지 조건인데 알뜰폰이 남음"
    assert eligible2["signup_notice"].fillna("").str.contains(
        "이미 KT 요금제를 쓰고 있다면"
    ).sum() == 0, "KT 사용자에게 KT 중복가입 제한 요금제가 안 걸러짐"
    # 만 30세면 "만 18세 이하"/"만 65세 이상" 전용 행은 없어야 한다
    for cond in eligible2["age_condition"].dropna().unique():
        assert _age_eligible(cond, 30), f"만 30세인데 '{cond}' 조건 요금제가 남음"

    result2 = recommend(profile_keep)
    assert result2["plan_id"].is_unique, "결과에 같은 plan_id가 중복됨"
    print("\n=== 프로필 2: KT 유지 / 만 30세 ===")
    print(result2.to_string(index=False))

    # 연령 변형이 여러 줄로 새어나오지 않는지: 나이를 안 물어본 경우에도
    # base_plan_id 기준으로는 한 줄이어야 한다.
    ranked_all = score(filter_eligible(plans, {}), {})
    assert ranked_all["base_plan_id"].is_unique, "base_plan_id 중복 제거가 안 됨"

    profile_senior = {"user_age": 70, "preferred_carrier_type": "MNO"}
    eligible3 = filter_eligible(plans, profile_senior)
    assert eligible3["age_condition"].notna().any(), (
        "만 70세인데 시니어 전용 요금제가 후보에서 통째로 사라짐"
    )
    result3 = recommend(profile_senior)
    print("\n=== 프로필 3: 만 70세 / MNO ===")
    print(result3.to_string(index=False))

    # 후보 전체가 결측인 축은 점수에서 빠져야 한다(있으면 순위엔 영향 없이
    # 점수만 밀어올려서 "매치율 N%"를 설명할 수 없게 된다).
    ranked1 = score(filter_eligible(plans, profile_budget), profile_budget)
    assert "ott" not in ranked1.attrs["scored_axes"], (
        f"OTT 선호를 안 물어봤는데 ott 축이 점수에 남음: {ranked1.attrs['scored_axes']}"
    )
    print(f"\n프로필1에서 실제 점수에 쓰인 축: {ranked1.attrs['scored_axes']}")

    # 테더링 빈칸 셋(미지원/제공량 내/미공개)이 서로 다른 점수를 받아야 한다.
    # 하나로 뭉치면 "테더링 못 씀"과 "데이터 전량 테더링 가능"이 같아진다.
    unsupported = ranked1["tethering_support"] == "unsupported"
    within = ranked1["tethering_support"] == "within_data"
    if unsupported.any():
        assert (ranked1.loc[unsupported, "_tethering_score"] == 0).all(), (
            "테더링 미지원인데 0점이 아님"
        )
    if within.any():
        assert (
            ranked1.loc[within, "_tethering_score"]
            == ranked1.loc[within, "_data_score"]
        ).all(), "within_data인데 데이터 점수를 안 따라감"
    print(
        f"테더링 상태 분포: {ranked1['tethering_support'].value_counts().to_dict()}"
    )

    # QoS 단위: 400Kbps가 1Mbps보다 크게 잡히면 안 된다(275건이 Kbps 표기).
    unit = _qos_mbps(pd.Series(["400Kbps", "1Mbps", "10Mbps"]))
    assert unit[0] < unit[1] < unit[2], f"QoS 단위 정규화 실패: {list(unit)}"

    # QoS 결측의 의미가 무제한 여부에 따라 갈려야 한다.
    probe = pd.DataFrame({
        "data_unlimited": [True, False],
        "data_throttle_speed": [None, None],
    })
    qs = _qos_score(probe)
    assert qs.iloc[0] == 1.0, "완전 무제한(감속 없음)이 만점이 아님"
    assert qs.iloc[1] == 0.0, "정량제인데 감속 표기 없음(=초과 과금)이 0점이 아님"

    print("\n자체 점검 통과.")


if __name__ == "__main__":
    demo()
