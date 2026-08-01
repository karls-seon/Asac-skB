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
from schema import BASE_DIR, final_path, PLAN_COLUMNS  # noqa: E402

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


def _latest_summary_date() -> date | None:
    """가장 최근 갱신 리포트의 날짜. 리포트가 하나도 없으면(첫 실행) None."""
    if not REVIEW_DIR.exists():
        return None
    files = sorted(REVIEW_DIR.glob("summary_*.json"))
    if not files:
        return None
    payload = json.loads(files[-1].read_text(encoding="utf-8"))
    return date.fromisoformat(payload["date"])


def is_stale(today: date | None = None) -> bool:
    """리포트가 없거나(첫 실행) 마지막 갱신이 REFRESH_INTERVAL_DAYS 이상 지났으면 True."""
    today = today or date.today()
    last = _latest_summary_date()
    return last is None or (today - last).days >= REFRESH_INTERVAL_DAYS


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

    missing_cols = set(PLAN_COLUMNS) - set(plans[0].keys())
    if missing_cols:
        errors.append(f"plans 컬럼 누락: {sorted(missing_cols)}")

    plan_ids = [p.get("plan_id", "") for p in plans]
    dup = len(plan_ids) - len(set(plan_ids))
    if dup:
        errors.append(f"plan_id 중복 {dup}건")

    orphans = {b["plan_id"] for b in benefits} - set(plan_ids)
    if orphans:
        errors.append(f"고아 혜택 {len(orphans)}건 (plans에 없는 plan_id를 참조)")

    by_site = Counter(p.get("host_mno", "") for p in plans)
    for site in ("KT", "SKT", "LGU+"):
        if by_site.get(site, 0) == 0:
            errors.append(f"{site} 요금제가 0행")

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

    return {
        "plans": plans,
        "benefits": benefits,
        "data_refreshed": refreshed,
        "data_stale_aborted": aborted,
        "data_as_of": date.today().isoformat(),
        "data_validation_errors": validation_errors,
    }
