"""실시간 추천 그래프. 사용자 요청 하나를 받아 답변까지 만든다.

    python src/agents/live_graph.py "출퇴근에 영상 자주 봐요. 월 2만원 안쪽으로"

배치 그래프(graph.py, START -> data_retrieval -> END)와 **일부러 분리**했다.
State 스키마(state.AppState)는 공유하지만 트리거가 다르다 - 저쪽은 스케줄러가
하루 한 번 돌리는 데이터 갱신이고, 이쪽은 사용자 요청마다 도는 추천이다.
한 그래프에 섞으면 추천 한 번에 크롤링이 딸려 갈 수 있다.

    START -> user_profiling -+-(슬롯 부족)------------------> explanation -> END
                             |                                    ^
                             +-> plan_matching -+-(후보 있음)------+
                                    ^           |
                                    |           +-(후보 0개)-> relax_conditions
                                    +---------------------------------+

**조건 완화 루프가 이 그래프의 존재 이유다.** 나머지는 직선이라 함수 세 개를
순서대로 부르는 것과 다를 게 없다(ask.py가 그렇게 한다). 후보가 0개일 때
조건을 풀고 되돌아오는 분기가 생기면서 비로소 그래프가 값을 한다.
"""
import sys
from pathlib import Path

from langgraph.graph import StateGraph, START, END

sys.path.insert(0, str(Path(__file__).resolve().parent))
from state import AppState  # noqa: E402
import explanation_agent  # noqa: E402
import scoring_agent  # noqa: E402
import user_agent  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 자동으로 풀어도 되는 조건. **예산은 절대 넣지 않는다** - 못 낼 요금제를
# 보여주는 건 아무것도 안 보여주는 것보다 나쁘다. 예산이 문제일 때는 완화
# 대신 "최소 얼마가 필요한지"를 알려주고 사용자가 정하게 한다.
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
    return {"match_result": scoring_agent.match(state["user_profile"])}


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


def explanation(state: AppState) -> dict:
    if not state["profiling_complete"]:
        text = explanation_agent.ask_more(
            {"questions": state["profiling_questions"]}
        )
        return {"report_text": text}

    text = explanation_agent.report(
        state["match_result"],
        {"questions": state["profiling_questions"]},
    )
    if state["relaxed_slots"]:
        labels = [scoring_agent._RELAXABLE.get(s, s) for s in state["relaxed_slots"]]
        # 자동으로 푼 조건은 반드시 밝힌다. 안 밝히면 사용자는 자기가 말한
        # 조건이 지켜진 결과라고 오해한다.
        text = f"({', '.join(labels)} 조건은 맞는 게 없어 빼고 찾았습니다)\n\n{text}"
    return {"report_text": text}


def _after_profiling(state: AppState) -> str:
    return "plan_matching" if state["profiling_complete"] else "explanation"


def _after_matching(state: AppState) -> str:
    """후보가 없고 아직 재시도 여유가 있으면 조건을 풀러 간다."""
    if (state["match_result"]["candidate_count"] == 0
            and state["matching_retry_count"] < MAX_RETRY):
        return "relax_conditions"
    return "explanation"


def _after_relax(state: AppState) -> str:
    """풀 게 없어서 재시도 횟수만 올라온 경우엔 그대로 설명으로 넘긴다."""
    return "explanation" if state["matching_retry_count"] >= MAX_RETRY else "plan_matching"


builder = StateGraph(AppState)
builder.add_node("user_profiling", user_profiling)
builder.add_node("plan_matching", plan_matching)
builder.add_node("relax_conditions", relax_conditions)
builder.add_node("explanation", explanation)

builder.add_edge(START, "user_profiling")
builder.add_conditional_edges("user_profiling", _after_profiling,
                              ["plan_matching", "explanation"])
builder.add_conditional_edges("plan_matching", _after_matching,
                              ["relax_conditions", "explanation"])
builder.add_conditional_edges("relax_conditions", _after_relax,
                              ["plan_matching", "explanation"])
builder.add_edge("explanation", END)

graph = builder.compile()


def ask(text: str) -> AppState:
    return graph.invoke({"user_input_raw": text})


def demo():
    """조건 완화 루프가 실제로 도는지 본다. LLM을 안 거치려고 user_profiling을
    직접 채운 state로 그래프 중간부터 돌린다 - 키 없이도 확인할 수 있어야 한다."""
    impossible = {
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
    sub.add_node("plan_matching", plan_matching)
    sub.add_node("relax_conditions", relax_conditions)
    sub.add_node("explanation", explanation)
    sub.add_edge(START, "plan_matching")
    sub.add_conditional_edges("plan_matching", _after_matching,
                              ["relax_conditions", "explanation"])
    sub.add_conditional_edges("relax_conditions", _after_relax,
                              ["plan_matching", "explanation"])
    sub.add_edge("explanation", END)
    result = sub.compile().invoke(impossible)

    assert result["relaxed_slots"], "후보가 0개인데 조건을 하나도 안 풀었다"
    assert "budget_krw" not in result["relaxed_slots"], (
        "예산을 자동으로 풀었다 - 못 낼 요금제를 보여주게 된다"
    )
    assert "budget_krw" in result["user_profile"], "예산 조건이 사라졌다"
    print("=== 조건 완화 루프 ===")
    print(f"푼 조건: {result['relaxed_slots']} / 재시도 {result['matching_retry_count']}회")
    print(f"후보 수: {result['match_result']['candidate_count']}")
    print()
    print(result["report_text"])
    print("\n자체 점검 통과.")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        print(ask(" ".join(sys.argv[1:]))["report_text"])
    else:
        demo()
