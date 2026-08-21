"""KT 요금제 전체 크롤러.

product.kt.com 모바일 요금제 페이지에 탭이 5개 있고 그 밑에 ItemCode가 34개 있다.
탭별 ItemCode 목록은 화면에 안 보이고 브라우저가 아래 AJAX로 채운다.
  GET /wDic/getOptionItemListAjax.ajax?cate_code=6002&pageNo=1&listSize=N&filter_code=F&option_code=O
탭 <-> (filter_code, option_code) 매핑은 각 탭을 눌러보며 확인한 값이라 KT가
사이트 구조를 바꾸면 깨진다.
"""
import json
import os
import re
import sys
import time
import urllib.request

from bs4 import BeautifulSoup

from schema import (
    write_plans, write_benefits, summarize_benefits, expand_select_variants,
    to_gb, to_won, extract_speed, make_benefit_row, agreement_discount, is_non_benefit,
    classify_benefit_name, normalize_age_condition, cache_dir, interim_path,
)

HEADERS = {"User-Agent": "Mozilla/5.0", "X-Requested-With": "XMLHttpRequest"}
CATE_CODE = "6002"
CACHE_DIR = cache_dir("kt")
# (탭 이름, filter_code, option_code). 수집 범위는 휴대폰 요금제다. 안 받는 탭:
#   - 태블릿/워치(189,258) : 보조회선 상품
#   - 기타(190,259)        : 구형 3G·피처폰·선불 모음
#   - 전체(191,260)        : 위 5개 탭의 합집합일 뿐 별도 상품이 없음
TABS = [
    ("통합요금제", 186, 255),
    ("온라인전용(요고)", 187, 256),
    ("키즈/외국인", 188, 257),
]
# 캐시에 예전 범위로 받아둔 ItemCode가 남아 있어도 파싱 단계에서 다시 걸러낸다.
COLLECTED_TABS = {name for name, _f, _o in TABS}


def _get(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="replace")


def discover_item_codes() -> list[tuple[str, str]]:
    """(item_code, 탭이름) 리스트. listSize를 크게 줘서 "더보기" 없이 한 번에 받는다."""
    found = []
    seen = set()
    for tab_name, filter_code, option_code in TABS:
        url = (
            "https://product.kt.com/wDic/getOptionItemListAjax.ajax"
            f"?cate_code={CATE_CODE}&pageNo=1&listSize=100&filter_code={filter_code}&option_code={option_code}"
        )
        html = _get(url)
        codes = list(dict.fromkeys(re.findall(r"ItemCode=(\d+)", html)))
        for code in codes:
            if code not in seen:
                seen.add(code)
                found.append((code, tab_name))
    return found


def fetch_combined_html(item_code: str) -> str:
    """상품 상세 페이지 + (있으면) JS가 가리키는 조각 HTML을 이어붙여서 반환."""
    list_url = f"https://product.kt.com/wDic/productDetail.do?ItemCode={item_code}"
    raw_html = _get(list_url)

    urls = []
    disptype_m = re.search(r'item_disptype\s*=\s*"([A-Za-z])"', raw_html)
    disptype = disptype_m.group(1) if disptype_m else None
    forte_path_m = re.search(r"var\s+forte_path\s*=\s*'([^']*)'", raw_html)
    forte_file_m = re.search(r"var\s+forte_File\s*=\s*'([^']*)'", raw_html)
    accordion_path_m = re.search(r"var\s+accordion_path\s*=\s*'([^']*)'", raw_html)
    accordion_file_m = re.search(r"var\s+accordion_File\s*=\s*'([^']*)'", raw_html)
    if forte_file_m and forte_file_m.group(1):
        urls.append(f"https://product.kt.com{forte_path_m.group(1)}{forte_file_m.group(1)}")
    if accordion_file_m and accordion_file_m.group(1):
        urls.append(f"https://product.kt.com{accordion_path_m.group(1)}{accordion_file_m.group(1)}")
    if disptype == "D":
        upload_m = re.search(r"htmlUploadType_\d+\.html", raw_html)
        if upload_m:
            urls.append(f"https://product.kt.com/static/prodetail/{item_code}/web/{upload_m.group(0)}")

    combined_html = raw_html
    for u in urls:
        try:
            combined_html += "\n" + _get(u)
        except Exception:
            pass

    # 2026-08 개편으로 연령별 요금표(tableHTML)가 페이지 <script>에서 외부 JS
    # 파일로 빠졌다. 안 받아오면 그 상품의 요금제가 통째로 0건이 된다(초이스 등
    # 4개가 이렇게 사라졌었다. docs/수정이력.md 35번). 파일명이 상품마다 다르므로
    # `js/data/*.js` 경로 패턴으로 찾는다. **앞서 붙인 조각까지 포함한**
    # combined_html에서 찾아야 한다 - 이 script 태그가 htmlUploadType 조각 안에
    # 들어있는 경우가 있다.
    for src in dict.fromkeys(re.findall(r'<script[^>]+src="([^"]*?/js/data/[^"]+\.js)"', combined_html)):
        try:
            combined_html += "\n" + _get(f"https://product.kt.com{src}")
        except Exception:
            pass
    return combined_html


# 셀 안의 <li> 경계를 표시하는 구분자. " / "로 이어붙이면 선택지 이름 자체에
# "/"가 든 경우("티빙/지니/밀리"는 KT 원문에서도 하나의 상품)를 다시 쪼갤 때
# 옵션 9개가 11개로 부풀고 존재하지 않는 "티빙" 단독 상품이 생긴다.
# HTML 텍스트에 나올 수 없는 제어문자로 경계를 보존한다.
LI_SEP = "\x1f"


def _disp(text: str) -> str:
    """CSV로 내보내거나 사람이 읽을 텍스트에서 <li> 구분자를 " / "로 되돌린다."""
    return (text or "").replace(LI_SEP, " / ")


def extract_tablehtml_by_age(page_html: str) -> dict:
    # 캐시 전수 조사로 확인한 연령 키 4개(y/school/65/75) + base.
    # KT가 새 연령군을 추가하면 여기도 늘려야 한다.
    pattern = re.compile(r"""(base|y|school|'65'|'75')\s*:\s*\{.*?tableHTML\s*:\s*'([^']*)'""", re.DOTALL)
    return {key.strip("'"): html_str for key, html_str in pattern.findall(page_html)}


def parse_html_table(table_html: str):
    soup = BeautifulSoup(table_html.replace("<br>", "\n").replace("<br/>", "\n"), "html.parser")
    table = soup.find("table")
    if table is None:
        return [], []

    def build_grid(section_rows):
        grid, span_map = [], {}
        for r_idx, tr in enumerate(section_rows):
            row, col_idx, ci = [], 0, 0
            cells = tr.find_all(["th", "td"])
            while True:
                while (r_idx, col_idx) in span_map:
                    row.append(span_map[(r_idx, col_idx)])
                    col_idx += 1
                if ci >= len(cells):
                    break
                cell = cells[ci]
                lis = cell.find_all("li")
                text = LI_SEP.join(li.get_text(" ", strip=True) for li in lis) if lis else cell.get_text(" ", strip=True)
                colspan = int(cell.get("colspan", 1))
                rowspan = int(cell.get("rowspan", 1))
                for c in range(colspan):
                    row.append(text)
                    if rowspan > 1:
                        for rr in range(1, rowspan):
                            span_map[(r_idx + rr, col_idx + c)] = text
                    col_idx += 1
                ci += 1
            grid.append(row)
        return grid

    thead, tbody = table.find("thead"), table.find("tbody")
    header_rows = build_grid(thead.find_all("tr")) if thead else []
    body_rows = build_grid(tbody.find_all("tr")) if tbody else []
    return header_rows, body_rows


def flatten_headers(header_rows):
    if not header_rows:
        return []
    # 헤더는 컬럼 키로 쓰이므로 <li> 구분자를 남기면 안 된다
    header_rows = [[_disp(v) for v in row] for row in header_rows]
    n_cols = max(len(r) for r in header_rows)
    cols = []
    for c in range(n_cols):
        # 이 컬럼 자신의 상/하위 라벨끼리만 중복 제거한다. 다른 컬럼의 확정된
        # 이름과 비교하면, 앞쪽에 단독 "음성" 컬럼이 있을 때 "65+덤 혜택_음성"의
        # 하위 라벨이 조용히 사라진다(음성18.7의 65+/75+덤 보너스가 유실됐었다).
        parts = [r[c] for r in header_rows if c < len(r) and r[c]]
        cols.append("_".join(dict.fromkeys(parts)) if parts else f"col{c}")
    return cols


def table_to_rows(table_html: str) -> list[dict]:
    header_rows, body_rows = parse_html_table(table_html)
    columns = flatten_headers(header_rows)
    if not columns and body_rows:
        columns = [f"col{i}" for i in range(len(body_rows[0]))]
    out = []
    for r in body_rows:
        row = r + [""] * (len(columns) - len(r))
        out.append(dict(zip(columns, row[: len(columns)])))
    return out


def get_plan_rows_by_age(page_html: str) -> dict:
    by_age = extract_tablehtml_by_age(page_html)
    if by_age:
        return {age: table_to_rows(html) for age, html in by_age.items()}

    soup = BeautifulSoup(page_html, "html.parser")
    # 페이지마다 요금제 표의 class명이 다르다. 못 찾으면 "월정액" 헤더가 있는
    # <table>로 대체한다.
    static_table = (
        soup.find("table", class_="N-pdt-tbl-plan")
        or soup.find("table", class_="pduct-tbl-plan")
        or soup.find("table", class_="table-plan")
    )
    if static_table is None:
        for table in soup.find_all("table"):
            if "월정액" in table.get_text():
                static_table = table
                break
    if static_table:
        return {"base": table_to_rows(str(static_table))}
    return {}


# 음성/문자 컬럼 헤더가 표마다 제각각이다. 확인된 것만:
#   "음성" / "문자" / "음성/문자" / "음성/문자 (영상/부가)"
#   "기본 제공_음성" / "문자_기본 제공" / "제공(월)" / "제공량(월)"
# 정확히 "음성"/"문자"만 보면 원문에 "200분/200건"이 적힌 요금제가 통째로 빈다.
#
# "기본제공"은 무제한이지만 "기본 제공량 초과 사용 시"의 "기본 제공량"은 아니다
# (손말 요금제가 이것 때문에 무제한으로 오판정됐다).
_UNLIMITED_RE = re.compile(r"무제한|기본\s*제공(?!량)")

_PROVISION_HEADERS = ("제공(월)", "제공량(월)", "제공량", "제공(월", "음성/문자")


def _voice_sms_col(row: dict, kind: str) -> str:
    """음성 또는 문자 제공량이 적힌 칸을 찾는다. kind는 "음성" 또는 "문자"."""
    # 1) 정확히 그 이름인 컬럼
    if row.get(kind):
        return row[kind]
    # 2) 이름이 섞인 컬럼. "국내 음성 통화료(1초)_평상"처럼 요율 컬럼도 "음성"을
    #    포함하므로 걸러야 한다(안 그러면 제공량 대신 "2.75원"을 집는다).
    for key, val in row.items():
        if kind in key and not re.search(r"혜택|통화료|요금|요율", key):
            if val:
                return val
    # 3) 음성·문자가 한 칸에 같이 적힌 표("제공(월)", "음성/문자" 등)
    for header in _PROVISION_HEADERS:
        value = _find_col(row, header)
        if value:
            return value
    return ""


def _parse_voice(text: str) -> tuple[bool, int | None, int | None]:
    """(무제한 여부, 음성 제공 분수, 영상/부가 추가 분수).

    "집/이동전화 무제한 (+ 영상/부가 200분)"처럼 무제한인데 뒤에 부가통화 분수가
    붙는 경우가 있다. 이걸 못 가리면 뒤의 200분을 음성 제공량으로 잡는다.
    """
    text = text or ""
    # "영상/부가" 이후는 별도 컬럼(voice_extra_minutes)이라 본 제공량과 섞으면 안 된다.
    split = re.split(r"영상\s*/?\s*부가|부가\s*통화", text, maxsplit=1)
    main = split[0]
    tail = split[1] if len(split) > 1 else ""

    m = re.search(r"(\d+)\s*분", tail)
    voice_extra = int(m.group(1)) if m else None

    is_unlimited = bool(_UNLIMITED_RE.search(main))
    if is_unlimited:
        # 키즈처럼 "기본제공 (300분)"이면 괄호 안 분수가 영상/부가 제공량이다
        if voice_extra is None:
            m = re.search(r"\(\s*(\d+)\s*분", text)
            voice_extra = int(m.group(1)) if m else None
        return True, None, voice_extra

    m = re.search(r"([\d,]+)\s*분", main)
    return False, int(m.group(1).replace(",", "")) if m else None, voice_extra


def _parse_sms(text: str) -> tuple[bool, int | None]:
    """(무제한 여부, 문자 제공 건수).

    "8,250알 (… 825건 상당)"처럼 알(포인트) 단위로 주고 건수를 괄호에 병기하는
    요금제가 있다. 알은 건수가 아니므로 건수 표기가 있을 때만 쓴다.
    """
    text = text or ""
    if _UNLIMITED_RE.search(text):
        return True, None
    m = re.search(r"([\d,]+)\s*건", text)
    return False, int(m.group(1).replace(",", "")) if m else None


def _find_col(row: dict, must_contain: str, must_not_contain: str = None) -> str:
    for key, val in row.items():
        if must_contain in key and (must_not_contain is None or must_not_contain not in key):
            return val or ""
    return ""


# 표 헤더 키워드 -> 통합 혜택 분류
BENEFIT_COLUMN_RULES = [
    ("초이스", "OTT/구독"),
    ("플러스", "OTT/구독"),
    ("멤버십", "멤버십"),
    ("단말보험", "기타"),
    ("스마트기기", "스마트기기"),
    ("쉐어링", "스마트기기"),
    ("공유데이터", "추가데이터"),
    ("덤", "추가데이터"),
]


def _benefit_category(header: str) -> str:
    for keyword, category in BENEFIT_COLUMN_RULES:
        if keyword in header:
            return category
    return "기타"


def parse_benefit_rows(row: dict, plan_id: str, plan_name: str, source_url: str) -> list[dict]:
    """KT는 혜택이 표의 "제공 혜택_*" 컬럼들로 흩어져 있다. "초이스 (택1)" 같은
    컬럼은 셀 안에 <li>로 선택지가 나열돼 있어 <li> 하나 = 선택지 하나로 쪼갠다.

    "택1"이 아닌데 <li>가 여러 개인 컬럼도 있다("초이스 더블"은 헤더가 그냥
    "초이스 혜택"이고 <li> 2개를 둘 다 제공한다). 그래서 선택형 판정은 헤더의
    "택1" 표기에만 의존한다.
    """
    rows = []
    for header, value in row.items():
        # 대부분 헤더에 "혜택"이 들어있지만 웰컴(1577)의 기간 한정 데이터 보너스는
        # 헤더가 "출시 프로모션(25.5.1~26.7.31)"이다. 캐시 전수 조사로 "프로모션"만
        # 있는 헤더 중 진짜 값이 있는 건 이 경우뿐임을 확인했다.
        if "혜택" not in header and "프로모션" not in header:
            continue
        value = (value or "").strip()
        if not value or value == "-" or is_non_benefit(value):
            continue

        header_clean = re.sub(r"\s+", " ", header.replace("제공 혜택_", "")).strip()
        # 아래 각주 제거가 "(택1)"의 "1)"까지 건드리므로 택1 여부를 **먼저** 읽는다.
        is_select = "택1" in header_clean or "택 1" in header_clean
        # 헤더에 각주 번호가 붙어 있다("플러스 1) (택1)"). 그대로 두면 "초이스 (택1)"과
        # 형태가 달라 그룹명으로 못 묶는다. 앞에 공백이 있는 번호만 각주로 본다.
        header_clean = re.sub(r"\s\d+\)", "", header_clean).strip()
        category = _benefit_category(header_clean)

        options = [o.strip() for o in value.split(LI_SEP) if o.strip()]

        if len(options) >= 2:
            # 카테고리는 옵션 이름으로 다시 정한다. 한 그룹 안에 성격이 다른 게
            # 섞여 있다(초이스 = 넷플릭스/폰케어/삼성). 헤더 category는 폴백이다.
            for opt in options:
                option_name = re.sub(r"\s*\d+\)\s*$", "", _disp(opt)).strip()
                rows.append(make_benefit_row(
                    plan_id, "KT", plan_name,
                    classify_benefit_name(option_name, category), option_name,
                    selectable=is_select,
                    select_group=header_clean if is_select else "",
                    detail=_disp(value), source_url=source_url,
                ))
        else:
            value = _disp(value)
            # 값만 넣으면 문맥이 사라진다("단말보험" 헤더 + "최대 4,500원 할인").
            # 값 안에 헤더명이 이미 있으면 그대로, 아니면 헤더를 앞에 붙인다.
            if header_clean and header_clean not in value:
                name = f"{header_clean} {value}"
            else:
                name = value
            rows.append(make_benefit_row(
                plan_id, "KT", plan_name, category,
                name if len(name) <= 60 else header_clean,
                detail=f"{header_clean}: {value}", source_url=source_url,
            ))
    return rows


# "키즈/외국인" 탭 상품은 일반 성인이 가입할 수 없는데, KT는 이 조건을 본문이
# 아니라 **이미지 alt와 <meta>에만** 적어 둔다. get_text()로 본문만 훑으면 놓친다.
_AGE_LIMIT_RE = re.compile(r"만\s*\d+\s*세\s*(?:이하|미만)")


def page_age_condition(page_html: str, tab_name: str, plan_name: str) -> str:
    """상세 페이지에서 가입 연령/자격 조건을 뽑는다. 없으면 빈 문자열."""
    if not tab_name.startswith("키즈/외국인"):
        return ""
    # 웰컴(외국인 전용)은 나이 제한이 없고 국적 조건만 있다
    if "웰컴" in plan_name:
        return "외국인"
    m = _AGE_LIMIT_RE.search(page_html)
    return re.sub(r"\s+", " ", m.group(0)).strip() if m else "키즈"


def _guess_network_gen(plan_name: str) -> str:
    """이름에 세대가 적혀 있을 때만 그 값을 쓰고, 없으면 **비운다**.

    표기가 없다고 5G로 보면 안 된다 - KT 상세페이지에는 5G/LTE 표기가 아예 없고
    수집 탭 이름부터 "통합요금제"다. 5G로 못박으면 LTE 사용자를 걸러낼 때 MNO
    요금제가 통째로 사라진다. 빈값은 "모름"이 아니라 **"세대 구분 없음"**이다.

    부분문자열로 찾으면 "데이터투게더 3GB"의 "3GB"가 3G로 걸리므로 뒤에 다른
    글자가 오면 제외한다.
    """
    if re.search(r"3G(?![A-Za-z0-9])", plan_name):
        return "3G"
    if re.search(r"LTE(?![A-Za-z0-9])", plan_name):
        return "LTE"
    return ""


def page_title_name(page_html: str) -> str:
    """<title>에서 요금제명만 뽑는다. ("신 표준 | 모바일 요금제 | KT닷컴" -> "신 표준")

    구형 3G/피처폰 페이지는 표에 요금제명 컬럼 자체가 없어서 여기서만 이름을 얻는다.
    """
    m = re.search(r"<title>([^<]*)</title>", page_html, re.I)
    if not m:
        return ""
    return m.group(1).split("|")[0].strip()


def table_row_to_unified(
    item_code: str, tab_name: str, row: dict, age_condition: str = "",
    fallback_name: str = "", page_html: str = "",
) -> tuple[dict, list[dict]] | None:
    # 요금제명 컬럼 헤더가 표마다 다르다(확인된 것만 5종). 부분일치로 넓게 잡되,
    # "요금제 월정액(원/월)"처럼 헤더가 합쳐진 표에서 금액 칸을 이름으로 집지
    # 않도록 정확 일치를 먼저 시도한다.
    plan_name = _disp(
        row.get("요금제")
        or row.get("구분")
        or row.get("요금제명")
        or row.get("상세요금제")
        or row.get("상품명")
        or _find_col(row, "요금제", must_not_contain="월정액")
        or _find_col(row, "상품명")
        or ""
    ).strip() or fallback_name or f"KT-{item_code}"
    # 헤더가 "요금제 월정액(원/월)"처럼 합쳐진 페이지가 있어 부분일치로 한 번 더
    # 찾는다. to_won이 첫 금액만 집는 게 중요하다 - 월정액 칸에 안내가 덧붙는
    # 경우가 있다("15,400원 (사회복지할인 35% 적용시 10,010원)").
    fee = to_won(row.get("월정액") or _find_col(row, "월정액") or "")
    if fee is None:
        return None  # 요금 정보가 없는 행(안내문 등)은 스킵

    # 요고 표는 "데이터" 밑에 "기본 제공"/"요고Y덤" 서브컬럼이 있어 "데이터_기본
    # 제공"으로 갈라진다. must_not_contain="덤"으로 나이대 보너스와 구분한다.
    data_text = row.get("데이터") or _find_col(row, "데이터", must_not_contain="덤")
    # 소진 후 속도가 별도 컬럼에 있는 표가 있다("데이터_기본제공 초과 시").
    overflow_text = _find_col(row, "초과")
    is_unlimited = "무제한" in data_text
    data_gb = None if is_unlimited else to_gb(data_text)
    roaming_text = _find_col(row, "로밍")
    tethering_gb = to_gb(_find_col(row, "공유데이터", must_not_contain="덤"))

    voice_text = _voice_sms_col(row, "음성")
    sms_text = _voice_sms_col(row, "문자")
    is_voice_unlimited, voice_minutes, voice_extra_minutes = _parse_voice(voice_text)
    is_sms_unlimited, sms_count = _parse_sms(sms_text)

    id_suffix = f"_{age_condition}" if age_condition else ""
    plan_id = f"{item_code}_{re.sub(r'\\s+', '', plan_name)}{id_suffix}"
    source_url = f"https://product.kt.com/wDic/productDetail.do?ItemCode={item_code}"
    benefits = parse_benefit_rows(row, plan_id, plan_name, source_url)

    # 요고 표는 나이대 보너스를 별도 표로 안 나누고 같은 행의 "데이터_요고Y덤"
    # 컬럼에 "기본 데이터 2배"라고만 적는다. 헤더에 "혜택"이 없어 parse_benefit_rows가
    # 지나치므로 여기서 챙긴다. 배율만 있고 절대량이 없어 혜택 행으로만 남긴다.
    ydum_value = _find_col(row, "요고Y덤")
    if ydum_value and ydum_value.strip() not in ("", "-"):
        benefits.append(make_benefit_row(
            plan_id, "KT", plan_name, "추가데이터",
            f"요고Y덤(만 34세 이하) {_disp(ydum_value).strip()}",
            detail="요고Y덤 혜택: 자세히보기", source_url=source_url,
        ))

    plan = {
        "carrier_type": "MNO",
        "host_mno": "KT",
        "mvno_brand": "",
        "plan_id": plan_id,
        "plan_id_type": "official_code",
        "plan_name": plan_name,
        "plan_category": f"KT-{tab_name}",
        "is_online_only": tab_name.startswith("온라인전용"),
        # 연령 덤 행이면 그 라벨을, 키즈/외국인 전용 상품이면 페이지에 적힌 조건을
        # 넣는다. 후자는 변형이 아니라 상품 자체의 조건이라 plan_id에는 안 붙인다.
        "age_condition": normalize_age_condition(
            age_condition or page_age_condition(page_html, tab_name, plan_name)),
        "network_gen": _guess_network_gen(plan_name),
        "base_data_gb": data_gb,
        "extra_data_gb": "",
        "data_gb": data_gb,
        "data_unlimited": is_unlimited,
        "data_throttle_speed": (extract_speed(data_text) or extract_speed(overflow_text)
                                or extract_speed(roaming_text)),
        "daily_data_gb": "",
        "base_tethering_gb": tethering_gb,
        "extra_tethering_gb": "",
        "tethering_gb": tethering_gb,
        "voice_unlimited": is_voice_unlimited,
        "voice_minutes": voice_minutes,
        "voice_extra_minutes": voice_extra_minutes,
        "sms_unlimited": is_sms_unlimited,
        "sms_count": sms_count,
        "monthly_fee": fee,
        "discount_period_months": "",
        "source_url": source_url,
        "crawled_at": "",
    }
    # 온라인전용(요고)은 무약정이라 선택약정 할인 대상이 아니다. LGU+ 너겟도 같다.
    if plan["is_online_only"]:
        plan["discounted_fee"], plan["discount_type"] = "", ""
    else:
        plan["discounted_fee"], plan["discount_type"] = agreement_discount(fee)
    plan.update(summarize_benefits(benefits))
    return plan, benefits


# 나이대 키 -> age_condition 텍스트. KT 원문의 자체 브랜드명("Y덤(만 19세~만 34세)")을
# 3사 공통 표기로 맞춘다 - 안 맞추면 같은 대상인데 값이 달라져 나이로 못 거른다.
# 이 라벨은 plan_id 접미사에도 쓰인다.
AGE_TIER_LABELS = {
    "y": "만 34세 이하",        # 원문 "Y덤(만 19세 ~ 만 34세)"
    "school": "만 18세 이하",    # 원문 "스쿨덤(~ 만 18세)"
    "65": "만 65세 이상",
    "75": "만 75세 이상",
}


def _dum_target(col_key: str, value: str) -> str | None:
    """'덤' 컬럼이 공유데이터(테더링)용 보너스인지 기본데이터용 보너스인지 판정.
    "공유데이터"가 붙으면 테더링, 그냥 "데이터"/"기본데이터"만 있으면 기본데이터."""
    blob = f"{col_key} {value}"
    if "공유데이터" in blob:
        return "tethering"
    if "데이터" in blob:
        return "data"
    return None


def _dum_subcategory(key: str) -> str:
    """"덤" 컬럼이 전부 데이터 보너스인 건 아니다. 65+/75+덤은 "_음성/문자",
    "_영상/부가" 서브 컬럼도 같이 오는데 이건 통화 혜택이라 구분해야 한다.
    """
    if "음성" in key or "문자" in key:
        return "voice_sms"
    if "영상" in key or "부가" in key:
        return "voice_extra"
    if "추가혜택" in key or "추가 혜택" in key:
        return "misc"
    return "data"


def dedupe_plan_id(plan: dict, benefits: list[dict], seen: dict) -> None:
    """plan_id는 혜택 테이블과 조인하는 기본키라 중복되면 안 된다.

    "데이터투게더"처럼 같은 이름/월정액 행이 옵션만 달리해 여러 번 나오는 표가
    있다. 그런 경우에만 뒤에 일련번호를 붙여(#2, #3) 기존 id를 안 건드린다.
    """
    pid = plan["plan_id"]
    if pid not in seen:
        seen[pid] = 1
        return
    seen[pid] += 1
    new_pid = f"{pid}#{seen[pid]}"
    plan["plan_id"] = new_pid
    for b in benefits:
        b["plan_id"] = new_pid


def age_variant_rows(item_code: str, tab_name: str, rows_by_age: dict,
                     seen_ids: dict | None = None) -> list[dict]:
    """KT는 나이대별 별도 요금제가 아니라 같은 요금제에 "덤" 혜택이 얹히는 방식이다.

    캐시 전수 대조 결과 "덤" 컬럼 값은 전부 "추가되는 양"이고 base에 더하면 총
    제공량이 나온다(베이직100 Y덤 "공유데이터 70GB" + base 70GB = 140GB). 65+/75+
    tier는 사이트의 "총 제공량" 문구가 안 바뀌는 표기 버그가 있어 직접 합산한다.

    "공유데이터" 덤은 tethering_gb에, "데이터" 덤은 data_gb에 더하고, 추가된 양
    자체는 extra_* 컬럼에 참고용으로 남긴다.
    """
    base_rows = rows_by_age.get("base") or []
    base_by_name = {_disp(r.get("요금제") or r.get("구분") or "").strip(): r for r in base_rows}

    extra_plans, extra_benefits = [], []
    for age_key, label in AGE_TIER_LABELS.items():
        for age_row in rows_by_age.get(age_key) or []:
            name = _disp(age_row.get("요금제") or age_row.get("구분") or "").strip()
            base_row = base_by_name.get(name)
            if base_row is None:
                continue

            result = table_row_to_unified(item_code, tab_name, base_row, age_condition=label)
            if result is None:
                continue
            plan, benefits = result
            # 연령 행은 새 상품이 아니라 나이별 변형이라 base_plan_id를 연령 접미사
            # 없는 id로 채운다. 안 채우면 write_plans가 자기 자신으로 채워서
            # "실제 요금제 수 = nunique(base_plan_id)"가 KT만 부풀어 오른다.
            base_result = table_row_to_unified(item_code, tab_name, base_row)
            if base_result is not None:
                plan["base_plan_id"] = base_result[0]["plan_id"]

            extra_data_gb = 0.0
            extra_tethering_gb = 0.0
            for key, value in age_row.items():
                if "덤" not in key:
                    continue
                value = _disp(value).strip()
                if not value or value == "-" or is_non_benefit(value):
                    continue

                subcat = _dum_subcategory(key)
                if subcat == "data":
                    gb = to_gb(value)
                    target = _dum_target(key, value) if gb is not None else None
                    if target == "tethering":
                        extra_tethering_gb += gb
                    elif target == "data":
                        extra_data_gb += gb
                    benefit_category = "추가데이터"
                elif subcat == "voice_sms":
                    # 값이 "무제한"이면 그 나이대는 통화 자체가 무제한으로 바뀐다.
                    # 아니면 "+30분"/"+50건"처럼 더 주는 경우다. "음성"/"문자"가 같은
                    # subcat이라 컬럼명으론 못 가리므로 값 뒤 단위(분/건)로 판단한다.
                    if "무제한" in value:
                        plan["voice_unlimited"] = True
                        plan["voice_minutes"] = ""
                    else:
                        m = re.search(r"(\d+)\s*(분|건)", value)
                        if m:
                            amount, unit = int(m.group(1)), m.group(2)
                            if unit == "분" and not plan["voice_unlimited"]:
                                plan["voice_minutes"] = (plan["voice_minutes"] or 0) + amount
                            elif unit == "건" and not plan["sms_unlimited"]:
                                plan["sms_count"] = (plan["sms_count"] or 0) + amount
                    benefit_category = "기타"
                elif subcat == "voice_extra":
                    m = re.search(r"(\d+)", value)
                    if m:
                        current = plan.get("voice_extra_minutes") or 0
                        plan["voice_extra_minutes"] = current + int(m.group(1))
                    benefit_category = "기타"
                else:
                    benefit_category = "기타"

                benefits.append(make_benefit_row(
                    plan["plan_id"], "KT", name, benefit_category, f"{label} {value}",
                    detail=f"{re.sub(r'\\s+', ' ', key).strip()}: {value}",
                    source_url=plan["source_url"],
                ))

            if extra_tethering_gb:
                plan["extra_tethering_gb"] = extra_tethering_gb
                plan["tethering_gb"] = round((plan["tethering_gb"] or 0) + extra_tethering_gb, 3)
            if extra_data_gb and not plan["data_unlimited"]:
                plan["extra_data_gb"] = extra_data_gb
                plan["data_gb"] = round((plan["data_gb"] or 0) + extra_data_gb, 3)

            plan.update(summarize_benefits(benefits))
            dedupe_plan_id(plan, benefits, seen_ids if seen_ids is not None else {})
            for variant, variant_benefits in expand_select_variants(plan, benefits):
                extra_plans.append(variant)
                extra_benefits.extend(variant_benefits)
    return extra_plans, extra_benefits


def fetch_all():
    """상세 페이지 원본 HTML을 data/raw_cache/kt/에 저장. 파싱은 여기서 안 함."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    item_codes = discover_item_codes()
    print(f"발견한 ItemCode: {len(item_codes)}개")
    with open(os.path.join(CACHE_DIR, "_tabs.json"), "w", encoding="utf-8") as f:
        json.dump(item_codes, f, ensure_ascii=False, indent=2)

    for item_code, tab_name in item_codes:
        try:
            html = fetch_combined_html(item_code)
            with open(os.path.join(CACHE_DIR, f"{item_code}.html"), "w", encoding="utf-8") as f:
                f.write(html)
            print(f"  {item_code} ({tab_name}): 저장 완료")
        except Exception as e:
            print(f"  {item_code} ({tab_name}): 오류 - {e}")
        time.sleep(0.5)


def parse_all() -> tuple[list[dict], list[dict]]:
    """data/raw_cache/kt/의 캐시된 HTML만 읽어서 파싱 (네트워크 요청 없음)."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()

    with open(os.path.join(CACHE_DIR, "_tabs.json"), encoding="utf-8") as f:
        item_codes = json.load(f)

    plan_rows, benefit_rows = [], []
    for item_code, tab_name in item_codes:
        if tab_name not in COLLECTED_TABS:
            continue
        path = os.path.join(CACHE_DIR, f"{item_code}.html")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            html = f.read()

        # 요고(item 1567)에는 표 말고 같은 정보를 보여주는 "가격 계산기" 슬라이더도
        # 있다. 슬라이더만 파싱하면 "요고 모바일 8GB" 같은 가짜 이름이 생기고 진짜
        # 이름·음성/문자·혜택이 유실되므로, 다른 KT 요금제와 같은 표 경로로 합친다.
        rows_by_age = get_plan_rows_by_age(html)
        seen_ids = {}  # 같은 상품 페이지 안에서만 중복을 따지면 된다
        title_name = page_title_name(html)
        for row in rows_by_age.get("base") or []:
            result = table_row_to_unified(item_code, tab_name, row,
                                          fallback_name=title_name, page_html=html)
            if result:
                plan, benefits = result
                plan["crawled_at"] = now
                dedupe_plan_id(plan, benefits, seen_ids)
                for variant, variant_benefits in expand_select_variants(plan, benefits):
                    plan_rows.append(variant)
                    benefit_rows.extend(variant_benefits)

        age_plans, age_benefits = age_variant_rows(item_code, tab_name, rows_by_age,
                                                   seen_ids=seen_ids)
        for plan in age_plans:
            plan["crawled_at"] = now
        plan_rows.extend(age_plans)
        benefit_rows.extend(age_benefits)

    return plan_rows, benefit_rows


if __name__ == "__main__":
    if "--parse-only" not in sys.argv:
        fetch_all()
    plans, benefits = parse_all()
    write_plans(plans, interim_path("kt_plans.csv"))
    write_benefits(benefits, interim_path("kt_benefits.csv"))
