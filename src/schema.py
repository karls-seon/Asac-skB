"""통신 3사(KT/SKT/LGU+) + 모요(MVNO) 요금제 통합 스키마.

요금제 하나가 "넷플릭스/유튜브/디즈니+/티빙… 중 택1"처럼 선택지를 최대 14개까지
갖는다. 한 행에 파이프로 밀어넣으면 "넷플릭스 주는 요금제 찾기"조차 문자열
검색이 되므로, 혜택은 별도 long-format 테이블로 분리했다.

- plans.csv   : 요금제 1개 = 1행 (혜택은 요약 컬럼으로만)
- benefits.csv: 요금제 1개 × 혜택 1개 = 1행 (조인키: plan_id)

plan_id 형식
- KT   : ItemCode_요금제명[_연령] (예: 1693_베이직100)
- SKT  : NA로 시작하는 상품코드 (예: NA00009818)
- LGU+ : Z/LPZ로 시작하는 상품코드 (예: Z202605251)
- 모요 : URL의 숫자 ID (예: 30954)
사이트에 고유 코드가 없어 요금제명을 대신 쓴 경우 plan_id_type=name_based.

carrier_type은 "어느 채널에서 수집했나"다. 너겟(LGU+)/다이렉트(SKT)/요고(KT)는
통신사 온라인전용 요금제지만 모요에도 올라와 있어서, 모요판(MVNO)과 통신사
사이트판(MNO) **두 행이 모두 존재**한다(2026-08-13 기준 45건). 요금제 개수·
평균가를 셀 때는 이 중복을 감안해야 한다.
"""
import csv
import re
from pathlib import Path

# 경로는 이 파일 위치 기준으로 잡는다. 상대경로면 프로젝트 루트에서 실행할 때만 동작한다.
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
RAW_CACHE_DIR = DATA_DIR / "raw_cache"   # 사이트 원본 HTML/JSON
INTERIM_DIR = DATA_DIR / "interim"       # 사이트별 중간 CSV
FINAL_DIR = DATA_DIR / "final"           # 합친 최종 CSV


def cache_dir(site: str) -> Path:
    return RAW_CACHE_DIR / site


def interim_path(name: str) -> Path:
    return INTERIM_DIR / name


def final_path(name: str) -> Path:
    return FINAL_DIR / name


PLAN_COLUMNS = [
    # --- 식별 ---
    "carrier_type",            # MNO / MVNO
    "host_mno",                # 실제 망 제공사
    "mvno_brand",
    "plan_id",
    # 택1 혜택을 선택지별 행으로 펼치기 전 원본 요금제의 id. 펼치지 않은 행은
    # plan_id와 같다. "실제 요금제 수"는 nunique(base_plan_id), "선택지까지
    # 포함한 조합 수"는 행 수로 센다.
    "base_plan_id",
    "plan_id_type",            # official_code / name_based
    "plan_name",
    "selected_option",         # 이 행이 택1에서 고른 선택지 이름. 택1 없으면 빈값
    "plan_category",           # 사이트 내 분류/탭명
    "is_online_only",
    "age_condition",
    # 가입 제한 원문(예: "번호이동만 가입 가능"). 모요만 채운다 - 3사 사이트에는
    # 이런 배너가 없다(제한이 없어서가 아니라 텍스트로 안 적혀 있어서다).
    "signup_notice",
    # --- 스펙 ---
    # 데이터/테더링은 base(나이 조건과 무관한 기본량) + extra(나이 조건 추가분)
    # = 총량 3단으로 나눠 담는다. extra가 붙는 건 KT의 Y덤/스쿨덤/65+덤/75+덤뿐.
    "network_gen",             # 5G / LTE / 3G
    "base_data_gb",            # 무제한이면 빈값
    "extra_data_gb",
    "data_gb",                 # 총 제공량, 무제한이면 빈값
    "data_unlimited",
    "data_throttle_speed",     # 소진 후 제어 속도 (예: 5Mbps)
    "daily_data_gb",
    "base_tethering_gb",
    "extra_tethering_gb",
    "tethering_gb",
    # tethering_gb가 비는 이유가 셋인데 의미가 정반대라 따로 남긴다.
    # quota=별도 한도 있음 / within_data=한도 없이 기본 데이터에서 차감 /
    # unsupported=테더링 못 씀 / undisclosed=사이트가 값을 안 알려줌.
    # 모요만 넷을 구분하고, 3사는 값 유무로 quota/undisclosed만 채운다.
    "tethering_support",
    "voice_unlimited",
    "voice_minutes",
    "voice_extra_minutes",     # 영상/부가통화(분)
    "sms_unlimited",
    "sms_count",
    # --- 가격 ---
    "monthly_fee",             # 정가 월정액(원)
    "discounted_fee",          # 대표 할인가(원)
    "discount_type",
    "discount_period_months",
    # --- 혜택 요약 (상세는 benefits.csv) ---
    "benefit_count",
    "ott_option_count",
    "ott_options",             # ' | ' 구분
    "membership_grade",
    "smart_device_benefit",
    "extra_data_benefit",
    "gift_benefit",
    # --- 참고 ---
    # 목록 카드의 "N명이 선택". 모요만 채운다. "10+명이 선택"(하한 표기)도 정수로
    # 그대로 저장하므로 값이 실제보다 낮게 잡힐 수 있다.
    "subscriber_count",
    # --- 출처 ---
    "source_url",
    "crawled_at",
]


def total_data_gb(row: dict):
    """`data_gb`에 넣을 월 환산 총 제공량 = 사이트 월 총량 + daily_data_gb * 30.

    사이트 표기가 "월 11GB + 매일 2GB"처럼 단위가 섞여 있어서, 월 총량만 담으면
    일 단위 제공분이 통째로 빠진다(11GB로만 보인다). 원래 값은 interim CSV와
    `daily_data_gb`에 남으므로 `data_gb - daily_data_gb * 30`으로 되돌릴 수 있다.
    무제한과 데이터 미제공은 빈값.

    ponytail: 한 달을 30일 고정으로 본다. 일 단위 제공량은 이월이 안 되므로
    실사용 상한은 이 값보다 낮다 - 일 단위 요금제가 과대추천되면 계수를 붙인다.
    """
    if str(row.get("data_unlimited", "")) == "True":
        return ""

    def _num(value):
        text = str(value if value is not None else "").strip()
        return float(text) if text else None

    monthly, daily = _num(row.get("data_gb")), _num(row.get("daily_data_gb"))
    if monthly is None and daily is None:
        return ""
    return round((monthly or 0) + (daily or 0) * 30, 3)


# 4개 사이트 공통 분류 체계. benefit_category에 이 중 하나가 들어간다.
BENEFIT_CATEGORIES = [
    "OTT/구독",
    "멤버십",
    "스마트기기",
    "추가데이터",
    "사은품/페이백",
    "기타",
]

BENEFIT_COLUMNS = [
    "plan_id",             # plans.csv와 조인하는 키
    "host_mno",
    "plan_name",
    "benefit_category",
    "benefit_name",        # 사이트에 적힌 혜택명 그대로
    "benefit_service",     # 정규화한 서비스명 (예: "넷플릭스"). 못 찾으면 빈값
    "benefit_tier",        # 구독 등급. 없으면 빈값
    "benefit_value_won",   # 혜택 정가/시장가(원) - 모르면 빈값
    "user_pay_won",        # 사용자 실부담금(원). 0이면 완전 무료
    "is_selectable",       # true면 같은 select_group 안에서 택1
    "select_group",
    # 이 혜택을 받으려면 충족해야 하는 **배타적 전제조건**. 같은 값을 가진 혜택끼리는
    # 함께 받을 수 있고, 값이 다르면 동시에 못 받는다(유심을 쿠팡에서 사면서 동시에
    # KT 바로유심으로 살 수는 없다). select_group이 "그룹 안에서 택1"인 것과 달리
    # 이 열은 "같은 값끼리 합산, 다른 값끼리 택1"이라 따로 둔다.
    "benefit_condition",
    "benefit_detail",      # 원문 설명
    "source_url",
]

# 같은 서비스를 사이트마다 다르게 적는다(넷플릭스 / Netflix / T 우주 Netflix …).
# 위에서부터 먼저 매칭되는 걸 쓰므로 순서가 중요하다(티빙&웨이브 -> 티빙).
SERVICE_ALIASES = [
    ("넷플릭스", ("넷플릭스", "netflix")),
    ("유튜브 프리미엄", ("유튜브", "youtube")),
    ("디즈니+", ("디즈니",)),
    ("티빙", ("티빙",)),
    ("웨이브", ("웨이브", "wavve")),
    ("왓챠", ("왓챠", "watcha")),
    ("데일리플러스", ("데일리",)),
    ("밀리의서재", ("밀리",)),
    ("지니뮤직", ("지니",)),
    ("FLO", ("flo",)),
    ("구글 원", ("구글 원", "구글원", "google one")),
    ("Google AI", ("google ai", "ai 구독")),
    ("T 우주", ("t 우주", "우주패스")),
    ("위버스", ("위버스",)),
    ("가전구독", ("가전구독",)),
    ("폰케어", ("폰케어",)),
    ("삼성 디바이스", ("삼성",)),
    ("애플 디바이스", ("애플",)),
]


def normalize_service(benefit_name: str) -> str:
    """혜택명 -> 대표 서비스명. 매칭 안 되면 빈 문자열."""
    text = (benefit_name or "").lower()
    for canonical, keywords in SERVICE_ALIASES:
        if any(k in text for k in keywords):
            return canonical
    return ""


# 같은 서비스라도 등급에 따라 시장가가 3배까지 차이난다(넷플릭스 광고형 5,500원 /
# 스탠다드 13,500원 / 프리미엄 17,000원). 긴 것부터 찾아야 "광고형 스탠다드"가
# "스탠다드"로 잘리지 않는다.
BENEFIT_TIERS = ("광고형 스탠다드", "광고형", "프리미엄", "스탠다드", "베이직", "Lite")

# "유튜브 프리미엄"/"YouTube Premium"의 '프리미엄'은 등급이 아니라 상품명 자체다.
# 이걸 등급으로 뽑으면 "넷플릭스 프리미엄"(진짜 상위 등급)과 같은 층위로 묶여버린다.
_PRODUCT_NAME_PREMIUM_RE = re.compile(r"유튜브\s*프리미엄|YouTube\s*Premium", re.IGNORECASE)


def extract_tier(benefit_name: str, benefit_service: str = "") -> str:
    """혜택명에서 구독 등급만 뽑는다. 등급 표기가 없으면 빈 문자열."""
    text = benefit_name or ""
    service = benefit_service or normalize_service(text)
    if service == "유튜브 프리미엄":
        # 상품명에 박힌 "프리미엄"을 지우고 남은 데서만 등급을 찾는다
        # (그래야 "YouTube Premium Lite"의 Lite는 살아남는다).
        text = _PRODUCT_NAME_PREMIUM_RE.sub(" ", text)
    lowered = text.lower()
    for tier in BENEFIT_TIERS:
        if tier.lower() in lowered:
            return tier
    return ""


# 가입 연령 조건은 표기가 제각각이라 원문 그대로 두면 16종으로 갈라진다
# (KT "청년(Y덤)" / SKT "청년(만 34세 이하)" / LGU+ "만 19세 ~ 35세 미만").
# **상한 나이 기준**으로 통일한다 - 추천에서 실제로 걸리는 건 상한이다.
# 연령이 아닌 자격(외국인, 복지카드)은 그대로 둔다. "만"은 사이트마다 붙기도
# 빠지기도 해서("65세 이상") 필수로 보지 않는다.
_AGE_UPPER_RE = re.compile(r"(\d+)\s*세\s*(이하|미만)")
_AGE_LOWER_RE = re.compile(r"(\d+)\s*세\s*이상")


def normalize_age_condition(text: str) -> str:
    """가입 연령 조건을 "만 N세 이하/이상"으로 통일. 연령이 아니면 원문 유지."""
    text = (text or "").strip()
    if not text:
        return ""

    # "만 4세 ~ 13세 미만"처럼 구간으로 적힌 것은 **상한만** 남긴다.
    # 상한은 마지막에 나오는 "N세 이하/미만"이다(앞쪽 "만 4세"는 하한).
    uppers = _AGE_UPPER_RE.findall(text)
    if uppers:
        num, bound = uppers[-1]
        # "13세 미만" = "12세 이하"로 맞춘다
        age = int(num) - (1 if bound == "미만" else 0)
        prefix = "외국인, " if "외국인" in text else ""
        return f"{prefix}만 {age}세 이하"
    lower = _AGE_LOWER_RE.search(text)
    if lower:
        return f"만 {lower.group(1)}세 이상"
    return text


# 멤버십 등급도 표기가 6종으로 갈라져서("T 멤버십 VIP 혜택" / "24개월간 VIP 등급")
# 등급만 뽑아 통일한다. 원문은 benefit_name에 그대로 남는다.
# VVIP를 먼저 봐야 한다 - "VVIP"에도 "VIP"가 들어있다.
MEMBERSHIP_GRADES = ("VVIP", "VIP")


def normalize_membership_grade(text: str) -> str:
    """'24개월간 VIP 등급' -> 'VIP'. 등급 표기가 없으면 원문을 그대로 돌려준다."""
    upper = (text or "").upper()
    for grade in MEMBERSHIP_GRADES:
        if grade in upper:
            return grade
    return (text or "").strip()


def classify_benefit_name(name: str, default: str = "기타") -> str:
    """혜택 **이름 하나**를 보고 카테고리를 정한다. 못 정하면 default.

    표 헤더로 그룹 전체를 한 카테고리로 묶으면 틀린다. KT "초이스(택1)"에는
    넷플릭스(구독)·폰케어(보험)·삼성 디바이스(기기)가 섞여 있다. 그래서 그룹
    카테고리는 default로만 쓰고, 이름에 단서가 있으면 그걸 우선한다.
    """
    text = name or ""
    # OTT/구독을 사은품보다 **먼저** 본다. "구글 AI프로+도미노피자 할인쿠폰"처럼
    # 구독에 쿠폰이 딸려오는 이름이 있어서, 쿠폰을 먼저 보면 구독 혜택이 통째로
    # 사은품으로 넘어간다(모요 44행). 3사 전수 확인: 쿠폰 + OTT 키워드가 같이
    # 걸리는 혜택명 0건이라 반대 방향 오분류는 없다.
    if any(k in text for k in OTT_KEYWORDS):
        return "OTT/구독"
    if any(k in text for k in ("보험", "폰케어")):
        return "기타"
    if any(k in text for k in ("디바이스", "워치", "태블릿", "액션캠", "스마트기기")):
        return "스마트기기"
    if "멤버십" in text:
        return "멤버십"
    # 사은품 판정도 추가데이터보다 뒤다. "데이터쿠폰 20GB"(모요 23행)처럼 데이터를
    # 더 주는 혜택에 '쿠폰'이 붙는 이름이 있다.
    if EXTRA_DATA_RE.search(text):
        return "추가데이터"
    if any(k in text for k in ("쿠폰", "상품권", "페이백", "캐시백", "사은품", "포인트")):
        return "사은품/페이백"
    return default


# 결합·추가·공유로 데이터를 더 주는 혜택. (데이터|결합) 뒤에 (추가|공유|쉐어|결합|용량)이
# 오는 형태로 잡는다. 용량(\d+G)까지 단서로 쓰는 건 '데이터'라는 말이 없는
# "솔로결합(+20GB)" 때문이고, 앞에 '결합/데이터'를 요구하므로 "네이버페이
# 5,000원" 같은 사은품은 걸리지 않는다.
EXTRA_DATA_RE = re.compile(
    r"(?:데이터|결합).*?(?:추가|공유|쉐어|결합|\d+\s*G)"
    r"|(?:추가|공유|쉐어).*?데이터"
)


def canonical_spelling(text: str) -> str:
    """문장 안의 서비스 표기를 대표 표기로 통일한 **비교용** 문자열을 만든다.

      "Netflix 광고형 스탠다드"  ->  "넷플릭스 광고형 스탠다드"

    사람에게 보여줄 값이 아니라 중복 판정용이다.
    """
    out = text or ""
    for canonical, keywords in SERVICE_ALIASES:
        for keyword in keywords:
            out = re.sub(re.escape(keyword), canonical, out, flags=re.IGNORECASE)
    return out


# 혜택명에 이 키워드가 있으면 OTT/구독. SKT/LGU+가 각자 목록을 두다가 조금씩
# 달라져서 하나로 합쳤다. KT는 혜택이 표 헤더 단위로 와서 대신 헤더명 규칙
# (crawl_kt.py의 BENEFIT_COLUMN_RULES)을 쓴다.
OTT_KEYWORDS = (
    "넷플릭스", "Netflix", "유튜브", "YouTube", "디즈니", "Disney", "티빙", "웨이브",
    "Wavve", "밀리", "지니", "FLO", "우주", "Google AI", "구글", "AI", "OTT", "구독",
    "데일리",
)


# "혜택" 칸에 적혀 있지만 실제로는 "별도로 더 주는 건 없다"는 뜻인 문구들
# ("기본 제공 데이터 내 사용", "테더링+쉐어링 기본 제공량 내"). 혜택으로 잡으면
# 공유데이터를 실제로 별도 제공하는 요금제와 구분이 안 된다. 뒤에 용량이 붙는
# LGU+ 변형("기본 제공량 내 60GB")도 한도 표기일 뿐이고 그 한도는 이미
# tethering_gb에 들어간다.
NON_BENEFIT_PATTERN = re.compile(
    r"기본\s*제공\s*데이터\s*내"
    r"|기본\s*제공\s*량?\s*내"
    r"|기본\s*데이터\s*내"
)


def is_non_benefit(text: str) -> bool:
    """'별도 제공 없음'을 뜻하는 문구면 True."""
    return bool(NON_BENEFIT_PATTERN.search(text or ""))


# 혜택명에 딸려 들어온 링크/버튼 글자.
# ("65세 이상 안심박스 자세히보기" -> "65세 이상 안심박스")
UI_LABEL_RE = re.compile(r"\s*(?:자세히\s*보기|자세히보기|바로\s*가기|바로가기|신청하기|더\s*보기|더보기)\s*$")


def strip_ui_label(text: str) -> str:
    cleaned = UI_LABEL_RE.sub("", text or "").strip()
    return cleaned or (text or "").strip()  # 라벨만 있던 값이면 원문 유지


def to_gb(text: str):
    """'100GB' -> 100.0, '600MB' -> 0.586(GB로 환산), '무제한'/빈값 -> None."""
    text = (text or "").strip()
    if not text or "무제한" in text:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*GB", text)
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*MB", text)
    if m:
        return round(float(m.group(1)) / 1024, 3)
    return None


def to_won(text: str):
    """'100,000원' -> 100000. `[\\d,]+`로만 찾으면 "네이버페이, 3대..."의 콤마까지
    잡혀 int()가 터지므로 숫자로 시작하는 덩어리만 매칭한다."""
    m = re.search(r"\d[\d,]*", text or "")
    return int(m.group(0).replace(",", "")) if m else None


def extract_speed(text: str) -> str:
    """'다 쓰면 최대 5Mbps' -> '5Mbps'. 없으면 빈 문자열.

    사이트마다 대소문자가 제각각이라(400Kbps / 400kbps) 표기를 맞춰서 내보낸다.
    """
    m = re.search(r"(\d+(?:\.\d+)?)\s*(kbps|mbps|gbps)", text or "", re.I)
    if not m:
        return ""
    unit = m.group(2).lower()
    return f"{m.group(1)}{unit[0].upper()}{unit[1:]}"


def agreement_discount(fee):
    """선택약정 25% 할인가. 사이트가 할인가를 안 알려줄 때 쓰는 3사 공통 표준 요율."""
    if fee is None:
        return "", ""
    return round(fee * 0.75), "선택약정 25% 할인"


def make_benefit_row(
    plan_id, host_mno, plan_name, benefit_category, benefit_name, *,
    value_won="", pay_won="", selectable=False, select_group="", condition="",
    detail="", source_url="",
):
    """BENEFIT_COLUMNS 순서에 맞는 혜택 행 하나."""
    return {
        "plan_id": plan_id,
        "host_mno": host_mno,
        "plan_name": plan_name,
        "benefit_category": benefit_category,
        # 혜택명에서만 UI 라벨을 뗀다. benefit_detail은 원문 그대로 남긴다.
        "benefit_name": strip_ui_label(benefit_name),
        "benefit_value_won": value_won,
        "user_pay_won": pay_won,
        "is_selectable": selectable,
        "select_group": select_group,
        "benefit_condition": condition,
        "benefit_detail": detail,
        "source_url": source_url,
    }


def _clean(value):
    """HTML에서 가져온 값의 줄바꿈·연속공백을 한 줄로 정리한다."""
    if isinstance(value, str):
        # 제로폭 문자(U+200B 등)는 \s에 안 걸려서 눈에 안 보이는 채로 값 끝에
        # 남는다(LGU+ 공유데이터 문구). 남으면 문자열 비교/그룹핑이 조용히 어긋난다.
        value = re.sub(r"[​-‍﻿]", "", value)
        return re.sub(r"\s+", " ", value).strip()
    return value


def _write(rows, path, columns):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        for row in rows:
            w.writerow({c: _clean(row.get(c, "")) for c in columns})
    print(f"{Path(path).name}: {len(rows)}행 저장")


def write_plans(rows, path):
    # base_*는 나이 조건별 추가 제공(KT Y덤 등)이 있는 크롤러만 직접 채운다.
    # 그런 개념이 없는 곳은 추가분 없음 = 기본이 곧 총량이라 여기서 채워준다.
    for row in rows:
        if row.get("base_data_gb", "") == "":
            row["base_data_gb"] = row.get("data_gb", "")
        if row.get("base_tethering_gb", "") == "":
            row["base_tethering_gb"] = row.get("tethering_gb", "")
        # 3사는 "지원/미지원" 섹션이 없어서 값 유무로 quota/undisclosed까지만
        # 말할 수 있다. 없는 걸 unsupported라고 단정하면 안 된다 - 미공시일 뿐이다.
        if not row.get("tethering_support"):
            # `!= ""` 로만 보면 파서가 못 찾아 넣은 None이 quota로 찍힌다(MNO 151건).
            has_quota = row.get("tethering_gb") not in ("", None)
            row["tethering_support"] = "quota" if has_quota else "undisclosed"
        # 펼치지 않은 행도 base_plan_id를 채워야 요금제 수를 nunique 하나로 센다.
        if not row.get("base_plan_id"):
            row["base_plan_id"] = row.get("plan_id", "")
    _write(rows, path, PLAN_COLUMNS)


def write_benefits(rows, path):
    for row in rows:
        if not row.get("benefit_service"):
            row["benefit_service"] = normalize_service(row.get("benefit_name", ""))
        if not row.get("benefit_tier"):
            row["benefit_tier"] = extract_tier(row.get("benefit_name", ""), row["benefit_service"])
    _write(rows, path, BENEFIT_COLUMNS)


def expand_select_variants(plan: dict, benefits: list[dict]) -> list[tuple[dict, list[dict]]]:
    """"택1" 혜택을 선택지 하나당 요금제 행 하나로 펼친다.

    SKT는 이미 선택지별로 별도 상품(prodId)을 만들어 놨는데(베스트 Max(넷플릭스)
    NA00009815 …) KT 초이스·LGU+ 프리미엄플러스는 한 요금제 안에 목록으로만
    들어있다. 그대로 두면 "SKT는 요금제마다 OTT 1개, KT는 9개"처럼 보여서 파싱
    방식 때문에 생긴 왜곡이 모델에 들어간다.

    택1 그룹이 여러 개면 **가장 선택지가 많은 그룹 하나만** 펼친다. 전 조합
    (9x3=27)으로 펼치면 행이 급격히 불어나는데, SKT가 쓰는 입도는 "주 OTT 하나"다.

    KT의 "플러스" 그룹은 **절대 primary가 되지 않는다** - 부가 선택지인데,
    경쟁하는 큰 그룹이 없는 페이지에서 "하나뿐이니 가장 크다"는 논리로 primary가
    돼 plan_id가 불필요하게 쪼개졌었다(KT 상품 4개 x 플러스 4개 = 16행).
    LGU+의 "프리미엄플러스"는 "플러스"로 시작하지 않으므로 영향 없다.

    펼친 행의 혜택에는 **고른 선택지 하나 + 택1이 아닌 나머지**만 남는다. 그래서
    혜택 금액을 붙일 때 그냥 sum 하면 되고 "택1은 max로" 같은 예외가 없어진다.
    """
    groups = {}
    for b in benefits:
        if b.get("is_selectable") and b.get("select_group"):
            groups.setdefault(b["select_group"], []).append(b)
    groups = {g: items for g, items in groups.items() if len(items) >= 2}
    expandable = {g: items for g, items in groups.items() if not g.startswith("플러스")}
    if not expandable:
        return [(plan, benefits)]

    primary = max(expandable, key=lambda g: len(expandable[g]))
    chosen_ids = {id(b) for b in groups[primary]}
    others = [b for b in benefits if id(b) not in chosen_ids]

    out = []
    for opt in groups[primary]:
        option_name = opt.get("benefit_name", "")
        option_slug = re.sub(r"\s+", "", option_name)
        new_id = f"{plan['plan_id']}_{option_slug}"
        # SKT가 쓰는 "베스트 Max(넷플릭스)" 표기를 따라간다.
        new_name = f"{plan.get('plan_name', '')} ({option_name})"
        new_benefits = [
            dict(b, plan_id=new_id, plan_name=new_name) for b in ([opt] + others)
        ]
        new_plan = dict(
            plan,
            plan_id=new_id,
            # 연령 변형 행을 다시 펼치는 경우 기존 base_plan_id를 유지한다.
            # 덮어쓰면 진짜 원본("초이스130")까지 한 번에 되짚을 수 없다.
            base_plan_id=plan.get("base_plan_id") or plan["plan_id"],
            selected_option=option_name,
            plan_name=new_name,
        )
        new_plan.update(summarize_benefits(new_benefits))
        out.append((new_plan, new_benefits))
    return out


def summarize_benefits(benefit_rows: list[dict]) -> dict:
    """혜택 long rows -> plans.csv에 넣을 요약 컬럼들."""
    ott = [b for b in benefit_rows if b["benefit_category"] == "OTT/구독"]
    membership = [b for b in benefit_rows if b["benefit_category"] == "멤버십"]
    smart = [b for b in benefit_rows if b["benefit_category"] == "스마트기기"]
    data = [b for b in benefit_rows if b["benefit_category"] == "추가데이터"]
    gift = [b for b in benefit_rows if b["benefit_category"] == "사은품/페이백"]

    def names(rows):
        return " | ".join(dict.fromkeys(r["benefit_name"] for r in rows if r.get("benefit_name")))

    # write_benefits가 benefit_service를 채우는 건 이 함수가 끝난 뒤라, 여기서는
    # 항상 이름에서 새로 뽑아야 한다.
    ott_services = dict.fromkeys(
        filter(None, (normalize_service(r.get("benefit_name", "")) for r in ott))
    )
    grades = dict.fromkeys(
        filter(None, (normalize_membership_grade(r.get("benefit_name", "")) for r in membership))
    )

    return {
        "benefit_count": len(benefit_rows),
        "ott_option_count": len(ott),
        "ott_options": " | ".join(ott_services) or names(ott),
        "membership_grade": " | ".join(grades),
        "smart_device_benefit": names(smart),
        "extra_data_benefit": names(data),
        "gift_benefit": names(gift),
    }


if __name__ == "__main__":
    # 순서가 결과를 바꾸는 규칙이라(OTT vs 사은품, 사은품 vs 추가데이터) 실제로
    # 걸렸던 이름들을 그대로 박아 둔다.
    CASES = [
        # 구독 이름이 들어간 사은품 링크 - 사은품보다 OTT가 먼저다
        ("구글 AI프로+도미노피자 할인쿠폰", "사은품/페이백", "OTT/구독"),
        ("밀리의 서재 평생 구독 0원", "사은품/페이백", "OTT/구독"),
        ("티빙 광고형 스탠다드 제공 (12개월)", "사은품/페이백", "OTT/구독"),
        # 결합/추가 데이터 - '데이터'라는 말이 없는 표기까지
        ("솔로결합(+20GB)", "사은품/페이백", "추가데이터"),
        ("SOLO결합 데이터 10GB 지급", "사은품/페이백", "추가데이터"),
        ("추가데이터 10GB 제공", "사은품/페이백", "추가데이터"),
        ("헬로모바일 결합시, 추가 데이터 10GB 증정", "사은품/페이백", "추가데이터"),
        ("데이터쿠폰 20GB", "사은품/페이백", "추가데이터"),  # '쿠폰'보다 데이터가 먼저
        # 진짜 사은품은 그대로 남아야 한다
        ("네이버페이 5,000원", "사은품/페이백", "사은품/페이백"),
        ("일반유심/배송비 무료", "사은품/페이백", "사은품/페이백"),
        ("쇼핑라운지 할인쿠폰 5천원권", "기타", "사은품/페이백"),
        ("에어팟4", "사은품/페이백", "사은품/페이백"),
        ("U+ 멤버십 VIP콕(24개월 간 매월 제공)", "사은품/페이백", "멤버십"),
        ("스마트기기 이용 요금 50% 할인 (1회선)", "사은품/페이백", "스마트기기"),
        ("폰케어 서비스", "기타", "기타"),
    ]
    for name, default, expected in CASES:
        got = classify_benefit_name(name, default)
        assert got == expected, f"{name!r} -> {got} (기대: {expected})"
    print(f"classify_benefit_name 점검 {len(CASES)}건 통과")

    # 크롤러는 실수/None으로, merge는 CSV에서 읽은 문자열로 같은 함수를 부른다.
    GB_CASES = [
        ({"data_gb": 11.0, "daily_data_gb": 2.0}, 71.0),
        ({"data_gb": "11.0", "daily_data_gb": "2.0"}, 71.0),  # merge가 넘기는 문자열
        ({"data_gb": "", "daily_data_gb": 5.0}, 150.0),       # 일 단위만 있는 모요 카드
        ({"data_gb": 100.0, "daily_data_gb": ""}, 100.0),
        ({"data_gb": 0, "daily_data_gb": ""}, 0.0),           # 0GB는 "값 없음"이 아니다
        ({"data_gb": "", "daily_data_gb": "", "data_unlimited": "True"}, ""),
        ({"data_gb": None, "daily_data_gb": None}, ""),       # 데이터 미제공
    ]
    for row, expected in GB_CASES:
        got = total_data_gb(row)
        assert got == expected, f"{row} -> {got} (기대: {expected})"
    print(f"total_data_gb 점검 {len(GB_CASES)}건 통과")
