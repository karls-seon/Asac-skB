"""필터 + 랭킹 추천.

웹·노트북이 같이 쓰는 순수 함수. 요금제 테이블과 입력 하나를 받아 상위 N개를 낸다.
LLM도 에이전트도 없다.

    from recommend import load_plans, recommend
    plans = load_plans()
    recommend(plans, budget=30000, data_gb=20, mvno_ok=True)
"""

import functools
import re

import pandas as pd

from schema import final_path

# 혜택 테이블에 금액이 안 적힌 OTT에 매길 값. 1,093개 OTT 혜택 중 금액이 있는 건
# 57개뿐이라 나머지는 이걸로 친다.
OTT_BONUS_KRW = 5_000

TOP_N = 5

# 일 단위로만 제공하는 요금제를 월로 환산할 때 쓰는 일수.
# ponytail: 30일 고정. 달마다 정확히 맞출 필요가 생기면 그때 달력을 본다.
DAYS_PER_MONTH = 30

_AGE_RE = re.compile(r"만\s*(\d+)\s*세\s*(이하|미만|이상|초과)")


# 가입하는 곳. 같은 요금제가 채널에 따라 가격이 다르다 - LGU+ `너겟49`는 공식몰
# 49,000원인데 모요를 통하면 프로모션가 7,000원이다. 둘 다 실재하는 조건이라
# 하나로 합치지 않고, 대신 어디서 가입하는 건지 결과에 적는다.
SIGNUP_CHANNELS = {
    "www.moyoplan.com": "모요",
    "product.kt.com": "KT 공식몰",
    "www.tworld.co.kr": "SKT 공식몰",
    "www.lguplus.com": "LG U+ 공식몰",
}


def load_plans() -> pd.DataFrame:
    """최종 CSV를 읽고 숫자 컬럼만 숫자로 바꾼다."""
    df = pd.read_csv(final_path("통신요금제_통합데이터_최종.csv"), encoding="utf-8-sig")
    for col in ("discounted_fee", "data_gb", "subscriber_count",
                "daily_data_gb", "tethering_gb", "voice_minutes", "sms_count"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # CSV에는 없는 파생값이라 읽을 때 만든다. 스키마를 늘리면 크롤러 4개를 다시
    # 돌려야 하는데, source_url만 보면 알 수 있는 값이라 그럴 이유가 없다.
    domain = df["source_url"].fillna("").str.extract(r"https?://([^/]+)", expand=False)
    df["signup_channel"] = domain.map(SIGNUP_CHANNELS).fillna("")
    return df


@functools.lru_cache(maxsize=1)
def ott_prices() -> dict[str, int]:
    """혜택명 -> 월 가치(원). 사이트가 적어 둔 금액만 쓴다.

    **혜택명 단위**라는 게 핵심이다. 같은 티빙이라도 사이트는 `티빙`(17,000원 명시),
    `티빙 광고형 스탠다드 제공 (12개월)`(19행, 금액 없음),
    `티빙&웨이브 광고형 50% 할인`(제공이 아니라 할인)을 따로 적는다. 서비스명으로
    뭉치면 광고형과 할인권까지 프리미엄 값을 받는다. 사이트가 값을 안 적은 등급은
    기본값(`OTT_BONUS_KRW`)으로 두는 게 없는 값을 지어내는 것보다 낫다.
    """
    b = _ott_benefits()
    priced = b[b["v"].notna() & b["v"].gt(0)]
    return {name: int(v) for name, v in priced.groupby("benefit_name")["v"].median().items()}


# 일회성 사은품을 월 요금과 더하려면 몇 개월로 나눌지 정해야 한다. 알뜰폰 사용자가
# 6개월 주기로 갈아탄다는 전제와, 프로모션 할인기간 최빈값이 7개월인 것에 맞춘다.
BENEFIT_HORIZON_MONTHS = 6

# 값을 세는 혜택. `사은품/페이백`만 쓴다 - 상품권·네이버페이·현금성이라 누구에게나
# 같은 값이다. OTT는 사용자가 원한다고 답했을 때만 `ott_bonus()`가 따로 세고(중복
# 방지), 스마트기기는 워치·태블릿이 있어야 값이 생기므로 빼둔다.
CASH_BENEFIT_CATEGORY = "사은품/페이백"

_MONTHS_RE = re.compile(r"(\d+)\s*개월")

# 조건이 붙은 혜택(111행)은 세지 않는다. `갤럭시 자급제 이벤트 신세계상품권 10만원`은
# 단말을 사야, `팝테일 가입·인증`은 다른 서비스에 가입해야 받는다. 요금제를 고른
# 것만으로 생기는 값이 아니라서, 넣으면 조건을 못 채운 사람에게 부풀린 값을 보인다.
_CONDITIONAL_RE = re.compile(r"자급제|이벤트|구매|기기|단말|결합|카드|제휴|인증|가입 시")


@functools.lru_cache(maxsize=1)
def cash_benefit_monthly() -> dict[str, int]:
    """plan_id -> 월로 환산한 사은품 가치(원).

    `benefit_value_won`이 총액인지 월액인지가 **행마다 다르다.** 개월 수가 적힌
    296행을 이름 속 금액과 대조해 보면 190행은 월액, 36행은 총액이었다.

        네이버페이 매달 2.5만원 페이백 (12개월)   300,000  <- 총액
        올리브영 12개월간 매월 5,000원 상품권       5,000  <- 월액
        S-머니3만원(5천원X6개월)                 30,000  <- 총액이지만 이름에
                                                          총액·월액이 같이 있다

    마지막 형태 때문에 이름 파싱만으로는 못 가른다. 그래서 **총액으로 보고 기간으로
    나눈다.** 월액인 행은 값이 실제보다 작게 잡히지만, 반대로 틀리면 39만원짜리
    가짜 혜택이 1위를 차지한다. 부풀리는 쪽보다 낮춰 잡는 쪽이 안전하다.

    ponytail: 월액 190행을 과소평가한다. 사이트가 총액·월액을 구분해 적기 시작하면
    그때 정확히 가른다.
    """
    b = pd.read_csv(final_path("통신요금제_혜택상세_최종.csv"), encoding="utf-8-sig")
    b = b[b["benefit_category"].eq(CASH_BENEFIT_CATEGORY)].copy()
    b["v"] = pd.to_numeric(b["benefit_value_won"], errors="coerce")
    b = b[b["v"] > 0]
    b = b[~b["benefit_name"].str.contains(_CONDITIONAL_RE, na=False)]

    months = b["benefit_name"].str.extract(_MONTHS_RE, expand=False).astype(float)
    # 기간이 없으면 한 번 받는 사은품이므로 갈아타기 주기로 나눈다.
    spread = months.fillna(BENEFIT_HORIZON_MONTHS).clip(lower=BENEFIT_HORIZON_MONTHS)
    total = (b["v"] / spread).groupby(b["plan_id"].astype(str)).sum().round()
    return {pid: int(v) for pid, v in total.items()}


@functools.lru_cache(maxsize=1)
def _ott_benefits() -> pd.DataFrame:
    """OTT/구독 혜택 행. `plan_id`로 요금제와 이어진다."""
    b = pd.read_csv(final_path("통신요금제_혜택상세_최종.csv"), encoding="utf-8-sig")
    b = b[b["benefit_category"].eq("OTT/구독")].copy()
    b["v"] = pd.to_numeric(b["benefit_value_won"], errors="coerce")
    b["plan_id"] = b["plan_id"].astype(str)
    b["benefit_service"] = b["benefit_service"].fillna("")
    return b[["plan_id", "benefit_service", "benefit_name", "v"]]


@functools.lru_cache(maxsize=1)
def _ott_by_plan() -> dict[str, list[tuple[str, str]]]:
    """plan_id -> [(서비스명, 혜택명)]. 요금제가 실제로 받는 OTT 혜택."""
    out: dict[str, list[tuple[str, str]]] = {}
    for pid, service, name in _ott_benefits()[
            ["plan_id", "benefit_service", "benefit_name"]].itertuples(index=False):
        out.setdefault(pid, []).append((service, name))
    return out


def ott_bonus(plan_id: str, services: list[str]) -> int:
    """이 요금제에서 그 OTT들이 갖는 값의 합.

    요금제가 실제로 받는 혜택 행의 **이름**으로 값을 찾는다. 요약 컬럼
    `ott_options`는 `schema.py`가 등급을 지우고 서비스명만 남긴 것이라
    (`티빙 광고형 스탠다드` -> `티빙`) 값을 매기는 근거로 쓸 수 없다.
    """
    if not services:
        return 0
    prices, rows = ott_prices(), _ott_by_plan().get(str(plan_id), [])
    total = 0
    for want in services:
        hit = [name for service, name in rows if want in service or want in name]
        # 한 요금제가 같은 OTT를 여러 등급으로 주는 경우는 없다(전수 확인: 0건).
        # 여러 개가 잡히면 이름이 겹친 것이므로 값이 큰 쪽을 쓴다.
        total += max((_one_ott_value(n, prices) for n in hit), default=OTT_BONUS_KRW)
    return total


def _one_ott_value(name: str, prices: dict[str, int]) -> int:
    """혜택 하나의 값. 사이트가 적어 둔 금액이 있으면 그 값.

    `유튜브 프리미엄 + OTT 1종 할인`처럼 **깎아주는** 혜택이 104행 있는데, 이건
    구독을 주는 게 아니라 사용자가 차액을 내고 올리는 것이다. 제공형과 같은 값을
    매기면 안 된다. 금액이 적힌 할인 혜택은 0행이라 값을 매길 근거도 없다.
    ponytail: 할인은 0으로 둔다. 사이트가 할인액을 적기 시작하면 그 값을 쓴다.
    """
    if name in prices:
        return prices[name]
    return 0 if "할인" in name else OTT_BONUS_KRW


def blocked_by_carrier(df: pd.DataFrame, current_carrier: str) -> pd.Series:
    """지금 쓰는 통신사 때문에 가입할 수 없는 요금제인가.

    3사의 온라인 전용(KT 요고 26 · LGU+ 너겟 50 · SKT 다이렉트 59)은 **번호이동이나
    신규 가입으로만** 받는다. 이미 그 통신사를 쓰고 있으면 기기변경으로 못 옮긴다.
    모요가 45행에 그대로 적어 뒀다 - "이미 SKT 요금제를 쓰고 있다면 이 요금제를
    가입할 수 없어요". 3사 자사 페이지에는 그 문구가 없어서 90행은 비어 있지만,
    같은 상품이므로 규칙으로 판단한다.

    알뜰폰은 대상이 아니다. 알뜰폰 사용자가 3사 온라인 전용으로 가는 건 번호이동이라
    막히지 않고, 알뜰폰끼리 옮기는 것도 제한이 없다.
    """
    return (df["is_online_only"].eq(True)
            & df["carrier_type"].eq("MNO")
            & df["host_mno"].eq(current_carrier))


def usable_gb(df: pd.DataFrame) -> pd.Series:
    """월에 온전한 속도로 쓸 수 있는 데이터량. 판단이 안 되면 NaN.

    일 단위로만 주는 요금제가 61개 있는데(`daily_data_gb`만 있고 `data_gb`가 빔),
    환산하지 않으면 숫자 조건에서 통째로 탈락한다.
    """
    return df["data_gb"].fillna(df["daily_data_gb"] * DAYS_PER_MONTH)


def usable_count(df: pd.DataFrame, unlimited_col: str, count_col: str) -> pd.Series:
    """월에 쓸 수 있는 통화 분수 / 문자 건수. 무제한은 inf, 판단이 안 되면 NaN.

    **무제한이면 사이트가 분수·건수를 아예 안 적는다.** 통화 결측 1,765행 중 1,761행
    (99.8%), 문자 결측 1,730행 중 1,685행(97.4%)이 무제한이고, 반대로 무제한인데
    숫자가 적힌 행은 0이다. 결측을 그대로 두면 "300분 이상"에서 무제한 요금제가
    통째로 탈락한다 - `usable_gb()`가 일 단위 요금제에 대해 하는 일과 같다.

    남은 4행(통화)·45행(문자)은 유제한인데 숫자도 없는 경우로 전부 SKT 공식몰이다.
    확인할 방법이 없으므로 NaN으로 두고 떨어뜨린다 - 못 지킬 조건을 지킨다고 하는
    것보다 낫다.
    """
    return df[count_col].mask(df[unlimited_col].eq(True), float("inf"))


def full_speed_unlimited(df: pd.DataFrame) -> pd.Series:
    """속도 제한 없이 데이터를 계속 쓸 수 있는 요금제인가.

    `data_throttle_speed`가 두 가지를 가리킨다. 속도제어가 붙은 무제한 186개 중
    **180개는 테더링 한도를 넘겼을 때의 속도**고(KT 초이스 더블: 테더링 90GB 후
    100Kbps — 본 데이터는 진짜 무제한), 나머지 6개만 **본 데이터 소진 후 속도**다
    (시월 무제한 7GB+1Mbps). 둘은 `tethering_gb`가 채워졌는지로 정확히 갈린다
    (180 대 6, 겹침 0).
    """
    unlimited = df["data_unlimited"].eq(True)
    throttles_tethering_only = df["tethering_gb"].notna()
    return unlimited & (df["data_throttle_speed"].isna() | throttles_tethering_only)


def _age_ok(cond, age: int | None) -> bool:
    """`age_condition` 한 칸이 이 나이를 받아주는가.

    빈값은 조건 없음이라 항상 통과한다(2,797행 중 2,495행). 나이를 안 받았으면
    조건이 붙은 요금제는 확인할 방법이 없으므로 통과시키고, 결과에 조건을 그대로
    실어 보낸다.
    """
    if not isinstance(cond, str) or not cond.strip() or cond.strip() == "제한없음":
        return True
    if age is None:
        return True
    m = _AGE_RE.search(cond)
    if not m:
        return True
    n, rel = int(m.group(1)), m.group(2)
    return {
        "이하": age <= n,
        "미만": age < n,
        "이상": age >= n,
        "초과": age > n,
    }[rel]


def recommend(
    plans: pd.DataFrame,
    *,
    budget: int | None = None,
    data_gb: float | None = None,
    data_unlimited: bool = False,
    voice_unlimited: bool = False,
    sms_unlimited: bool = False,
    voice_minutes: float | None = None,
    sms_count: float | None = None,
    mvno_ok: bool = True,
    current_carrier: str | None = None,
    age: int | None = None,
    ott_want: tuple[str, ...] = (),
    ott_required: bool = False,
    current_fee: int | None = None,
    top_n: int = TOP_N,
) -> pd.DataFrame:
    """조건을 만족하는 요금제를 싼 순으로 top_n개. 조건은 전부 선택값이다."""
    df = plans

    # 가격을 모르면 추천할 수 없다. 예산 비교도 절감액 계산도 안 된다.
    df = df[df["discounted_fee"].notna()]

    if budget is not None:
        df = df[df["discounted_fee"] <= budget]

    if data_unlimited:
        df = df[df["data_unlimited"].eq(True)]
    elif data_gb is not None:
        # 본 데이터가 속도 제한 없이 무제한이면 무조건 통과. 아니면 실제로 온전한
        # 속도로 쓸 수 있는 양(일 단위 요금제는 월 환산)으로 판단한다.
        df = df[full_speed_unlimited(df) | usable_gb(df).ge(data_gb)]

    if voice_unlimited:
        df = df[df["voice_unlimited"].eq(True)]
    if sms_unlimited:
        df = df[df["sms_unlimited"].eq(True)]
    # "통화 300분 이상"처럼 숫자로 말한 경우. 무제한은 inf라 항상 통과한다.
    if voice_minutes is not None:
        df = df[usable_count(df, "voice_unlimited", "voice_minutes").ge(voice_minutes)]
    if sms_count is not None:
        df = df[usable_count(df, "sms_unlimited", "sms_count").ge(sms_count)]
    if not mvno_ok:
        df = df[df["carrier_type"].eq("MNO")]
    if current_carrier:
        df = df[~blocked_by_carrier(df, current_carrier)]
    if len(df):
        df = df[df["age_condition"].map(lambda c: _age_ok(c, age))]

    if df.empty:
        return df.assign(ott_matched=[], benefit_monthly=[], score=[])

    matched = df["ott_options"].map(lambda o: _ott_matches(o, ott_want))

    # 사용자가 "이것만 보겠다"고 켠 경우에만 필터가 된다. 기본은 가점이다 - 예산
    # 3만원 이하 2,014개 중 OTT가 붙은 건 221개라, 항상 걸면 89%가 날아간다.
    if ott_required and ott_want:
        keep = matched.str.len() > 0
        df, matched = df[keep], matched[keep]
        if df.empty:
            return df.assign(ott_matched=[], benefit_monthly=[], score=[])

    # 가점은 사이트가 적어 둔 구독 가치만큼. 등급별로 다르므로 요금제마다 따로 본다.
    bonus = pd.Series(
        [ott_bonus(pid, ms) for pid, ms in zip(df["plan_id"], matched)],
        index=df.index,
    )
    # 사은품·페이백은 **점수에서 빼지 않는다.** 금액이 붙은 1,148행이 전부 모요인데,
    # 모요의 `discounted_fee`는 이미 페이백을 뺀 체감가다(crawl_moyo.py 참고 -
    # LGU+ 너겟49가 공식몰 49,000원, 모요 7,000원인 게 42,000원 페이백을 미리
    # 반영한 값이다). 또 빼면 같은 돈을 두 번 세는 것이고, 실제로 그렇게 넣었다가
    # 상위권이 페이백 큰 요금제로 뒤집혔다. 값은 화면에 보여주기 위해 싣기만 한다.
    cash = df["plan_id"].astype(str).map(cash_benefit_monthly()).fillna(0)
    df = df.assign(
        ott_matched=matched,
        benefit_monthly=cash.astype(int),
        score=df["discounted_fee"] - bonus,
    )
    # 점수가 같으면 스펙 대비 싼 쪽(회귀 가성비 점수)을 위로. 가격·스펙이 똑같아
    # 순서를 매길 근거가 없던 동점을 여기서 가른다. `value_score`가 안 붙어 있으면
    # 그 단계만 건너뛴다 - fair_price.attach_value_score()로 붙인다.
    keys = ["score"]
    order = [True]
    if "value_score" in df.columns:
        keys.append("value_score")
        order.append(False)
    # 그래도 같으면 데이터가 많은 쪽을 위로. 끝까지 같으면 순서에 근거가 없으므로
    # 요금제 ID로 고정해 실행할 때마다 결과가 흔들리지 않게 한다.
    df = df.sort_values([*keys, "data_gb", "plan_id"], ascending=[*order, False, True])

    # 같은 요금제가 나이 조건별로 따로 한 행씩 있다(`베이직30GB`가 5행). 나이를 안
    # 받으면 다섯이 다 통과해 결과가 같은 이름으로 채워진다. 정렬이 끝난 뒤 가장
    # 위의 것만 남긴다.
    dedupe_key = df["base_plan_id"].fillna(df["plan_id"])
    df = df[~dedupe_key.duplicated()].head(top_n)

    # 지금 내는 돈을 알려줬으면 "월 얼마 아낀다"를 같이 낸다. 신규 가입이면 없다.
    if current_fee is not None:
        df = df.assign(savings=current_fee - df["discounted_fee"])
    return df


# 조건을 못 맞출 때 푸는 순서. 앞엣것부터 뺀다 - 요구가 약한 것, 사용자가 "있으면
# 좋다"는 뜻으로 답했을 가능성이 큰 것이 먼저다. 예산·데이터는 절대 풀지 않는다.
# 그 둘은 사용자가 명시한 핵심 조건이고, 몰래 풀면 못 내는 돈을 추천하게 된다.
RELAXABLE: list[tuple[str, object, str]] = [
    ("ott_required", False, "OTT 필수 조건"),
    ("sms_count", None, "문자 최소 건수"),
    ("sms_unlimited", False, "문자 무제한"),
    ("voice_minutes", None, "통화 최소 분수"),
    ("voice_unlimited", False, "통화 무제한"),
    ("age", None, "연령 전용 요금제 제외"),
    ("mvno_ok", True, "알뜰폰 제외"),
]


def relax(plans: pd.DataFrame, query: dict, top_n: int = TOP_N) -> tuple[pd.DataFrame, list[str]]:
    """결과가 0건이면 조건을 하나씩 풀어 다시 찾는다.

    푼 조건을 같이 돌려주므로 화면에서 "통화 무제한을 빼면 이런 게 있습니다"라고
    말할 수 있다. 240칸짜리 입력 격자에서 36칸(15%)이 0건인데, 지금은 빈 화면만
    나온다.
    """
    got = recommend(plans, **query, top_n=top_n)
    if not got.empty:
        return got, []

    q, dropped = dict(query), []
    for key, off, label in RELAXABLE:
        if q.get(key) in (off, None) or key not in q:
            continue
        q[key] = off
        dropped.append(label)
        got = recommend(plans, **q, top_n=top_n)
        if not got.empty:
            return got, dropped
    return got, dropped


def min_budget_for(plans: pd.DataFrame, query: dict) -> int | None:
    """예산만 빼고 나머지 조건을 만족하는 가장 싼 요금제의 값. 없으면 None.

    조건을 다 풀어도 0건이면 남은 원인은 예산이다. "얼마면 되는지"를 말해 줄 수
    있어야 사용자가 다음 행동을 안다.
    """
    q = {**query, "budget": None}
    got = recommend(plans, **q, top_n=1)
    return int(got.iloc[0]["discounted_fee"]) if len(got) else None


def ott_note(plans: pd.DataFrame, candidates: pd.DataFrame, ott_want: tuple[str, ...],
             budget: int | None = None) -> str | None:
    """원하는 OTT가 조건에 맞는 요금제 어디에도 없을 때 보여줄 안내. 없으면 None.

    OTT는 필터가 아니라 가점이라, 없으면 그냥 없는 채로 추천이 나간다. 아무 말도
    안 하면 사용자는 요청이 무시됐다고 느낀다. 넷플릭스가 붙은 요금제는 35개인데
    전부 3만원을 넘는다.

    `candidates`는 필터를 통과한 **전체** 후보를 넣는다(`top_n`을 크게 준 결과).
    상위 5개만 넣으면 6위에 있는 것도 "없다"고 말하게 된다.
    """
    if not ott_want or candidates.empty:
        return None
    if candidates["ott_matched"].str.len().gt(0).any():
        return None

    names = ", ".join(f"'{o}'" for o in ott_want)
    has = plans[plans["ott_options"].map(lambda o: bool(_ott_matches(o, ott_want)))]
    has = has[has["discounted_fee"].notna()]
    if has.empty:
        return f"{names} 제공 요금제를 찾지 못했습니다."

    cheapest = has.loc[has["discounted_fee"].idxmin()]
    fee, name = int(cheapest["discounted_fee"]), cheapest["plan_name"]

    # 예산 안인데도 후보에 없다면 걸린 건 가격이 아니라 나머지 조건이다.
    if budget is not None and fee <= budget:
        return (f"{names} 포함 요금제는 예산 안에 있지만({fee:,}원 {name}), "
                f"데이터·통화 조건까지 맞는 게 없습니다.")

    where = f"예산 {int(budget):,}원 안에는" if budget is not None else "조건에 맞는 요금제 중에는"
    return f"{where} {names} 포함 요금제가 없습니다. 가장 싼 것은 {fee:,}원({name})입니다."


def broken_rules(df: pd.DataFrame, query: dict) -> list[str]:
    """이 결과가 요청의 어느 조건을 어겼는가. `recommend()`가 성하면 항상 빈 목록.

    필터의 거울이다 - 걸러낸 조건을 결과에서 다시 확인한다. 방식 비교(군집형은
    조건을 보장하지 않는다)와 노트북에서 같은 잣대를 쓰려고 여기 둔다.
    한 줄만 넣으면(`df.iloc[[i]]`) 그 요금제 하나의 위반이 된다.
    """
    if df.empty:
        return []
    bad = []
    if query.get("budget") is not None and df["discounted_fee"].gt(query["budget"]).any():
        bad.append("예산")
    if query.get("data_unlimited"):
        if not df["data_unlimited"].eq(True).all():
            bad.append("무제한")
    elif query.get("data_gb") is not None and not (
            full_speed_unlimited(df) | usable_gb(df).ge(query["data_gb"])).all():
        bad.append("데이터")
    for flag, label in (("voice_unlimited", "통화무제한"), ("sms_unlimited", "문자무제한")):
        if query.get(flag) and not df[flag].eq(True).all():
            bad.append(label)
    for need, flag, col, label in (("voice_minutes", "voice_unlimited", "voice_minutes", "통화분수"),
                                   ("sms_count", "sms_unlimited", "sms_count", "문자건수")):
        if query.get(need) is not None and not usable_count(df, flag, col).ge(query[need]).all():
            bad.append(label)
    if query.get("mvno_ok") is False and not df["carrier_type"].eq("MNO").all():
        bad.append("알뜰폰")
    if query.get("current_carrier") and blocked_by_carrier(df, query["current_carrier"]).any():
        bad.append("가입불가")
    if query.get("age") is not None and not df["age_condition"].map(
            lambda c: _age_ok(c, query["age"])).all():
        bad.append("연령")
    return bad


def _ott_matches(options, want: tuple[str, ...]) -> list[str]:
    if not want or not isinstance(options, str):
        return []
    have = [o.strip() for o in options.split("|")]
    return [w for w in want if any(w in h for h in have)]


def check() -> None:
    plans = load_plans()

    # 예산은 반드시 지켜진다.
    r = recommend(plans, budget=30_000, data_gb=20)
    assert len(r) == 5
    assert (r["discounted_fee"] <= 30_000).all()
    assert (full_speed_unlimited(r) | usable_gb(r).ge(20)).all()

    # 조건을 좁히면 후보도 좁아진다.
    assert len(recommend(plans, budget=30_000, mvno_ok=False, top_n=999)) < len(
        recommend(plans, budget=30_000, top_n=999)
    )

    # 나이 조건: 만 65세 이상 전용 요금제는 27세에게 안 나온다.
    senior = plans[plans["age_condition"].eq("만 65세 이상")]
    assert len(senior)
    young = recommend(senior, age=27, top_n=999)
    assert young.empty
    assert len(recommend(senior, age=70, top_n=999))

    # OTT는 기본이 가점이다 - 걸어도 후보 수가 줄지 않는다.
    base = recommend(plans, budget=30_000, top_n=999)
    with_ott = recommend(plans, budget=30_000, ott_want=("넷플릭스",), top_n=999)
    assert len(base) == len(with_ott)

    # 가점은 사이트가 적어 둔 구독 가치를 쓴다. 넷플릭스는 17,000원.
    prices = ott_prices()
    # 값은 혜택명 단위다. 사이트가 값을 적은 이름만 들어 있어야 한다.
    assert prices["티빙"] == 17_000, prices["티빙"]
    assert "티빙 광고형 스탠다드 제공 (12개월)" not in prices, "값 없는 등급에 값이 생겼다"

    # 같은 티빙이라도 등급에 따라 가점이 다르다. 이게 이 구조를 만든 이유다.
    tv = recommend(plans, budget=200_000, ott_want=("티빙",), ott_required=True, top_n=99_999)
    gained = (tv["discounted_fee"] - tv["score"]).unique()
    assert 17_000 in gained and OTT_BONUS_KRW in gained, sorted(gained)

    # 깎아주는 혜택은 구독을 주는 게 아니다. 값이 0이어야 한다.
    assert _one_ott_value("티빙&웨이브 광고형 50% 할인", prices) == 0
    assert _one_ott_value("티빙 광고형 스탠다드 제공 (12개월)", prices) == OTT_BONUS_KRW
    assert _one_ott_value("티빙", prices) == 17_000

    nf = recommend(plans, budget=100_000, ott_want=("넷플릭스",), top_n=99_999)
    hit = nf[nf["ott_matched"].str.len() > 0]
    # 명시 금액 / 제공형 기본값 / 할인형 0, 셋 중 하나여야 한다.
    assert (hit["discounted_fee"] - hit["score"]).isin([17_000, OTT_BONUS_KRW, 0]).all()

    # "이것만 보기"를 켜면 필터가 된다.
    only = recommend(plans, budget=100_000, ott_want=("넷플릭스",), ott_required=True, top_n=999)
    assert len(only) and only["ott_matched"].str.len().gt(0).all()
    assert len(only) < len(recommend(plans, budget=100_000, top_n=999))
    # 켰는데 하나도 없으면 빈 결과. 예외를 던지지 않는다.
    assert recommend(plans, budget=30_000, ott_want=("넷플릭스",), ott_required=True).empty
    # 겹치는 요금제가 있으면 가점만큼 앞으로 온다.
    hit = with_ott[with_ott["ott_matched"].str.len() > 0]
    if len(hit):
        assert (hit["score"] < hit["discounted_fee"]).all()

    # 같은 요금제가 나이 조건별 변형으로 여러 번 나오지 않는다.
    wide = recommend(plans, budget=50_000, data_gb=50, mvno_ok=False, top_n=999)
    assert not wide["base_plan_id"].fillna(wide["plan_id"]).duplicated().any()

    # 본 데이터가 제한되는 QoS형(시월 무제한 7GB+1Mbps 등 6개)은 "N GB 필요"를
    # 통과하지 못한다. 테더링만 제한되는 MNO 무제한 180개는 통과한다.
    qos = plans[plans["data_unlimited"].eq(True)
                & plans["data_throttle_speed"].notna()
                & plans["tethering_gb"].isna()]
    assert len(qos) == 6, len(qos)
    # 그중 "매일 2GB" 같은 일 단위 제공이 붙은 건 월 환산으로 통과해도 맞다.
    no_quota = qos[qos["daily_data_gb"].isna()]
    assert len(no_quota) == 4, len(no_quota)
    assert recommend(no_quota, data_gb=50, top_n=999).empty
    assert len(recommend(qos, data_unlimited=True, top_n=999)) == 6

    tether_only = plans[full_speed_unlimited(plans) & plans["data_throttle_speed"].notna()]
    assert len(tether_only) == 180, len(tether_only)
    assert len(recommend(tether_only, budget=99_000, data_gb=50, top_n=999)) > 0

    # 일 단위로만 주는 요금제도 월 환산으로 후보에 들어온다.
    daily = plans[plans["data_gb"].isna() & plans["daily_data_gb"].notna()
                  & ~plans["data_unlimited"].eq(True)]
    assert len(daily) > 0
    assert len(recommend(daily, data_gb=30, top_n=999)) > 0, "일 단위 요금제가 전부 탈락"

    # 통화·문자를 숫자로 요구해도 무제한 요금제는 탈락하지 않는다. 무제한이면
    # 사이트가 분수를 안 적어서 결측인데, 그걸 그대로 두면 통째로 떨어진다.
    assert plans["voice_minutes"].isna().sum() > 1_000, "전제가 바뀌었다"
    unl = plans[plans["voice_unlimited"].eq(True)]
    assert unl["voice_minutes"].isna().all(), "무제한인데 분수가 적힌 행이 생겼다"
    assert len(recommend(unl, budget=99_000, voice_minutes=9_999, top_n=99_999)) == len(
        recommend(unl, budget=99_000, top_n=99_999)), "무제한이 분수 조건에서 탈락한다"

    # 유제한은 실제 분수로 걸린다. 조건을 올리면 후보가 줄어든다.
    limited = plans[plans["voice_minutes"].notna()]
    assert len(recommend(limited, budget=99_000, voice_minutes=300, top_n=99_999)) < len(
        recommend(limited, budget=99_000, voice_minutes=100, top_n=99_999))
    got = recommend(plans, budget=30_000, data_gb=20, voice_minutes=300, top_n=99_999)
    assert (got["voice_unlimited"].eq(True) | got["voice_minutes"].ge(300)).all()
    # 무제한을 살려 두는 게 핵심이다 - 안 살리면 후보가 1/8로 줄어든다.
    assert got["voice_unlimited"].eq(True).sum() > got["voice_minutes"].ge(300).sum()

    # 유제한인데 숫자도 없는 행(SKT 공식몰 4행)은 확인이 안 되므로 떨어뜨린다.
    murky = plans[plans["voice_minutes"].isna() & ~plans["voice_unlimited"].eq(True)]
    assert len(murky) == 4, len(murky)
    assert recommend(murky, budget=99_000, voice_minutes=1, top_n=99_999).empty
    # 안 물으면 남는다. 조건을 걸었을 때만 떨어진다는 게 요점이다.
    assert not recommend(murky, budget=99_000, top_n=99_999).empty

    # 문자도 같은 규칙이다.
    sms = recommend(plans, budget=30_000, sms_count=100, top_n=99_999)
    assert (sms["sms_unlimited"].eq(True) | sms["sms_count"].ge(100)).all()

    # 0건이면 숫자 조건도 풀 수 있어야 한다. 무제한은 inf라 어떤 분수든 통과하므로
    # 유제한만 남긴 표에서 본다.
    hard = dict(budget=99_000, voice_minutes=99_999.0)
    assert recommend(limited, **hard).empty
    relaxed, dropped_num = relax(limited, hard)
    assert "통화 최소 분수" in dropped_num, dropped_num
    assert not relaxed.empty, "풀었는데도 0건이다"

    # 지금 쓰는 통신사의 온라인 전용은 기기변경으로 못 옮긴다.
    for carrier in ("SKT", "KT", "LGU+"):
        got = recommend(plans, budget=90_000, mvno_ok=False,
                        current_carrier=carrier, top_n=99_999)
        assert not blocked_by_carrier(got, carrier).any(), carrier
        # 다른 통신사의 온라인 전용은 번호이동이라 그대로 남는다.
        others = got[got["is_online_only"].eq(True) & got["carrier_type"].eq("MNO")]
        assert len(others), f"{carrier} 사용자에게 타사 온라인전용이 하나도 없다"
    # 안 물어보면 아무것도 빼지 않는다.
    assert len(recommend(plans, budget=90_000, mvno_ok=False, top_n=99_999)) > len(
        recommend(plans, budget=90_000, mvno_ok=False, current_carrier="SKT", top_n=99_999))

    # 현재 요금을 주면 절감액이 붙고, 안 주면 컬럼 자체가 없다.
    now = recommend(plans, budget=30_000, data_gb=20, current_fee=69_000)
    assert (now["savings"] == 69_000 - now["discounted_fee"]).all()
    assert "savings" not in recommend(plans, budget=30_000, data_gb=20).columns

    # 큰 요금제도 싸면 올라온다. 50GB를 요구했는데 100GB짜리가 1위여도 정상이다.
    fifty = recommend(plans, budget=30_000, data_gb=50)
    assert (full_speed_unlimited(fifty) | usable_gb(fifty).ge(50)).all()
    assert usable_gb(fifty).gt(50).any(), "요구량보다 큰 요금제가 하나도 안 올라왔다"

    # 사은품·페이백은 화면에 싣되 **점수는 건드리지 않는다.** 모요 가격이 이미
    # 페이백을 반영한 값이라, 빼면 같은 돈을 두 번 센다.
    cash = cash_benefit_monthly()
    assert len(cash) > 500, len(cash)
    withb = recommend(plans, budget=30_000, top_n=99_999)
    rich = withb[withb["benefit_monthly"] > 0]
    assert len(rich), "혜택 금액이 실려 나가지 않는다"
    assert (rich["score"] == rich["discounted_fee"]).all(), "사은품이 점수에 섞였다"
    # 금액이 붙은 사은품은 전부 모요 출처다. 다른 채널에서 나오면 전제가 깨진 것이다.
    assert rich["signup_channel"].eq("모요").all(), set(rich["signup_channel"])

    # 원하는 OTT가 예산 안에 없으면 그 사실과 최저가를 알려준다.
    want = ("넷플릭스",)
    cheap = recommend(plans, budget=30_000, data_gb=20, ott_want=want, top_n=99_999)
    note = ott_note(plans, cheap, want, budget=30_000)
    # 최저가를 값으로 박지 않는다. 요금제가 늘면 바뀐다(너겟이 후보에 들어오며
    # 66,750원 -> 59,000원이 됐다). 데이터에서 계산한 값과 맞는지만 본다.
    has_nf = plans[plans["ott_options"].map(lambda o: bool(_ott_matches(o, want)))]
    cheapest = int(has_nf["discounted_fee"].min())
    assert note and "넷플릭스" in note and f"{cheapest:,}원" in note, note

    # 상위 5개만 넘기면 6위에 있는 것도 "없다"고 말한다. 전체 후보를 넘겨야 한다.
    top5 = recommend(plans, budget=90_000, data_unlimited=True, ott_want=("티빙",))
    allc = recommend(plans, budget=90_000, data_unlimited=True, ott_want=("티빙",), top_n=99_999)
    assert ott_note(plans, top5, ("티빙",), 90_000) is not None
    assert ott_note(plans, allc, ("티빙",), 90_000) is None, "후보 안에 있는데 없다고 한다"

    # 예산은 넉넉한데 다른 조건에 걸린 경우는 가격 탓을 하지 않는다.
    no_ott = plans[plans["ott_options"].isna()]
    note2 = ott_note(plans, recommend(no_ott, budget=90_000, ott_want=want, top_n=99_999),
                     want, budget=90_000)
    assert note2 and "예산 안에 있지만" in note2, note2
    # 겹치는 게 있으면 안내하지 않는다.
    only_ott = plans[plans["ott_options"].map(lambda o: bool(_ott_matches(o, want)))]
    rich = recommend(only_ott, budget=100_000, ott_want=want)
    assert len(rich) and ott_note(plans, rich, want, budget=100_000) is None
    # OTT를 안 물었으면 아무 말도 하지 않는다.
    assert ott_note(plans, cheap, (), budget=30_000) is None

    # 가입 채널이 모든 행에 붙는다. 같은 요금제가 채널에 따라 가격이 다르므로
    # 어느 쪽인지 안 보이면 사용자가 왜 두 번 나오는지 알 수 없다.
    assert plans["signup_channel"].ne("").all(), plans.loc[
        plans["signup_channel"].eq(""), "source_url"].head(3).tolist()
    nugget = plans[plans["plan_name"].str.replace(" ", "").eq("너겟49")]
    assert set(nugget["signup_channel"]) == {"모요", "LG U+ 공식몰"}, set(nugget["signup_channel"])
    # 모요 쪽이 프로모션가라 더 싸다.
    by_ch = nugget.set_index("signup_channel")["discounted_fee"]
    assert by_ch["모요"] < by_ch["LG U+ 공식몰"], by_ch.to_dict()

    # 필터의 거울. 어떤 조건 조합을 넣어도 결과가 그 조건을 어기지 않아야 한다.
    for q in (
        dict(budget=30_000, data_gb=20.0, voice_minutes=300.0, sms_count=100.0),
        dict(budget=90_000, data_unlimited=True, mvno_ok=False, current_carrier="SKT"),
        dict(budget=50_000, data_gb=50.0, age=70, voice_unlimited=True),
    ):
        assert not broken_rules(recommend(plans, **q, top_n=99_999), q), q
    # 거울이 실제로 잡는지. 필터를 안 거친 표를 넣으면 위반이 나와야 한다.
    assert "예산" in broken_rules(plans, {"budget": 1_000})

    # 만족할 수 없는 조건이면 빈 결과. 예외를 던지지 않는다.
    assert recommend(plans, budget=100, data_gb=100).empty

    # 0건이면 조건을 풀어 다시 찾고, 무엇을 풀었는지 알려준다.
    tight = dict(budget=5_000, data_gb=5.0, data_unlimited=False,
                 voice_unlimited=True, sms_unlimited=True, mvno_ok=False,
                 age=None, ott_want=(), ott_required=False, current_fee=None)
    assert recommend(plans, **tight).empty
    got, dropped = relax(plans, tight)
    assert dropped, "0건인데 아무것도 풀지 않았다"
    # 예산과 데이터는 절대 풀지 않는다.
    assert got.empty or (got["discounted_fee"] <= 5_000).all()

    # 조건이 맞으면 아무것도 풀지 않는다.
    fine = dict(budget=30_000, data_gb=20.0, data_unlimited=False,
                voice_unlimited=False, sms_unlimited=False, mvno_ok=True,
                age=None, ott_want=(), ott_required=False, current_fee=None)
    assert relax(plans, fine)[1] == []

    # 다 풀어도 0건이면 얼마면 되는지 말해 준다.
    need = min_budget_for(plans, {**tight, "data_gb": 100.0})
    assert need and need > 5_000, need

    # 회귀 점수를 붙이면 가격 동점의 순서가 스펙 대비 가성비로 갈린다.
    from fair_price import attach_value_score

    scored = attach_value_score(plans)
    tied = recommend(scored, budget=30_000, data_gb=20, top_n=999)
    tied = tied[tied["score"].eq(tied["score"].iloc[0])]
    assert tied["value_score"].is_monotonic_decreasing, "동점 구간이 가성비 순이 아니다"

    print(f"check ok: 3만원/20GB 1위 {r.iloc[0]['plan_name']} {int(r.iloc[0]['discounted_fee']):,}원")


if __name__ == "__main__":
    check()
