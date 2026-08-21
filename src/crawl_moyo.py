"""모요(moyoplan.com) 알뜰폰 요금제 + 혜택 크롤러.

Next.js SSR이라 requests로 받은 HTML에 데이터가 그대로 들어있다(브라우저 불필요).
공개 API는 없다. 원본은 data/raw_cache/moyo/에 저장하고 `--parse-only`로 재파싱.

2단계로 수집한다.
1) 목록 `/plans?page=N` (10개씩)  -> 스펙(데이터/통화/문자/망/가격/프로모션)
2) 상세 `/plans/{id}`             -> 브랜드명 + 사은품 + 페이백
   요금제가 2,000개가 넘어 상세까지 받으면 요청이 많지만, 혜택이 전부 상세에만
   있어서 생략하면 MVNO 쪽 혜택이 통째로 빈다.
   - 브랜드명은 <title>의 "[핀다이렉트] …"에만 있다.
   - 사은품·페이백은 <a href="/gift-group/...">의 aria-label에 이름이 있고 본문에
     "대상:"/"시기:" 조건이 따라온다. "매달 N원 페이백 (M개월)"처럼 1회분 금액만
     적힌 라벨은 총액으로 환산한다(_recurring_payback_won). "페이백"이라는 말이
     없는 변형("Npay 5천원 (7만)")은 아직 못 잡는다 - docs/수정이력.md 34번.
   - 목록 카드의 "페이백 포함 월 X원"이 실제 청구액과 다를 수 있다(모요가 체감가를
     보여줌). "N개월 이후 Y원"의 Y로 대체하려다 되돌렸다 - parse_card_only 참고.
"""
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from schema import (
    write_plans, write_benefits, summarize_benefits, to_gb, to_won, make_benefit_row,
    classify_benefit_name, cache_dir, interim_path,
)

HEADERS = {"User-Agent": "Mozilla/5.0"}
# 통신 3사 자체 브랜드명 -> host_mno 표기.
HOST_BRAND_MAP = {"KT": "KT", "SKT": "SKT", "LG U+": "LGU+"}
BASE = "https://www.moyoplan.com"
LIST_URL = BASE + "/plans?page={page}"
DETAIL_URL = BASE + "/plans/{plan_id}"
CACHE_DIR = cache_dir("moyo")


# 한 요청에 허용할 **총** 시간. urlopen(timeout=N)의 N은 총 소요 시간이 아니라
# 소켓 작업 하나당 제한이라, 서버가 데이터를 조금씩 흘려보내면 타임아웃이 영영
# 안 걸린다(실측: timeout=20인데 한 건 269초, 이런 정체 106건이 상세 수집 34분
# 중 31분을 먹었다. 중앙값은 0.4초). 산발적이라 끊고 다시 요청하면 대개 바로
# 성공하므로 짧게 자르고 재시도한다.
REQUEST_DEADLINE_SEC = 6.0
REQUEST_ATTEMPTS = 3
# deadline보다 짧아야 초과분이 작아진다 - 경과 확인은 덩어리 사이에서만 된다.
SOCKET_TIMEOUT_SEC = 3.0


def _get_once(url: str, deadline: float) -> str:
    """deadline초를 넘기면 TimeoutError. 받는 도중에도 경과 시간을 확인한다."""
    started = time.monotonic()
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=SOCKET_TIMEOUT_SEC) as resp:
        chunks = []
        while True:
            if time.monotonic() - started > deadline:
                raise TimeoutError(f"{deadline}초 초과")
            # read()가 아니라 read1()이어야 한다. read(n)은 n바이트가 다 모일
            # 때까지 내부에서 기다려서, 서버가 찔끔찔끔 보내면 위 경과 시간
            # 확인이 실행될 기회조차 없다.
            chunk = resp.read1(65536)
            if not chunk:
                break
            chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")


def _get(url: str) -> str:
    last_error = None
    for attempt in range(REQUEST_ATTEMPTS):
        try:
            return _get_once(url, REQUEST_DEADLINE_SEC)
        except Exception as e:
            last_error = e
            if attempt < REQUEST_ATTEMPTS - 1:
                time.sleep(0.5 * (attempt + 1))
    raise last_error


_SUBSCRIBER_RE = re.compile(r"([\d,]+)\+?\s*명이\s*선택")

_KRW_RE = re.compile(r"([\d,.]+)\s*(만|천)?\s*원")
_KRW_UNIT = {None: 1, "천": 1_000, "만": 10_000}


def _parse_krw(text: str):
    """사은품 이름에서 금액을 뽑는다. '19.2만원' -> 192000, '5천원' -> 5000.

    사은품명은 대체로 "총액(월별 분할)" 형태라("네이버페이 19.2만원(매월 3.2만원씩)")
    **금액이 여러 개면 가장 큰 값**을 쓴다. 등장 순서로 고르면 사이트가 "매월
    3.2만원씩(총 19.2만원)"으로 어순을 바꿀 때 조용히 분할 금액을 총액으로 기록한다.
    """
    amounts = []
    for m in _KRW_RE.finditer(text or ""):
        try:
            value = float(m.group(1).replace(",", "").rstrip("."))
        except ValueError:
            continue
        amounts.append(int(value * _KRW_UNIT[m.group(2)]))
    return max(amounts) if amounts else None


# "네이버페이 매달 2만원 페이백 (6개월)"처럼 한 달치 금액만 적힌 링크가 있다.
# 그대로 _parse_krw에 넘기면 실제 가치의 6분의 1로 저평가되므로 총액으로 바로잡는다.
# "평생"처럼 개월 수가 없으면 총액을 낼 수 없어 한 달치만 남긴다.
_RECURRING_PAYBACK_RE = re.compile(r"매달\s*([\d,.]+\s*(?:만|천)?\s*원)\s*(?:씩)?\s*페이백.*?(\d+)\s*개월")


def _recurring_payback_won(label: str):
    m = _RECURRING_PAYBACK_RE.search(label or "")
    if not m:
        return None
    per_month = _parse_krw(m.group(1))
    if per_month is None:
        return None
    return per_month * int(m.group(2))


def fetch_list_pages() -> list[str]:
    os.makedirs(CACHE_DIR, exist_ok=True)
    html = _get(LIST_URL.format(page=1))
    total_m = re.search(r"([\d,]+)개의 요금제", html)
    total_count = int(total_m.group(1).replace(",", "")) if total_m else None
    total_pages = (total_count + 9) // 10 if total_count else 999
    print(f"전체 요금제 수: {total_count}, 페이지 수: {total_pages}")

    seen = set()
    page = 1
    while page <= total_pages:
        with open(os.path.join(CACHE_DIR, f"page_{page}.html"), "w", encoding="utf-8") as f:
            f.write(html)
        ids = set(re.findall(r'href="/plans/(\d+)"', html))
        new = len(ids - seen)
        seen |= ids
        if page % 20 == 0 or page == 1:
            print(f"  page {page}: 누적 {len(seen)}개")
        if new == 0 and page > 1:
            break
        page += 1
        if page > total_pages:
            break
        time.sleep(0.25)
        html = _get(LIST_URL.format(page=page))
    return sorted(seen)


# 모요 서버는 같은 URL에 두 가지 응답을 준다. 불완전한 쪽은 "기타 비용" 탭 라벨만
# 있고 안의 표가 비어 있다(캐시 2,267개 중 977개). 라벨만 있고 표가 없으면 덜 받은
# 것으로 보고 다시 받는다. 표도 라벨도 없는 페이지(331개)는 모요 비제휴/타사 안내
# 페이지라 원래 해당 섹션이 없다 - 재수집 대상이 아니다.
def _detail_incomplete(html: str) -> bool:
    return "기타 비용" in html and "기본 제공 초과 시" not in html


def fetch_details(plan_ids: list[str], retries: int = 2, force_ids: set[str] | None = None):
    """상세 페이지 수집. 이미 받아둔 건 기본적으로 건너뛴다.

    force_ids에 든 요금제는 캐시가 멀쩡해도 다시 받는다. 목록 카드 값이 바뀐
    요금제는 상세도 바뀌었을 가능성이 높다.
    """
    force_ids = force_ids or set()
    print(f"상세 페이지 수집 중... ({len(plan_ids)}개, 강제 재수집 {len(force_ids)}개)")
    refetched = 0
    for i, pid in enumerate(plan_ids, 1):
        path = os.path.join(CACHE_DIR, f"detail_{pid}.html")
        cached = None
        if os.path.exists(path) and pid not in force_ids:
            with open(path, encoding="utf-8") as f:
                cached = f.read()
            if not _detail_incomplete(cached):
                continue
            refetched += 1
        for _ in range(retries if cached else 1):
            try:
                html = _get(DETAIL_URL.format(plan_id=pid))
            except Exception as e:
                print(f"  {pid} 실패: {e}")
                break
            # 다시 받아도 불완전하면 그대로 두고 넘어간다(원래 없는 요금제일 수 있다)
            if not _detail_incomplete(html) or cached is None:
                cached = html
                break
            cached = html
            time.sleep(0.5)
        if cached:
            with open(path, "w", encoding="utf-8") as f:
                f.write(cached)
        if i % 200 == 0:
            print(f"  {i}/{len(plan_ids)}")
        time.sleep(0.2)
    if refetched:
        print(f"  불완전 캐시 재수집 대상: {refetched}개")


SNAPSHOT_PATH = os.path.join(CACHE_DIR, "list_snapshot.json")


def _iter_list_cards():
    """캐시된 목록 페이지의 요금제 카드를 순회한다(중복 id는 한 번만)."""
    seen = set()
    files = sorted(
        (f for f in os.listdir(CACHE_DIR) if f.startswith("page_")),
        key=lambda f: int(re.search(r"\d+", f).group()),
    )
    for fname in files:
        with open(os.path.join(CACHE_DIR, fname), encoding="utf-8") as f:
            soup = BeautifulSoup(f.read(), "html.parser")
        for a in soup.find_all("a", href=re.compile(r"^/plans/\d+$")):
            card = parse_card_only(a)
            if card and card["plan_id"] not in seen:
                seen.add(card["plan_id"])
                yield card


def list_snapshot() -> dict:
    """지금 캐시된 목록 기준 {plan_id: 지문}."""
    return {c["plan_id"]: card_fingerprint(c) for c in _iter_list_cards()}


def _load_snapshot() -> dict:
    if not os.path.exists(SNAPSHOT_PATH):
        return {}
    with open(SNAPSHOT_PATH, encoding="utf-8") as f:
        return json.load(f)


def fetch_all():
    """목록은 항상 새로 받고, 상세는 **바뀐 것만** 다시 받는다.

    모요는 상세 페이지가 2,200여 건이라 매번 전부 받으면 비용이 크다. 반면
    요금·데이터·음성·문자는 전부 목록 카드에 있어서, 목록만 비교하면 어떤
    요금제가 달라졌는지 알 수 있다. 달라진 것과 새로 생긴 것만 상세를 받는다.
    """
    prev = _load_snapshot()
    plan_ids = fetch_list_pages()
    current = list_snapshot()

    added = [pid for pid in current if pid not in prev]
    changed = [pid for pid in current if pid in prev and current[pid] != prev[pid]]
    removed = [pid for pid in prev if pid not in current]
    print(f"목록 비교: 신규 {len(added)} / 변경 {len(changed)} / 사라짐 {len(removed)}")

    fetch_details(plan_ids, force_ids=set(added) | set(changed))

    # 목록에서 사라진 요금제(단종)의 상세 캐시는 지운다.
    for pid in removed:
        path = os.path.join(CACHE_DIR, f"detail_{pid}.html")
        if os.path.exists(path):
            os.remove(path)

    with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(current, f, ensure_ascii=False, indent=1)


GIFT_LABEL_SUFFIX = " 상세 페이지 새 탭 열기"

# 사은품이 3개 이상이면 앞의 2개만 렌더하고 나머지는 "펼쳐보기" 뒤에 감춘다.
# 감춘 항목은 <a href="/gift-group/...">가 아예 없고 Next.js 플라이트 페이로드
# (self.__next_f.push)에만 있다. 전수 확인 결과 DOM 2,413건 vs 페이로드 2,726건으로
# 109개 요금제에서 313건이 빠져 있었고, 페이로드가 DOM의 상위집합이라 DOM에 없는
# 것만 여기서 보탠다.
_FLIGHT_CHUNK_RE = re.compile(r'self\.__next_f\.push\(\[1,(".*?")\]\)</script>', re.S)
_GIFT_OBJ_RE = re.compile(r'\{"giftGroup":\{"id":')
_JSON_DECODER = json.JSONDecoder()


# 사은품 조건이 "유심을 어디서 사서 개통했는지"인 경우가 있는데, 구매처는 하나만
# 고를 수 있어 이런 사은품끼리는 **동시에 못 받는다**(37132는 쿠팡캐시 2만원과
# 3대 마트 상품권 2만원이 둘 다 붙어 있지만 실제로는 하나뿐이다). 반대로 같은
# 구매처가 조건인 사은품끼리는 전부 함께 받는다. 그래서 select_group이 아니라
# benefit_condition으로 적고, 값이 같으면 합산 / 다르면 택1로 쓴다.
#
# 사은품 281종을 전수 확인해서 만든 패턴이다. "유심/배송비 무료"처럼 이름에 유심이
# 들어가지만 구매처 조건이 아닌 사은품(46건 중 9건)이 안 섞이게 좁게 잡았다.
_GIFT_CHANNEL_PATTERNS = [
    ("KT바로유심", r"바로유심|바로배송\s*유심"),
    ("쿠팡유심", r"쿠팡\s*유심"),
    ("모요유심", r"모요에서\s*신청한\s*유심"),
    ("택배일반유심", r"일반유심을\s*['\"]?택배로\s*받을래요"),
    ("T다이렉트샵", r"T\s*다이렉트샵"),
]


def _gift_condition(text: str) -> str:
    for name, pattern in _GIFT_CHANNEL_PATTERNS:
        if re.search(pattern, text or ""):
            return name
    return ""


def parse_flight_gifts(html: str) -> dict:
    """플라이트 페이로드의 사은품을 {사은품id: (이름, 조건설명, 배타조건)}으로 뽑는다.

    페이로드는 <script> 청크 여러 개로 쪼개져 있고 같은 값이 두 번 나오면 뒤엣것이
    참조("$3e:props:giftGroupList")로 치환돼 있어서 통째로 JSON 파싱할 수 없다.
    giftGroup 객체가 시작하는 위치마다 raw_decode로 하나씩 떼어낸다.
    """
    try:
        payload = "".join(json.loads(chunk) for chunk in _FLIGHT_CHUNK_RE.findall(html))
    except ValueError:
        return {}
    gifts = {}
    for m in _GIFT_OBJ_RE.finditer(payload):
        try:
            obj, _ = _JSON_DECODER.raw_decode(payload, m.start())
        except ValueError:
            continue
        group = obj.get("giftGroup") or {}
        gift_id = group.get("id")
        if gift_id is None:
            continue
        # 렌더된 카드의 "대상: … 시기: …" 문구와 같은 자리의 원본 필드.
        condition = (obj.get("rewardConditionList") or {}).get("description") or ""
        timing = group.get("rewardTimingDescription") or ""
        title = group.get("title") or ""
        subtitle = group.get("subtitle") or ""
        detail = " ".join(
            part for part in (
                f"대상: {condition}" if condition else "",
                f"시기: {timing}" if timing else "",
            ) if part
        )
        # 구매처가 조건설명에만 적힌 사은품도, 부제에만 적힌 사은품도 있어서
        # 세 필드를 다 본다.
        gifts[str(gift_id)] = (
            title, detail, _gift_condition(f"{title} {subtitle} {condition}"),
        )
    return gifts


def parse_support_services(soup) -> dict:
    """
    상세페이지 "지원 / 미지원" 섹션을 읽는다.

        지원    인터넷 결합
                모바일 핫스팟   월 60GB 이용 가능
                소액 결제
                해외 로밍       통신사 문의
        미지원  없음

    목록 카드에는 테더링 정보가 없어서 핫스팟 용량은 여기서만 얻을 수 있다.
    "월 60GB 이용 가능"은 용량이 있는 경우, "데이터 제공량 내 이용 가능"은 별도
    할당 없이 기본 데이터에서 차감하는 경우라 용량을 비워 둔다.

    **지원 여부는 렌더된 섹션이 아니라 <meta name="description">에서 읽는다.**
    이 섹션이 통째로 안 실린 캐시 페이지가 많은데(_detail_incomplete가 잡는 그
    케이스) 메타 설명에는 "모바일 핫스팟 제공"이 항상 들어 있다. 섹션만 보면
    테더링이 안 되는 요금제 상당수를 "미공개"로 오인한다(79건 -> 1,023건).

    클래스명이 Tailwind 해시라 클래스로 찾으면 배포마다 깨진다. "지원" 라벨을
    찾아 그 컨테이너의 **텍스트**에서 뽑는다 - 항목명과 부가설명이 서로 다른
    깊이의 요소에 있어서 특정 요소를 집으면 둘 중 하나만 잡힌다.
    """
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and "모바일 핫스팟 제공" not in (meta.get("content") or ""):
        return {"tethering_gb": None, "tethering_support": "unsupported"}

    label = next((e for e in soup.find_all("span") if e.get_text(strip=True) == "지원"), None)
    if label is None or label.parent is None:
        return {"tethering_support": "undisclosed"}

    section = re.sub(r"\s+", " ", label.parent.get_text(" ", strip=True))
    m = _HOTSPOT_RE.search(section)
    if not m:
        return {"tethering_support": "undisclosed"}

    note = m.group(1)
    if is_non_benefit_share(note):
        return {"tethering_gb": None, "tethering_support": "within_data"}
    gb = to_gb(note)
    if gb is None:
        return {"tethering_support": "undisclosed"}
    return {"tethering_gb": gb, "tethering_support": "quota"}


# "모바일 핫스팟" 뒤부터 다음 항목명 전까지가 그 항목의 설명이다.
_HOTSPOT_RE = re.compile(
    r"핫스팟\s*(.*?)\s*(?:소액|해외\s*로밍|데이터\s*쉐어링|인터넷\s*결합|미지원|$)"
)


# schema의 is_non_benefit는 "기본"이 붙은 표현만 보므로 모요 문구는 따로 처리한다.
def is_non_benefit_share(text: str) -> bool:
    return bool(re.search(r"제공량\s*내|기본\s*데이터\s*내", text or ""))


def parse_addon_voice(soup) -> str:
    """
    상세페이지 "부가통화" 한 줄 항목에서 추가 제공 분수를 뽑는다.

        부가통화 300분
        부가통화 제공 안 함
        부가통화 통신사 문의
        부가통화 통화 제공량 내 이용 가능

    표기가 네 갈래인데 숫자(분)가 있는 경우만 실제 추가 제공량이다. 나머지 셋은
    "추가로 주는 게 없거나 확인 불가"라 빈 값으로 둔다.
    """
    label = next((e for e in soup.find_all("span") if e.get_text(strip=True) == "부가통화"), None)
    if label is None or label.parent is None or label.parent.parent is None:
        return ""
    row = re.sub(r"\s+", " ", label.parent.parent.get_text(" ", strip=True))
    m = re.search(r"(\d+)\s*분", row)
    return m.group(1) if m else ""


def parse_signup_notice(soup) -> str:
    """상세페이지 "꼭 확인해 주세요" 배너 텍스트(가입 제한/비제휴 등 경고문).

    없는 요금제가 약 73%라 있는 것만 채운다. 배너 하나에 메시지가 여러 개 붙는
    경우가 있어 ' | '로 모아 담는다.
    """
    heading = next((s for s in soup.find_all("span") if s.get_text(strip=True) == "꼭 확인해 주세요"), None)
    if heading is None or heading.parent is None:
        return ""
    # 메시지 span이 중첩돼 있어 그냥 훑으면 같은 문구가 두 번 잡힌다.
    # span 자식이 없는 span만 진짜 메시지다.
    texts = [
        s.get_text(" ", strip=True)
        for s in heading.parent.find_all("span")
        if s is not heading and not s.find("span") and s.get_text(strip=True)
    ]
    return " | ".join(dict.fromkeys(texts))


def parse_detail(plan_id: str, plan_name: str):
    """returns (mvno_brand, benefit_rows, support, signup_notice)"""
    path = os.path.join(CACHE_DIR, f"detail_{plan_id}.html")
    if not os.path.exists(path):
        return "", [], {}, ""
    with open(path, encoding="utf-8") as f:
        html = f.read()
    soup = BeautifulSoup(html, "html.parser")
    url = DETAIL_URL.format(plan_id=plan_id)
    signup_notice = parse_signup_notice(soup)

    # 브랜드: <title>[핀다이렉트] [모요only] ... | 13,100원 | 모요...</title>
    brand = ""
    title = soup.find("title")
    if title:
        m = re.match(r"\s*\[([^\]]+)\]", title.get_text())
        if m:
            brand = m.group(1).strip()

    benefits = []

    # 링크가 /gift-group/이라고 다 사은품인 건 아니다. OTT 구독권·결합 추가데이터·
    # 멤버십까지 전부 같은 링크라, 링크 종류로 카테고리를 정하면 MVNO 혜택이
    # 통째로 사은품으로 몰린다. 이름 기반 규칙을 쓰고 단서가 없을 때만 사은품이다.
    def add_gift(label: str, detail: str, condition: str):
        benefits.append(make_benefit_row(
            plan_id, "", plan_name, classify_benefit_name(label, "사은품/페이백"), label,
            value_won=_recurring_payback_won(label) or _parse_krw(label) or "",
            condition=condition, detail=detail, source_url=url,
        ))

    # 렌더된 사은품도 배타조건은 페이로드 쪽 원본 필드로 판정한다. 카드 문구는
    # 조건설명이 잘려 있어서 같은 사은품이 렌더 여부에 따라 다르게 분류된다.
    flight_gifts = parse_flight_gifts(html)

    rendered_gift_ids = set()
    for a in soup.select('a[href^="/gift-group/"]'):
        label = (a.get("aria-label") or "").strip()
        if label.endswith(GIFT_LABEL_SUFFIX):
            label = label[: -len(GIFT_LABEL_SUFFIX)].strip()
        if not label:
            continue
        detail = " ".join(s.get_text(" ", strip=True) for s in a.select("span") if "대상:" in s.get_text() or "시기:" in s.get_text())
        id_m = re.search(r"/gift-group/(\d+)", a.get("href", ""))
        gift_id = id_m.group(1) if id_m else ""
        if gift_id:
            rendered_gift_ids.add(gift_id)
        add_gift(label, detail, flight_gifts.get(gift_id, ("", "", ""))[2])

    # "펼쳐보기" 뒤에 감춰져 DOM에 렌더되지 않은 사은품 보충. parse_flight_gifts 참고.
    for gift_id, (label, detail, condition) in flight_gifts.items():
        if gift_id in rendered_gift_ids or not label:
            continue
        add_gift(label, detail, condition)

    support = parse_support_services(soup)
    support["voice_extra_minutes"] = parse_addon_voice(soup)
    return brand, benefits, support, signup_notice


def parse_card_only(a_tag) -> dict | None:
    """목록 카드 하나에서 **상세 페이지 없이 알 수 있는 값**만 뽑는다.

    parse_card()와 갱신용 지문(list_snapshot) 계산이 이 함수를 공유한다. 상세를
    열지 않고 "값이 바뀌었나"를 판단할 수 있어야 2,200여 건을 매번 안 받는다.
    """
    href = a_tag.get("href", "")
    id_m = re.search(r"/plans/(\d+)", href)
    if not id_m:
        return None
    plan_id = id_m.group(1)

    # get_text('\n')로 뽑으면 "월 8,100원"이 태그 경계마다 쪼개진다. 한 줄로 합친 뒤
    # 정규식으로 구간을 잘라낸다.
    card_text = re.sub(r"\s+", " ", a_tag.get_text(" ", strip=True))

    # 데이터 표기가 "월 100GB" 말고도 "매일 5GB", 라벨 없는 "무제한",
    # "데이터 제공안함" 세 가지가 더 있다 - "월 …"만 인식하면 이 카드들이 통째로
    # 안 읽힌다(95/2528건). 바로 뒤 "통화"가 온다는 lookahead로 이름 안에
    # "무제한"이 들어간 경우("무제한일 5GB")와 헷갈리지 않게 막는다.
    name_m = re.match(
        r"^(?:\d\.\d\s+)?(.+?)\s+(?:(?:월|매일)\s+(?:무제한|[\d.]+\s*(?:GB|MB))"
        r"|데이터\s*제공안함|무제한(?=\s+통화))",
        card_text,
    )
    if not name_m:
        return None
    plan_name = name_m.group(1).strip()

    # 요금·기간·데이터는 **이름 뒤 구간에서만** 찾는다. 이름에 금액이 박힌 요금제가
    # 있어("무제한 7GB+1M(다이소 매월 5000원)") 카드 전체를 훑으면 이름 속 금액이
    # 먼저 걸리고, 이름 끝의 "월"("시월 무제한 7GB")이 데이터의 "월 …"로 오인된다.
    spec_text = card_text[name_m.end(1):]

    data_m = re.search(
        r"((?:월|매일)\s+(?:무제한|[\d.]+\s*(?:GB|MB)).*?|데이터\s*제공안함|무제한)\s+통화",
        spec_text,
    )
    data_text = data_m.group(1) if data_m else ""
    data_unlimited = "무제한" in data_text
    # "매일 5GB"만 있는 카드는 월 총량이 없으므로 data_gb를 비워야 한다. to_gb()는
    # 첫 "N GB"를 그대로 집어서, 안 걸러주면 일일 재충전량을 월 총량으로 채운다.
    is_daily_only = data_text.startswith("매일")
    qos = re.search(r"([\d.]+)\s*(Kbps|Mbps)", data_text, re.I)
    daily = re.search(r"매일\s*([\d.]+)\s*GB", data_text)

    # "통화 제공안함"(데이터 전용 유심)을 못 잡으면 voice_minutes가 빈 값이 되는데,
    # 빈 값은 컬럼 정의상 "무제한"이라 통화가 아예 안 되는 요금제가 "통화 무제한"으로
    # 둔갑한다 - 126/130건.
    voice_m = re.search(r"통화\s*(무제한|제공안함|[\d,]+분)", card_text)
    voice_text = voice_m.group(1) if voice_m else ""
    voice_unlimited = voice_text == "무제한"

    sms_m = re.search(r"문자\s*(무제한|제공안함|[\d,]+건)", card_text)
    sms_text = sms_m.group(1) if sms_m else ""
    sms_unlimited = sms_text == "무제한"

    # "38,285명이 선택" 또는 "10+명이 선택"(하한 표기). "+"는 캡처하지 않는다.
    select_m = _SUBSCRIBER_RE.search(card_text)
    subscriber_count = to_won(select_m.group(1)) if select_m else None

    # \S+망 으로 느슨하게 잡으면 "5G망"까지 통신사로 잡혀서 3사만 매칭한다.
    net_m = re.search(r"(KT|SKT|LG\s*U\+)\s*망", card_text)
    host_mno = HOST_BRAND_MAP.get(net_m.group(1), net_m.group(1)) if net_m else ""
    gen_m = re.search(r"\b(5G|LTE|3G)\b", card_text)

    # 프로모션 표기가 두 종류다. (A)만 처리하면 (B)는 **할인가를 정가로 오인**한다.
    #   (A) "... 월 7,000원 ... 6개월 이후 49,000원"   - 1,477건
    #   (B) "혜택가 6개월간 월 7,000원   월 49,000원"    -   228건
    fees = [to_won(m.group(1)) for m in re.finditer(r"월\s*([\d,]+)\s*원", spec_text)]
    fees = [f for f in fees if f is not None]
    if not fees:
        return None

    after_m = re.search(r"(\d+)개월\s*이후\s*([\d,]+)\s*원", spec_text)
    span_m = re.search(r"혜택가\s*(\d+)\s*개월간", spec_text)
    if after_m:
        promo_fee, regular_fee = fees[0], to_won(after_m.group(2))
        discount_period = int(after_m.group(1))
    elif span_m and len(fees) >= 2:
        # "혜택가 …" 뒤 첫 금액이 할인가, 마지막 금액이 정가
        promo_fee, regular_fee = fees[0], fees[-1]
        discount_period = int(span_m.group(1))
    else:
        promo_fee = regular_fee = fees[0]
        discount_period = ""

    is_payback = "페이백 포함" in card_text
    # ⚠️ discounted_fee를 실제 청구액으로 바꾸는 시도를 했다가 되돌렸다(2026-08-03).
    # "페이백 포함 월 X원 N개월 이후 Y원"에서 실제 청구액이 X도 Y도 아닌 제3의 값인
    # 경우가 있다(plan 30954: 카드는 12,800/7개월 후 43,400인데 상세 "월 납부액"은
    # 17,800). Y는 "체감가 기간이 끝난 뒤의 미래 가격"이라, X를 Y로 바꾸면 진짜
    # 임시 할인이라는 정보가 사라진다. 상세의 "월 납부액"을 긁어오기 전까지 X를 쓴다.
    if is_payback:
        discount_type = "모요 프로모션 페이백/할인"
    elif promo_fee != regular_fee:
        discount_type = "모요 프로모션 할인"
    else:
        discount_type = ""

    return {
        "carrier_type": "MVNO",
        "host_mno": host_mno,
        "plan_id": plan_id,
        "plan_id_type": "official_code",
        "plan_name": plan_name,
        "plan_category": "moyo",
        "is_online_only": True,  # 모요는 온라인 가입 채널
        "age_condition": "",
        "network_gen": gen_m.group(1) if gen_m else "",
        "data_gb": None if (data_unlimited or is_daily_only) else to_gb(data_text),
        "data_unlimited": data_unlimited,
        "data_throttle_speed": f"{qos.group(1)}{qos.group(2)}" if qos else "",
        "daily_data_gb": float(daily.group(1)) if daily else "",
        "voice_unlimited": voice_unlimited,
        "voice_minutes": "" if voice_unlimited else (0 if voice_text == "제공안함" else (to_won(voice_text) or "")),
        "sms_unlimited": sms_unlimited,
        "sms_count": "" if sms_unlimited else (0 if sms_text == "제공안함" else (to_won(sms_text) or "")),
        "monthly_fee": regular_fee,        # 프로모션 종료 후 정상가(없으면 현재가가 정가)
        "discounted_fee": promo_fee,        # 프로모션 적용된 현재 월 납부액
        "discount_type": discount_type,
        "discount_period_months": discount_period,
        "subscriber_count": subscriber_count if subscriber_count is not None else "",
        "source_url": DETAIL_URL.format(plan_id=plan_id),
    }


# 갱신 판단에 쓸 지문. 목록 카드 값만 넣는다 - 이 값들이 그대로면 상세도 안
# 바뀌었다고 보고 재수집을 건너뛴다. subscriber_count는 스펙이 그대로여도 매일
# 바뀌므로 일부러 뺀다(넣으면 매일 전체를 다시 받게 된다).
_FINGERPRINT_FIELDS = (
    "plan_name", "host_mno", "network_gen",
    "data_gb", "data_unlimited", "data_throttle_speed", "daily_data_gb",
    "voice_unlimited", "voice_minutes", "sms_unlimited", "sms_count",
    "monthly_fee", "discounted_fee", "discount_type", "discount_period_months",
)


def card_fingerprint(card: dict) -> str:
    return "|".join(str(card.get(k, "")) for k in _FINGERPRINT_FIELDS)


def parse_card(a_tag, now: str):
    """목록 카드 + 상세 페이지를 합쳐 최종 요금제 행을 만든다."""
    card = parse_card_only(a_tag)
    if card is None:
        return None, []

    brand, benefits, support, signup_notice = parse_detail(card["plan_id"], card["plan_name"])
    for b in benefits:
        b["host_mno"] = card["host_mno"]

    # 모요에서 수집한 행은 브랜드와 무관하게 carrier_type=MVNO다(수집 출처 기준).
    # 너겟/요고/다이렉트는 통신사 사이트에서 직접 수집한 MNO 행이 따로 남아, 같은
    # 요금제가 두 채널에 한 행씩 존재한다. 어느 쪽인지는 plan_category로 구분한다.
    plan = {
        **card,
        "mvno_brand": brand,
        "signup_notice": signup_notice,
        # 목록 카드에 테더링 정보가 없어 상세 "지원" 섹션에서 가져온다.
        "tethering_gb": support.get("tethering_gb") or "",
        "tethering_support": support.get("tethering_support", ""),
        # 상세페이지 "부가통화" 항목에서 가져온다(목록 카드엔 없음).
        "voice_extra_minutes": support.get("voice_extra_minutes") or "",
        "crawled_at": now,
    }
    plan.update(summarize_benefits(benefits))
    return plan, benefits


def parse_all() -> tuple[list[dict], list[dict]]:
    now = datetime.now(timezone.utc).isoformat()
    plans, benefits, seen = [], [], set()
    by_id = {}
    files = sorted(
        (f for f in os.listdir(CACHE_DIR) if f.startswith("page_")),
        key=lambda f: int(re.search(r"\d+", f).group()),
    )
    for fname in files:
        with open(os.path.join(CACHE_DIR, fname), encoding="utf-8") as f:
            soup = BeautifulSoup(f.read(), "html.parser")
        for a in soup.find_all("a", href=re.compile(r"^/plans/\d+$")):
            # parse_card()가 상세 페이지 파일 I/O까지 하므로 이미 처리한 id는
            # 호출 자체를 건너뛴다.
            plan_id_m = re.search(r"/plans/(\d+)", a.get("href", ""))
            pid = plan_id_m.group(1) if plan_id_m else None
            if pid and pid in seen:
                # "지금 HOT" 배너는 같은 요금제를 페이지마다 한 번 더 보여주는데
                # 그 카드에는 "N명이 선택"이 없다. 먼저 파싱된 쪽이 배너면
                # subscriber_count가 영영 빈값으로 남으므로 카드만 다시 읽는다.
                if pid in by_id and not by_id[pid].get("subscriber_count"):
                    card = parse_card_only(a)
                    if card and card.get("subscriber_count"):
                        by_id[pid]["subscriber_count"] = card["subscriber_count"]
                continue
            plan, plan_benefits = parse_card(a, now)
            if plan and plan["plan_id"] not in seen:
                seen.add(plan["plan_id"])
                by_id[plan["plan_id"]] = plan
                plans.append(plan)
                benefits.extend(plan_benefits)
    return plans, benefits


if __name__ == "__main__":
    if "--parse-only" not in sys.argv:
        fetch_all()
    plan_rows, benefit_rows = parse_all()
    write_plans(plan_rows, interim_path("moyo_plans.csv"))
    write_benefits(benefit_rows, interim_path("moyo_benefits.csv"))
