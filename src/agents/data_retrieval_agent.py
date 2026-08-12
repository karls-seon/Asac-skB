"""최종 CSV를 읽어 뒤쪽 소비자에게 넘긴다. **읽기 전용.**

**신선도 확인도, 재수집 트리거도 하지 않는다.** 예전 버전은 `is_stale()`로
날짜를 보고 오래됐으면 `refresh_plans.py`를 subprocess로 돌렸는데, 그러면
사용자 요청 한 번에 크롤링 34분이 딸려 갈 수 있다. 수집·갱신은 배치
(`python src/refresh_plans.py`, 매일 05:00)로만 돌린다. 데이터가 오래됐는지
알려야 하면 막지 말고 `data_as_of`를 같이 넘겨 표시하게 한다.

읽은 데이터가 쓸 만한지는 확인한다 - 파일이 없거나 컬럼이 어긋나면 뒤쪽
소비자가 엉뚱한 결과를 내는 대신 여기서 사유를 남긴다.

    python src/agents/data_retrieval_agent.py   # 최종 CSV 구조 점검
"""
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from schema import BASE_DIR, final_path, PLAN_COLUMNS, BENEFIT_COLUMNS  # noqa: E402

REVIEW_DIR = BASE_DIR / "data" / "review"
PLAN_OUT = final_path("통신요금제_통합데이터_최종.csv")
BENEFIT_OUT = final_path("통신요금제_혜택상세_최종.csv")


def _read_csv(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _data_as_of() -> str:
    """데이터 기준 날짜. 갱신 리포트 중 가장 최근에 성공한 날짜를 쓴다.

    리포트가 없어도 CSV만 있으면 동작해야 하므로(다른 컴퓨터에서 clone만 한
    경우) 못 찾으면 빈 문자열을 돌려주고 막지 않는다.
    """
    if not REVIEW_DIR.exists():
        return ""
    dates = []
    for path in REVIEW_DIR.glob("summary_*.json"):
        try:
            with open(path, encoding="utf-8") as f:
                summary = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue  # 리포트 하나가 깨졌다고 데이터 공급을 막을 이유는 없다
        if summary.get("status") == "applied" and summary.get("date"):
            dates.append(summary["date"])
    return max(dates) if dates else ""


def validate(plans: list[dict], benefits: list[dict]) -> list[str]:
    """뒤쪽 에이전트가 믿고 쓸 수 있는 데이터인지. 사유 목록을 돌려준다."""
    errors = []
    if not plans:
        errors.append(f"요금제 CSV가 비어 있거나 없음: {PLAN_OUT}")
    elif list(plans[0]) != PLAN_COLUMNS:
        errors.append("요금제 CSV 컬럼이 schema.PLAN_COLUMNS와 다름")
    if not benefits:
        errors.append(f"혜택 CSV가 비어 있거나 없음: {BENEFIT_OUT}")
    elif list(benefits[0]) != BENEFIT_COLUMNS:
        errors.append("혜택 CSV 컬럼이 schema.BENEFIT_COLUMNS와 다름")

    ids = [r["plan_id"] for r in plans]
    if len(ids) != len(set(ids)):
        errors.append("plan_id가 중복됨 - 혜택 조인이 어긋난다")
    orphan = {r["plan_id"] for r in benefits} - set(ids)
    if orphan:
        errors.append(f"요금제에 없는 plan_id를 가진 혜택 {len(orphan)}건")
    return errors


def data_retrieval_agent() -> dict:
    """plans/benefits와 검증 결과를 dict로 돌려준다."""
    plans = _read_csv(PLAN_OUT)
    benefits = _read_csv(BENEFIT_OUT)
    return {
        "plans": plans,
        "benefits": benefits,
        "data_as_of": _data_as_of(),
        "data_validation_errors": validate(plans, benefits),
    }


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    out = data_retrieval_agent()
    print(f"plans: {len(out['plans'])}행 / benefits: {len(out['benefits'])}행")
    print(f"data_as_of: {out['data_as_of'] or '(갱신 기록 없음)'}")
    print(f"검증: {out['data_validation_errors'] or '통과'}")
    assert not out["data_validation_errors"], out["data_validation_errors"]
