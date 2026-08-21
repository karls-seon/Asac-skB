"""LG U+ 요금제 + 혜택 크롤러.

lguplus.com은 Vue(Nuxt) SPA라 목록 페이지는 브라우저 없이는 카드가 안 보이고,
혜택(OTT)은 별도 JSON API로 내려온다. 그래서 셋을 나눠 쓴다.

1. 목록에서 요금제 링크 수집  -> Selenium (여기만 브라우저 필요)
2. 요금제 상세 스펙           -> Selenium 렌더링본 + BeautifulSoup
3. 요금제 혜택(OTT/구독)      -> requests + JSON
   GET /uhdc/fo/prdv/mblppexhi/v2/premium-benefit/{planCode}
   - baseAmount            = 구독 정가
   - customerPaymentAmount = 가입자가 실제로 더 내는 금액 (0이면 완전 무료)
   - 이 목록이 곧 "프리미엄플러스 (택1)" 선택지라 전부 is_selectable=True다.
   ※ 상세 HTML에는 이 목록이 없어서, HTML만 긁으면 혜택이 통째로 빈다.

수집 범위: 통합 탭 + 서브탭 8종(키즈~복지, LTE) + 온라인 전용(너겟).
태블릿/스마트워치, 듀얼넘버 플러스 탭은 보조회선 상품이라 제외.

원본은 data/raw_cache/lguplus/에 저장하고 `--parse-only`로 재파싱한다.
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from bs4 import BeautifulSoup

from schema import (
    write_plans, write_benefits, summarize_benefits, expand_select_variants,
    to_gb, to_won, extract_speed, make_benefit_row, agreement_discount, is_non_benefit,
    classify_benefit_name, normalize_age_condition, cache_dir, interim_path,
)

BASE = "https://www.lguplus.com"
UNIFIED_LIST_URL = f"{BASE}/mobile/plan/mplan/5g-all"
DIRECT_LIST_URL = f"{BASE}/mobile/plan/mplan/direct"
AGE_TAB_LIST_URL = f"{BASE}/mobile/plan/mplan/plan-all?tab={{tab}}"
BENEFIT_API = f"{BASE}/uhdc/fo/prdv/mblppexhi/v2/premium-benefit/{{code}}"
CACHE_DIR = cache_dir("lguplus")
JSON_HEADERS = {"user-agent": "Mozilla/5.0", "accept": "application/json"}

# "통합" 탭 안의 연령/자격별 서브탭 (?tab=NN). 브라우저로 하나씩 눌러 확인한 값.
AGE_TABS = {
    "01": ("키즈", "만 4세 ~ 13세 미만"),
    "02": ("청소년", "만 13세 ~ 19세 미만"),
    "03": ("청년(유쓰)", "만 19세 ~ 35세 미만"),
    "04": ("시니어", "만 65세 이상"),
    "05": ("외국인(Global)", "외국인"),
    "06": ("외국인 청년(Uth)", "외국인, 만 19세 ~ 35세 미만"),
    "07": ("복지", "복지카드 소지 시"),
    # LTE 전용 4종. 다른 탭에는 안 나온다. 연령/자격이 요금제마다 달라서
    # (군인·키즈·시니어·일반) 탭 단위로 못 정하고 상세 페이지에서 읽는다.
    "08": ("LTE", ""),
}


def _cache(name: str) -> str:
    return os.path.join(CACHE_DIR, name)


def _new_driver():
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,2400")
    return webdriver.Chrome(options=opts)


def _collect_links(driver, url: str, selector: str) -> list[str]:
    driver.get(url)
    try:
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.CSS_SELECTOR, selector)))
    except Exception:
        return []
    hrefs = [a.get_attribute("href") for a in driver.find_elements(By.CSS_SELECTOR, selector)]
    return sorted({h for h in hrefs if h and not h.rstrip("/").endswith(("plan-all", "5g-all", "direct"))})


def _fetch_detail_and_benefit(driver, url: str, prefix: str):
    """상세 HTML(브라우저 렌더링본) + 혜택 JSON을 코드 기준으로 캐시에 저장.

    requests로 받으면 같은 URL인데도 스펙 패널(.plan-list__item)이 빠진 변형이
    내려올 때가 있다. 가격/이름은 나오는데 스펙만 비어서 조용히 잘못된 데이터가
    쌓이므로 상세 페이지는 브라우저 렌더링본을 쓴다.
    """
    code = url.rstrip("/").rsplit("/", 1)[-1]
    driver.get(url)
    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "li.plan-list__item"))
        )
    except Exception:
        pass  # 스펙 패널이 원래 없는 요금제도 있어서 실패해도 계속 진행
    with open(_cache(f"{prefix}_{code}.html"), "w", encoding="utf-8") as f:
        f.write(driver.page_source)

    try:
        b = requests.get(BENEFIT_API.format(code=code), headers={**JSON_HEADERS, "referer": url}, timeout=15)
        b.raise_for_status()
        with open(_cache(f"benefit_{code}.json"), "w", encoding="utf-8") as f:
            f.write(b.text)
    except Exception as e:
        print(f"    {code} 혜택 API 실패: {e}")


def expand_direct_list(driver):
    driver.get(DIRECT_LIST_URL)
    WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.CSS_SELECTOR, ".plan-list")))
    for _ in range(10):
        buttons = driver.find_elements(By.XPATH, '//*[contains(text(),"더보기")]')
        if not buttons:
            break
        try:
            driver.execute_script("arguments[0].click();", buttons[0])
            time.sleep(0.6)
        except Exception:
            break


def collect_direct_codes(driver) -> dict:
    """너겟 카드의 '상세보기' 버튼을 눌러 이름 -> {code, url} 매핑을 만든다.

    버튼이 <a href>가 아니라 <button>이라 클릭 후 이동한 URL을 읽어야 한다(헤더가
    버튼을 가려서 일반 click()은 가로채이므로 JS click). **URL 전체를 저장한다** -
    코드만 남기고 `/direct/nerget/{code}`로 재구성하면, 경로가 다른 너겟 상품에서
    엉뚱한 목록 페이지가 저장돼 이름·요금이 통째로 빈다.
    """
    found = {}
    expand_direct_list(driver)
    count = len(driver.find_elements(By.CSS_SELECTOR, ".plan-list > li"))
    for i in range(count):
        expand_direct_list(driver)
        cards = driver.find_elements(By.CSS_SELECTOR, ".plan-list > li")
        if i >= len(cards):
            break
        btn = cards[i].find_element(By.CSS_SELECTOR, ".btn-plan")
        name = btn.text.strip()
        try:
            driver.execute_script("arguments[0].click();", btn)
            WebDriverWait(driver, 10).until(
                lambda d: "/direct/" in d.current_url and d.current_url.rstrip("/") != DIRECT_LIST_URL
            )
            url = driver.current_url.rstrip("/")
            found[name] = {"code": url.rsplit("/", 1)[-1], "url": url}
        except Exception as e:
            print(f"  {name}: 코드 확보 실패 ({type(e).__name__})")
    return found


def _direct_entries() -> dict:
    """direct_codes.json을 {이름: {code, url}}로 읽는다. 예전 형식({이름: 코드})의
    캐시도 그대로 읽는다."""
    path = _cache("direct_codes.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    out = {}
    for name, value in raw.items():
        if isinstance(value, dict):
            out[name] = value
        else:
            out[name] = {"code": value, "url": f"{BASE}/mobile/plan/mplan/direct/nerget/{value}"}
    return out


def fetch_all():
    os.makedirs(CACHE_DIR, exist_ok=True)
    driver = _new_driver()
    try:
        links = _collect_links(driver, UNIFIED_LIST_URL, 'a[href*="/mobile/plan/mplan/5g-all/"]')
        print(f"통합 요금제 링크: {len(links)}개")

        age_links = {}
        for tab, (label, _cond) in AGE_TABS.items():
            found = _collect_links(driver, AGE_TAB_LIST_URL.format(tab=tab), 'a[href*="/mobile/plan/mplan/"]')
            age_links[tab] = found
            print(f"  {label}(tab={tab}): {len(found)}개")
        with open(_cache("age_links.json"), "w", encoding="utf-8") as f:
            json.dump(age_links, f, ensure_ascii=False, indent=2)

        expand_direct_list(driver)
        with open(_cache("direct_list.html"), "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        print("너겟 상품코드 확보 중...")
        name_to_code = collect_direct_codes(driver)
        with open(_cache("direct_codes.json"), "w", encoding="utf-8") as f:
            json.dump(name_to_code, f, ensure_ascii=False, indent=2)
        print(f"  {len(name_to_code)}개 확보")

        # 목록 카드에는 "+다 쓰면 최대 5Mbps"가 없어서 카드만 보면 소진 후 속도가
        # 통째로 빠진다. 너겟도 상세 페이지를 같이 받아 둔다.
        print("너겟 상세 수집 중...")
        for entry in name_to_code.values():
            _fetch_detail_and_benefit(driver, entry["url"], "direct")

        print("상세/혜택 수집 중...")
        total = len(links) + sum(len(v) for v in age_links.values())
        done = 0
        for url in links:
            _fetch_detail_and_benefit(driver, url, "unified")
            done += 1
        for tab, urls in age_links.items():
            for url in urls:
                _fetch_detail_and_benefit(driver, url, f"age_{tab}")
                done += 1
                if done % 20 == 0:
                    print(f"  {done}/{total}")
    finally:
        driver.quit()


def _benefits_from_api(code: str, plan_name: str, source_url: str) -> list[dict]:
    """premium-benefit API 응답 -> 혜택 행들. 이 목록 전체가 '택1' 선택지다."""
    path = _cache(f"benefit_{code}.json")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        items = json.load(f)
    if not isinstance(items, list):
        return []

    rows = []
    for it in items:
        name = (it.get("valueAddedServiceNm") or "").strip()
        if not name:
            continue
        base = it.get("baseAmount")
        pay = it.get("customerPaymentAmount")
        # "프리미엄플러스(택1)" 안에는 OTT뿐 아니라 디바이스·홈 IoT·지역상권
        # 할인까지 섞여 있어서, 전부 OTT로 뭉치면 안 된다.
        rows.append(make_benefit_row(
            code, "LGU+", plan_name, classify_benefit_name(name), name,
            value_won=base if base is not None else "",
            pay_won=pay if pay is not None else "",
            selectable=True, select_group="프리미엄플러스(택1)",
            detail=(
                f"정가 {base:,}원 중 월 {pay:,}원 부담" if isinstance(base, int) and isinstance(pay, int) else ""
            ),
            source_url=source_url,
        ))
    return rows


def _benefit_row(code, plan_name, source_url, category, name, detail=""):
    return make_benefit_row(code, "LGU+", plan_name, category, name, detail=detail, source_url=source_url)


def _extra_minutes(text: str) -> str:
    """'부가통화 100분 추가 제공'처럼 붙는 부가통화 추가분(숫자)만 뽑는다."""
    m = re.search(r"부가통화\s*(\d+)", text or "")
    return m.group(1) if m else ""


# "공유 데이터" 칸에는 성격이 다른 둘이 같이 들어온다. "테더링+쉐어링 100GB"는
# 내 회선의 한도(tethering_gb)이고 "참 쉬운 가족 데이터 50GB"는 별도 부가서비스라
# 아니다. 둘 다 있는 요금제가 실제로 있다(플러스플랜115).
_TETHERING_RE = re.compile(r"테더링|쉐어링")
_FAMILY_SHARE_RE = re.compile(r"가족")


def _tethering_gb(lines: list[str]):
    """'공유 데이터' 줄들 중 테더링/쉐어링 한도만 골라 GB로 돌려준다.

    칸 전체를 이어붙여 첫 GB를 집으면 사이트가 순서를 바꿀 때 조용히 가족 데이터
    용량이 들어가므로 어떤 줄을 쓸지 명시적으로 고른다.

    "테더링+쉐어링 기본 제공량 내 60GB"도 값을 쓴다. 추가 제공은 아니지만 기본
    제공량 안에서 테더링으로 쓸 수 있는 한도라 이 컬럼이 담을 값이 맞다.
    """
    for line in lines:
        if _FAMILY_SHARE_RE.search(line):
            continue
        if _TETHERING_RE.search(line):
            gb = to_gb(line)
            if gb is not None:
                return gb
    return None


# LGU+는 "무제한"과 "기본제공"을 섞어 쓰고 띄어쓰기도 제각각이라("문자 기본 제공")
# 한쪽만 보면 조용히 False가 된다.
_UNLIMITED_RE = re.compile(r"무제한|기본\s*제공")


def _voice_spec(voice_feature: str) -> tuple[bool, str]:
    """음성 feature에서 (무제한 여부, 제공 분수)를 뽑는다.

    LGU+ 음성 표기는 "<기본> +<덤>" 꼴이고 `+` 뒤는 본 제공량이 아니다.
      "집/이동전화 기본제공 +부가통화 300분"  -> 300은 voice_extra_minutes 쪽
      "60분 +지정번호 2개(망내) 음성통화 무제한" -> 무제한은 지정번호에만 적용
    그래서 `+` 앞부분만 본다.
    """
    main = re.split(r"\+", voice_feature or "")[0]
    if _UNLIMITED_RE.search(main):
        return True, ""
    m = re.search(r"([\d,]+)\s*분", main)
    return False, m.group(1).replace(",", "") if m else ""


# LTE 탭은 자격이 제각각이라(군인·키즈·시니어·일반) 상세 페이지에서 직접 읽는다.
# 제목("LTE 키즈 22(만 12세 이하)")이나 본문에 적혀 있고, 현역병사는 나이가 아니라
# 복무 상태 조건이다.
_PAGE_AGE_RE = re.compile(r"만\s*(\d+)\s*세\s*(이상|이하|미만)")


def page_age_condition(page_html: str, plan_name: str, category: str) -> str:
    """상세 페이지에서 가입 자격을 뽑는다. 없으면 빈 문자열.

    **LTE 탭에서만 쓴다.** 다른 탭은 탭 정의가 이미 자격을 알려주고, 본문을 훑으면
    유의사항의 일반 안내 문구까지 자격으로 잡는다(통합·너겟 95개가 오염됐었다).
    """
    if not category.endswith("-LTE"):
        return ""
    if "현역병사" in plan_name:
        return "현역병사"
    m = _PAGE_AGE_RE.search(plan_name) or _PAGE_AGE_RE.search(page_html)
    return f"만 {m.group(1)}세 {m.group(2)}" if m else ""


def _sms_spec(sms_feature: str) -> tuple[bool, str]:
    """문자 feature에서 (무제한 여부, 제공 건수)를 뽑는다."""
    if _UNLIMITED_RE.search(sms_feature or ""):
        return True, ""
    m = re.search(r"([\d,]+)\s*건", sms_feature or "")
    return False, m.group(1).replace(",", "") if m else ""


# "연령/복지/외국인 맞춤 혜택"처럼 무엇을 주는지 안 적힌 안내 문구. 통합 탭에
# 붙는데 실제 혜택은 연령별 페이지에 구체적으로 나온다.
_GENERIC_CUSTOM_RE = re.compile(r"[가-힣/,·\s]*맞춤\s*혜택")

# 너겟75/69에는 "프리미엄플러스 (택1)" 말고 두 번째 택1 그룹("데일리플러스 5개 중
# 택1")이 더 있다. premium-benefit API에는 없고 상세 페이지에만 있어서 요금제당
# 5개씩 놓치고 있었다. 항목이 <li>로 끊겨 있어 그대로 선택지로 쓴다.
_DAILY_PLUS_RE = re.compile(r"데일리플러스.*택\s*1")


def _daily_text(el) -> str:
    # 페이지에 zero-width space가 섞여 있어 그냥 쓰면 이름이 붙어 나온다
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True).replace("​", "")).strip()


def _daily_plus_rows(soup, code: str, name: str, url: str) -> list[dict]:
    for head in soup.select("div.mobile-plan-module__head"):
        if not _DAILY_PLUS_RE.search(_daily_text(head)):
            continue
        return [
            make_benefit_row(
                code, "LGU+", name, classify_benefit_name(opt), opt,
                selectable=True, select_group="데일리플러스(택1)", source_url=url)
            for opt in map(_daily_text, head.parent.select("li")) if opt
        ]
    return []


def parse_detail(html: str, code: str, url: str, now: str, category: str, age_condition: str):
    soup = BeautifulSoup(html, "html.parser")

    title = soup.select_one("h2.plan-title")
    name = title.get_text(strip=True) if title else code

    fee_el = soup.select_one("strong.price-summary")
    base_fee = to_won(fee_el.get_text(" ", strip=True)) if fee_el else None
    disc_el = soup.select_one(".price-discount__price")
    agreement_fee = to_won(disc_el.get_text()) if disc_el else None

    features = [li.get_text(strip=True) for li in soup.select("ul.plan-features li.plan-features__item")]
    network_gen = next((t for t in features if t in ("5G", "LTE", "3G")), "5G")
    # 음성 항목에 "전화"가 없는 요금제가 있다(LTE 시니어·키즈: "100분 +지정번호
    # 3개 음성통화 50분"). 세대·문자 항목을 걸러낸 나머지 중 통화 표기를 쓴다.
    voice_feature = next(
        (t for t in features
         if t not in ("5G", "LTE", "3G") and "문자" not in t
         and re.search(r"전화|통화|\d+\s*분|무제한", t)),
        "")
    sms_feature = next((t for t in features if "문자" in t), "")
    voice_unlimited, voice_minutes = _voice_spec(voice_feature)
    sms_unlimited, sms_count = _sms_spec(sms_feature)

    # 한 줄로 합치면 <br>로 구분된 서로 다른 두 혜택이 하나처럼 보인다. 숫자
    # 추출에는 공백으로 합친 버전을, 혜택 이름에는 줄별 버전을 따로 쓴다.
    info = {}
    info_lines = {}
    for li in soup.select("li.plan-list__item"):
        dt = li.select_one(".plan-list__title")
        dd = li.select_one(".plan-list__text")
        if dt and dd:
            key = dt.get_text(strip=True)
            info[key] = dd.get_text(" ", strip=True)
            info_lines[key] = [line.strip() for line in dd.get_text("\n").split("\n") if line.strip()]

    data_text = info.get("데이터", "")
    share_lines = info_lines.get("공유 데이터", [])

    benefits = _benefits_from_api(code, name, url)
    if info.get("멤버십 혜택"):
        benefits.append(_benefit_row(code, name, url, "멤버십", info["멤버십 혜택"]))
    if info.get("스마트기기"):
        benefits.append(_benefit_row(code, name, url, "스마트기기", info["스마트기기"]))
    # "맞춤형 혜택"은 자격에 따라 더 주는 것이다("국제전화 최대 90분 무료").
    # 통합 탭의 내용 없는 안내 문구는 뺀다.
    benefits.extend(_daily_plus_rows(soup, code, name, url))
    custom = info.get("맞춤형 혜택", "")
    if custom and not _GENERIC_CUSTOM_RE.fullmatch(custom.strip()):
        benefits.append(_benefit_row(
            code, name, url,
            "추가데이터" if "데이터" in custom else "기타", custom))
    for line in share_lines:
        # "기본 제공량 내"류는 별도 제공이 없다는 뜻이라 혜택이 아니다. 뒤에 용량이
        # 붙은 변형도 테더링 한도일 뿐이고 그 값은 tethering_gb에 이미 들어간다.
        if is_non_benefit(line):
            continue
        # 원문이 "테더링+쉐어링 100GB"처럼 이미 무엇인지 말하므로 접두사를 안 붙인다.
        benefits.append(_benefit_row(code, name, url, "추가데이터", line))

    # 너겟은 온라인 전용이고 약정 할인 자체가 없다(사이트 안내문 기준).
    is_nerget = "너겟" in category
    plan = {
        "carrier_type": "MNO",
        "host_mno": "LGU+",
        "mvno_brand": "",
        "plan_id": code,
        "plan_id_type": "official_code",
        "plan_name": name,
        "plan_category": category,
        "is_online_only": is_nerget,
        "age_condition": normalize_age_condition(
            age_condition or page_age_condition(html, name, category)),
        "network_gen": network_gen,
        "data_gb": to_gb(data_text),
        "data_unlimited": "무제한" in data_text,
        "data_throttle_speed": extract_speed(data_text),
        "daily_data_gb": "",
        "tethering_gb": _tethering_gb(share_lines),
        "voice_unlimited": voice_unlimited,
        "voice_minutes": voice_minutes,
        "voice_extra_minutes": _extra_minutes(voice_feature),
        "sms_unlimited": sms_unlimited,
        "sms_count": sms_count,
        "monthly_fee": base_fee,
        "discounted_fee": "" if is_nerget else (agreement_fee or agreement_discount(base_fee)[0]),
        "discount_type": "" if is_nerget else "선택약정 25% 할인",
        "discount_period_months": "",
        "source_url": url,
        "crawled_at": now,
    }
    plan.update(summarize_benefits(benefits))
    return plan, benefits


def parse_direct_card(card, now: str, name_to_code: dict):
    title_sec = card.select_one(".plan-title")
    if not title_sec:
        return None, []
    name_el = title_sec.select_one(".btn-plan")
    if not name_el:
        return None, []
    name = name_el.get_text(strip=True)

    price_el = title_sec.select_one(".plan-price strong")
    fee = to_won(price_el.get_text(strip=True)) if price_el else None
    if fee is None:
        return None, []

    specs = [em.get_text(" ", strip=True) for em in title_sec.select(".plan-info em")]
    data_spec = next((t for t in specs if "데이터" in t), "")
    tether_spec = next((t for t in specs if "테더링" in t), "")

    info = {}
    benefit_sec = card.select_one(".benefit-list")
    if benefit_sec:
        for dt in benefit_sec.select("dt"):
            dd = dt.find_next_sibling("dd")
            if dd:
                info[dt.get_text(strip=True)] = dd.get_text(" ", strip=True)

    entry = name_to_code.get(name) or {}
    code = entry.get("code")
    plan_id = code or name
    url = f"{BASE}/mobile/plan/mplan/direct/nerget/{code}" if code else DIRECT_LIST_URL

    benefits = _benefits_from_api(code, name, url) if code else []
    for key, category in (("멤버십 혜택", "멤버십"), ("스마트기기", "스마트기기")):
        if info.get(key):
            benefits.append(_benefit_row(plan_id, name, url, category, info[key]))
    for key in ("프리미엄플러스", "데일리플러스"):
        # 혜택 API가 비어있는 요금제는 카드에 적힌 요약 문구라도 남긴다
        if info.get(key) and not any(b["benefit_category"] == "OTT/구독" for b in benefits):
            benefits.append(_benefit_row(plan_id, name, url, "OTT/구독", key, info[key]))
    # tether_spec은 이미 "테더링+쉐어링 80GB" 형태라 접두사를 덧붙이지 않는다
    if tether_spec and not is_non_benefit(tether_spec):
        benefits.append(_benefit_row(plan_id, name, url, "추가데이터", tether_spec))
    for b in benefits:
        b["plan_id"] = plan_id

    voice_text = info.get("음성통화", "")
    sms_text = info.get("문자메시지", "")
    voice_unlimited, voice_minutes = _voice_spec(voice_text)
    sms_unlimited, sms_count = _sms_spec(sms_text)

    plan = {
        "carrier_type": "MNO",
        "host_mno": "LGU+",
        "mvno_brand": "",
        "plan_id": plan_id,
        "plan_id_type": "official_code" if code else "name_based",
        "plan_name": name,
        "plan_category": "LGU+-온라인가입전용(너겟)",
        "is_online_only": True,
        "age_condition": "",
        "network_gen": "5G",
        "data_gb": to_gb(data_spec),
        "data_unlimited": "무제한" in data_spec,
        "data_throttle_speed": extract_speed(" ".join(specs)),
        "daily_data_gb": "",
        "tethering_gb": to_gb(tether_spec),
        "voice_unlimited": voice_unlimited,
        "voice_minutes": voice_minutes,
        "voice_extra_minutes": _extra_minutes(voice_text),
        "sms_unlimited": sms_unlimited,
        "sms_count": sms_count,
        "monthly_fee": fee,
        "discounted_fee": "",  # 너겟은 약정 할인 자체가 없음 (사이트 안내문 기준)
        "discount_type": "",
        "discount_period_months": "",
        "source_url": url,
        "crawled_at": now,
    }
    plan.update(summarize_benefits(benefits))
    return plan, benefits


def _looks_like_detail_page(html: str) -> bool:
    """요금제 상세 페이지가 실제로 렌더된 파일인지. 제목과 스펙 패널이 다 있어야 한다."""
    soup = BeautifulSoup(html, "html.parser")
    if soup.select_one("h2.plan-title") is None:
        return False
    return any(
        li.select_one(".plan-list__title") and li.select_one(".plan-list__text")
        for li in soup.select("li.plan-list__item")
    )


def parse_all() -> tuple[list[dict], list[dict]]:
    now = datetime.now(timezone.utc).isoformat()
    plans, benefits = [], []

    age_links = {}
    if os.path.exists(_cache("age_links.json")):
        with open(_cache("age_links.json"), encoding="utf-8") as f:
            age_links = json.load(f)
    code_to_url = {u.rstrip("/").rsplit("/", 1)[-1]: u for urls in age_links.values() for u in urls}

    for fname in sorted(os.listdir(CACHE_DIR)):
        if not fname.endswith(".html") or fname == "direct_list.html":
            continue
        if fname.startswith("unified_"):
            code = fname[len("unified_"):-len(".html")]
            category, age_condition = "LGU+-통합", ""
            url = f"{BASE}/mobile/plan/mplan/5g-all/5g-unlimited/{code}"
        elif fname.startswith("direct_"):
            # 너겟. 상세 페이지 스펙 패널이 목록 카드보다 정확하다.
            code = fname[len("direct_"):-len(".html")]
            category, age_condition = "LGU+-온라인가입전용(너겟)", ""
            url = f"{BASE}/mobile/plan/mplan/direct/nerget/{code}"
        elif fname.startswith("age_"):
            tab, code = fname[len("age_"):-len(".html")].split("_", 1)
            label, age_condition = AGE_TABS.get(tab, (tab, ""))
            category = f"LGU+-{label}"
            url = code_to_url.get(code, AGE_TAB_LIST_URL.format(tab=tab))
        else:
            continue

        with open(_cache(fname), encoding="utf-8") as f:
            html = f.read()
        # 상세 페이지가 제대로 안 내려온 파일은 이름/요금이 통째로 비어서, 그대로
        # 쓰면 목록 카드로 얻던 값보다 나빠진다. 건너뛴다.
        if not _looks_like_detail_page(html):
            continue
        plan, plan_benefits = parse_detail(html, code, url, now, category, age_condition)
        for variant, variant_benefits in expand_select_variants(plan, plan_benefits):
            plans.append(variant)
            benefits.extend(variant_benefits)

    # 너겟 중 상세 페이지가 없는 것만 목록 카드로 보충한다(카드에는 소진 후 속도가
    # 없어 정확도가 떨어지는 폴백이다). base_plan_id는 아직 안 채워졌을 수 있다.
    parsed_codes = {p.get("base_plan_id") or p["plan_id"] for p in plans}
    direct_path = _cache("direct_list.html")
    if os.path.exists(direct_path):
        name_to_code = _direct_entries()
        with open(direct_path, encoding="utf-8") as f:
            soup = BeautifulSoup(f.read(), "html.parser")
        plan_list = soup.select_one(".plan-list")
        for card in (plan_list.find_all("li", recursive=False) if plan_list else []):
            plan, plan_benefits = parse_direct_card(card, now, name_to_code)
            if not plan or plan["plan_id"] in parsed_codes:
                continue
            for variant, variant_benefits in expand_select_variants(plan, plan_benefits):
                plans.append(variant)
                benefits.extend(variant_benefits)

    return plans, benefits


if __name__ == "__main__":
    if "--parse-only" not in sys.argv:
        fetch_all()
    plan_rows, benefit_rows = parse_all()
    write_plans(plan_rows, interim_path("lguplus_plans.csv"))
    write_benefits(benefit_rows, interim_path("lguplus_benefits.csv"))
