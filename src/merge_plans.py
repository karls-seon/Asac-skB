"""
사이트별 중간 CSV(data/interim/)를 최종 CSV 2개(data/final/)로 합친다.

  통신요금제_통합데이터_최종.csv : 요금제 1개 = 1행
  통신요금제_혜택상세_최종.csv   : 요금제 1개 × 혜택 1개 = 1행 (plan_id로 조인)

각 크롤러가 schema.py의 컬럼을 그대로 쓰므로 헤더는 항상 같다고 보고,
다르면 assert에서 바로 잡히게 해뒀다.

**범위 필터는 여기서만 건다.** 크롤러와 data/interim/에는 사이트에 있는 걸 전부
그대로 남겨 두고, 최종 CSV를 만들 때만 걸러낸다. 그래야 범위를 다시 넓히고 싶을 때
재크롤링 없이 이 파일만 고치면 된다.
"""
import csv
from collections import Counter
from pathlib import Path

from schema import (PLAN_COLUMNS, BENEFIT_COLUMNS, total_data_gb,
                    interim_path, final_path)

SITES = ("kt", "skt", "lguplus", "moyo")
PLAN_FILES = [interim_path(f"{s}_plans.csv") for s in SITES]
BENEFIT_FILES = [interim_path(f"{s}_benefits.csv") for s in SITES]
PLAN_OUT = final_path("통신요금제_통합데이터_최종.csv")
BENEFIT_OUT = final_path("통신요금제_혜택상세_최종.csv")


# ---------------- 수집 범위: 휴대폰 5G/LTE 요금제만 ----------------
#
# 태블릿·스마트워치·3G·선불폰 요금제는 휴대폰 요금제와 스펙 구조가 달라서
# (음성/문자가 아예 없거나, 공유회선 전용이거나, 종량 과금이거나) 추천 대상으로
# 섞이면 이상치로 작동한다.
#
# 반면 **연령** 한정(키즈/청소년/청년/시니어)은 스펙 구조가 일반 요금제와 같고
# 가입 자격만 다르므로 **남긴다** - age_condition으로 구분된다. 빼버리면 만 25세
# 사용자에게 실제로 가장 유리한 청년 요금제를 영영 추천할 수 없다.
# **외국인·복지**는 나이와 달리 서류·자격 확인이 필요해 일반 추천 대상이 아니라 뺀다.
#
# 사이트마다 판단 근거가 다르다.
#   KT   : 크롤러(crawl_kt.TABS)에서 "통합요금제"/"온라인전용(요고)" 두 탭만 수집.
#   SKT  : 크롤러(crawl_skt.COLLECTED_CATEGORIES)에서 "베스트"/"라이트"만 수집.
#   LGU+ : 수집 대상 탭(통합/너겟/연령)에 애초에 태블릿·워치가 없다.
#   모요  : 카드 텍스트에 5G/LTE/3G가 그대로 적혀 있어 network_gen이 정확하다.
#
# 외국인·복지 전용은 여기서 뺀다(SKT는 원래 안 받았고, LGU+만 카테고리로
# 들어와 있었다). 나이 조건과 달리 서류/자격 확인이 필요해 일반 추천 대상으로
# 적합하지 않다는 판단.
EXCLUDED_CATEGORIES: set[str] = {
    "LGU+-외국인(Global)",
    "LGU+-외국인 청년(Uth)",
    "LGU+-복지",
}
# KT는 "키즈/외국인"이 한 탭이라 웰컴(외국인) 3개가 키즈 5개와 같은 카테고리에
# 섞여 있다. 카테고리로는 못 걸러내므로 age_condition 값으로 뺀다.
EXCLUDED_AGE_CONDITIONS_PREFIXES = ("외국인", "복지카드")
# 화이트리스트(5G/LTE만 통과)가 아니라 3G만 배제한다. KT는 상세페이지에
# 세대 표기가 아예 없어서 network_gen이 비는데, 화이트리스트면 그 271행이
# 통째로 사라진다. 빈값은 "모름"이 아니라 "세대 구분 없는 통합요금제"라는
# 뜻이므로 수집 범위에서 뺄 이유가 없다(crawl_kt._guess_network_gen 참고).
EXCLUDED_NETWORK_GENS = {"3G"}


def has_data(plan: dict) -> bool:
    """데이터를 조금이라도 주는 요금제인지. 무제한/월단위/일단위 중 하나."""
    return (plan["data_unlimited"] == "True"
            or plan["data_gb"] != ""
            or plan["daily_data_gb"] != "")


def in_scope(plan: dict) -> bool:
    # 데이터를 아예 안 주는 음성전용 요금제(KT 음성 12.1, SKT 표준요금제 등)는
    # 태블릿·워치를 뺀 것과 같은 이유로 뺀다 - 휴대폰 요금제 추천 대상의
    # 스펙 구조가 아니다. 이걸 빼면 network_gen 빈값의 뜻도 하나로 정리된다
    # ("데이터 요금제가 아니라 세대가 무의미"한 경우가 사라지므로).
    return (plan["plan_category"] not in EXCLUDED_CATEGORIES
            and plan["network_gen"] not in EXCLUDED_NETWORK_GENS
            and has_data(plan)
            and not plan["age_condition"].startswith(EXCLUDED_AGE_CONDITIONS_PREFIXES))


def _read(path, columns):
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == columns, f"{path} 컬럼이 schema.py와 다름:\n{reader.fieldnames}"
        return list(reader)


def _write(rows, columns, out_path):
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        w.writerows(rows)
    print(f"=> {Path(out_path).name}: 총 {len(rows)}행\n")


def build(verbose: bool = True) -> tuple[list[dict], list[dict], Counter, list[dict]]:
    """interim CSV를 읽어 필터·병합한 결과를 **파일로 쓰지 않고** 돌려준다.

    갱신 파이프라인(refresh_plans.py)이 "쓰기 전에 이전 결과와 비교"할 수 있도록
    쓰기와 분리해 둔다. main()은 이걸 호출해 파일로 저장할 뿐이다.

    반환: (요금제 행, 혜택 행, 제외 사유 카운터, 필터 전 전체 요금제 행)
    마지막 값은 "사이트에는 있는데 필터에서 탈락"(범위이탈)과 "사이트에서 아예
    사라짐"(단종)을 구분하는 데 쓴다.
    """
    plans, dropped, all_rows = [], Counter(), []
    if verbose:
        print("[요금제]")
    for site, path in zip(SITES, PLAN_FILES):
        rows = _read(path, PLAN_COLUMNS)
        all_rows.extend(rows)
        kept = [r for r in rows if in_scope(r)]
        plans.extend(kept)
        for r in rows:
            if in_scope(r):
                continue
            if r["plan_category"] in EXCLUDED_CATEGORIES:
                reason = r["plan_category"]
            elif r["age_condition"].startswith(EXCLUDED_AGE_CONDITIONS_PREFIXES):
                reason = f"{site}: age_condition={r['age_condition']}"
            else:
                reason = f"{site}: network_gen={r['network_gen'] or '(빈값)'}"
            dropped[reason] += 1
        if verbose:
            print(f"  {Path(path).name}: {len(rows)}행 중 {len(kept)}행 유지 ({len(rows) - len(kept)}행 제외)")

    # 요금제가 빠지면 그 요금제의 혜택도 같이 빠져야 조인이 깨지지 않는다
    plan_ids = {p["plan_id"] for p in plans}
    benefits = []
    if verbose:
        print("[혜택]")
    for path in BENEFIT_FILES:
        rows = _read(path, BENEFIT_COLUMNS)
        kept = [r for r in rows if r["plan_id"] in plan_ids]
        benefits.extend(kept)
        if verbose:
            print(f"  {Path(path).name}: {len(rows)}행 중 {len(kept)}행 유지 ({len(rows) - len(kept)}행 제외)")

    # 후처리는 build() 안에 둔다. main()에만 있었더니 refresh_plans가 build()만
    # 부르는 경로에서 통째로 빠졌다(discounted_fee 90행·age_condition 14행 결측).
    borrowed = borrow_age_condition(plans)
    filled = fill_undiscounted_fee(plans)
    # 일 단위 제공량(매일 2GB 등)을 월 환산해 data_gb에 합산한다. 사이트 표기를
    # 그대로 둔 interim과 달리, 최종 CSV의 data_gb는 "한 달에 쓸 수 있는 총량"이다.
    # 되돌리려면 data_gb - daily_data_gb * 30.
    daily_merged = sum(1 for p in plans if str(p.get("daily_data_gb", "")).strip())
    for p in plans:
        p["data_gb"] = total_data_gb(p)
    if verbose:
        print(f"  모요발 {borrowed}행의 가입 연령을 3사 수집분에서 채움")
        print(f"  할인 없는 요금제 {filled}행의 discounted_fee를 정가로 채움")
        print(f"  일 단위 제공량이 있는 {daily_merged}행의 data_gb에 daily×30을 합산")
    return plans, benefits, dropped, all_rows


def fill_undiscounted_fee(plans: list[dict]) -> int:
    """할인이 없는 요금제의 `discounted_fee`를 정가로 채운다. 채운 행 수를 돌려준다.

    `discounted_fee`는 명세서상 **실제 내는 돈**이고, 통신사 유형을 함께 비교할 때
    기준이 되는 컬럼이다. 그런데 할인 자체가 없는 온라인 전용(KT 요고 13 · LGU+
    너겟 46 · SKT 다이렉트 31)은 이 값이 비어 있었다. "할인 없음"을 "가격 모름"과
    같은 모양으로 둔 셈이라, 가격 결측을 거르는 쪽에서는 3사 최저가 라인 90개가
    통째로 사라진다.

    할인 여부는 `discount_type`(빈값)과 `monthly_fee == discounted_fee`로 여전히
    구분된다. docs/수정이력.md 38번.
    """
    filled = 0
    for p in plans:
        if not str(p.get("discounted_fee", "")).strip() and str(p.get("monthly_fee", "")).strip():
            p["discounted_fee"] = p["monthly_fee"]
            filled += 1
    return filled


def borrow_age_condition(plans: list[dict]) -> int:
    """모요가 안 적은 가입 연령을, 같은 통신사·같은 이름의 3사 수집분에서 가져온다.

    모요는 3사 자사상품도 함께 싣는데 **가입 연령을 적지 않는다.** SKT `0 청년
    다이렉트 48`의 경우 SKT 페이지에는 "만 34세 이하 고객님이…"가 있지만 모요
    상세에는 그 문구가 아예 없다(원본 캐시 확인). 그래서 모요발 행만 있으면
    40세에게도 청년 요금제가 추천된다.

    이름이 같아도 통신사가 다르면 다른 상품이므로 `host_mno`까지 맞을 때만 쓴다.
    """
    def key(p):
        return (p.get("host_mno", ""), (p.get("plan_name") or "").replace(" ", "").strip())

    known = {key(p): p["age_condition"] for p in plans
             if str(p.get("age_condition", "")).strip() and not str(p["plan_id"]).isdigit()}

    borrowed = 0
    for p in plans:
        if str(p["plan_id"]).isdigit() and not str(p.get("age_condition", "")).strip():
            found = known.get(key(p))
            if found:
                p["age_condition"] = found
                borrowed += 1
    return borrowed


def main():
    plans, benefits, dropped, _all_rows = build()
    _write(plans, PLAN_COLUMNS, PLAN_OUT)
    _write(benefits, BENEFIT_COLUMNS, BENEFIT_OUT)

    plan_ids = {p["plan_id"] for p in plans}
    print("[범위 제외 내역]")
    for reason, n in dropped.most_common():
        print(f"  {reason}: {n}행")

    print("\n[검증]")
    print("  통신사별 요금제:", dict(Counter(p["host_mno"] for p in plans)))
    print("  망 세대별:", dict(Counter(p["network_gen"] for p in plans)))
    print("  혜택 분류별:", dict(Counter(b["benefit_category"] for b in benefits)))
    orphans = {b["plan_id"] for b in benefits if b["plan_id"] not in plan_ids}
    print(f"  고아 혜택: {len(orphans)}건")
    with_ott = sum(1 for p in plans if p["ott_option_count"] not in ("", "0"))
    print(f"  OTT 혜택이 있는 요금제: {with_ott} / {len(plans)}")
    print(f"  실제 요금제 수(base_plan_id): {len({p['base_plan_id'] for p in plans})}")


if __name__ == "__main__":
    main()
