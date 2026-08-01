"""요금제 데이터 일일 갱신 파이프라인.

    python src/refresh_plans.py            # 수집부터 전부
    python src/refresh_plans.py --parse-only   # 이미 받아둔 캐시로 파싱·비교만

하는 일:
  ① 지금 최종본을 data/final/history/YYYY-MM-DD/ 로 백업
  ② 4개 사이트 수집 (모요는 목록 비교로 바뀐 상세만 재수집)
  ③ 파싱 -> data/interim/*.csv
  ④ 필터·병합 (merge_plans.build) -> 새 최종 후보 (아직 파일로 안 씀)
  ⑤ 이전 최종본과 plan_id 기준 비교 -> 신규/단종/범위이탈/변경
  ⑥ 이상 징후가 있으면 **최종본을 안 건드리고** 중단, 아니면 저장
  ⑦ data/review/ 에 리포트 2종 기록

리포트를 사람이 아니라 **에이전트가 읽는다**는 전제로 설계했다. summary JSON만
보면 "볼 게 있는지"를 바로 알 수 있고, 볼 게 있으면 changes CSV의 source_url로
라이브 페이지까지 직접 확인할 수 있다.
"""
import csv
import json
import shutil
import subprocess
import sys
from collections import Counter
from datetime import date
from pathlib import Path

import merge_plans
from schema import BASE_DIR, PLAN_COLUMNS, BENEFIT_COLUMNS, final_path

REVIEW_DIR = BASE_DIR / "data" / "review"
HISTORY_DIR = BASE_DIR / "data" / "final" / "history"
PLAN_OUT = final_path("통신요금제_통합데이터_최종.csv")
BENEFIT_OUT = final_path("통신요금제_혜택상세_최종.csv")

CRAWLERS = ("crawl_kt", "crawl_skt", "crawl_lguplus", "crawl_moyo")

# 값이 바뀌면 "변경"으로 기록할 필드. crawled_at은 매번 바뀌므로 뺀다.
WATCHED_FIELDS = (
    "plan_name", "plan_category", "age_condition", "network_gen",
    "monthly_fee", "discounted_fee", "discount_type", "discount_period_months",
    "data_gb", "data_unlimited", "data_throttle_speed", "daily_data_gb",
    "base_tethering_gb", "extra_tethering_gb", "tethering_gb",
    "voice_unlimited", "voice_minutes", "voice_extra_minutes",
    "sms_unlimited", "sms_count",
    "benefit_count", "ott_option_count", "ott_options", "membership_grade",
)

# 자동 실행이라 사람이 안 보므로, 파싱이 조용히 망가진 경우를 수치로 막는다.
MAX_TOTAL_CHANGE_RATIO = 0.20   # 전체 행 수가 ±20% 넘게 흔들리면 중단
MAX_REMOVED_RATIO = 0.10        # 단종이 이전 전체의 10%를 넘으면 중단


def _read_csv(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _write_csv(rows: list[dict], columns, path: Path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        w.writerows(rows)


def backup_current(run_date: str) -> Path | None:
    """지금 최종본을 history/날짜/ 로 복사. 되돌릴 수 있게 남겨 둔다."""
    if not Path(PLAN_OUT).exists():
        return None
    dest = HISTORY_DIR / run_date
    dest.mkdir(parents=True, exist_ok=True)
    for src in (PLAN_OUT, BENEFIT_OUT):
        if Path(src).exists():
            shutil.copy2(src, dest / Path(src).name)
    return dest


def run_crawlers(parse_only: bool):
    """각 크롤러를 별도 프로세스로 실행한다.

    한 사이트가 죽어도 나머지는 돌아야 하고, 크롤러마다 전역 상태(CACHE_DIR 등)를
    들고 있어서 한 프로세스에서 import해 돌리면 서로 간섭할 수 있다.
    """
    for name in CRAWLERS:
        # "src"를 하드코딩하지 않고 이 파일 자신의 위치를 기준으로 잡는다.
        # 그래야 폴더 이름을 바꿔도(예: GitHub용 01_Src) 깨지지 않는다.
        cmd = [sys.executable, str(Path(__file__).resolve().parent / f"{name}.py")]
        if parse_only:
            cmd.append("--parse-only")
        print(f"\n=== {name} ===", flush=True)
        result = subprocess.run(cmd, cwd=BASE_DIR)
        if result.returncode != 0:
            raise RuntimeError(f"{name} 실패 (exit {result.returncode})")


def diff_plans(prev: list[dict], new: list[dict], all_new_rows: list[dict]) -> list[dict]:
    """이전 최종본 vs 새 후보를 plan_id로 비교해 변경 목록을 만든다."""
    prev_by_id = {r["plan_id"]: r for r in prev}
    new_by_id = {r["plan_id"]: r for r in new}
    # 필터 전 전체(사이트에 존재하는 것) - "단종"과 "범위이탈"을 가르는 기준
    still_on_site = {r["plan_id"] for r in all_new_rows}

    changes = []

    for pid, row in new_by_id.items():
        if pid in prev_by_id:
            continue
        changes.append({"change_type": "신규", **_ident(row)})

    for pid, row in prev_by_id.items():
        if pid in new_by_id:
            continue
        kind = "범위이탈" if pid in still_on_site else "단종"
        changes.append({"change_type": kind, **_ident(row)})

    for pid, new_row in new_by_id.items():
        old_row = prev_by_id.get(pid)
        if old_row is None:
            continue
        for field in WATCHED_FIELDS:
            old_val, new_val = old_row.get(field, ""), new_row.get(field, "")
            if old_val != new_val:
                changes.append({
                    "change_type": "변경", **_ident(new_row),
                    "field": field, "old_value": old_val, "new_value": new_val,
                })
    return changes


def _ident(row: dict) -> dict:
    return {
        "plan_id": row.get("plan_id", ""),
        "base_plan_id": row.get("base_plan_id", ""),
        "host_mno": row.get("host_mno", ""),
        "plan_category": row.get("plan_category", ""),
        "plan_name": row.get("plan_name", ""),
        "field": "", "old_value": "", "new_value": "",
        "source_url": row.get("source_url", ""),
    }


def check_guards(prev: list[dict], new: list[dict], benefits: list[dict],
                 changes: list[dict]) -> list[str]:
    """조용한 파싱 회귀를 수치로 잡아낸다. 위반 사유 목록을 돌려준다."""
    violations = []

    if prev:
        ratio = abs(len(new) - len(prev)) / len(prev)
        if ratio > MAX_TOTAL_CHANGE_RATIO:
            violations.append(
                f"전체 행 수 급변: {len(prev)} -> {len(new)} ({ratio:.1%})")
        removed = sum(1 for c in changes if c["change_type"] == "단종")
        if removed / len(prev) > MAX_REMOVED_RATIO:
            violations.append(f"단종 과다: {removed}행 (이전 {len(prev)}행 대비)")

    by_site = Counter(r["host_mno"] for r in new)
    for site in ("KT", "SKT", "LGU+"):
        if by_site.get(site, 0) == 0:
            violations.append(f"{site} 요금제가 0행")

    ids = [r["plan_id"] for r in new]
    dup = len(ids) - len(set(ids))
    if dup:
        violations.append(f"plan_id 중복 {dup}건")
    orphans = {b["plan_id"] for b in benefits} - set(ids)
    if orphans:
        violations.append(f"고아 혜택 {len(orphans)}건")
    return violations


def write_reports(run_date: str, status: str, prev: list[dict], new: list[dict],
                  changes: list[dict], violations: list[str]) -> tuple[Path, Path]:
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    changes_path = REVIEW_DIR / f"changes_{run_date}.csv"
    summary_path = REVIEW_DIR / f"summary_{run_date}.json"

    columns = ["change_type", "plan_id", "base_plan_id", "host_mno", "plan_category",
               "plan_name", "field", "old_value", "new_value", "source_url"]
    _write_csv(changes, columns, changes_path)

    kinds = Counter(c["change_type"] for c in changes)
    by_site = {}
    for site in ("KT", "SKT", "LGU+"):
        rows = [c for c in changes if c["host_mno"] == site]
        by_site[site] = dict(Counter(c["change_type"] for c in rows))
    by_site["MVNO(모요)"] = dict(Counter(
        c["change_type"] for c in changes if c["plan_category"] == "moyo"))

    summary = {
        "date": run_date,
        "status": status,
        "counts": {
            "total_prev": len(prev),
            "total_new": len(new),
            "신규": kinds.get("신규", 0),
            "단종": kinds.get("단종", 0),
            "범위이탈": kinds.get("범위이탈", 0),
            "변경": kinds.get("변경", 0),
        },
        "by_site": by_site,
        "guard_violations": violations,
        "changes_file": str(changes_path.relative_to(BASE_DIR)),
    }
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return changes_path, summary_path


def main() -> int:
    parse_only = "--parse-only" in sys.argv
    run_date = date.today().isoformat()
    print(f"[갱신 시작] {run_date} (parse_only={parse_only})")

    prev = _read_csv(PLAN_OUT)
    backup = backup_current(run_date)
    print(f"이전 최종본: {len(prev)}행" + (f" (백업: {backup})" if backup else " (없음)"))

    run_crawlers(parse_only)

    print("\n=== 병합·필터 ===")
    new, benefits, _dropped, all_rows = merge_plans.build()

    changes = diff_plans(prev, new, all_rows)
    violations = check_guards(prev, new, benefits, changes)
    status = "aborted" if violations else "applied"

    if violations:
        print("\n[중단] 이상 징후가 있어 최종 CSV를 갱신하지 않았습니다:")
        for v in violations:
            print(f"  - {v}")
    else:
        _write_csv(new, PLAN_COLUMNS, PLAN_OUT)
        _write_csv(benefits, BENEFIT_COLUMNS, BENEFIT_OUT)
        print(f"\n[반영] 요금제 {len(new)}행 / 혜택 {len(benefits)}행 저장")

    changes_path, summary_path = write_reports(
        run_date, status, prev, new, changes, violations)

    kinds = Counter(c["change_type"] for c in changes)
    print(f"\n[변경 요약] 신규 {kinds.get('신규', 0)} / 변경 {kinds.get('변경', 0)} / "
          f"단종 {kinds.get('단종', 0)} / 범위이탈 {kinds.get('범위이탈', 0)}")
    print(f"  {summary_path.relative_to(BASE_DIR)}")
    print(f"  {changes_path.relative_to(BASE_DIR)}")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
