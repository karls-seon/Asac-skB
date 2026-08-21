"""SKT 요금제 + 혜택 크롤러 (전부 공개 JSON API, 셀레니움 불필요).

tworld.co.kr은 화면만 JS로 그리고 데이터는 JSON API로 내려온다. 인증키는 필요
없고 **`referer` 헤더만 있으면** 된다(없으면 401 "invalid request url").

쓰는 API 3종:
1. /core-product/v1/product/mobile/plan-overall-list  - 목록 + 기본 스펙
2. /core-product/v1/ledger/{prodId}                   - 연령 제한 등 속성 태그
3. /core-product/v1/benefits/{prodId}/price-plan      - 혜택 상세

3번이 혜택의 핵심이다. prodBenfFrndExpsPhrs 안에 작은따옴표로 선택형 옵션이
들어있어("T 우주 'YouTube Premium' 또는 …") 옵션마다 별도 혜택 행으로 분해한다.

원본은 data/raw_cache/skt/에 저장하고 파싱은 캐시만 읽는다(`--parse-only`).
"""
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

from schema import (
    write_plans, write_benefits, summarize_benefits, expand_select_variants,
    extract_speed, make_benefit_row, canonical_spelling, OTT_KEYWORDS,
    normalize_age_condition,
    to_gb,
    cache_dir, interim_path,
)

BASE = "https://www.tworld.co.kr"
LIST_API = f"{BASE}/core-product/v1/product/mobile/plan-overall-list"
LEDGER_API = f"{BASE}/core-product/v1/ledger/{{prod_id}}"
BENEFIT_API = f"{BASE}/core-product/v1/benefits/{{prod_id}}/price-plan"
# 상세 페이지의 "요금제 이용 시 유의 사항" 본문. 여기에만 영상/부가통화 초과요금이
# 들어있다("영상/부가통화 제공량 소진 시 부가통화 초당 1.98원, 영상통화 초당 3.3원").
# ledger 응답에는 없어서 별도로 받아야 한다.
CONTENTS_API = f"{BASE}/core-product/v1/ledger/{{prod_id}}/contents"
# 카테고리별 요금제 묶음. 목록 API는 카테고리 구분 없이 평면으로 내려주는데
# 사이트는 2단으로 나눠 보여준다. 그 분류를 따로 받아 plan_category에 채운다.
GROUP_API = f"{BASE}/core-product/v1/submain/grp-prcplns"
LIST_REFERER = f"{BASE}/web/product/plan/list?filters=all"

HEADERS = {
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
    "accept": "application/json, text/javascript, */*; q=0.01",
    "referer": LIST_REFERER,
}
CACHE_DIR = cache_dir("skt")
AGE_FILTER_GROUP = "F01160"  # prodFilterFlagList에서 연령 제한 카테고리
# 망/기기 종류도 같은 prodFilterFlagList에 있다. 목록 API에는 없어서 여기서 읽는다.
NETWORK_FILTER_GROUP = "F01120"
# 이 그룹의 prodFltNm 중 망 세대가 아닌 것들(기기/가입형태 구분)
NON_NETWORK_FLAGS = {"선불폰", "태블릿/스마트 기기"}

# "전체 요금제" 아래 카테고리 (filters API의 F02086 "통합_2026" 하위)
ALL_CATEGORIES = {
    "F02087": "베스트",
    "F02088": "라이트",
    "F02100": "전용",        # 기본/표준·연령특화·복지·3G·외국인·선불폰
    "F02089": "스마트기기",    # 워치·태블릿·함께쓰기/투넘버
    "F02101": "다이렉트",     # T다이렉트샵 전용
}
# 수집 범위는 **휴대폰 요금제**다. 카테고리 단위로 받되 "전용"만은 성격이 다른
# 그룹이 섞여 있어(3G 24 · 선불폰 12 · 연령특화 8 · 복지 4 · 외국인 4 · 기본/표준 3)
# 그룹 단위로 고른다. 3G·선불폰은 종량 과금이고 스마트기기는 보조회선이라 뺀다.
# "다이렉트"는 SKT의 온라인 전용 라인으로, KT 요고·LGU+ 너겟과 짝을 맞추려고 넣는다.
COLLECTED_CATEGORIES = {"베스트", "라이트", "다이렉트"}
COLLECTED_GROUPS = {
    ("전용", "연령특화"),      # ZEM플랜 · 주말엔 팅 · 5G 시니어 · T끼리 어르신
    ("전용", "기본/표준"),     # T플랜 세이브 · 뉴 T끼리 맞춤형 · 표준요금제
}


def _selective_discount(item: dict, fee: int) -> str | int:
    """선택약정 25% 적용가. 할인 대상이 아니면 빈값.

    SKT는 할인 대상이 아닌 요금제에 `selAgrmtAplyMfixAmt = "0"`을 준다. "공짜"가
    아니라 "해당 없음"이다.
    """
    sel = _to_int(item.get("selAgrmtAplyMfixAmt"))
    return sel if sel and fee and sel < fee else ""


def _in_scope(category: str, group: str) -> bool:
    return category in COLLECTED_CATEGORIES or (category, group) in COLLECTED_GROUPS


def _get(url: str, params: dict, referer: str) -> requests.Response:
    resp = requests.get(url, params=params, headers={**HEADERS, "referer": referer}, timeout=15)
    resp.raise_for_status()
    return resp


def fetch_categories():
    """카테고리별 요금제 묶음을 받아 categories.json에 저장."""
    out = {}
    for code, name in ALL_CATEGORIES.items():
        resp = _get(GROUP_API, {"idxCtgCd": code, "size": 100, "page": 1, "order": ""}, LIST_REFERER)
        groups = (resp.json().get("result", {}) or {}).get("groupProdList", []) or []
        for g in groups:
            for p in g.get("prodList", []) or []:
                out[p["prodId"]] = [name, g.get("prodGrpNm", "")]
        print(f"  {name}: 그룹 {len(groups)}개")
        time.sleep(0.3)
    with open(f"{CACHE_DIR}/categories.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"카테고리 매핑: {len(out)}개 요금제")


def fetch_all():
    os.makedirs(CACHE_DIR, exist_ok=True)
    print("카테고리 수집 중...")
    fetch_categories()

    prod_ids = []
    page, total_count = 1, None
    while True:
        resp = _get(LIST_API, {"idxCtgCd": "F01100", "size": 10, "page": page, "order": ""}, LIST_REFERER)
        with open(f"{CACHE_DIR}/page_{page}.json", "w", encoding="utf-8") as f:
            f.write(resp.text)

        data = resp.json()["result"]
        if total_count is None:
            total_count = data["totalCount"]
            print(f"전체 요금제 수: {total_count}, 페이지 수: {(total_count + 9) // 10}")
        items = data.get("mobilePlanList", [])
        prod_ids.extend(i["prodId"] for i in items if i.get("prodId"))
        print(f"  page {page}: {len(items)}개")
        if not items or page * 10 >= total_count:
            break
        page += 1
        time.sleep(0.3)

    print(f"요금제별 상세(ledger/혜택) 수집 중... ({len(prod_ids)}개)")
    for i, pid in enumerate(prod_ids, 1):
        detail_referer = f"{BASE}/web/product/callplan/{pid}"
        for name, url, params in (
            ("ledger", LEDGER_API.format(prod_id=pid), {"prodExpsTypCd": "P"}),
            ("benefit", BENEFIT_API.format(prod_id=pid), {}),
            ("contents", CONTENTS_API.format(prod_id=pid), {}),
        ):
            try:
                resp = _get(url, params, detail_referer)
                with open(f"{CACHE_DIR}/{name}_{pid}.json", "w", encoding="utf-8") as f:
                    f.write(resp.text)
            except Exception as e:
                print(f"  {pid} {name} 실패: {e}")
            time.sleep(0.15)
        if i % 20 == 0:
            print(f"  {i}/{len(prod_ids)}")


def _strip_html(text: str) -> str:
    # 엔티티도 풀어야 한다. 안 풀면 본문의 "티빙&amp;웨이브"와 요금제명
    # "티빙&웨이브"가 달라서 표에서 자기 행을 찾는 매칭이 조용히 실패한다.
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_gb(text: str):
    text = (text or "").strip()
    if not text or text == "무제한":
        return None
    m = re.search(r"[\d.]+", text)
    return float(m.group(0)) if m else None


def _load(prefix: str, prod_id: str):
    path = os.path.join(CACHE_DIR, f"{prefix}_{prod_id}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _contents_text(prod_id: str) -> str:
    """contents API 본문을 사람이 읽는 형태의 한 줄 텍스트로 합친다."""
    payload = _load("contents", prod_id)
    if not payload:
        return ""
    parts = []
    for block in (payload.get("result", {}) or {}).get("contentsList", []) or []:
        parts.append(str(block.get("titleNm") or ""))
        parts.append(_strip_html(str(block.get("ledItmDesc") or "")))
    return re.sub(r"\s+", " ", " ".join(parts))


# SKT는 KT의 "덤"처럼 연령별 표를 따로 주지 않고 상세 페이지 본문에 섞어 놓는다.
# 형태가 두 가지다.
#
#  (요약) "연령에 따라 추가 혜택을 더 받을 수 있어요 / 청소년(만 18세 이하) 기본
#         데이터 1.5GB 추가 제공 / 청년(만 19세~34세) 기본 데이터 1GB 추가 제공 …"
#         -> **라벨별로 값이 다르다.** 라벨을 무시하고 첫 매치를 집으면 청소년 값을
#            청년 값으로 잘못 기록하므로 라벨로 쪼갠 뒤 각각 파싱한다.
#  (표)   "청년 혜택 안내표 …", "기본 제공 데이터 추가 제공 안내표 …"
#         -> 여러 요금제가 함께 나열되므로 **자기 이름의 행**을 찾아야 하고, 표
#            구간을 안 좁히면 다른 표를 잘못 집는다. 청소년·청년 값이 셀 병합으로
#            같아서 우산 라벨 하나로 충분하다.
UMBRELLA_AGE = "만 34세 이하"
# 본문 표기 -> age_condition 값. KT/LGU+와 같은 "만 N세 이하/이상"으로 통일한다.
# "청년(만 19세~34세)"도 상한이 같아 우산 라벨과 같은 값이 된다 - 한 요금제가 둘 다
# 제공량 증가를 갖는 경우는 없어서 행이 겹치지 않는다.
_AGE_LABELS = {
    "청년 혜택(만 34세 이하)": UMBRELLA_AGE,
    "청소년(만 18세 이하)": "만 18세 이하",
    "청년(만 19세~34세)": "만 34세 이하",
    "시니어 혜택(만 65세 이상)": "만 65세 이상",
}
_AGE_BLOCK_RE = re.compile(r"연령에 따라 추가 \S+ 더 받을 수 있어요(.*?)연령에 따라 자동으로")
_AGE_LABEL_RE = re.compile("(%s)" % "|".join(re.escape(k) for k in _AGE_LABELS))
# "기본 데이터 2.5GB 추가 제공" / "공유/테더링 데이터 20GB 추가 제공" / "데이터 350MB 추가제공"
_AGE_DATA_RE = re.compile(r"(공유/테더링\s*데이터|데이터)\s*([\d.]+)\s*(GB|MB)\s*추가\s?제공")
# 표에서 "<요금제명> 250GB + 최대 5Mbps 300GB" 처럼 속도 문구가 끼어들 수 있다.
_QOS_TAIL = r"(?:\s*\+\s*최대\s*[\d.]+\s*[MmKk]bps)?"
_AGE_TABLES = (
    (re.compile(r"청년\s*혜택\s*안내표"), "extra_tethering_gb"),
    (re.compile(r"기본\s*제공\s*데이터\s*추가\s*제공\s*안내표"), "extra_data_gb"),
)
# "커피/영화/로밍 50% 할인"처럼 데이터와 무관한 연령 혜택 문구. 항목별로 찾으면
# 같은 문구를 세 번 잡아 혜택이 3배로 부푸므로 슬래시 목록을 통째로 잡는다.
_AGE_PERK_RE = re.compile(
    r"((?:[가-힣]+/)*(?:커피|영화|로밍)(?:/[가-힣]+)*\s*\d+\s*%\s*할인)")
# 시니어 혜택은 "집전화/이동전화 무제한(영상 200분/부가 300분)"처럼 음성 무제한
# 전환으로 오기도 한다. 분 수는 3단 컬럼이 없어 혜택 행 문구로만 남긴다.
_AGE_VOICE_RE = re.compile(r"(집전화/이동전화\s*무제한(?:\s*\([^)]*\))?)")
# 요금제 자체가 연령 전용인지 판단(age_condition에는 "복지" 같은 비연령 자격도 온다)
_AGE_RESTRICTED_RE = re.compile(r"만\s*\d+\s*세")


def _age_benefits(prod_id: str, plan_name: str) -> list[dict]:
    """요금제의 연령 혜택 목록. 없으면 빈 리스트.

    각 항목의 키
      age_condition       : "청년(만 34세 이하)" / "청소년(만 18세 이하)" /
                            "청년(만 19세~34세)" / "시니어(만 65세 이상)"
      extra_data_gb       : 기본 데이터 추가 제공량(GB)
      extra_tethering_gb  : 공유/테더링 한도 증가분(GB)
      voice_unlimited     : 음성 무제한으로 전환되면 True
      notes               : 제공량으로 환산하지 않은 혜택 문구 목록
    """
    text = _contents_text(prod_id)
    if "청년" not in text and "시니어" not in text:
        return []

    block = _AGE_BLOCK_RE.search(text)
    if block:
        return _parse_age_block(block.group(1))

    # 요약 블록이 없는 요금제는 표에서 자기 행을 찾는다(우산 라벨 하나).
    for start_re, key in _AGE_TABLES:
        m0 = start_re.search(text)
        if not m0:
            continue
        region = text[m0.end(): m0.end() + 3000]
        m = re.search(
            re.escape(plan_name) + r"\s+([\d.]+)\s*(GB|MB)" + _QOS_TAIL
            + r"\s+([\d.]+)\s*(GB|MB)", region)
        if not m:
            continue
        base = to_gb(f"{m.group(1)}{m.group(2)}")
        aged = to_gb(f"{m.group(3)}{m.group(4)}")
        if base is not None and aged is not None and aged > base:
            return [{"age_condition": UMBRELLA_AGE, key: round(aged - base, 3),
                     "notes": _age_perks(text)}]
    return []


def _parse_age_block(block: str) -> list[dict]:
    """요약 블록을 라벨별로 쪼개 연령 혜택 목록으로 만든다."""
    parts = _AGE_LABEL_RE.split(block)
    out = []
    for label, body in zip(parts[1::2], parts[2::2]):
        body = body.replace("알아보기", "")
        item = {"age_condition": _AGE_LABELS[label], "notes": []}
        m = _AGE_DATA_RE.search(body)
        if m:
            gb = to_gb(f"{m.group(2)}{m.group(3)}")
            if gb:
                item["extra_tethering_gb" if "공유" in m.group(1) else "extra_data_gb"] = gb
        voice = _AGE_VOICE_RE.search(body)
        if voice:
            item["voice_unlimited"] = True
            item["notes"].append(re.sub(r"\s+", " ", voice.group(1)).strip())
        item["notes"].extend(_age_perks(body))
        if len(item) > 2 or item["notes"]:
            out.append(item)
    return out


def _age_perks(text: str) -> list[str]:
    perks = []
    for m in _AGE_PERK_RE.finditer(text):
        perk = re.sub(r"\s+", " ", m.group(1)).strip()
        if perk not in perks:
            perks.append(perk)
    return perks


def _filter_flag(prod_id: str, group: str) -> str:
    """ledger의 prodFilterFlagList에서 해당 그룹의 태그명. 없으면 빈 문자열."""
    ledger = _load("ledger", prod_id)
    if not ledger:
        return ""
    for tag in ledger.get("result", {}).get("prodFilterFlagList", []) or []:
        if tag.get("supProdFltId") == group:
            return tag.get("prodFltNm", "")
    return ""


def _age_condition(prod_id: str) -> str:
    name = _filter_flag(prod_id, AGE_FILTER_GROUP)
    return "" if "제한 없음" in name else name


def _load_categories() -> dict:
    """prodId -> (카테고리, 그룹).

    카테고리 API는 그룹의 **대표 상품만** 준다(베스트는 27개 중 10개). 나머지는
    ledger의 repProdId로 같은 상품군에 묶이므로 대표의 카테고리를 상품군 전체에
    퍼뜨린다. 남는 10개는 포켓파이·IoT처럼 사이트 카테고리에 없는 레거시 상품이다.
    """
    path = os.path.join(CACHE_DIR, "categories.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        direct = {k: tuple(v) for k, v in json.load(f).items()}

    # 상품군 대표 -> 카테고리
    by_rep = {}
    for pid, info in direct.items():
        by_rep.setdefault(_rep_prod_id(pid), info)

    resolved = dict(direct)
    for fname in os.listdir(CACHE_DIR):
        if not fname.startswith("ledger_"):
            continue
        pid = fname[len("ledger_"):-len(".json")]
        if pid in resolved:
            continue
        hit = by_rep.get(_rep_prod_id(pid))
        if hit:
            resolved[pid] = hit
    return resolved


def _rep_prod_id(prod_id: str) -> str:
    ledger = _load("ledger", prod_id)
    if not ledger:
        return prod_id
    return (ledger.get("result", {}) or {}).get("repProdId") or prod_id


def _network_gen(prod_id: str) -> str:
    """망 세대(5G/LTE/3G). 목록 API엔 없고 ledger 필터 태그에만 있다.

    F01120 그룹에는 망 세대와 기기·가입형태(태블릿/스마트 기기, 선불폰)가 같은
    층위로 섞여 있다. 후자는 망 세대가 아니므로 비워 둔다.
    """
    flag = _filter_flag(prod_id, NETWORK_FILTER_GROUP)
    return "" if flag in NON_NETWORK_FLAGS else flag


def _classify(name: str, detail: str) -> str:
    blob = f"{name} {detail}"
    if any(k in blob for k in OTT_KEYWORDS):
        return "OTT/구독"
    if "멤버십" in blob:
        return "멤버십"
    if "스마트기기" in blob or "워치" in blob or "태블릿" in blob:
        return "스마트기기"
    if "데이터" in blob and ("추가" in blob or "공유" in blob or "쉐어" in blob):
        return "추가데이터"
    if any(k in blob for k in ("상품권", "페이백", "캐시백", "사은품", "쿠폰")):
        return "사은품/페이백"
    return "기타"


# 상세 문구 안의 '작은따옴표' 로 감싸인 선택형 옵션명
SELECT_OPTION_RE = re.compile(r"[‘’']([^‘’']{2,40})[‘’']")

# name 끝에 붙는, 그 자체로는 정보가 없는 말들. 중복 판정 전에 떼어낸다.
_GENERIC_TAIL_RE = re.compile(r"\s*(?:혜택|서비스)\s*$")


def _merge_name_summary(name: str, summary: str) -> str:
    """prodBenfNm은 "커피"/"로밍"처럼 짧아서 요약 문구("50% 할인")를 붙여야 무슨
    혜택인지 알 수 있다.

    그냥 이어붙이면 겹치는 경우가 문제다("T 우주 Netflix 스탠다드" + "넷플릭스
    스탠다드 제공"). name은 영문, summary는 한글이라 글자로 비교하면 못 알아보므로
    canonical_spelling으로 표기를 통일한 뒤 비교하고 안 겹치는 부분만 덧붙인다.
    """
    if not (name and summary):
        return name or summary

    name_c = canonical_spelling(name)
    summary_c = canonical_spelling(summary)
    # 끝의 "혜택"만 다른 경우를 겹치는 것으로 보려고 떼고 비교한다.
    name_core = _GENERIC_TAIL_RE.sub("", name_c)

    if name_core in summary_c:
        return summary
    if summary_c in name_c:
        return name

    # 일부만 겹치면 summary에서 name에 없는 토큰만 덧붙인다.
    def token_key(word):
        return re.sub(r"\W", "", canonical_spelling(word))

    name_tokens = {token_key(w) for w in name.split()}
    extra = [w for w in summary.split() if token_key(w) not in name_tokens]
    return f"{name} {' '.join(extra)}".strip() if extra else name


def _benefits_for(prod_id: str, plan_name: str, source_url: str) -> list[dict]:
    payload = _load("benefit", prod_id)
    if not payload:
        return []
    areas = payload.get("result") or []

    rows = []
    for area in areas:
        area_title = _strip_html(area.get("prodBenfAreaTitleNm", ""))
        for b in area.get("prodBenfList", []) or []:
            name = (b.get("prodBenfNm") or "").strip()
            detail = (b.get("prodBenfFrndExpsPhrs") or "").strip()
            summary = (b.get("prodBenfExpsPhrs") or "").strip()
            category = _classify(f"{name} {area_title}", detail)

            # "'YouTube Premium' 또는 'YouTube Premium Lite & 배달의민족'" 처럼
            # 선택지가 2개 이상 나오면 각각을 별도 혜택 행으로 분해한다.
            options = [o.strip() for o in SELECT_OPTION_RE.findall(detail)]
            options = [o for o in dict.fromkeys(options) if o]

            if len(options) >= 2 and category == "OTT/구독":
                for opt in options:
                    rows.append(make_benefit_row(
                        prod_id, "SKT", plan_name, category, opt,
                        selectable=True, select_group=name or area_title,
                        detail=detail or summary, source_url=source_url,
                    ))
            else:
                display_name = _merge_name_summary(name, summary) or area_title
                rows.append(make_benefit_row(
                    prod_id, "SKT", plan_name, category, display_name,
                    detail=detail or summary, source_url=source_url,
                ))
    return rows


def parse_all() -> tuple[list[dict], list[dict]]:
    now = datetime.now(timezone.utc).isoformat()
    plan_rows, benefit_rows = [], []
    seen = set()
    categories = _load_categories()

    files = sorted(
        (f for f in os.listdir(CACHE_DIR) if f.startswith("page_")),
        key=lambda f: int(re.search(r"\d+", f).group()),
    )
    for fname in files:
        with open(os.path.join(CACHE_DIR, fname), encoding="utf-8") as f:
            data = json.load(f)

        for item in data["result"].get("mobilePlanList", []):
            pid = item.get("prodId")
            if not pid or pid in seen:
                continue
            seen.add(pid)

            # 목록 API는 카테고리 구분 없이 전부 내려주므로 수집 대상만 남긴다.
            # 매핑이 없는 레거시 상품(포켓파이·IoT)도 여기서 걸러진다.
            category, group = categories.get(pid, ("", ""))
            if not _in_scope(category, group):
                continue

            fee = _to_int(item.get("basFeeInfo"))
            if fee is None:
                # PPS(선불폰)류는 월정액이 아니라 초/건당 과금이라 월정액 비교 대상이 아님
                continue

            plan_name = item.get("prodNm", "")
            source_url = f"{BASE}/web/product/callplan/{pid}"
            my_benefits = _benefits_for(pid, plan_name, source_url)

            data_text = item.get("basOfrGbDataQtyCtt", "")
            is_unlimited = data_text == "무제한"
            # 소용량 요금제는 데이터가 GB 필드가 아니라 MB 필드로만 온다(단위 없는
            # 순수 숫자). "함께쓰기"처럼 숫자가 아닌 값도 섞여 있어 먼저 확인한다.
            mb_text = (item.get("basOfrMbDataQtyCtt") or "").strip()
            data_gb_mb_fallback = round(int(mb_text) / 1024, 3) if mb_text.isdigit() else None
            voice_text = item.get("basOfrVcallTmsCtt", "")
            voice_unlimited = voice_text == "무제한"
            sms_text = item.get("basOfrCharCntCtt", "")
            sms_unlimited = sms_text == "기본제공"
            sel_fee = _selective_discount(item, fee)

            plan = {
                "carrier_type": "MNO",
                "host_mno": "SKT",
                "mvno_brand": "",
                "plan_id": pid,
                "plan_id_type": "official_code",
                "plan_name": plan_name,
                # 그룹명(prodGrpNm)은 안 붙인다. 카테고리 API가 그룹 대표만 줘서
                # 나머지가 대표의 그룹명을 물려받으면 "베스트 Max(넷플릭스)"가
                # "AI + OTT 혜택" 그룹으로 잘못 붙는다. 카테고리 층위까지만 믿는다.
                "plan_category": f"SKT-{category}",
                "is_online_only": "다이렉트" in plan_name,
                "age_condition": normalize_age_condition(_age_condition(pid)),
                "network_gen": _network_gen(pid),
                "data_gb": None if is_unlimited else (_to_gb(data_text) or data_gb_mb_fallback),
                "data_unlimited": is_unlimited,
                "data_throttle_speed": extract_speed(item.get("qosDataQtyCtt", "")),
                "daily_data_gb": "",
                "tethering_gb": _to_gb(item.get("shrDataQtyCtt", "")),
                "voice_unlimited": voice_unlimited,
                "voice_minutes": "" if voice_unlimited else _to_int(re.sub(r"[^\d]", "", voice_text) or "") or "",
                "voice_extra_minutes": item.get("addTcCtt", "") or "",
                "sms_unlimited": sms_unlimited,
                "sms_count": "" if sms_unlimited else _to_int(re.sub(r"[^\d]", "", sms_text) or "") or "",
                "monthly_fee": fee,
                # 온라인전용(다이렉트)은 무약정이라 선택약정 할인 대상이 아닌데 API가
                # `selAgrmtAplyMfixAmt`에 0을 준다. 그대로 쓰면 "0원 요금제" 31개가
                # 생겨 추천 맨 위를 차지한다(_selective_discount가 거른다).
                "discounted_fee": sel_fee,
                "discount_type": "선택약정 25% 할인" if sel_fee else "",
                "discount_period_months": "",
                "source_url": source_url,
                "crawled_at": now,
            }
            ages = _age_benefits(pid, plan_name)
            my_benefits.extend(_age_benefit_rows(ages, pid, plan_name, source_url))

            plan.update(summarize_benefits(my_benefits))
            for variant, variant_benefits in expand_select_variants(plan, my_benefits):
                plan_rows.append(variant)
                benefit_rows.extend(variant_benefits)

            # 연령 혜택으로 **스펙이 실제로 달라지는** 것만 변형 행을 만든다(KT 덤과
            # 같은 방식). 커피/영화 할인만 있으면 같은 스펙 두 줄이 될 뿐이다.
            # 이미 연령 전용인 요금제는 또 나누지 않는다. 단 age_condition에는
            # "복지"처럼 연령 아닌 자격도 오므로("소리누리"는 복지인데 시니어 혜택이
            # 있다) "만 N세"가 적힌 것만 연령 상품으로 본다.
            if _AGE_RESTRICTED_RE.search(plan["age_condition"] or ""):
                continue
            for age in ages:
                if not (age.get("extra_data_gb") or age.get("extra_tethering_gb")
                        or age.get("voice_unlimited")):
                    continue
                for variant, variant_benefits in expand_select_variants(
                    _age_variant(plan, age), my_benefits
                ):
                    plan_rows.append(variant)
                    benefit_rows.extend(
                        dict(b, plan_id=variant["plan_id"]) for b in variant_benefits)

    return plan_rows, benefit_rows


def _age_benefit_rows(ages: list[dict], pid: str, plan_name: str, source_url: str) -> list[dict]:
    """연령 혜택을 혜택 행으로 만든다(제공량 증가 + 음성 전환 + 할인성 혜택)."""
    rows = []
    for age in ages:
        cond = age["age_condition"]
        for key, label in (("extra_data_gb", "기본 데이터"),
                           ("extra_tethering_gb", "공유/테더링 데이터")):
            if age.get(key):
                rows.append(make_benefit_row(
                    pid, "SKT", plan_name, "추가데이터",
                    f"{cond} {label} {age[key]}GB 추가 제공",
                    detail="연령에 따라 자동으로 추가 제공됩니다", source_url=source_url))
        for note in age["notes"]:
            rows.append(make_benefit_row(
                pid, "SKT", plan_name, "기타", f"{cond} {note}",
                detail="연령에 따라 자동으로 제공됩니다", source_url=source_url))
    return rows


def _age_variant(plan: dict, age: dict) -> dict:
    """연령 변형 행. base/extra/총량 3단 컬럼을 KT 덤과 같은 규칙으로 채운다."""
    cond = age["age_condition"]
    variant = dict(
        plan,
        plan_id=f"{plan['plan_id']}_{cond}",
        base_plan_id=plan["plan_id"],
        age_condition=cond,
    )
    for base_key, extra_key, total_key in (
        ("base_data_gb", "extra_data_gb", "data_gb"),
        ("base_tethering_gb", "extra_tethering_gb", "tethering_gb"),
    ):
        extra = age.get(extra_key)
        if not extra:
            continue
        base = plan.get(total_key)
        variant[base_key] = base
        variant[extra_key] = extra
        # 무제한이면 총량 개념이 없어 그대로 둔다
        variant[total_key] = round(base + extra, 3) if isinstance(base, (int, float)) else base
    if age.get("voice_unlimited"):
        variant["voice_unlimited"] = True
        variant["voice_minutes"] = ""
    return variant


if __name__ == "__main__":
    if "--parse-only" not in sys.argv:
        fetch_all()
    plans, benefits = parse_all()
    write_plans(plans, interim_path("skt_plans.csv"))
    write_benefits(benefits, interim_path("skt_benefits.csv"))
