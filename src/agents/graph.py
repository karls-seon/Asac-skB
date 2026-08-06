"""Orchestrator — PoC 4개 에이전트를 잇는 LangGraph 그래프.

    python src/agents/graph.py "출퇴근에 영상 자주 봐요. 월 2만원 안쪽으로"
    python src/agents/graph.py            # LLM 없이 완화 루프만 점검

PoC 범위(2026-08-06): Orchestrator / User Profiling / Data Retrieval /
Plan Matching 넷만 돌린다. Usage Prediction, Explanation & Report,
Evaluation은 이 단계에서 제외했고 관련 코드는 _archive/로 옮겼다.

    START -> user_profiling -+-(슬롯 부족)------------------------> END
                             |
                             +-> data_retrieval -> plan_matching -+-(후보 있음)-> END
                                        ^                          |
                                        |                          +-(후보 0개)-> relax
                                        +----------------------------------------+

**Data Retrieval을 Matching보다 앞에 둔 이유**: 매칭이 CSV를 직접 읽으면
어느 시점 데이터로 추천했는지가 State에 안 남는다. 데이터는 한 곳에서 읽어
State에 올리고, 매칭은 그걸 받아 쓴다(data_as_of가 결과와 함께 남는다).

**슬롯이 부족하면 데이터를 읽지도 않는다**. 되물어야 하는 상황에 2,780행을
읽어 봐야 버린다.

이전에는 배치용(수집·갱신)과 실시간용 그래프가 나뉘어 있었는데, Data
Retrieval을 읽기 전용으로 줄이면서 배치 쪽이 통째로 _archive/pipeline/으로
빠졌다. 그래서 그래프도 하나로 합쳤다.
"""
import sys
from pathlib import Path

from langgraph.graph import StateGraph, START, END

sys.path.insert(0, str(Path(__file__).resolve().parent))
from state import AppState  # noqa: E402
import scoring_agent  # noqa: E402
import user_agent  # noqa: E402
from data_retrieval_agent import data_retrieval_agent  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 자동으로 풀어도 되는 조건. **예산은 절대 넣지 않는다** - 못 낼 요금제를
# 보여주는 건 아무것도 안 보여주는 것보다 나쁘다. 예산이 문제일 때는 완화
# 대신 "최소 얼마가 필요한지"(min_cost_krw)를 State에 남겨 사용자가 정하게 한다.
# 순서는 "풀었을 때 사용자가 덜 손해 보는" 순이다. data_usage_gb는 풀면
# 실제로 데이터가 모자란 요금제를 주게 되므로 제일 뒤에 둔다.
AUTO_RELAXABLE = (
    "preferred_network",
    "data_unlimited_required",
    "voice_unlimited_required",
    "target_carrier_type",
    "data_usage_gb",
)
MAX_RETRY = 2


def user_profiling(state: AppState) -> dict:
    out = user_agent.profile_from_text(state["user_input_raw"])
    return {
        "user_profile": out["profile"],
        "profiling_complete": out["profiling_complete"],
        "profiling_questions": out["questions"],
        "matching_retry_count": 0,
        "relaxed_slots": [],
    }


def plan_matching(state: AppState) -> dict:
    # Data Retrieval이 State에 올린 행을 쓴다. 파일을 다시 읽지 않는다.
    plans = scoring_agent.from_rows(state["plans"])
    return {"match_result": scoring_agent.match(state["user_profile"], plans=plans)}


def relax_conditions(state: AppState) -> dict:
    """후보가 0개일 때 조건 하나를 풀고 다시 매칭하게 한다.

    무엇을 풀지는 Plan Matching이 계산해 둔 relaxation(조건별로 몇 개가
    열리는지)에서 고르되, AUTO_RELAXABLE 순서를 우선한다 - 제일 많이 열리는
    건 보통 예산인데 그건 풀면 안 되기 때문이다.
    """
    opens = {r["slot"]: r["opens"] for r in state["match_result"]["relaxation"]}
    profile = dict(state["user_profile"])
    for slot in AUTO_RELAXABLE:
        if slot in profile and opens.get(slot):
            profile.pop(slot)
            return {
                "user_profile": profile,
                "relaxed_slots": state["relaxed_slots"] + [slot],
                "matching_retry_count": state["matching_retry_count"] + 1,
            }
    # 풀 수 있는 게 없다(예산만 남았거나 조건 자체가 없다). 재시도를 끝낸다.
    return {"matching_retry_count": MAX_RETRY}


def _after_profiling(state: AppState) -> str:
    return "data_retrieval" if state["profiling_complete"] else END


def _after_matching(state: AppState) -> str:
    """후보가 없고 아직 재시도 여유가 있으면 조건을 풀러 간다."""
    if (state["match_result"]["candidate_count"] == 0
            and state["matching_retry_count"] < MAX_RETRY):
        return "relax_conditions"
    return END


def _after_relax(state: AppState) -> str:
    """풀 게 없어서 재시도 횟수만 올라온 경우엔 그대로 끝낸다."""
    return END if state["matching_retry_count"] >= MAX_RETRY else "plan_matching"


builder = StateGraph(AppState)
builder.add_node("user_profiling", user_profiling)
builder.add_node("data_retrieval", data_retrieval_agent)
builder.add_node("plan_matching", plan_matching)
builder.add_node("relax_conditions", relax_conditions)

builder.add_edge(START, "user_profiling")
builder.add_conditional_edges("user_profiling", _after_profiling, ["data_retrieval", END])
builder.add_edge("data_retrieval", "plan_matching")
builder.add_conditional_edges("plan_matching", _after_matching, ["relax_conditions", END])
builder.add_conditional_edges("relax_conditions", _after_relax, ["plan_matching", END])

graph = builder.compile()


def run(text: str) -> AppState:
    return graph.invoke({"user_input_raw": text})


def _show(state: AppState) -> None:
    """State를 사람이 볼 수 있게 찍는다. 자연어 리포트를 만드는 게 아니라
    (그건 제외된 Explanation 몫) State가 제대로 찼는지 확인하는 용도다."""
    print(f"[슬롯] {state.get('user_profile')}")
    if not state.get("profiling_complete"):
        print("[중단] 슬롯 부족 - 되물어야 함")
        for q in state.get("profiling_questions", []):
            print(f"   - {q}")
        return
    print(f"[데이터] {len(state.get('plans', []))}행 / 기준일 "
          f"{state.get('data_as_of') or '(기록 없음)'} / 검증 "
          f"{state.get('data_validation_errors') or '통과'}")
    if state.get("relaxed_slots"):
        print(f"[완화] {state['relaxed_slots']} (재시도 {state['matching_retry_count']}회)")
    res = state["match_result"]
    print(f"[매칭] 후보 {res['candidate_count']}개 / 조건 완전 충족 {res['total_exact']}개")
    if res["candidate_count"] == 0:
        if res.get("min_cost_krw"):
            print(f"   최소 필요 금액 {res['min_cost_krw']:,.0f}원")
        for r in res["relaxation"][:3]:
            print(f"   {r['label']} 빼면 {r['opens']}개")
        return
    cols = ["plan_name", "monthly_cost", "data_gb", "data_unlimited", "match_score"]
    print(res["candidates"][cols].to_string(index=False))


def demo():
    """LLM 없이 그래프 뒷부분(데이터 조회 -> 매칭 -> 완화 루프)만 돌린다.
    API 키가 없어도 배선이 맞는지 확인할 수 있어야 한다."""
    seed = {
        "user_input_raw": "(테스트)",
        # 예산 8천원에 30GB 무제한 5G - 실제로 후보가 0개인 조합
        "user_profile": {"data_usage_gb": 30, "budget_krw": 8000,
                         "preferred_network": "5G", "data_unlimited_required": True},
        "profiling_complete": True,
        "profiling_questions": [],
        "matching_retry_count": 0,
        "relaxed_slots": [],
    }
    sub = StateGraph(AppState)
    sub.add_node("data_retrieval", data_retrieval_agent)
    sub.add_node("plan_matching", plan_matching)
    sub.add_node("relax_conditions", relax_conditions)
    sub.add_edge(START, "data_retrieval")
    sub.add_edge("data_retrieval", "plan_matching")
    sub.add_conditional_edges("plan_matching", _after_matching, ["relax_conditions", END])
    sub.add_conditional_edges("relax_conditions", _after_relax, ["plan_matching", END])
    state = sub.compile().invoke(seed)

    assert state["plans"], "Data Retrieval이 요금제를 못 읽음"
    assert not state["data_validation_errors"], state["data_validation_errors"]
    assert state["relaxed_slots"], "후보가 0개인데 조건을 하나도 안 풀었다"
    assert "budget_krw" not in state["relaxed_slots"], (
        "예산을 자동으로 풀었다 - 못 낼 요금제를 보여주게 된다"
    )
    _show(state)
    print("\n자체 점검 통과.")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        _show(run(" ".join(sys.argv[1:])))
    else:
        demo()
