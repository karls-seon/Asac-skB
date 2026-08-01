"""③ Data Retrieval Agent — 최신 요금제 데이터를 조회한다.

기획서 상 역할: "최신 요금제·지원금 데이터를 조회하고 표준 스키마로 변환" +
"캐싱, 데이터 정합성 체크, 스케줄러(주기적 갱신)". 이 프로젝트는 이미
src/refresh_plans.py로 이 역할을 구현해 뒀다 - 캐싱은 data/raw_cache,
정합성 체크는 check_guards, 표준 스키마는 schema.py. 이 에이전트는 그
파이프라인을 LangGraph 노드 하나로 감싸는 얇은 래퍼일 뿐이다.

신선도 기준: data/review/summary_*.json 중 가장 최근 날짜가
REFRESH_INTERVAL_DAYS 이상 지났으면 오래됐다고 보고 refresh_plans.py를
통째로 돌려 재수집한다. 아직 신선하면 재수집 없이 최종 CSV를 그대로 읽어
돌려준다(주 1회면 충분한데 요청마다 크롤링하면 사이트에 불필요한 부담).

재수집은 **subprocess로** 돌린다. import해서 함수를 직접 부르면 각
crawl_*.py가 갖는 전역 상태(CACHE_DIR 등)가 이 에이전트 프로세스와 뒤섞일
수 있다 - refresh_plans.run_crawlers가 크롤러마다 subprocess를 쓰는 것과
같은 이유다.
"""
import csv
import json
import subprocess
import sys
from collections import Counter
from datetime import date
from pathlib import Path

# schema.py는 src/ 바로 아래 있으므로, src/agents/에서 실행될 때도
# import가 되도록 src/를 경로에 추가한다.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from schema import BASE_DIR, final_path, PLAN_COLUMNS, BENEFIT_COLUMNS  # noqa: E402

REVIEW_DIR = BASE_DIR / "data" / "review"
PLAN_OUT = final_path("통신요금제_통합데이터_최종.csv")
BENEFIT_OUT = final_path("통신요금제_혜택상세_최종.csv")
REFRESH_PLANS_SCRIPT = BASE_DIR / "src" / "refresh_plans.py"

REFRESH_INTERVAL_DAYS = 7


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _read_summaries() -> list[tuple[date, str]]:
    """갱신 리포트를 (날짜, status) 목록으로 읽는다. 오래된 것부터 정렬.

    깨진 파일은 조용히 건너뛴다. 리포트 하나가 손상됐다고 에이전트 전체가
    죽으면, 정작 멀쩡한 최종 CSV가 있는데도 데이터를 못 넘겨주게 된다.
    """
    if not REVIEW_DIR.exists():
        return []
    out = []
    for path in REVIEW_DIR.glob("summary_*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            run_date = date.fromisoformat(payload["date"])
        except (json.JSONDecodeError, KeyError, ValueError, OSError):
            print(f"[Data Retrieval Agent] 경고: 리포트를 읽을 수 없음 - {path.name}")
            continue
        # status가 없는 건 이 필드가 생기기 전에 쓰인 리포트다. 그 시절엔
        # 리포트가 곧 성공을 뜻했으므로 applied로 본다.
        out.append((run_date, payload.get("status", "applied")))
    return sorted(out)


def last_applied_date() -> date | None:
    """**성공적으로 반영된** 마지막 갱신 날짜. = 지금 최종 CSV에 담긴 데이터의 기준일.

    refresh_plans.py는 가드 위반으로 중단(aborted)해도 리포트는 남긴다.
    그래서 "리포트가 있다"는 "데이터가 갱신됐다"와 다르다 - status를 봐야 한다.
    """
    applied = [d for d, status in _read_summaries() if status == "applied"]
    return applied[-1] if applied else None


def is_stale(today: date | None = None) -> bool:
    """재수집이 필요한가.

    필요한 경우: 성공한 갱신이 아예 없거나(첫 실행), 마지막 성공이
    REFRESH_INTERVAL_DAYS 이상 지났을 때. **중단된 갱신은 성공으로 치지
    않으므로**, 지난주 갱신이 실패했다면 다음 주까지 기다리지 않고 다시 시도한다.

    단, **하루에 한 번만** 시도한다. 사이트 구조가 바뀌어 계속 실패하는
    상황에서, 사용자 요청마다 이 에이전트가 불리면 그때마다 전체 재크롤링이
    돌아 사이트를 두드리게 된다. 오늘 이미 시도했으면(성공이든 실패든)
    오늘은 더 시도하지 않는다.
    """
    today = today or date.today()
    summaries = _read_summaries()
    if summaries and summaries[-1][0] >= today:
        return False   # 오늘 이미 시도함

    applied = last_applied_date()
    return applied is None or (today - applied).days >= REFRESH_INTERVAL_DAYS


def validate_output(plans: list[dict], benefits: list[dict]) -> list[str]:
    """이 에이전트가 **매번** 돌려주는 데이터 자체가 구조적으로 멀쩡한지 본다.

    refresh_plans.check_guards()와 목적이 다르다 - 그건 "재수집 직후 이전
    대비 급변했는지"만 보고, 캐시 경로(재수집을 안 한 날)에서는 아예 실행되지
    않는다. 이 함수는 캐시 경로든 재수집 경로든 **매번** 돌아서, "지금 이
    상태로 다음 에이전트에 넘겨도 되는가"를 최종 확인한다. 판단 기준은
    check_guards와 겹치는 게 당연한데, 같은 종류의 조용한 파싱 붕괴를 잡는
    거라서다.
    """
    errors = []
    if not plans:
        errors.append("plans가 비어 있음")
        return errors  # 이후 검사는 plans가 있어야 의미가 있다
    if not benefits:
        errors.append("benefits가 비어 있음")

    # 컬럼 검사는 .get()으로 값을 읽기 전에 한다. 아래 검사들이 전부
    # .get(키, 기본값)을 쓰는데, 컬럼이 통째로 없으면 "전부 빈 값"으로
    # 조용히 통과해버리기 때문이다.
    missing_plan_cols = set(PLAN_COLUMNS) - set(plans[0].keys())
    if missing_plan_cols:
        errors.append(f"plans 컬럼 누락: {sorted(missing_plan_cols)}")
    if benefits:
        missing_benefit_cols = set(BENEFIT_COLUMNS) - set(benefits[0].keys())
        if missing_benefit_cols:
            errors.append(f"benefits 컬럼 누락: {sorted(missing_benefit_cols)}")

    plan_ids = [p.get("plan_id", "") for p in plans]
    dup = len(plan_ids) - len(set(plan_ids))
    if dup:
        errors.append(f"plan_id 중복 {dup}건")

    # b["plan_id"]로 읽으면 컬럼이 없을 때 KeyError로 죽는다. 검증 함수가
    # 죽으면 "무엇이 잘못됐는지"를 알려주지 못한 채 에이전트가 멈춘다.
    orphans = {b.get("plan_id", "") for b in benefits} - set(plan_ids)
    if orphans:
        errors.append(f"고아 혜택 {len(orphans)}건 (plans에 없는 plan_id를 참조)")

    by_site = Counter(p.get("host_mno", "") for p in plans)
    for site in ("KT", "SKT", "LGU+"):
        if by_site.get(site, 0) == 0:
            errors.append(f"{site} 요금제가 0행")

    # host_mno는 **망 제공사**라 알뜰폰도 KT/SKT/LGU+로 찍힌다. 그래서 위
    # 검사만으로는 모요(MVNO) 크롤러가 통째로 죽은 걸 못 잡는다 - 통신 3사
    # 자사 요금제가 남아 있어 세 값이 모두 0이 아니기 때문이다.
    # 실제로 MVNO가 전체의 8할이므로 별도로 확인한다.
    by_carrier = Counter(p.get("carrier_type", "") for p in plans)
    for carrier in ("MNO", "MVNO"):
        if by_carrier.get(carrier, 0) == 0:
            errors.append(f"{carrier} 요금제가 0행")

    return errors


def data_retrieval_agent(state: dict) -> dict:
    """LangGraph 노드 함수. state를 받아 바뀐 필드만 dict로 돌려주면
    LangGraph가 알아서 기존 state에 병합한다."""
    refreshed = False
    aborted = False

    if is_stale():
        print("[Data Retrieval Agent] 데이터가 오래됨 -> refresh_plans.py 실행")
        # --parse-only를 안 붙인다: 주간 갱신의 목적 자체가 사이트에 실제
        # 변경이 있었는지 확인하는 것이라, 캐시만 다시 파싱하면 의미가 없다.
        result = subprocess.run(
            [sys.executable, str(REFRESH_PLANS_SCRIPT)], cwd=BASE_DIR,
        )
        refreshed = True
        # refresh_plans.py는 가드 위반(파싱이 조용히 망가진 경우)이면
        # 최종 CSV를 안 건드리고 exit code != 0으로 끝낸다. 그 경우에도
        # 이 에이전트는 죽지 않고 "이전 최종본"을 그대로 반환한다 -
        # 판단(재시도할지, 사람에게 알릴지)은 뒤쪽 에이전트/오케스트레이터 몫이다.
        aborted = result.returncode != 0
    else:
        print("[Data Retrieval Agent] 데이터가 신선함 -> 캐시된 최종 CSV 사용")

    plans = _read_csv(PLAN_OUT)
    benefits = _read_csv(BENEFIT_OUT)
    validation_errors = validate_output(plans, benefits)
    if validation_errors:
        print("[Data Retrieval Agent] 검증 실패:")
        for e in validation_errors:
            print(f"  - {e}")
    else:
        print(f"[Data Retrieval Agent] 검증 통과: plans {len(plans)}행 / benefits {len(benefits)}행")

    # 오늘 날짜가 아니라 **마지막으로 성공한 갱신 날짜**를 넘긴다. 캐시를 쓴
    # 날에도 오늘로 찍으면, 리포트 에이전트가 일주일 묵은 값을 "오늘 기준"이라고
    # 사용자에게 말하게 된다. 재수집 뒤에 다시 읽어야 방금 쓰인 리포트가 반영된다.
    as_of = last_applied_date()
    if as_of and (date.today() - as_of).days >= REFRESH_INTERVAL_DAYS:
        print(f"[Data Retrieval Agent] 주의: 데이터가 {(date.today() - as_of).days}일 지남 (기준일 {as_of})")

    return {
        "plans": plans,
        "benefits": benefits,
        "data_refreshed": refreshed,
        "data_stale_aborted": aborted,
        "data_as_of": as_of.isoformat() if as_of else "",
        "data_validation_errors": validation_errors,
    }
