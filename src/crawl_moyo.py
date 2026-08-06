"""
모요(moyoplan.com) 알뜰폰 요금제 + 혜택 크롤러.

moyoplan.com은 Next.js SSR이라 requests로 받은 HTML에 데이터가 그대로 들어있다
(브라우저 불필요). 별도 공개 API는 없다.

2단계로 수집한다.
1) 목록 `/plans?page=N` (10개씩)  -> 요금제 스펙(데이터/통화/문자/망/가격/프로모션)
2) 상세 `/plans/{id}`              -> 알뜰폰 브랜드명 + 통신사 지급 사은품 + 모요 페이백
   - 브랜드명은 목록 카드에 없고 상세 페이지에만 있다 (<title>의 "[핀다이렉트] ...").
   - 사은품·페이백은 전부 <a href="/gift-group/...">의 aria-label에 이름이
     들어있고(모요 제휴 페이백도 여기 포함됨), 본문에 "대상:"/"시기:" 조건이
     따라온다. "매달 N원 페이백 (M개월)"처럼 **1회분 금액**만 적힌 라벨은
     총액(1회분 x 개월수)으로 환산해서 저장한다(_recurring_payback_won).
     단 "페이백"이라는 말이 없는 변형("Npay 5천원 (7만)", "hy 금액권
     1만원(12만)")은 아직 못 잡는다 - 알려진 한계, docs/수정이력.md 34번 참고.
   - 목록 카드의 "페이백 포함 월 X원"이 실제 청구액과 다른 경우가 있다(모요가
     페이백을 미리 뺀 체감가를 보여줌). "N개월 이후 Y원"의 Y로 대체하려던
     시도를 한 번 했었는데, Y가 "체감가 기간이 끝난 뒤의 미래 가격"인 경우가
     있어(예: plan 30954 - 카드엔 12,800/7개월 후 43,400이라 나오지만 실제
     청구액은 17,800) 되돌렸다. discounted_fee는 여전히 카드의 X를 쓴다 -
     완벽히 정확하진 않지만, 상세페이지의 "월 납부액"을 새로 긁어오기 전까지는
     이게 그나마 덜 틀린 값이다.

요금제가 2,000개가 넘어서 상세 페이지까지 받으면 요청이 많지만, 혜택(사은품/페이백)이
전부 상세에만 있어서 생략하면 MVNO 쪽 혜택이 통째로 비어버린다.

원본은 data/raw_cache/moyo/(page_N.html, detail_{id}.html)에 저장하고 `--parse-only`로 재파싱.
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
    cache_dir, interim_path,
)

HEADERS = {"User-Agent": "Mozilla/5.0"}
# 통신 3사 자체 브랜드명 -> host_mno 표기. parse_card_only의 "OOO망" 매칭과
# parse_card의 브랜드(<title>의 "[OOO]") 판별에 공통으로 쓴다.
HOST_BRAND_MAP = {"KT": "KT", "SKT": "SKT", "LG U+": "LGU+"}
BASE = "https://www.moyoplan.com"
LIST_URL = BASE + "/plans?page={page}"
DETAIL_URL = BASE + "/plans/{plan_id}"
CACHE_DIR = cache_dir("moyo")


# 한 요청에 허용할 **총** 시간과 재시도 횟수.
#
# urlopen(timeout=N)의 N은 "총 소요 시간"이 아니라 **소켓 작업 하나당** 제한이다.
# 그래서 서버가 데이터를 조금씩 흘려보내면 타임아웃이 영영 걸리지 않는다.
# 실측(2026-08-01 갱신)에서 timeout=20인데 한 건이 **269초** 걸렸고, 이런 정체
# 106건이 상세 수집 34분 중 31분(89%)을 먹었다. 정작 중앙값은 0.4초였다.
#
# 정체는 특정 시간대에 몰리지 않고 산발적이라(서버가 우리를 제한하는 게 아니다)
# 끊고 다시 요청하면 대개 바로 성공한다. 그래서 짧게 자르고 재시도한다.
# 6초는 중앙값의 15배라 정상 응답이 잘릴 일은 없다.
REQUEST_DEADLINE_SEC = 6.0
REQUEST_ATTEMPTS = 3
# 소켓 작업 하나당 제한. deadline보다 짧게 둬야 초과분이 작아진다 - 경과 시간
# 확인은 덩어리 사이에서만 할 수 있어서, 한 번 받는 데 오래 걸리면 그만큼
# deadline을 넘겨서야 멈춘다. 정상 응답은 0.4초(중앙값)라 3초면 넉넉하다.
SOCKET_TIMEOUT_SEC = 3.0


def _get_once(url: str, deadline: float) -> str:
    """deadline초를 넘기면 TimeoutError. 받는 도중에도 경과 시간을 확인한다."""
    started = time.monotonic()
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=SOCKET_TIMEOUT_SEC) as resp:
        chunks = []
        while True:
            # 소켓 timeout만으로는 "느리지만 끊기지는 않는" 응답을 못 자른다.
            # 덩어리를 받을 때마다 총 경과 시간을 직접 확인한다.
            if time.monotonic() - started > deadline:
                raise TimeoutError(f"{deadline}초 초과")
            # read(n)이 아니라 read1(n)이어야 한다. read(n)은 n바이트가 다
            # 모일 때까지 내부에서 계속 기다려서, 서버가 찔끔찔끔 보내면
            # 위 경과 시간 확인이 실행될 기회조차 없다(=이 함수를 만든 이유가
            # 무의미해진다). read1은 한 번 받은 만큼 바로 돌려준다.
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


_KRW_RE = re.compile(r"([\d,.]+)\s*(만|천)?\s*원")
_KRW_UNIT = {None: 1, "천": 1_000, "만": 10_000}


def _parse_krw(text: str):
    """사은품 이름에서 금액을 뽑는다. '19.2만원' -> 192000, '5천원' -> 5000.

    사은품명은 대체로 "총액(월별 분할)" 형태다.
        "네이버페이 19.2만원(매월 3.2만원씩)"   -> 192,000
        "프레딧 쿠폰 2만원(5천원*4개월)"        ->  20,000
    그래서 **금액이 여러 개 나오면 가장 큰 값**을 쓴다.

    예전에는 "만원 -> 천원 -> 원" 순으로 첫 매치를 썼다. 지금 데이터에서는
    총액이 앞에 오기 때문에 결과가 같지만, 사이트가 "매월 3.2만원씩(총 19.2만원)"
    처럼 어순을 바꾸면 조용히 분할 금액을 총액으로 기록하게 된다.
    등장 순서에 의존하지 않게 바꿔 둔다.
    """
    amounts = []
    for m in _KRW_RE.finditer(text or ""):
        try:
            value = float(m.group(1).replace(",", "").rstrip("."))
        except ValueError:
            continue
        amounts.append(int(value * _KRW_UNIT[m.group(2)]))
    return max(amounts) if amounts else None


# "네이버페이 매달 2만원 페이백 (6개월)"처럼 **한 달치 금액**만 적힌 사은품 링크가
# 있다. 그대로 _parse_krw에 넘기면 한 달치(2만원)가 이 혜택의 전체 가치로 잡혀서
# 6개월이면 실제 가치(12만원)의 6분의 1로 저평가된다. "매달 …씩 N개월" 구조를
# 알아보면 총액(1회분 x 개월수)으로 바로잡는다. "평생"처럼 개월 수가 없으면
# 총액을 낼 수 없으므로 한 달치 금액만 남긴다(과대평가보다는 낫다).
_RECURRING_PAYBACK_RE = re.compile(r"매달\s*([\d,.]+\s*(?:만|천)?\s*원)\s*(?:씩)?\s*페이백.*?(\d+)\s*개월")


def _recurring_payback_won(label: str):
    m = _RECURRING_PAYBACK_RE.search(label or "")
    if not m:
        return None
    per_month = _parse_krw(m.group(1))
    if per_month is None:
        return None
    return per_month * int(m.group(2))


# ---------------- 수집 ----------------

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


# 모요 서버는 같은 URL에 대해 두 가지 응답을 준다. 완전한 응답은 "기본 제공 초과 시"
# 표까지 펼쳐서 주는데, 불완전한 응답은 "기타 비용" 같은 탭 라벨만 있고 그 안의 내용이
# 비어 있다. 캐시 2,267개 중 977개가 이 상태였고(라이브로 다시 받으면 표가 나온다)
# 그만큼 초과요금을 통째로 놓치고 있었다. 탭 라벨만 있고 표가 없으면 덜 받은 것으로
# 보고 다시 받는다. 표도 탭 라벨도 없는 페이지(331개)는 모요 비제휴/타사 안내
# 페이지("신청 페이지로 이동")라서 원래 해당 섹션이 없다 - 재수집 대상이 아니다.
def _detail_incomplete(html: str) -> bool:
    return "기타 비용" in html and "기본 제공 초과 시" not in html


def fetch_details(plan_ids: list[str], retries: int = 2, force_ids: set[str] | None = None):
    """상세 페이지 수집. 이미 받아둔 건 기본적으로 건너뛴다.

    force_ids에 든 요금제는 캐시가 멀쩡해도 다시 받는다. 목록 카드의 값
    (요금·데이터·음성 등)이 바뀐 요금제는 상세 내용도 바뀌었을 가능성이 높기
    때문이다. 2,200여 건을 매번 다시 받지 않으려고 이렇게 선별한다.
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

    # 목록에서 사라진 요금제의 상세 캐시는 지운다(단종). 안 지우면 캐시가
    # 계속 쌓이고, 나중에 "왜 목록에 없는 파일이 있지" 하고 헷갈린다.
    for pid in removed:
        path = os.path.join(CACHE_DIR, f"detail_{pid}.html")
        if os.path.exists(path):
            os.remove(path)

    with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(current, f, ensure_ascii=False, indent=1)


# ---------------- 상세 페이지 파싱 (브랜드 / 사은품 / 페이백) ----------------

GIFT_LABEL_SUFFIX = " 상세 페이지 새 탭 열기"


def parse_support_services(soup) -> dict:
    """
    상세페이지 "지원 / 미지원" 섹션을 읽는다.

        지원    인터넷 결합
                모바일 핫스팟   월 60GB 이용 가능
                소액 결제
                해외 로밍       통신사 문의
        미지원  없음

    목록 카드에는 테더링 정보가 아예 없어서, 핫스팟 용량은 여기서만 얻을 수 있다.
    (예: 너겟49는 LG U+ 원본에도 "테더링+쉐어링 60GB"로 나오는데 모요 카드에는 없다.)

    "월 60GB 이용 가능"은 용량이 있는 경우, "데이터 제공량 내 이용 가능"은
    별도 할당 없이 기본 데이터에서 차감하는 경우다. 후자는 값을 비워 둔다.

    클래스명이 Tailwind 해시라 클래스로 찾으면 배포마다 깨진다. "지원"이라고
    적힌 라벨을 찾아 그 컨테이너의 **텍스트**에서 뽑는다. 항목명("모바일 핫스팟")과
    부가설명("월 60GB 이용 가능")이 서로 다른 깊이의 요소에 들어있어서, 특정
    요소를 집으려 하면 둘 중 하나만 잡힌다.
    """
    label = next((e for e in soup.find_all("span") if e.get_text(strip=True) == "지원"), None)
    if label is None or label.parent is None:
        return {}

    section = re.sub(r"\s+", " ", label.parent.get_text(" ", strip=True))
    m = _HOTSPOT_RE.search(section)
    if not m:
        return {}
    note = m.group(1)
    return {"tethering_gb": None if is_non_benefit_share(note) else to_gb(note)}


# "모바일 핫스팟" 뒤부터 다음 항목명 전까지가 그 항목의 설명이다.
_HOTSPOT_RE = re.compile(
    r"핫스팟\s*(.*?)\s*(?:소액|해외\s*로밍|데이터\s*쉐어링|인터넷\s*결합|미지원|$)"
)


# "데이터 제공량 내 이용 가능" = 별도 할당 없음. schema의 is_non_benefit는
# "기본"이 붙은 표현만 보므로 모요 문구를 여기서 따로 처리한다.
def is_non_benefit_share(text: str) -> bool:
    return bool(re.search(r"제공량\s*내|기본\s*데이터\s*내", text or ""))


def parse_addon_voice(soup) -> str:
    """
    상세페이지 "부가통화" 한 줄 항목에서 추가 제공 분수를 뽑는다.

        부가통화 300분
        부가통화 제공 안 함
        부가통화 통신사 문의
        부가통화 통화 제공량 내 이용 가능

    표기가 네 갈래인데, 숫자(분)가 있는 경우만 실제로 추가 제공량이다.
    나머지 셋("제공 안 함"/"통신사 문의"/"제공량 내 이용")은 전부 "추가로 주는
    게 없거나 확인 불가"라는 뜻이라 빈 값으로 둔다. 예를 들어 LG U+ 너겟49는
    원본에도 "+부가통화 300분"이 있는데, 이 항목을 안 읽으면 같은 요금제라도
    LG U+ 직영 쪽에만 값이 있고 모요(MVNO 등록)는 빈 채로 남아 두 소스가
    어긋나 보인다.
    """
    label = next((e for e in soup.find_all("span") if e.get_text(strip=True) == "부가통화"), None)
    if label is None or label.parent is None or label.parent.parent is None:
        return ""
    row = re.sub(r"\s+", " ", label.parent.parent.get_text(" ", strip=True))
    m = re.search(r"(\d+)\s*분", row)
    return m.group(1) if m else ""


def parse_signup_notice(soup) -> str:
    """상세페이지 "꼭 확인해 주세요" 배너 텍스트(가입 제한/비제휴 등 경고문).

    없는 요금제가 다수(약 73%)라 있는 것만 채운다. 배너 하나에 메시지가
    여러 개(비제휴 + 번호이동조건 등) 붙는 경우가 있어 ' | '로 모아 담는다.
    """
    heading = next((s for s in soup.find_all("span") if s.get_text(strip=True) == "꼭 확인해 주세요"), None)
    if heading is None or heading.parent is None:
        return ""
    # 헤딩 바로 아래 메시지 span은 감싸는 span 안에 다시 span이 중첩된 구조라
    # find_all("span")로 그냥 훑으면 같은 문구가 두 번 잡힌다. span 자식이
    # 없는(더 쪼갤 게 없는) span만 진짜 메시지다.
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

    for a in soup.select('a[href^="/gift-group/"]'):
        label = (a.get("aria-label") or "").strip()
        if label.endswith(GIFT_LABEL_SUFFIX):
            label = label[: -len(GIFT_LABEL_SUFFIX)].strip()
        if not label:
            continue
        detail = " ".join(s.get_text(" ", strip=True) for s in a.select("span") if "대상:" in s.get_text() or "시기:" in s.get_text())
        benefits.append(make_benefit_row(
            plan_id, "", plan_name, "사은품/페이백", label,
            value_won=_recurring_payback_won(label) or _parse_krw(label) or "",
            detail=detail, source_url=url,
        ))

    support = parse_support_services(soup)
    support["voice_extra_minutes"] = parse_addon_voice(soup)
    return brand, benefits, support, signup_notice


# ---------------- 목록 카드 파싱 (스펙) ----------------

def parse_card_only(a_tag) -> dict | None:
    """목록 카드 하나에서 **상세 페이지 없이 알 수 있는 값**만 뽑는다.

    parse_card()와 갱신용 지문(list_snapshot) 계산이 이 함수를 공유한다.
    상세 페이지를 열지 않고도 "이 요금제 값이 바뀌었나"를 판단할 수 있어야
    모요 상세 2,200여 건을 매번 다시 받지 않을 수 있다.
    """
    href = a_tag.get("href", "")
    id_m = re.search(r"/plans/(\d+)", href)
    if not id_m:
        return None
    plan_id = id_m.group(1)

    # get_text('\n')로 뽑으면 "월 8,100원"이 태그 경계마다 쪼개져서 줄이 갈라진다.
    # 한 줄로 합친 뒤 정규식으로 구간을 잘라내는 편이 안정적이다.
    card_text = re.sub(r"\s+", " ", a_tag.get_text(" ", strip=True))

    # 데이터 표기가 "월 100GB"(월간 총량) 말고도 "매일 5GB"(일 단위, 월 총량 없이
    # 이것만 있는 카드), 라벨 없는 "무제한", "데이터 제공안함"(0GB 통화전용) 세
    # 가지가 더 있다 - 원래는 "월 …"만 인식해서 이 3가지 카드가 통째로 안 읽혔다
    # (29062 "데일리 너겟59" 등 95/2528건, 2026-08-06 발견).
    # 바로 뒤 "통화"가 온다는 조건(lookahead)으로 "무제한일 5GB"처럼 이름 안에
    # "무제한"이 들어간 경우와 헷갈리지 않게 막는다.
    name_m = re.match(
        r"^(?:\d\.\d\s+)?(.+?)\s+(?:(?:월|매일)\s+(?:무제한|[\d.]+\s*(?:GB|MB))"
        r"|데이터\s*제공안함|무제한(?=\s+통화))",
        card_text,
    )
    if not name_m:
        return None
    plan_name = name_m.group(1).strip()

    # 요금·기간은 **이름 뒤 구간에서만** 찾는다.
    # 요금제 이름에 금액이 박힌 것들이 있어서(예: "무제한 7GB+1M(다이소 매월 5000원)")
    # 카드 전체에서 "월 N원"을 찾으면 이름 속 "매월 5000원"이 먼저 걸린다.
    # 실제로 그 요금제는 21,560원인데 5,000원으로 기록돼 있었다.
    spec_text = card_text[name_m.end(1):]

    data_m = re.search(
        r"((?:월|매일)\s+(?:무제한|[\d.]+\s*(?:GB|MB)).*?|데이터\s*제공안함|무제한)\s+통화",
        card_text,
    )
    data_text = data_m.group(1) if data_m else ""
    data_unlimited = "무제한" in data_text
    # "매일 5GB"만 있고 "월 …" 총량이 안 붙은 카드는 월 총량이 존재하지 않는
    # 거라 data_gb를 비워야 한다. to_gb()는 텍스트 안 첫 "N GB"를 그대로 집어서
    # 여기서 걸러주지 않으면 일일 재충전량(5)을 월 총량인 것처럼 채워버린다.
    is_daily_only = data_text.startswith("매일")
    qos = re.search(r"([\d.]+)\s*(Kbps|Mbps)", data_text, re.I)
    daily = re.search(r"매일\s*([\d.]+)\s*GB", data_text)

    voice_m = re.search(r"통화\s*(무제한|[\d,]+분)", card_text)
    voice_text = voice_m.group(1) if voice_m else ""
    voice_unlimited = voice_text == "무제한"

    sms_m = re.search(r"문자\s*(무제한|[\d,]+건)", card_text)
    sms_text = sms_m.group(1) if sms_m else ""
    sms_unlimited = sms_text == "무제한"

    # \S+망 처럼 느슨하게 잡으면 "5G망" 같은 문구까지 통신사로 잘못 잡혀서 3사만 매칭
    net_m = re.search(r"(KT|SKT|LG\s*U\+)\s*망", card_text)
    host_mno = HOST_BRAND_MAP.get(net_m.group(1), net_m.group(1)) if net_m else ""
    gen_m = re.search(r"\b(5G|LTE|3G)\b", card_text)

    # 모요 카드의 프로모션 표기가 두 종류다. 둘 다 "할인가 + 정가 + 기간"을
    # 담고 있는데, 예전엔 (A)만 처리해서 (B)는 **할인가를 정가로 오인**했다
    # (너겟49: 혜택가 7,000원을 월정액으로 기록, 실제 정가는 49,000원).
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
    # 예전엔 "페이백 포함"일 때만 discount_type을 채워서, 프로모션 할인가가
    # 있는데도 할인 종류가 빈 행이 대부분이었다.
    #
    # ⚠️ discounted_fee를 실제 청구액으로 바꾸는 시도를 했다가 되돌렸다(2026-08-03).
    # "페이백 포함 월 X원 N개월 이후 Y원"에서 실제로 지금 청구되는 금액이
    # X도 Y도 아닌 **제3의 값**인 경우가 있었다(plan 30954: 카드엔 12,800원/
    # 7개월 이후 43,400원이라고 나오지만, 상세페이지 "월 납부액"은 17,800원 -
    # Y는 "체감가 기간이 끝난 뒤의 미래 가격"이지 "지금 청구액"이 아니었다).
    # X를 Y로 바꾸면 이런 플랜의 **진짜 임시 할인**(17,800원이 정가보다 낮다는
    # 사실) 정보가 통째로 사라진다. 상세페이지 "월 납부액"을 긁어와야 정확한데
    # 그건 별도 필드 수집이 필요해 아직 안 했다 - 그때까진 X(카드에 적힌 값)를
    # 그대로 쓴다. X도 정확한 청구액이 아닌 경우가 있다는 걸 알지만(마케팅
    # 체감가), 최소한 Y로 덮어써서 생기는 새로운 오류보다는 낫다.
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
        "voice_minutes": "" if voice_unlimited else (to_won(voice_text) or ""),
        "sms_unlimited": sms_unlimited,
        "sms_count": "" if sms_unlimited else (to_won(sms_text) or ""),
        "monthly_fee": regular_fee,        # 프로모션 종료 후 정상가(없으면 현재가가 정가)
        "discounted_fee": promo_fee,        # 프로모션 적용된 현재 월 납부액
        "discount_type": discount_type,
        "discount_period_months": discount_period,
        "source_url": DETAIL_URL.format(plan_id=plan_id),
    }


# 갱신 판단에 쓸 지문. 목록 카드에서 나오는 값만 넣는다(상세 페이지 값은 제외).
# 이 값들이 그대로면 상세 페이지도 안 바뀌었다고 보고 재수집을 건너뛴다.
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

    # 브랜드가 "KT"/"SKT"/"LG U+" 자체면 알뜰폰이 아니라 통신 3사 자체
    # 온라인전용 요금제(너겟/요고 등)를 모요가 그대로 중개하는 것이다.
    # carrier_type을 MVNO로 두면 "알뜰폰"으로 잘못 취급된다.
    is_mno_brand = brand in HOST_BRAND_MAP
    plan = {
        **card,
        "carrier_type": "MNO" if is_mno_brand else card["carrier_type"],
        "mvno_brand": "" if is_mno_brand else brand,
        "signup_notice": signup_notice,
        # 모요 목록 카드에는 테더링 정보가 없다. 상세페이지 "지원" 섹션의
        # "모바일 핫스팟 월 60GB" 에서 가져온다.
        "tethering_gb": support.get("tethering_gb") or "",
        # 상세페이지 "부가통화" 항목에서 가져온다(목록 카드엔 없음).
        "voice_extra_minutes": support.get("voice_extra_minutes") or "",
        "crawled_at": now,
    }
    plan.update(summarize_benefits(benefits))
    return plan, benefits


def parse_all() -> tuple[list[dict], list[dict]]:
    now = datetime.now(timezone.utc).isoformat()
    plans, benefits, seen = [], [], set()
    files = sorted(
        (f for f in os.listdir(CACHE_DIR) if f.startswith("page_")),
        key=lambda f: int(re.search(r"\d+", f).group()),
    )
    for fname in files:
        with open(os.path.join(CACHE_DIR, fname), encoding="utf-8") as f:
            soup = BeautifulSoup(f.read(), "html.parser")
        for a in soup.find_all("a", href=re.compile(r"^/plans/\d+$")):
            # parse_card()가 내부에서 parse_detail()로 상세 페이지 파일 I/O까지
            # 하기 때문에, 이미 처리한 plan_id면 parse_card 호출 자체를 건너뛴다.
            plan_id_m = re.search(r"/plans/(\d+)", a.get("href", ""))
            if plan_id_m and plan_id_m.group(1) in seen:
                continue
            plan, plan_benefits = parse_card(a, now)
            if plan and plan["plan_id"] not in seen:
                seen.add(plan["plan_id"])
                plans.append(plan)
                benefits.extend(plan_benefits)
    return plans, benefits


if __name__ == "__main__":
    if "--parse-only" not in sys.argv:
        fetch_all()
    plan_rows, benefit_rows = parse_all()
    write_plans(plan_rows, interim_path("moyo_plans.csv"))
    write_benefits(benefit_rows, interim_path("moyo_benefits.csv"))
