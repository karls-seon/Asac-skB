"""갱신된 요금제가 원문과 맞는지 표본으로 대조한다. **진단만 하고 안 고친다.**

    python src/agents/data_verify.py --demo         # LLM 없이 배선·정규화 점검
    python src/agents/data_verify.py --samples 10   # 실제 대조

## 왜 필요한가

기존 검증은 전부 "개수와 구조"만 본다 - check_guards()는 행 수 급변, schema_drift는
어제 되던 소스가 오늘 0행인지, validate()는 헤더와 조인 무결성. **행 수는 그대로인데
값이 틀린 경우를 보는 코드가 없다.**

CSV만 봐서는 못 잡는다는 걸 실측으로 확인했다. 과거 스냅샷 7개에 필수값 결측 검사와
내부 일관성 검사(정가<=0, 할인가>정가, 무제한인데 GB값 있음 등 7종)를 돌렸더니 **전부
0건**이었다. 같은 파서가 만든 값이라 내부적으로는 항상 일관되기 때문이다 - 파서가
일관되게 틀리면 검사도 통과한다.

## 원칙 (docs/수정이력.md 15번에서 가져옴)

과거에 라이브 표본 220개를 대조해 실제 버그 2건을 찾은 기록이 있다. 거기 남은 원칙:

  "우리가 파싱한 곳과 **다른 경로로** 값을 다시 얻어 비교한다.
   같은 파서로 재파싱하면 항상 일치하니 검증이 안 된다."

여기서는 같은 캐시 원문을 **정규식이 아니라 LLM으로** 읽는 것이 그 다른 경로다.

  "불일치가 나오면 어느 쪽이 틀렸는지 **원문으로 판정하는 절차**가 반드시 필요하다."

그때 불일치의 대부분이 검증 코드 자체의 결함이었다(표본 오염 37건, 콤마 처리 54건,
렌더 대기 23건, 조각 HTML 누락 16건). 그래서 여기서도 **자동 수정하지 않는다.**
리포트만 쓰고 사람이 링크를 열어 판정한다.

## 한계

캐시된 원문을 본다. 캐시 자체가 불완전하게 받아졌으면(모요에서 실제로 있었던 문제)
이 점검으로는 못 잡는다. 리포트에도 그 사실을 적는다.
"""
import argparse
import csv
import glob
import json
import random
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from schema import BASE_DIR, RAW_CACHE_DIR, final_path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from user_agent import _load_key  # noqa: E402  (같은 .env 로딩 재사용)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

REVIEW_DIR = BASE_DIR / "data" / "review"
MODEL = "gpt-5"

# 대조할 필드. 파싱 난이도가 높고 추천에 직접 쓰이는 것만 고른다.
VERIFY_FIELDS = ("monthly_fee", "data_unlimited", "data_gb",
                 "voice_unlimited", "sms_unlimited")

# plan_name 주변에서 잘라낼 범위(문자). 전체를 보내면 안 된다 - KT는 태그를 떼고도
# 16,647자이고 가격 정보가 처음부터 끝까지 흩어져 있어서 앞부분만 자르면 절반을 잃는다.
CONTEXT_BEFORE, CONTEXT_AFTER = 300, 900


def _read_csv(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def source_of(row: dict) -> str:
    """이 행을 **어느 크롤러가** 수집했는지. refresh_plans.check_guards의
    _by_source와 같은 규칙이다(그쪽은 중첩 함수라 import가 안 된다).

    host_mno는 **망 제공사**라 알뜰폰도 KT/SKT/LGU+로 찍힌다. 그래서 host_mno만
    보고 표본을 뽑으면 SKT망 알뜰폰이 SKT 표본에 섞인다 - 과거 검증에서 정확히
    이것 때문에 37건이 실패했다(수정이력 15번).
    """
    return "MVNO(모요)" if row.get("carrier_type") == "MVNO" else row.get("host_mno", "")


# ---------------------------------------------------------------------------
# 표본 추출
# ---------------------------------------------------------------------------

def _is_synthesized_name(row: dict) -> bool:
    """요금제명이 우리가 조립한 것인지. 원문에 그대로 없어서 반드시 오탐이 난다.

    택1 전개("초이스130 (넷플릭스)")와 연령 변형("음성 12.1_만 65세 이상")이
    여기 해당한다. 과거에도 우리가 만든 이름("요고 모바일 8GB")을 페이지에서
    찾다가 오탐이 났다.
    """
    return bool(row.get("selected_option")) or row.get("base_plan_id") != row.get("plan_id")


def pick_samples(final_rows: list[dict], change_rows: list[dict], n: int,
                 rng: random.Random | None = None) -> list[dict]:
    """신규/변경으로 찍힌 요금제 중 표본 n건. 사이트별로 고루, 신규 우선.

    한 사이트에 몰리면 다른 사이트의 파서가 깨진 걸 통째로 놓친다.
    """
    rng = rng or random.Random(0)
    by_id = {r["plan_id"]: r for r in final_rows}

    # 신규를 앞에 둔다. 파서가 처음 보는 구조라 위험이 크다.
    ordered, seen = [], set()
    for want in ("신규", "변경"):
        for c in change_rows:
            pid = c.get("plan_id")
            if c.get("change_type") != want or pid in seen or pid not in by_id:
                continue
            seen.add(pid)
            row = by_id[pid]
            if _is_synthesized_name(row):
                continue
            ordered.append({**row, "change_type": want})

    buckets = defaultdict(list)
    for row in ordered:
        buckets[source_of(row)].append(row)
    for rows in buckets.values():
        rng.shuffle(rows)

    # 사이트를 돌아가며 하나씩 뽑아 한쪽으로 쏠리지 않게 한다.
    picked, sites = [], sorted(buckets)
    while len(picked) < n and any(buckets[s] for s in sites):
        for s in sites:
            if buckets[s] and len(picked) < n:
                picked.append(buckets[s].pop())
    return picked


# ---------------------------------------------------------------------------
# 원문 구간 추출
# ---------------------------------------------------------------------------

def cache_path_for(row: dict) -> Path | None:
    """source_url로 캐시된 원문 파일을 찾는다. 크롤러마다 저장 규칙이 다르다."""
    url, host = row.get("source_url", ""), row.get("host_mno", "")
    if row.get("carrier_type") == "MVNO" or "moyoplan.com/plans/" in url:
        m = re.search(r"/plans/(\d+)", url)
        return RAW_CACHE_DIR / "moyo" / f"detail_{m.group(1)}.html" if m else None
    if host == "KT":
        m = re.search(r"ItemCode=(\d+)", url)
        return RAW_CACHE_DIR / "kt" / f"{m.group(1)}.html" if m else None
    if host == "SKT":
        m = re.search(r"/callplan/([A-Za-z0-9]+)", url)
        return RAW_CACHE_DIR / "skt" / f"contents_{m.group(1)}.json" if m else None
    if host == "LGU+":
        m = re.search(r"/([A-Za-z0-9]+)$", url.rstrip("/"))
        if not m:
            return None
        # 파일명 앞에 탭 구분(age_01_ / unified_ / direct_)이 붙어서 glob으로 찾는다.
        hits = sorted(glob.glob(str(RAW_CACHE_DIR / "lguplus" / f"*_{m.group(1)}.html")))
        return Path(hits[0]) if hits else None
    return None


def _plain_text(path: Path) -> str:
    raw = path.read_text(encoding="utf-8", errors="replace")
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text)


_MOYO_LIST_CACHE: list[str] = []


def _moyo_list_text() -> list[str]:
    """모요 **목록 페이지**들의 텍스트. 232개라 한 번만 읽고 재사용한다."""
    if not _MOYO_LIST_CACHE:
        for path in sorted((RAW_CACHE_DIR / "moyo").glob("page_*.html")):
            _MOYO_LIST_CACHE.append(_plain_text(path))
    return _MOYO_LIST_CACHE


def _source_text(row: dict) -> str | None:
    """**파서가 실제로 읽은 곳**의 텍스트를 준다.

    출처를 맞추지 않으면 대조가 성립하지 않는다. 모요는 요금·데이터·통화·문자를
    **목록 카드**에서 읽고 상세페이지는 브랜드·사은품·테더링용으로만 쓴다.
    상세페이지를 대조했더니 "7개월 이후 11,550원"이 거기 없어서 멀쩡한
    monthly_fee가 불일치로 잡혔다(수정이력 15번이 경고한 출처 불일치).
    """
    if source_of(row) == "MVNO(모요)":
        needle = re.sub(r"\s+", "", row.get("plan_name", ""))
        if not needle:
            return None
        for text in _moyo_list_text():
            if needle in re.sub(r"\s+", "", text):
                return text
        return None
    path = cache_path_for(row)
    return _plain_text(path) if path and path.exists() else None


def excerpt_for(row: dict) -> str | None:
    """plan_name 주변만 잘라낸다. 못 찾으면 None(=판정 불가).

    공백을 무시하고 찾는다. 원문은 "베이직 110GB"인데 우리 이름은
    "베이직110GB"인 식으로 띄어쓰기가 다른 경우가 흔하다.
    """
    text = _source_text(row)
    if text is None:
        return None

    flat, index_map = [], []
    for i, ch in enumerate(text):
        if not ch.isspace():
            flat.append(ch)
            index_map.append(i)
    flat = "".join(flat)

    needle = re.sub(r"\s+", "", row.get("plan_name", ""))
    pos = flat.find(needle) if needle else -1
    if pos < 0:
        return None
    at = index_map[pos]
    return text[max(0, at - CONTEXT_BEFORE): at + CONTEXT_AFTER]


# ---------------------------------------------------------------------------
# 값 정규화 — 과거 오탐의 최대 원인이 여기였다
# ---------------------------------------------------------------------------

def norm_money(v) -> int | None:
    """"69,000원" / "69000" / 69000.0 -> 69000. 과거 오탐 54건이 콤마 때문이었다."""
    if v is None or v == "":
        return None
    m = re.search(r"\d[\d,]*", str(v))
    return int(m.group(0).replace(",", "")) if m else None


def norm_gb(v) -> float | None:
    """"300MB" -> 0.29, "15GB" -> 15.0. 단위가 섞여서 나온다."""
    if v is None or v == "":
        return None
    s = str(v)
    m = re.search(r"(\d+(?:\.\d+)?)\s*MB", s, re.I)
    if m:
        return round(float(m.group(1)) / 1024, 2)
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    return round(float(m.group(1)), 2) if m else None


def norm_bool(v) -> bool | None:
    if isinstance(v, bool):
        return v
    if v is None or v == "":
        return None
    return str(v).strip().lower() in ("true", "1", "y", "yes")


_NORMALIZERS = {
    "monthly_fee": norm_money,
    "data_gb": norm_gb,
    "data_unlimited": norm_bool,
    "voice_unlimited": norm_bool,
    "sms_unlimited": norm_bool,
}


def compare(csv_row: dict, extracted: dict) -> list[dict]:
    """필드별로 일치/불일치/판정불가를 매긴다.

    LLM이 값을 못 찾은 건(None) **불일치가 아니다.** 둘을 섞으면 과거처럼
    검증 코드의 한계가 데이터 버그로 둔갑한다.
    """
    out = []
    for field in VERIFY_FIELDS:
        norm = _NORMALIZERS[field]
        ours, theirs = norm(csv_row.get(field)), norm(extracted.get(field))
        if theirs is None:
            verdict = "판정불가"
        elif ours == theirs:
            verdict = "일치"
        else:
            verdict = "불일치"
        out.append({"field": field, "csv": ours, "llm": theirs, "verdict": verdict})
    return out


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

EXTRACT_PROMPT = """통신 요금제 페이지에서 잘라낸 텍스트를 준다.
지정한 요금제 하나에 대해 아래 필드를 찾아 JSON으로만 답해라.

monthly_fee     정수. **할인·프로모션이 끝난 뒤의 정상가**(원).
                "N개월 이후 X원"이 적혀 있으면 그 X가 정답이다.
                "선택약정 할인 시", "페이백 포함하면" 같은 할인가가 아니다.
                할인 표기가 아예 없으면 적힌 월정액이 곧 정상가다.
data_unlimited  불리언. **기본 제공량 자체가 무제한**일 때만 true.
data_gb         숫자. 기본 제공량. **GB 단위로 환산**해서 넣어라(600MB면 0.59).
voice_unlimited 불리언. 음성통화가 무제한이면 true.
sms_unlimited   불리언. 문자가 무제한이면 true.

**"소진 후 저속 무제한"은 무제한이 아니다.** 한국 요금제는 "월 7GB + 1Mbps",
"7GB 소진 후 최대 3Mbps", "다 쓰면 400Kbps"처럼 적는데, 이건 기본 7GB를 다 쓰면
그 뒤로 느리게 쓸 수 있다는 뜻이다. 이때는 data_unlimited=false, data_gb=7이다.
data_unlimited=true는 "데이터 무제한"처럼 **제공량에 숫자가 아예 없을 때**만 쓴다.

규칙:
- 텍스트에 **명시되지 않은 필드는 null**로 둬라. 추측하지 마라. 모르면 null이 정답이다.
- 한 페이지에 여러 요금제가 있을 수 있다. **지정된 이름의 요금제**만 봐라.
- JSON 외에는 아무것도 출력하지 마라."""


def _call_llm(plan_name: str, excerpt: str) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=_load_key())
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": EXTRACT_PROMPT},
            {"role": "user", "content": f"요금제 이름: {plan_name}\n\n텍스트:\n{excerpt}"},
        ],
        response_format={"type": "json_object"},
    )
    return resp.choices[0].message.content


def verify_row(row: dict, call=_call_llm) -> dict:
    excerpt = excerpt_for(row)
    if excerpt is None:
        return {"plan_id": row["plan_id"], "plan_name": row.get("plan_name"),
                "source": source_of(row), "skipped": "원문에서 요금제명을 못 찾음"}
    extracted = json.loads(call(row.get("plan_name", ""), excerpt))
    return {
        "plan_id": row["plan_id"],
        "plan_name": row.get("plan_name"),
        "source": source_of(row),
        "change_type": row.get("change_type", ""),
        "source_url": row.get("source_url", ""),
        "fields": compare(row, extracted),
    }


# ---------------------------------------------------------------------------
# 리포트
# ---------------------------------------------------------------------------

def write_report(results: list[dict], run_date: str) -> Path | None:
    """불일치가 있을 때만 파일을 만든다. 빈 리포트가 매일 쌓이면 정작 볼 게
    있을 때 눈에 안 띈다(schema_drift.py와 같은 규칙)."""
    bad = [r for r in results
           if any(f["verdict"] == "불일치" for f in r.get("fields", []))]
    if not bad:
        return None

    lines = [
        f"# 원문 대조 점검 {run_date}",
        "",
        f"표본 {len(results)}건 중 **{len(bad)}건**에서 원문과 다른 값이 나왔습니다.",
        "",
        "> 아래는 **LLM이 캐시된 원문에서 읽은 값**과 CSV 값의 차이입니다. "
        "LLM이 맞다는 뜻이 아닙니다 - 파서가 아는 맥락(예: KT는 세대 표기가 원문에 "
        "없다, 테더링 지원 여부는 meta 태그에만 있다)을 LLM은 모릅니다. "
        "**source_url을 열어 어느 쪽이 맞는지 사람이 판정하세요.**",
        "",
        "> 캐시된 원문 기준입니다. 캐시 자체가 불완전하게 받아졌으면 이 점검으로는 "
        "잡히지 않습니다.",
        "",
    ]
    for r in bad:
        lines.append(f"## {r['plan_name']} (`{r['plan_id']}`)")
        lines.append("")
        lines.append(f"- 수집: {r['source']} / {r.get('change_type', '')}")
        lines.append(f"- 원문: {r['source_url']}")
        lines.append("")
        lines.append("| 필드 | CSV | LLM이 원문에서 읽은 값 | 판정 |")
        lines.append("|---|---|---|---|")
        for f in r["fields"]:
            mark = "**다름**" if f["verdict"] == "불일치" else f["verdict"]
            lines.append(f"| `{f['field']}` | {f['csv']} | {f['llm']} | {mark} |")
        lines.append("")

    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    path = REVIEW_DIR / f"verify_{run_date}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def latest_changes() -> Path | None:
    files = sorted(REVIEW_DIR.glob("changes_*.csv"))
    return files[-1] if files else None


def run(n: int) -> None:
    changes_path = latest_changes()
    if changes_path is None:
        print("data/review/changes_*.csv가 없음 - 갱신 이력이 있어야 대조할 수 있다.")
        return
    final_rows = _read_csv(final_path("통신요금제_통합데이터_최종.csv"))
    samples = pick_samples(final_rows, _read_csv(changes_path), n)
    if not samples:
        print(f"[{changes_path.name}] 대조할 신규/변경 요금제가 없음.")
        return

    spread = Counter(source_of(r) for r in samples)
    print(f"[{changes_path.name}] 표본 {len(samples)}건 ({dict(spread)})")
    if len(spread) == 1:
        # 그날 한 사이트만 바뀌면 표본도 거기 몰린다. 나머지 사이트의 파서가
        # 깨져 있어도 이 실행으로는 알 수 없다는 뜻이라 말해 준다.
        print(f"  주의: 이번 갱신에 바뀐 사이트가 {next(iter(spread))} 하나뿐이라 "
              f"다른 사이트는 이번 점검에서 확인되지 않았다.")
    if not _load_key():
        print("OPENAI_API_KEY 없음 - 표본만 뽑고 대조는 건너뜀.")
        for r in samples:
            print(f"  {source_of(r):10s} {r['plan_id']:24s} {r.get('plan_name')}")
        return

    results = [verify_row(r) for r in samples]
    for r in results:
        if "skipped" in r:
            print(f"  건너뜀 {r['plan_id']}: {r['skipped']}")
            continue
        counts = Counter(f["verdict"] for f in r["fields"])
        print(f"  {r['source']:10s} {r['plan_name'][:24]:26s} {dict(counts)}")

    path = write_report(results, date.today().isoformat())
    print(f"\n{'불일치 리포트: ' + str(path) if path else '불일치 없음 - 리포트 생략.'}")


# ---------------------------------------------------------------------------
# 자체 점검
# ---------------------------------------------------------------------------

def demo() -> None:
    """LLM 없이 배선과 정규화를 확인한다. 정상 데이터만으로는 검사가 실제로
    도는지 알 수 없으므로, 값을 일부러 틀리게 만든 행으로 불일치 탐지까지 본다."""
    final_rows = _read_csv(final_path("통신요금제_통합데이터_최종.csv"))
    assert final_rows, "최종 CSV가 비어 있음"

    # 정규화: 과거 오탐의 최대 원인이었던 콤마와 단위
    assert norm_money("69,000원") == norm_money(69000) == 69000
    assert norm_gb("300MB") == 0.29 and norm_gb("15GB") == 15.0
    assert norm_bool("True") is True and norm_bool("") is None
    print("정규화: 콤마/단위/불리언 정상")

    # 표본 추출: 조립된 이름 제외 + 사이트 분산
    changes = [{"change_type": "변경", "plan_id": r["plan_id"]} for r in final_rows]
    samples = pick_samples(final_rows, changes, 12, random.Random(0))
    assert samples, "표본을 하나도 못 뽑음"
    assert not any(_is_synthesized_name(r) for r in samples), (
        "택1 전개/연령 변형 행이 표본에 섞임 - 원문에 없는 이름이라 오탐이 난다"
    )
    spread = Counter(source_of(r) for r in samples)
    assert len(spread) >= 3, f"표본이 한쪽에 쏠림: {dict(spread)}"
    print(f"표본 {len(samples)}건 분산: {dict(spread)}")

    # 모요는 요금을 **목록 카드**에서 읽는다. 상세페이지를 대조하면 "N개월 이후
    # X원"이 거기 없어서 멀쩡한 monthly_fee가 불일치로 잡힌다(실제로 겪음).
    moyo = next(r for r in final_rows
                if source_of(r) == "MVNO(모요)" and not _is_synthesized_name(r)
                and r.get("discount_period_months") and excerpt_for(r))
    flat = re.sub(r"\s+", "", excerpt_for(moyo))
    assert re.sub(r"\s+", "", f"{int(float(moyo['monthly_fee'])):,}") in flat, (
        f"모요 발췌에 정상가가 없음 - 목록 카드가 아니라 상세페이지를 보고 있다"
        f" ({moyo['plan_name']}, {moyo['monthly_fee']})"
    )
    print(f"모요 출처 OK - 목록 카드에서 정상가 확인 ({moyo['plan_name']})")

    # 캐시 매핑 + 구간 추출: 4사 전부
    for site in ("KT", "SKT", "LGU+", "MVNO(모요)"):
        pool = [r for r in final_rows
                if source_of(r) == site and not _is_synthesized_name(r)]
        assert pool, f"{site} 행이 없음"
        ok = next((r for r in pool[:60] if excerpt_for(r)), None)
        assert ok is not None, f"{site}: 60건을 봐도 원문 구간을 못 잘라냄"
        text = excerpt_for(ok)
        assert re.sub(r"\s+", "", ok["plan_name"]) in re.sub(r"\s+", "", text)
        print(f"{site:10s} 구간 추출 OK ({len(text):,}자) - {ok['plan_name']}")

    # 불일치 탐지: 값을 일부러 틀리게 만든다
    row = dict(samples[0])
    verdicts = {f["field"]: f["verdict"] for f in compare(
        {**row, "monthly_fee": "99999"}, {"monthly_fee": "69,000원"})}
    assert verdicts["monthly_fee"] == "불일치", "틀린 값을 못 잡음"
    assert verdicts["data_gb"] == "판정불가", "LLM이 안 준 필드를 불일치로 셈"
    print("불일치 탐지 OK (없는 필드는 '판정불가'로 분리)")

    assert write_report([{"plan_id": "x", "fields": [
        {"field": "monthly_fee", "csv": 1, "llm": 1, "verdict": "일치"}]}],
        "9999-01-01") is None, "불일치가 없는데 리포트를 만듦"
    print("리포트: 불일치 없으면 파일 안 만듦")
    print("\n자체 점검 통과.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="갱신 요금제를 원문과 표본 대조한다")
    ap.add_argument("--demo", action="store_true", help="LLM 없이 자체 점검")
    ap.add_argument("--samples", type=int, default=10, help="대조할 표본 수")
    args = ap.parse_args()
    demo() if args.demo else run(args.samples)
