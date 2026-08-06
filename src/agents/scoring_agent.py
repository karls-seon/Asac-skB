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

# 이동 방향에 따라 동기가 다르므로 가중치도 다르다.
#
# 대상 사용자는 세 갈래다(2026-08-06 결정): MNO->MVNO, MVNO->MVNO, MVNO->MNO.
# **MNO->MNO는 뺀다.** 타사 번호이동은 단말기 구매에 묶여 있어 요금제 비교로
# 풀 문제가 아니고, 같은 통신사 안에서 온라인전용(요고/너겟/다이렉트)으로
# 갈아타는 길은 실제로 막혀 있다 - 그 45개 요금제에 "이미 OO 요금제를 쓰고
# 있다면 가입할 수 없어요"가 붙어 있다(signup_notice).
#
# MVNO->MNO는 비용이 아니라 멤버십/결합/고객센터/품질이 동기라, 가격을 제일
# 무겁게 둔 기본 가중치를 그대로 쓰면 MNO가 전멸한다. 가격을 낮추고 혜택을
# 올린 프리셋을 따로 둔다.
WEIGHT_PRESETS = {
    # 비용 절감 (MNO->MVNO, MVNO->MVNO). 기본값.
    "cost_saving": DEFAULT_WEIGHTS,
    # 통신3사로 올라가기 (MVNO->MNO).
    # ponytail: 멤버십 등급(membership_grade)을 점수 축으로 안 만들었다.
    # MNO 선택의 큰 동기인데 지금은 ott 축이 혜택을 대신 재고 있다 -
    # 이 세그먼트 추천 품질이 실제로 부족하면 membership 축을 추가할 것.
    "upgrade_to_mno": {
        "price": 0.20,
        "data": 0.25,
        "qos": 0.15,
        "tethering": 0.10,
        "ott": 0.30,
    },
}

# signup_notice가 "이미 OO 요금제를 쓰고 있다면 이 요금제를 가입할 수
# 없어요" 형태일 때, 사용자의 현재 통신사와 겹치는지 보려면 이 표기가
# 필요하다. host_mno 컬럼값("LGU+")과 원문 표기("LG U+")가 다르다.
_HOST_DISPLAY = {"KT": "KT", "SKT": "SKT", "LGU+": "LG U+"}

# 데이터 사용량을 얼마나 믿을 수 있는지에 따른 여유분 배수.
#
# "월 10GB 써요"(high)와 "출퇴근에 영상 자주 봐요"(low, 생활패턴에서 추정)는
# 같은 15GB라도 다뤄야 하는 방식이 다르다. 추정이면 실제 값이 위아래로 벌어질
# 수 있으므로 ① 후보 하한을 낮춰(실제가 더 적으면 작은 요금제도 정답이다)
# ② 여유분을 더 높게 쳐준다(모자라면 초과 과금이나 감속을 맞는다).
#
# 주의: 이건 "확신이 없으니 이 축을 덜 본다"가 아니다. 그건 정작 중요한데
# 확신만 없는 조건을 묻어버린다. 여기서 하는 건 값 자체의 불확실 구간을
# 넓히는 것이고, 축의 비중은 그대로다.
CONFIDENCE_HEADROOM = {"high": 1.0, "medium": 1.3, "low": 1.6}


def _headroom(profile: dict) -> float:
    """데이터 사용량 추정의 불확실 구간 배수. 안 주면 확신한 값으로 본다."""
    return CONFIDENCE_HEADROOM.get(profile.get("data_usage_confidence", "high"), 1.0)


def _monthly_data_gb(df: pd.DataFrame) -> pd.Series:
    """월 환산 데이터 제공량. 일 단위 제공 요금제는 daily_data_gb에만 값이
    있고 data_gb는 비어 있어서(월 총량 개념이 없음) 그대로 쓰면 "데이터를
    아예 안 주는 요금제"로 오인된다 - 실제로 62건이 이 상태다.
    30일로 환산해 같은 잣대에 올린다."""
    return df["data_gb"].fillna(df["daily_data_gb"] * 30)


def _has_data(df: pd.DataFrame) -> pd.Series:
    """데이터를 조금이라도 주는 요금제인지. 무제한/월단위/일단위 셋 중 하나."""
    return df["data_unlimited"].fillna(False) | _monthly_data_gb(df).notna()


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
    # 일 단위 제공을 월로 환산한 값. 이후 점수·설명이 전부 이 컬럼을 쓴다.
    df["monthly_data_gb"] = _monthly_data_gb(df)

    budget = profile.get("budget_krw")
    if budget is not None:
        df = df[df["monthly_cost"] <= budget]

    # 통신3사 사용자에게는 통신3사 요금제를 추천하지 않는다(위 WEIGHT_PRESETS
    # 주석의 범위 결정). 알뜰폰 사용자는 양쪽 다 후보다.
    if profile.get("current_carrier_type") == "MNO":
        df = df[df["carrier_type"] == "MVNO"]

    # 어디로 갈지를 사용자가 직접 지정한 경우(알뜰폰 사용자가 통신3사로
    # 올라가고 싶다는 등). 위 범위 필터보다 뒤에 둬서 범위를 넓히지 못하게 한다.
    carrier_type = profile.get("target_carrier_type") or profile.get("preferred_carrier_type")
    if carrier_type:
        df = df[df["carrier_type"] == carrier_type]

    network = profile.get("preferred_network")
    if network:
        # network_gen이 빈 요금제는 "세대 구분 없는 통합요금제"라 5G/LTE 어느
        # 쪽을 원하든 후보로 남아야 한다. 값이 있는 것만 비교한다 - 안 그러면
        # LTE 사용자에게 통신3사 요금제가 통째로 사라진다(KT는 271행 전부가
        # 이 상태다).
        df = df[df["network_gen"].isna() | (df["network_gen"] == network)]

    # 데이터를 아예 안 주는 음성전용 요금제(KT 음성 12.1, SKT 표준요금제 등
    # 19건)는 휴대폰 요금제를 찾는 사람에게 답이 될 수 없다. 사용자가 데이터를
    # 언급하지 않았어도(=조건이 없어도) 후보에 넣으면 안 된다 - 실제로 이것들이
    # 값이 싸다는 이유만으로 상위에 올라왔다. 명시적으로 원할 때만 남긴다.
    if not profile.get("voice_only_ok"):
        df = df[_has_data(df)]

    if profile.get("data_unlimited_required"):
        df = df[df["data_unlimited"].fillna(False)]
    elif profile.get("data_usage_gb") is not None:
        # 필터는 **명백히 부족한 것만** 자른다(사용량의 70%). 사용량으로 딱
        # 자르면 통과 후보의 80%가 점수 포화점을 넘어버려서 데이터 축이
        # 판별을 못 하고, 그 결과 유일하게 연속적인 가격이 순위를 독식한다
        # (실측: 가격-순위 상관 0.863). 필터와 점수가 같은 조건을 두 번 보면
        # 점수 쪽이 죽는다 - 필터는 넓게, 판별은 점수가 한다.
        # 추정치(confidence)면 여기서 더 낮춘다.
        floor = profile["data_usage_gb"] * 0.7 / _headroom(profile)
        df = df[df["data_unlimited"].fillna(False) | (_monthly_data_gb(df).fillna(0) >= floor)]

    if profile.get("voice_unlimited_required"):
        df = df[df["voice_unlimited"].fillna(False)]

    user_age = profile.get("user_age")
    if user_age is not None:
        df = df[df["age_condition"].apply(lambda c: _age_eligible(c, user_age))]

    current_carrier = profile.get("current_carrier")
    if current_carrier:
        blocked = df.apply(lambda r: _current_carrier_blocks(r, current_carrier), axis=1)
        df = df[~blocked]

    return df


# ---------------------------------------------------------------------------
# 설명: 점수가 아니라 "요청한 조건과 무엇이 다른가"를 말한다.
#
# match_score 0.841937은 사용자에게 아무 의미가 없다. 필요한 건 "통화가 요청한
# 101~300분이 아니라 100분이라 살짝 부족하다" 같은 문장이다. 조건은 우리가
# 다 알고 있으므로 요금제마다 조건별로 대조해서 만들면 된다.
# ---------------------------------------------------------------------------

def _fmt(n) -> str:
    """숫자를 사람이 읽는 형태로. 15.0 -> "15", 4.5 -> "4.5"."""
    return f"{n:,.10g}"


def shortfalls(row, profile: dict) -> list[str]:
    """이 요금제가 요청 조건에 **못 미치는** 점. 비어 있으면 조건을 다 만족한다.

    하드 필터를 통과한 행이라도 부족할 수 있다 - 데이터 사용량은 필터가
    70%까지만 자르기 때문이다(그래야 데이터 축이 판별력을 갖는다).
    """
    out = []
    usage = profile.get("data_usage_gb")
    gb = row.get("monthly_data_gb")
    if usage and not row.get("data_unlimited") and pd.notna(gb) and gb < usage:
        out.append(f"데이터 {_fmt(gb)}GB로 사용량 {_fmt(usage)}GB보다 부족")
    if profile.get("data_unlimited_required") and not row.get("data_unlimited"):
        out.append("데이터 무제한 아님")
    if profile.get("voice_unlimited_required") and not row.get("voice_unlimited"):
        out.append("통화 무제한 아님")
    # 감속 없이 정량제면 다 쓰고 나서 초과 과금이다. 요청 조건은 아니지만
    # 사용자가 모르고 고르면 손해라 부족분과 같은 자리에서 알려준다.
    if (not row.get("data_unlimited")) and pd.isna(row.get("data_throttle_speed")):
        out.append("데이터 소진 후 초과 과금")
    if row.get("tethering_support") == "unsupported":
        out.append("테더링 불가")
    return out


def strengths(row, profile: dict) -> list[str]:
    """요청보다 **나은** 점. 부족분만 보여주면 왜 추천했는지가 안 보인다."""
    out = []
    budget = profile.get("budget_krw")
    cost = row.get("monthly_cost")
    if budget and pd.notna(cost) and cost < budget * 0.7:
        out.append(f"월 {_fmt(cost)}원으로 예산({_fmt(budget)}원)보다 저렴")
    usage = profile.get("data_usage_gb")
    gb = row.get("monthly_data_gb")
    if row.get("data_unlimited"):
        out.append("데이터 무제한")
    elif usage and pd.notna(gb) and gb >= usage * 1.5:
        out.append(f"데이터 {_fmt(gb)}GB로 사용량의 {gb / usage:.1f}배 여유")
    if row.get("voice_unlimited") and not profile.get("voice_unlimited_required"):
        out.append("통화 무제한")
    if pd.isna(row.get("promo_ends_after")):
        out.append("프로모션 없이 가격 그대로 유지")
    return out


# 완화해볼 조건과 사람이 읽을 이름. 자격(나이·현재 통신사)은 사용자가 바꿀 수
# 있는 게 아니라서 후보에 넣지 않는다.
_RELAXABLE = {
    "budget_krw": "예산",
    "data_usage_gb": "데이터 사용량",
    "data_unlimited_required": "데이터 무제한",
    "voice_unlimited_required": "통화 무제한",
    "preferred_network": "통신 세대(5G/LTE)",
    "target_carrier_type": "통신사 유형",
}


def suggest_relaxation(plans: pd.DataFrame, profile: dict) -> list[str]:
    """조건이 서로 충돌해 후보가 없을 때, 무엇을 풀면 몇 개가 열리는지.

    "영상 자주 봄 + 월 2만원"처럼 사용자 스스로는 모순을 모르는 경우가 많다.
    조건을 하나씩 빼고 다시 세어 보면 어느 조건이 막고 있는지 바로 나온다.
    """
    out = []
    for key, label in _RELAXABLE.items():
        if profile.get(key) in (None, False):
            continue
        opened = len(filter_eligible(plans, {k: v for k, v in profile.items() if k != key}))
        if opened:
            out.append((opened, f"{label} 조건을 빼면 {opened}개"))
    out.sort(reverse=True)
    return [msg for _, msg in out]


def cheapest_for(plans: pd.DataFrame, profile: dict) -> float | None:
    """예산만 빼고 다른 조건을 다 지킬 때 최소 월 납부액. 예산이 원인일 때
    "얼마면 되는지"를 숫자로 말해주기 위한 것."""
    relaxed = {k: v for k, v in profile.items() if k != "budget_krw"}
    df = filter_eligible(plans, relaxed)
    return None if df.empty else float(df["monthly_cost"].min())


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

    # 사용량이 추정치면 QoS의 가치가 올라간다. 추정이 틀려 데이터를 다 써도
    # 감속 요금제는 느려질 뿐이지만, 감속이 없는 요금제는 초과 과금을 맞는다
    # (_qos_score 참고). 즉 QoS는 추정 오차에 대한 보험이고, 오차가 클수록
    # 보험료를 더 쳐줘야 한다. 축 자체를 덜 보는 게 아니라 더 보는 쪽이다.
    headroom = _headroom(profile)
    if headroom > 1.0 and profile.get("data_usage_gb"):
        w = {**w, "qos": w["qos"] * headroom}

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
        # 추정치일수록 여유분을 더 높게 쳐준다(포화점이 위로 올라간다).
        # 확신하는 10GB에겐 15GB면 충분하지만, 추정한 10GB에겐 실제가 더
        # 클 수 있어서 24GB짜리가 실제로 더 안전하다.
        ratio = _monthly_data_gb(df) / (usage * 1.5 * _headroom(profile))
        ratio[df["data_unlimited"].fillna(False)] = 1.0
        df["_data_score"] = ratio.clip(0, 1).fillna(0.5)
    else:
        data_score = _monthly_data_gb(df).copy()
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
        "data": _has_data(df).any(),
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


def _character(row, usage: float | None) -> tuple:
    """요금제의 "성격". 같은 성격끼리는 사실상 대체재다.

    점수순으로만 상위 N을 뽑으면 목록이 복제본으로 찬다 - 실측하면 상위 5개가
    전부 12~20GB에 전부 프로모션 요금제였고, 후보 안에 있던 프로모션 없는
    안정형 48건과 100GB 이상 대용량 48건은 하나도 안 보였다. 점수가 비슷한
    건 맞지만 사용자 입장에선 고를 게 없는 목록이다.
    """
    promo = "특가" if pd.notna(row.get("promo_ends_after")) else "안정"
    if row.get("data_unlimited"):
        data = "무제한"
    elif usage and pd.notna(row.get("data_gb")) and row["data_gb"] >= usage * 3:
        data = "대용량"
    else:
        data = "적정"
    return (promo, data)


def diversify(ranked: pd.DataFrame, profile: dict, top_n: int) -> pd.DataFrame:
    """성격이 다른 것부터 한 개씩 뽑고, 남는 자리는 점수순으로 채운다.

    ponytail: 성격을 (프로모션 유무 x 데이터 규모) 2축으로만 나눴다. 통신사나
    혜택으로도 갈릴 수 있지만, 목록이 복제본이 되는 주된 이유가 이 둘이라
    거기까지만 한다. 실제로 부족하면 축을 늘릴 것.
    """
    usage = profile.get("data_usage_gb")
    chars = ranked.apply(lambda r: _character(r, usage), axis=1)
    # ranked는 이미 점수순이므로 각 성격의 첫 행이 그 성격의 최고점이다.
    best_of_each = ranked[~chars.duplicated()]
    filler = ranked.drop(index=best_of_each.index)
    picked = pd.concat([best_of_each, filler]).head(top_n)
    # 무엇을 뽑을지는 다양성이 정하지만, 보여주는 순서는 점수순이어야 한다.
    # 안 그러면 목록에서 낮은 점수가 높은 점수 위에 올라와 이상해 보인다.
    return picked.sort_values("match_score", ascending=False)


def pick_preset(profile: dict) -> dict:
    """이동 방향에서 가중치 프리셋을 고른다. 사용자에게 따로 묻지 않는다 -
    현재 통신사 유형과 목표만 있으면 방향은 저절로 정해진다."""
    if profile.get("target_carrier_type") == "MNO":
        return WEIGHT_PRESETS["upgrade_to_mno"]
    return WEIGHT_PRESETS["cost_saving"]


def recommend(profile: dict, weights: dict | None = None, top_n: int = 5) -> pd.DataFrame:
    plans = pd.read_csv(final_path("통신요금제_통합데이터_최종.csv"), encoding="utf-8-sig")
    eligible = filter_eligible(plans, profile)
    ranked = score(eligible, profile, weights or pick_preset(profile))
    cols = ["plan_id", "plan_name", "carrier_type", "host_mno", "monthly_cost",
            "promo_ends_after", "price_after_promo", "data_gb", "data_unlimited",
            "match_score", "exact_match", "shortfall", "strength"]
    # 조건이 빡세면 후보가 0개일 수 있다. 이때 score()는 점수 컬럼을 못
    # 만들고 빈 프레임을 그대로 돌려주므로, 컬럼을 맞춰서 내보낸다.
    # (호출부가 "결과 없음"과 "에러"를 구분할 수 있어야 한다 - 조건을
    #  완화하라고 안내하려면 크래시가 아니라 빈 결과로 와야 한다.)
    if ranked.empty:
        return pd.DataFrame(columns=cols)

    out = diversify(ranked, profile, top_n).copy()
    gaps = out.apply(lambda r: shortfalls(r, profile), axis=1)
    out["shortfall"] = gaps.apply(" / ".join)
    out["strength"] = out.apply(lambda r: " / ".join(strengths(r, profile)), axis=1)
    out["exact_match"] = gaps.apply(len) == 0
    return out[cols]


def explain(profile: dict, top_n: int = 5) -> str:
    """추천 결과를 사람이 읽는 답변으로 만든다.

    점수 숫자를 그대로 보여주는 대신 ① 조건을 다 만족하는 게 몇 개인지
    ② 각 요금제가 요청과 무엇이 다른지 ③ 하나도 없으면 어느 조건을 풀면
    되는지를 말한다. KT M모바일 AI 추천을 직접 돌려보고 가져온 구조다
    (2026-08-06) - 거기서 제일 쓸모 있었던 게 "정확히 일치하는 요금제는
    없습니다"를 먼저 말하고 조건별 차이를 항목으로 적어주는 부분이었다.
    """
    plans = pd.read_csv(final_path("통신요금제_통합데이터_최종.csv"), encoding="utf-8-sig")
    eligible = filter_eligible(plans, profile)

    if eligible.empty:
        lines = ["조건을 모두 만족하는 요금제가 없습니다."]
        cheapest = cheapest_for(plans, profile)
        budget = profile.get("budget_krw")
        if budget and cheapest and cheapest > budget:
            lines.append(f"- 다른 조건을 그대로 두면 최소 월 {_fmt(cheapest)}원이 필요합니다"
                         f"(현재 예산 {_fmt(budget)}원).")
        for msg in suggest_relaxation(plans, profile)[:3]:
            lines.append(f"- {msg}가 후보에 들어옵니다.")
        return "\n".join(lines)

    result = recommend(profile, top_n=top_n)
    n_exact = int(result["exact_match"].sum())
    ranked_all = score(eligible, profile, pick_preset(profile))
    total_exact = int(
        (ranked_all.apply(lambda r: len(shortfalls(r, profile)), axis=1) == 0).sum()
    )

    lines = []
    if total_exact == 0:
        lines.append("요청하신 조건에 **정확히** 맞는 요금제는 없습니다. "
                     "가장 가까운 것들을 어떤 점이 다른지와 함께 보여드립니다.")
    else:
        lines.append(f"조건을 모두 만족하는 요금제가 {total_exact}개 있습니다. "
                     f"성격이 다른 것들로 추려서 보여드립니다.")
    lines.append("")

    for i, (_, r) in enumerate(result.iterrows(), 1):
        price = f"월 {_fmt(r['monthly_cost'])}원"
        if pd.notna(r["promo_ends_after"]):
            price += (f" ({_fmt(r['promo_ends_after'])}개월 후 "
                      f"{_fmt(r['price_after_promo'])}원)")
        lines.append(f"{i}. {r['plan_name']} — {price}")
        if r["strength"]:
            lines.append(f"   좋은 점: {r['strength']}")
        if r["shortfall"]:
            lines.append(f"   다른 점: {r['shortfall']}")
    return "\n".join(lines)


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

    # 통신3사 사용자 -> 알뜰폰만 추천되어야 한다(MNO->MNO는 범위 밖).
    profile_mno_user = {
        "current_carrier_type": "MNO",
        "current_carrier": "KT",
        "user_age": 30,
    }
    eligible2 = filter_eligible(plans, profile_mno_user)
    assert (eligible2["carrier_type"] == "MVNO").all(), (
        "통신3사 사용자인데 통신3사 요금제가 후보에 남음(MNO->MNO는 범위 밖)"
    )
    assert eligible2["signup_notice"].fillna("").str.contains(
        "이미 KT 요금제를 쓰고 있다면"
    ).sum() == 0, "KT 사용자에게 KT 중복가입 제한 요금제가 안 걸러짐"
    for cond in eligible2["age_condition"].dropna().unique():
        assert _age_eligible(cond, 30), f"만 30세인데 '{cond}' 조건 요금제가 남음"

    result2 = recommend(profile_mno_user)
    assert result2["plan_id"].is_unique, "결과에 같은 plan_id가 중복됨"
    print("\n=== 프로필 2: KT 사용중 / 만 30세 -> 알뜰폰 추천 ===")
    print(result2.to_string(index=False))

    # 알뜰폰 사용자 -> 통신3사로 올라가기. 가격 대신 혜택 위주 프리셋이어야 한다.
    profile_upgrade = {"current_carrier_type": "MVNO", "target_carrier_type": "MNO"}
    assert pick_preset(profile_upgrade) is WEIGHT_PRESETS["upgrade_to_mno"], (
        "MVNO->MNO인데 비용절감 프리셋이 선택됨"
    )
    assert pick_preset({"current_carrier_type": "MNO"}) is WEIGHT_PRESETS["cost_saving"]
    result_up = recommend(profile_upgrade)
    assert (result_up["carrier_type"] == "MNO").all(), "MNO로 올라가려는데 알뜰폰이 추천됨"
    print("\n=== 프로필 2b: 알뜰폰 사용중 -> 통신3사로 (혜택 위주 프리셋) ===")
    print(result_up.to_string(index=False))

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

    # 데이터를 아예 안 주는 음성전용 요금제는 어떤 프로필에서도 안 나와야 한다
    # (싸다는 이유만으로 상위에 올라왔었다).
    for prof in (profile_budget, {"user_age": 70, "preferred_carrier_type": "MNO"}, {}):
        cand = filter_eligible(plans, prof)
        assert _has_data(cand).all(), (
            f"데이터 없는 음성전용 요금제가 후보에 남음: "
            f"{cand.loc[~_has_data(cand), 'plan_name'].head(3).tolist()}"
        )

    # 일 단위 제공(매일 5GB 등)은 data_gb가 비어 있을 뿐 데이터가 있는 요금제다.
    daily = plans[plans["daily_data_gb"].notna() & plans["data_gb"].isna()]
    assert _has_data(daily).all(), "일 단위 제공 요금제가 '데이터 없음'으로 걸러짐"

    # 상위 목록이 복제본이면 안 된다. 점수순으로만 뽑으면 실제로 전부
    # "12~20GB 특가 프로모션"만 나왔고, 후보에 있던 안정형/대용량은 묻혔다.
    chars = {_character(row, profile_budget["data_usage_gb"]) for _, row in result.iterrows()}
    assert len(chars) >= 3, f"상위 {len(result)}개의 성격이 {len(chars)}종뿐 - 선택지가 안 됨: {chars}"
    print(f"상위 목록 성격: {sorted(chars)}")

    # 사용량이 추정치(confidence=low)면 확신할 때보다 후보가 넓어지고,
    # 감속 없는 요금제(초과 과금 위험)가 상대적으로 불리해져야 한다.
    sure = {"data_usage_gb": 10, "preferred_network": "5G"}
    guess = {**sure, "data_usage_confidence": "low"}
    n_sure = len(filter_eligible(plans, sure))
    n_guess = len(filter_eligible(plans, guess))
    assert n_guess > n_sure, (
        f"추정치인데 후보가 안 늘어남(확신 {n_sure} vs 추정 {n_guess}) - "
        "하한을 낮추는 처리가 안 먹었다"
    )
    r_sure = score(filter_eligible(plans, sure), sure)
    r_guess = score(filter_eligible(plans, guess), guess)
    no_qos = lambda d: d[d["data_throttle_speed"].isna() & ~d["data_unlimited"].fillna(False)]
    if len(no_qos(r_sure)) and len(no_qos(r_guess)):
        assert no_qos(r_guess)["match_score"].mean() < no_qos(r_sure)["match_score"].mean(), (
            "추정치인데 감속 없는(초과 과금) 요금제가 안 불리해짐"
        )
    print(f"\n사용량 확신 시 후보 {n_sure}개 / 추정 시 {n_guess}개 (여유분 반영)")

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

    # 설명: 조건과 무엇이 다른지를 말할 수 있어야 한다(점수 숫자는 설명이 아니다).
    tight = {"data_usage_gb": 8, "budget_krw": 12000, "preferred_network": "5G"}
    r_tight = recommend(tight)
    assert not r_tight.empty and (r_tight["strength"] != "").any(), (
        "추천했는데 좋은 점을 한 줄도 못 대고 있음"
    )
    near = r_tight[~r_tight["exact_match"]]
    if not near.empty:
        assert (near["shortfall"] != "").all(), "조건 미달인데 무엇이 부족한지가 비어 있음"

    # 조건이 충돌하면 "없다"고 말하고 무엇을 풀면 되는지 알려줘야 한다.
    impossible = {"data_usage_gb": 30, "budget_krw": 8000,
                  "preferred_network": "5G", "data_unlimited_required": True}
    assert filter_eligible(plans, impossible).empty, "충돌 프로필인데 후보가 남음"
    msg = explain(impossible)
    assert "없습니다" in msg, f"후보가 없는데 그렇게 말하지 않음:\n{msg}"
    assert suggest_relaxation(plans, impossible), "완화 제안을 하나도 못 내놓음"
    print("\n=== 조건 충돌 시 안내 ===")
    print(msg)

    print("\n자체 점검 통과.")


if __name__ == "__main__":
    demo()
