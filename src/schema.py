"""
통신 3사(KT/SKT/LGU+) + 모요(MVNO) 요금제 통합 스키마.

## 왜 테이블을 2개로 나눴나

혜택(특히 OTT)이 이 데이터의 핵심인데, 요금제 하나가 "넷플릭스/유튜브/디즈니+/티빙…
중 택1" 처럼 선택지를 최대 14개까지 갖는다. 이걸 한 행에 파이프로 밀어넣으면
"넷플릭스 주는 요금제 찾기" 같은 기본적인 조회조차 문자열 검색으로 해야 해서,
혜택을 별도 long-format 테이블로 분리했다.

- plans.csv   : 요금제 1개 = 1행 (혜택은 요약 컬럼으로만)
- benefits.csv: 요금제 1개 × 혜택 1개 = 1행 (조인키: plan_id)

두 파일 모두 4개 사이트가 같은 컬럼을 쓴다.

## plan_id 주의사항
- KT   : ItemCode_요금제명[_연령] (예: 1693_베이직100, 1681_초이스90_청년(Y덤))
- SKT  : NA로 시작하는 상품코드 (예: NA00009818)
- LGU+ : Z/LPZ로 시작하는 상품코드 (예: Z202605251, LPZ1004907)
- 모요 : URL의 숫자 ID (예: 30954)
사이트에 고유 코드가 없어 요금제명을 대신 쓴 경우 plan_id_type=name_based로 표시한다
(현재 LGU+ 너겟 일부만 해당).

## carrier_type 주의사항
너겟(LGU+)/다이렉트(SKT)/요고(KT)는 "온라인 전용 채널"일 뿐 통신사가 직접 파는
자사 요금제라 MNO다. MVNO(알뜰폰)는 KT/SKT/LGU+ 망을 빌려쓰는 별도 사업자
(핀다이렉트, 쉐이크모바일 등)로, 현재 데이터에선 모요에서 수집한 요금제가 여기 해당.
온라인 전용 여부는 is_online_only 컬럼으로 따로 표시한다.
"""
import csv
import re
from pathlib import Path

# ---------------- 경로 ----------------
# 프로젝트 구조
#   src/    크롤러 + 이 파일
#   data/raw_cache/<사이트>/   수집한 원본 HTML·JSON
#   data/interim/              사이트별 중간 CSV
#   data/final/                합친 최종 CSV  <- 분석/모델링은 여기만 보면 된다
#   docs/                      컬럼 명세서, 수정 이력
#
# 경로는 이 파일 위치를 기준으로 잡는다(상대경로를 쓰면 프로젝트 루트에서
# 실행할 때만 동작한다). 경로를 여기 한 곳에 모아 두면 폴더 구조를 바꿀 때
# 이 상수들만 손대면 되고 크롤러는 건드릴 필요가 없다.
BASE_DIR = Path(__file__).resolve().parent.parent   # src/의 부모 = 프로젝트 루트
DATA_DIR = BASE_DIR / "data"
RAW_CACHE_DIR = DATA_DIR / "raw_cache"   # 사이트 원본 (HTML/JSON)
INTERIM_DIR = DATA_DIR / "interim"       # 사이트별 중간 CSV
FINAL_DIR = DATA_DIR / "final"           # 합친 최종 CSV


def cache_dir(site: str) -> Path:
    """해당 사이트의 원본 캐시 디렉터리. (예: cache_dir("kt") -> data/raw_cache/kt)"""
    return RAW_CACHE_DIR / site


def interim_path(name: str) -> Path:
    """사이트별 중간 CSV 경로. (merge_plans.py의 입력)"""
    return INTERIM_DIR / name


def final_path(name: str) -> Path:
    """합친 최종 CSV 경로."""
    return FINAL_DIR / name


PLAN_COLUMNS = [
    # --- 식별 ---
    "carrier_type",            # MNO / MVNO
    "host_mno",                 # KT / SKT / LGU+ (실제 망 제공사)
    "mvno_brand",                # 알뜰폰 브랜드명 (MNO는 빈값)
    "plan_id",
    # 택1(선택형) 혜택을 선택지별 행으로 펼쳤을 때, 펼치기 전 원본 요금제의 id.
    # 펼치지 않은 행은 plan_id와 같은 값이 들어간다(write_plans가 자동으로 채움).
    # "실제 요금제가 몇 개냐"는 nunique(base_plan_id)로 세면 되고,
    # "선택지까지 포함해 고를 수 있는 조합이 몇 개냐"는 행 수로 센다.
    "base_plan_id",
    "plan_id_type",               # official_code / name_based
    "plan_name",
    # 이 행이 택1에서 고른 선택지 이름 (예: "넷플릭스"). 택1이 없는 요금제는 빈값.
    "selected_option",
    "plan_category",                # 사이트 내 분류/탭명
    "is_online_only",                # 온라인 전용 채널 요금제 (너겟/다이렉트/요고/모요)
    "age_condition",                  # 가입 자격/나이 조건 (일반이면 빈값)
    # 가입 제한/유의사항 원문 (예: "신규 가입을 받지 않는 통신사입니다. 번호이동만
    # 가입 가능"). 지금은 모요만 채운다(상세페이지 "꼭 확인해 주세요" 배너,
    # 27%가량에 있음) - KT/SKT/LGU+ 자사 요금제는 이런 배너가 없다(제한이 없어서가
    # 아니라 사이트에 텍스트로 안 적혀 있어서다. docs/컬럼_명세서.md 참고).
    "signup_notice",
    # --- 스펙 ---
    # 데이터/테더링은 기본제공/추가제공/총제공 3단으로 나눠서 다 보여준다.
    # age_condition이 없는 일반 행은 base == data_gb(추가분 없음)이고,
    # age_condition이 있는 행(KT의 Y덤/스쿨덤/65+덤/75+덤)은 base(나이 조건 없이
    # 받는 기본량) + extra(나이 조건으로 더 받는 양) = data_gb(총 제공량) 순으로 채운다.
    "network_gen",                     # 5G / LTE / 3G
    "base_data_gb",                     # 기본 제공량(나이 조건과 무관한 원래 데이터량), 무제한이면 빈값
    "extra_data_gb",                      # 나이 조건으로 추가되는 데이터량(없으면 빈값)
    "data_gb",                              # 총 제공량 = base_data_gb + extra_data_gb, 무제한이면 빈값
    "data_unlimited",
    "data_throttle_speed",                    # 소진 후 제어 속도 (예: 5Mbps)
    "daily_data_gb",
    "base_tethering_gb",                        # 기본 테더링/공유데이터 제공량
    "extra_tethering_gb",                         # 나이 조건으로 추가되는 테더링량(없으면 빈값)
    "tethering_gb",                                 # 총 테더링 제공량 = base_tethering_gb + extra_tethering_gb
    # tethering_gb가 비는 이유가 셋(미지원/데이터 제공량 내/미공개)인데 의미가
    # 정반대라 따로 남긴다. quota=별도 한도 있음(tethering_gb에 값) /
    # within_data=별도 한도 없이 기본 데이터에서 차감(테더링 자체는 됨) /
    # unsupported=테더링 못 씀 / undisclosed=사이트가 값을 안 알려줌.
    # 지금은 모요만 넷을 구분하고, 3사는 값 유무로 quota/undisclosed만 채운다.
    "tethering_support",
    "voice_unlimited",
    "voice_minutes",
    "voice_extra_minutes",                  # 영상/부가통화(분)
    "sms_unlimited",
    "sms_count",
    # --- 가격 ---
    "monthly_fee",                           # 정가 월정액(원)
    "discounted_fee",                          # 대표 할인가(원)
    "discount_type",
    "discount_period_months",
    # --- 혜택 요약 (상세는 benefits.csv) ---
    "benefit_count",                             # 이 요금제에 달린 혜택 개수
    "ott_option_count",                            # 선택 가능한 OTT/구독 개수
    "ott_options",                                  # OTT/구독 목록 (' | ' 구분)
    "membership_grade",                               # 멤버십 등급 (VIP/VVIP 등)
    "smart_device_benefit",                             # 스마트기기 회선 혜택
    "extra_data_benefit",                                # 추가/공유 데이터 혜택
    "gift_benefit",                                       # 사은품/페이백
    # --- 참고 ---
    # 목록 카드의 "N명이 선택" 인기 지표. 지금은 모요만 채운다(3사 사이트에는
    # 이런 표시 자체가 없음). "10+명이 선택"(하한 표기)도 정수로 그대로
    # 저장한다 - "최소 10명"이라는 뜻이라 값이 실제보다 낮게 잡힐 수 있다.
    "subscriber_count",
    # --- 출처 ---
    "source_url",
    "crawled_at",
]

# 혜택 분류 체계 (4개 사이트 공통)
BENEFIT_CATEGORIES = [
    "OTT/구독",        # 넷플릭스, 유튜브 프리미엄, 디즈니+, 티빙, 밀리, AI구독 등
    "멤버십",           # VIP/VVIP 등급 혜택
    "스마트기기",         # 워치/태블릿 회선 무료·할인
    "추가데이터",          # 연령별 덤, 공유데이터, 결합 추가데이터
    "사은품/페이백",         # 상품권, 네이버페이, 캐시백
    "기타",
]

BENEFIT_COLUMNS = [
    "plan_id",             # plans.csv와 조인하는 키
    "host_mno",
    "plan_name",            # 사람이 보기 편하라고 중복 저장
    "benefit_category",      # 위 BENEFIT_CATEGORIES 중 하나
    "benefit_name",           # 사이트에 적힌 혜택명 그대로 (예: "넷플릭스 스탠다드 할인")
    "benefit_service",         # 위를 정규화한 서비스명 (예: "넷플릭스"). 못 찾으면 빈값
    "benefit_tier",             # 구독 등급 (광고형 스탠다드/스탠다드/프리미엄 등). 없으면 빈값
    "benefit_value_won",        # 혜택 정가/시장가(원) - 모르면 빈값
    "user_pay_won",              # 사용자 실부담금(원). 0이면 완전 무료
    "is_selectable",              # true면 같은 select_group 안에서 택1
    "select_group",                # 택1 그룹명 (예: 프리미엄플러스(택1))
    "benefit_detail",               # 원문 설명
    "source_url",
]

# 같은 서비스를 사이트마다 다르게 적는다:
#   넷플릭스 / Netflix 광고형 스탠다드 / T 우주 Netflix 스탠다드 ...
# "넷플릭스 주는 요금제"를 문자열 검색 없이 찾을 수 있도록 대표 이름으로 정규화한다.
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


# OTT 구독 등급. 같은 서비스라도 등급에 따라 시장가가 3배까지 차이나므로
# (넷플릭스 광고형 5,500원 / 스탠다드 13,500원 / 프리미엄 17,000원)
# 서비스명과 별도 컬럼으로 뽑는다. 긴 것부터 찾아야 "광고형 스탠다드"가
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


# 가입 연령 조건도 통신사마다 표기가 제각각이라 원문 그대로 두면 16종으로 갈라진다.
# 같은 "키즈"인데도 이렇게 다르다.
#   KT   : "만 12세 이하"            (이미지 alt)
#   SKT  : "만 12세 이하"            (ledger 태그)
#   LGU+ : "만 4세 ~ 13세 미만"       (탭 정의)
# "청년"도 KT "청년(Y덤)" / SKT "청년(만 34세 이하)" / LGU+ "만 19세 ~ 35세 미만"으로
# 갈린다. 이러면 "만 30세가 가입 가능한 요금제"를 고르는 것조차 안 된다.
#
# **상한 나이 기준**으로 통일한다("만 N세 이하" / "만 N세 이상"). 추천에서 실제로
# 걸리는 건 상한이기 때문이다(만 30세는 키즈 요금제에 가입할 수 없다). LGU+의
# 하한("만 4세 이상")은 버려지지만, 그 조건이 추천 결과를 가르는 경우는 없다.
#
# 연령이 아닌 자격(외국인, 복지카드)은 그대로 둔다.
# "만"이 앞에 붙는지는 사이트마다 다르고("65세 이상"처럼 빠지기도 한다), 구간
# 표기에서는 상한 쪽에만 "만"이 없다("만 4세 ~ 13세 미만"). 그래서 "만"을 필수로
# 보지 않는다.
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


# 멤버십 등급은 통신사마다 부르는 말이 달라서 원문 그대로 두면 6종으로 갈라진다.
#   KT   : "멤버십 VVIP" / "멤버십 VIP"
#   SKT  : "T 멤버십 VIP 혜택"
#   LGU+ : "VVIP 등급" / "VIP 등급" / "24개월간 VIP 등급"
# 이러면 "VIP 이상인 요금제"를 고르는 것조차 안 된다. 등급만 뽑아 통일한다.
# 원문(기간 조건 같은 부수 정보 포함)은 혜택 테이블의 benefit_name에 그대로 남는다.
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

    표 헤더나 API 영역명으로 그룹 전체를 한 카테고리로 묶으면 틀리는 경우가 많다.
    예를 들어 KT "플러스(택1)" 그룹에는 지니뮤직·밀리의서재(구독)와 함께
    "쇼핑라운지 할인쿠폰 5천원권"(사은품)이 들어있고, "초이스(택1)"에는
    넷플릭스(구독)·폰케어(보험)·삼성/디바이스(기기)가 섞여 있다.
    그래서 그룹 카테고리는 default로만 쓰고, 이름에 단서가 있으면 그걸 우선한다.
    """
    text = name or ""
    # OTT/구독을 사은품보다 **먼저** 본다. "구글 AI프로+도미노피자 할인쿠폰"처럼
    # 구독 상품에 쿠폰이 딸려오는 이름이 있어서, 쿠폰을 먼저 보면 구독 혜택이
    # 통째로 사은품으로 넘어간다(모요 44행). 거꾸로 "쇼핑라운지 할인쿠폰
    # 5천원권"처럼 OTT 이름이 없는 진짜 사은품은 이 순서에 영향받지 않는다
    # (3사 전수 확인: 쿠폰류 + OTT 키워드가 같이 걸리는 혜택명 0건).
    if any(k in text for k in OTT_KEYWORDS):
        return "OTT/구독"
    if any(k in text for k in ("보험", "폰케어")):
        return "기타"
    if any(k in text for k in ("디바이스", "워치", "태블릿", "액션캠", "스마트기기")):
        return "스마트기기"
    if "멤버십" in text:
        return "멤버십"
    # 사은품 판정도 추가데이터보다 뒤다. "데이터쿠폰 20GB"(모요 23행)처럼 데이터를
    # 더 주는 혜택에 '쿠폰'이 붙는 이름이 있다. 반대로 쿠폰류이면서 추가데이터
    # 형태인 3사 혜택명은 0건이라 순서를 바꿔도 3사는 영향이 없다.
    if EXTRA_DATA_RE.search(text):
        return "추가데이터"
    if any(k in text for k in ("쿠폰", "상품권", "페이백", "캐시백", "사은품", "포인트")):
        return "사은품/페이백"
    return default


# 결합·추가·공유로 데이터를 더 주는 혜택. 표기가 사이트마다 갈린다.
#   "추가데이터 10GB 제공" / "데이터 매월 10GB 추가 증정" / "우리결합시, 데이터 10GB 추가 증정"
#   "SOLO결합 데이터 10GB 지급" / "솔로결합(+20GB)"  <- 마지막 건 '데이터'라는 말 자체가 없다
# 그래서 (데이터|결합) 뒤에 (추가|공유|쉐어|결합|용량) 중 하나가 오는 형태로 잡는다.
# 용량(\d+G)까지 단서로 쓰는 건 "솔로결합(+20GB)" 때문이고, 앞에 '결합/데이터'를
# 요구하므로 "네이버페이 5,000원" 같은 사은품이 걸리지는 않는다.
EXTRA_DATA_RE = re.compile(
    r"(?:데이터|결합).*?(?:추가|공유|쉐어|결합|\d+\s*G)"
    r"|(?:추가|공유|쉐어).*?데이터"
)


def canonical_spelling(text: str) -> str:
    """문장 안의 서비스 표기를 대표 표기로 통일한 **비교용** 문자열을 만든다.

      "Netflix 광고형 스탠다드"  ->  "넷플릭스 광고형 스탠다드"

    같은 걸 가리키는데 표기만 다른 경우(Netflix / 넷플릭스)를 "같다"고 판정하려고
    쓴다. 사람에게 보여줄 값이 아니라 중복 판정용이다.
    """
    out = text or ""
    for canonical, keywords in SERVICE_ALIASES:
        for keyword in keywords:
            out = re.sub(re.escape(keyword), canonical, out, flags=re.IGNORECASE)
    return out


# SKT/LGU+ 둘 다 "혜택명에 이 키워드가 있으면 OTT/구독"으로 분류하는데, 각자
# 따로 목록을 만들면서 조금씩 달라졌었다(SKT만 있던 "AI"/"구독" 등). 하나로
# 합쳐서 어느 사이트든 같은 이름이면 같은 카테고리로 분류되게 한다.
# KT는 혜택이 "제공 혜택_초이스 (택1)" 같은 표 헤더 단위로 오는 구조가 달라서
# 이 키워드 목록 대신 헤더명 기반 규칙(crawl_kt.py의 BENEFIT_COLUMN_RULES)을 쓴다.
OTT_KEYWORDS = (
    "넷플릭스", "Netflix", "유튜브", "YouTube", "디즈니", "Disney", "티빙", "웨이브",
    "Wavve", "밀리", "지니", "FLO", "우주", "Google AI", "구글", "AI", "OTT", "구독",
    "데일리",
)


# ---------------- 공용 파싱 헬퍼 (4개 크롤러가 같은 텍스트 패턴을 다룬다) ----------------

# "혜택" 칸에 적혀 있지만 실제로는 "별도로 더 주는 건 없다"는 뜻인 문구들.
# 그대로 혜택으로 잡으면 "데이터를 추가로 준다"처럼 보여서, 공유데이터를 실제로
# 별도 제공하는 요금제와 구분이 안 된다.
#   KT   : "기본 제공 데이터 내 사용", "기본데이터 내 이용"
#   LGU+ : "테더링+쉐어링 기본 제공량 내", "… 기본제공량 내 50GB"
# 뒤에 용량이 붙는 LGU+ 변형(예: "기본 제공량 내 60GB")도 추가 제공이 아니라
# "기본 제공량 안에서 최대 60GB까지 테더링 가능"이라는 한도 표기다. 그 한도는
# 이미 tethering_gb 컬럼에 들어가므로 혜택 행으로 또 만들면 중복이 된다.
NON_BENEFIT_PATTERN = re.compile(
    r"기본\s*제공\s*데이터\s*내"
    r"|기본\s*제공\s*량?\s*내"
    r"|기본\s*데이터\s*내"
)


def is_non_benefit(text: str) -> bool:
    """'별도 제공 없음'을 뜻하는 문구면 True."""
    return bool(NON_BENEFIT_PATTERN.search(text or ""))


# 페이지의 링크/버튼 글자가 혜택명에 딸려 들어온 것. 혜택 내용이 아니라
# UI 라벨이라 떼어낸다. ("65세 이상 안심박스 자세히보기" -> "65세 이상 안심박스")
UI_LABEL_RE = re.compile(r"\s*(?:자세히\s*보기|자세히보기|바로\s*가기|바로가기|신청하기|더\s*보기|더보기)\s*$")


def strip_ui_label(text: str) -> str:
    """혜택명 끝에 붙은 링크/버튼 라벨을 제거한다."""
    cleaned = UI_LABEL_RE.sub("", text or "").strip()
    # 라벨만 있던 값이면 원문을 그대로 둔다(빈 이름이 되는 것보다 낫다)
    return cleaned or (text or "").strip()


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
    """'100,000원' -> 100000. `[\\d,]+`로만 찾으면 "네이버페이, 3대..." 같은
    글자 사이 콤마까지 숫자로 잡혀서 int() 변환이 터지므로, 반드시 숫자로
    시작하는 덩어리만 매칭한다."""
    m = re.search(r"\d[\d,]*", text or "")
    return int(m.group(0).replace(",", "")) if m else None


def extract_speed(text: str) -> str:
    """'다 쓰면 최대 5Mbps' -> '5Mbps'. 없으면 빈 문자열.

    MNO는 데이터를 다 써도 추가 과금이 없고 이 속도로 계속 쓸 수 있다
    (= 초과요금이 아니라 속도 제한). 그래서 이 값이 소진 후 동작을 나타낸다.

    사이트마다 대소문자가 제각각이라(400Kbps / 400kbps) 그대로 두면 같은 값이
    두 종류로 갈라진다. 표기를 하나로 맞춰서 내보낸다.
    """
    m = re.search(r"(\d+(?:\.\d+)?)\s*(kbps|mbps|gbps)", text or "", re.I)
    if not m:
        return ""
    unit = m.group(2).lower()
    return f"{m.group(1)}{unit[0].upper()}{unit[1:]}"


def agreement_discount(fee):
    """선택약정 25% 할인가 계산. 사이트가 직접 할인가를 안 알려줄 때 쓰는
    표준 요율(KT/SKT/LGU+ 공통, LGU+ 안내문에도 "선택 약정 할인(25%)"로 명시됨)."""
    if fee is None:
        return "", ""
    return round(fee * 0.75), "선택약정 25% 할인"


def make_benefit_row(
    plan_id, host_mno, plan_name, benefit_category, benefit_name, *,
    value_won="", pay_won="", selectable=False, select_group="", detail="", source_url="",
):
    """BENEFIT_COLUMNS 순서에 맞는 혜택 행 하나를 만든다. 크롤러마다 같은
    11개 키를 따로 손으로 채우던 걸 한 곳으로 모았다."""
    return {
        "plan_id": plan_id,
        "host_mno": host_mno,
        "plan_name": plan_name,
        "benefit_category": benefit_category,
        # 혜택명에서만 UI 라벨을 뗀다. benefit_detail은 원문 그대로 남겨서
        # 나중에 원문을 다시 확인할 수 있게 한다.
        "benefit_name": strip_ui_label(benefit_name),
        "benefit_value_won": value_won,
        "user_pay_won": pay_won,
        "is_selectable": selectable,
        "select_group": select_group,
        "benefit_detail": detail,
        "source_url": source_url,
    }


def _clean(value):
    """표/HTML에서 그대로 가져온 값에는 줄바꿈·연속공백이 섞여 있어 CSV가 지저분해진다.
    한 줄 텍스트로 정리해서 내보낸다."""
    if isinstance(value, str):
        # 제로폭 문자(U+200B 등)는 \s에 안 걸려서 그냥 두면 눈에 안 보이는 채로
        # 값 끝에 남는다. 실제로 LGU+ 공유데이터 문구 하나에 섞여 있었는데,
        # 이런 게 남으면 문자열 비교/그룹핑이 조용히 어긋난다.
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
    # 절대경로를 그대로 찍으면 로그가 길어져서 파일명만 보여준다
    print(f"{Path(path).name}: {len(rows)}행 저장")


def write_plans(rows, path):
    # base_data_gb/base_tethering_gb는 나이 조건별 추가 제공(KT Y덤 등)이 있는
    # 크롤러만 직접 채운다. SKT/LGU+/모요처럼 그런 개념이 없는 곳은 "추가분 없음
    # = 기본이 곧 총량"이므로 여기서 자동으로 채워준다(크롤러마다 반복 안 해도 됨).
    for row in rows:
        if row.get("base_data_gb", "") == "":
            row["base_data_gb"] = row.get("data_gb", "")
        if row.get("base_tethering_gb", "") == "":
            row["base_tethering_gb"] = row.get("tethering_gb", "")
        # 모요만 넷을 구분해서 직접 채운다. 나머지 3사는 "지원/미지원" 섹션 자체가
        # 없어서 값이 있으면 quota, 없으면 undisclosed까지만 말할 수 있다.
        # 없는 걸 unsupported라고 단정하면 안 된다 - 실제로 미공시일 뿐이다.
        if not row.get("tethering_support"):
            # `!= ""` 로만 보면 파서가 못 찾아서 넣은 None이 "값이 있다"로
            # 통과해 quota로 찍힌다(MNO 151건이 이 상태였다). 빈 문자열과
            # None을 모두 "값 없음"으로 봐야 한다.
            has_quota = row.get("tethering_gb") not in ("", None)
            row["tethering_support"] = "quota" if has_quota else "undisclosed"
        # 택1로 펼치지 않은 행도 base_plan_id를 비워두지 않는다. 그래야
        # "실제 요금제 수"를 언제나 nunique(base_plan_id) 하나로 셀 수 있다.
        if not row.get("base_plan_id"):
            row["base_plan_id"] = row.get("plan_id", "")
    _write(rows, path, PLAN_COLUMNS)


def write_benefits(rows, path):
    # 크롤러마다 따로 채우지 않도록 여기서 한 번에 정규화한다
    for row in rows:
        if not row.get("benefit_service"):
            row["benefit_service"] = normalize_service(row.get("benefit_name", ""))
        if not row.get("benefit_tier"):
            row["benefit_tier"] = extract_tier(row.get("benefit_name", ""), row["benefit_service"])
    _write(rows, path, BENEFIT_COLUMNS)


def expand_select_variants(plan: dict, benefits: list[dict]) -> list[tuple[dict, list[dict]]]:
    """
    "택1" 혜택을 선택지 하나당 요금제 행 하나로 펼친다.

    왜 필요한가: SKT는 이미 선택지별로 별도 상품(prodId)을 만들어 놨다
    (베스트 Max(넷플릭스) NA00009815 / 베스트 Max(디즈니+) NA00009816 …).
    반면 KT의 초이스, LGU+의 프리미엄플러스는 한 요금제 안에 선택지가 목록으로만
    들어있다. 그대로 두면 "SKT는 요금제마다 OTT 1개, KT는 요금제마다 OTT 9개"처럼
    보여서, 순전히 파싱 방식 때문에 생긴 왜곡이 그대로 모델에 들어간다.
    KT/LGU+도 SKT와 같은 입도로 맞춰 준다.

    택1 그룹이 여러 개면 **가장 선택지가 많은 그룹 하나만** 펼친다(KT의 경우
    초이스 9개 vs 플러스 3개 -> 초이스만). 전 조합(9x3=27)으로 펼치면 행이
    급격히 불어나는 데 비해, SKT가 실제로 쓰는 입도는 "주 OTT 하나"라서
    거기에 맞추는 편이 통신사 간 비교에 일관적이다.

    KT의 "플러스" 그룹은 위 규칙과 무관하게 **절대 primary가 되지 않는다** -
    사은품/음악 구독처럼 부가 선택지라 항상 작은 쪽인 게 KT 관례인데, "초이스 더블"
    처럼 경쟁하는 큰 그룹이 아예 없는 페이지에서는 "그룹이 하나뿐이니 그게
    가장 크다"는 논리로 플러스가 primary가 돼 버려 plan_id가 불필요하게
    쪼개졌었다(예: 원래 KT 상품 4개 x 플러스 옵션 4개 = 16행). "프리미엄플러스"
    (LGU+의 진짜 primary 그룹)는 이름이 "플러스"로 시작하지 않으므로 영향 없다.

    펼친 행의 혜택 목록에는 **고른 선택지 하나 + 택1이 아닌 나머지 혜택**만 남는다.
    그래서 나중에 혜택 금액을 붙일 때 그냥 sum 하면 되고, "택1 그룹은 max로
    묶어야 한다" 같은 예외 규칙이 필요 없어진다.
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
        # SKT가 쓰는 "베스트 Max(넷플릭스)" 표기를 따라간다. 원래 이름은
        # base_plan_id로 되짚을 수 있다.
        new_name = f"{plan.get('plan_name', '')} ({option_name})"
        new_benefits = [
            dict(b, plan_id=new_id, plan_name=new_name) for b in ([opt] + others)
        ]
        new_plan = dict(
            plan,
            plan_id=new_id,
            # 이미 base_plan_id가 있으면(= 연령 변형 행을 다시 펼치는 경우)
            # 그걸 유지한다. 덮어쓰면 "초이스130 청년(Y덤) 넷플릭스"의 원본이
            # "초이스130 청년(Y덤)"이 돼서, 진짜 원본인 "초이스130"까지
            # 한 번에 되짚을 수 없다.
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

    # OTT는 정규화된 서비스명으로 요약해야 "넷플릭스"로 바로 검색된다.
    # (write_benefits가 benefit_service를 채우는 건 이 함수가 끝난 뒤라
    # 여기 시점의 row에는 아직 그 키가 없다 - 항상 이름에서 새로 뽑는다)
    ott_services = dict.fromkeys(
        filter(None, (normalize_service(r.get("benefit_name", "")) for r in ott))
    )
    # 멤버십도 같은 이유로 등급만 남긴다("24개월간 VIP 등급" -> "VIP").
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
    # `python src/schema.py` - 분류 규칙 자체 점검. 순서가 결과를 바꾸는 규칙이라
    # (OTT vs 사은품, 사은품 vs 추가데이터) 실제로 걸렸던 이름들을 그대로 박아 둔다.
    CASES = [
        # 구독 이름이 들어간 사은품 링크(모요) - 사은품보다 OTT가 먼저다
        ("구글 AI프로+도미노피자 할인쿠폰", "사은품/페이백", "OTT/구독"),
        ("밀리의 서재 평생 구독 0원", "사은품/페이백", "OTT/구독"),
        ("티빙 광고형 스탠다드 제공 (12개월)", "사은품/페이백", "OTT/구독"),
        # 결합/추가 데이터 - '데이터'라는 말이 없는 표기까지 잡아야 한다
        ("솔로결합(+20GB)", "사은품/페이백", "추가데이터"),
        ("SOLO결합 데이터 10GB 지급", "사은품/페이백", "추가데이터"),
        ("추가데이터 10GB 제공", "사은품/페이백", "추가데이터"),
        ("헬로모바일 결합시, 추가 데이터 10GB 증정", "사은품/페이백", "추가데이터"),
        ("데이터쿠폰 20GB", "사은품/페이백", "추가데이터"),  # '쿠폰'보다 데이터가 먼저
        # 진짜 사은품은 그대로 남아야 한다(default로 떨어지는 것 포함)
        ("네이버페이 5,000원", "사은품/페이백", "사은품/페이백"),
        ("일반유심/배송비 무료", "사은품/페이백", "사은품/페이백"),
        ("쇼핑라운지 할인쿠폰 5천원권", "기타", "사은품/페이백"),
        ("에어팟4", "사은품/페이백", "사은품/페이백"),
        # 3사 규칙이 바뀌지 않았는지
        ("U+ 멤버십 VIP콕(24개월 간 매월 제공)", "사은품/페이백", "멤버십"),
        ("스마트기기 이용 요금 50% 할인 (1회선)", "사은품/페이백", "스마트기기"),
        ("폰케어 서비스", "기타", "기타"),
    ]
    for name, default, expected in CASES:
        got = classify_benefit_name(name, default)
        assert got == expected, f"{name!r} -> {got} (기대: {expected})"
    print(f"classify_benefit_name 점검 {len(CASES)}건 통과")
