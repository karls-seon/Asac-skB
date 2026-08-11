"""추천 워크플로 (원문 ① Orchestrator Agent).

노드가 **기존 순수 함수를 부르기만 한다.** 로직은 여기 없다 - 웹·노트북·에이전트가
같은 `recommend()`를 쓰게 하려는 것이고, 모듈별 자체 검증이 그 함수들에 걸려 있다.

    profile ─┬─(필수 미충족)─> ask ─> END        되물을 문장을 남기고 끝
             └─> retrieve ─> rank ─> report ─> END

    python src/graph.py "3만원대 50기가 넷플릭스 있으면 좋겠어"
"""

import sys
from typing import Any, TypedDict

import pandas as pd
from langgraph.graph import END, START, StateGraph

from fair_price import attach_value_score
from profile_input import InputError, next_question, to_query, update_slots
from recommend import TOP_N, min_budget_for, ott_note, relax
from report import write_report


class State(TypedDict, total=False):
    """에이전트들이 주고받는 상태. 한 번의 추천 요청이 이 하나로 표현된다."""
    text: str                   # 자유입력 문장 (없어도 됨)
    form: dict                  # 폼에서 온 값 (LLM보다 우선)
    slots: dict                 # 모인 슬롯
    question: str               # 되물을 문장 (있으면 추천을 안 한다)
    query: dict                 # recommend() 인자
    plans: Any                  # 요금제 테이블 + 가성비 점수
    results: Any                # 추천 상위 N개
    dropped: list[str]          # 0건이라 푼 조건
    note: str                   # OTT 안내
    need_budget: int            # 다 풀어도 0건일 때 필요한 최소 예산
    report: str                 # 사람이 읽는 설명


# 요금제 테이블과 회귀 학습은 요청마다 하면 4.3초씩 든다. 한 번만 만든다.
_PLANS: pd.DataFrame | None = None


def _plans() -> pd.DataFrame:
    global _PLANS
    if _PLANS is None:
        _PLANS = attach_value_score()
    return _PLANS


def profile(state: State) -> State:
    """② User Profiling - 입력을 슬롯으로. 필수가 비면 되물을 문장을 남긴다."""
    slots = dict(state.get("slots") or {})
    if state.get("text"):
        slots = update_slots(slots, state["text"])
    # 폼 값이 LLM 추출보다 세다. 사용자가 직접 고른 것이기 때문이다.
    slots.update({k: v for k, v in (state.get("form") or {}).items() if v is not None})

    out: State = {"slots": slots}
    try:
        out["query"] = to_query(slots)
    except InputError as e:
        out["question"] = str(e) or next_question(slots) or "조건을 다시 알려주세요."
    return out


def retrieve(state: State) -> State:
    """③ Data Retrieval - 최신 요금제 테이블(+가성비 점수)을 올린다."""
    return {"plans": _plans()}


def rank(state: State) -> State:
    """⑤ Plan Matching & Ranking - 필터 + 랭킹. 0건이면 조건을 풀어 재시도."""
    plans, query = state["plans"], state["query"]
    results, dropped = relax(plans, query, top_n=TOP_N)

    out: State = {"results": results, "dropped": dropped}
    if results.empty:
        need = min_budget_for(plans, query)
        if need:
            out["need_budget"] = need
        return out

    # 안내는 상위 N개가 아니라 조건을 통과한 후보 전체를 보고 판단해야 한다.
    q = {**query, **{k: v for k, v in _relaxed_pairs(dropped)}}
    cands = relax(plans, {**q}, top_n=99_999)[0]
    note = ott_note(plans, cands, query.get("ott_want", ()), query.get("budget"))
    if note:
        out["note"] = note
    return out


def _relaxed_pairs(dropped: list[str]):
    from recommend import RELAXABLE
    return [(key, off) for key, off, label in RELAXABLE if label in dropped]


def report(state: State) -> State:
    """⑥ Explanation & Report - 결과를 문장으로. 결과가 없으면 이유를 말한다."""
    results = state.get("results")
    if results is None or results.empty:
        need = state.get("need_budget")
        msg = "조건에 맞는 요금제를 찾지 못했습니다."
        if need:
            msg += f" 예산을 월 {need:,}원까지 올리면 추천할 수 있습니다."
        return {"report": msg}

    return {"report": write_report(state["query"], results,
                                   dropped=state.get("dropped"), note=state.get("note"))}


def ask(state: State) -> State:
    """되물어야 할 때는 추천 단계로 가지 않는다."""
    return {"report": state["question"]}


def _needs_question(state: State) -> str:
    return "ask" if state.get("question") else "retrieve"


def build_graph():
    g = StateGraph(State)
    for name, fn in [("profile", profile), ("retrieve", retrieve),
                     ("rank", rank), ("report", report), ("ask", ask)]:
        g.add_node(name, fn)

    g.add_edge(START, "profile")
    g.add_conditional_edges("profile", _needs_question,
                            {"ask": "ask", "retrieve": "retrieve"})
    g.add_edge("retrieve", "rank")
    g.add_edge("rank", "report")
    g.add_edge("report", END)
    g.add_edge("ask", END)
    return g.compile()


GRAPH = build_graph()


def ask_once(text: str = "", **form) -> State:
    """한 번 돌린다. 되물을 게 있으면 `question`이 채워져 온다."""
    return GRAPH.invoke({"text": text, "form": form})


def check() -> None:
    # 되물음 경로: 필수가 비면 추천으로 안 넘어간다.
    s = ask_once("요금제 추천해줘", **{})
    assert s.get("question") and "results" not in s, s.keys()

    # 정상 경로: 폼만으로도 끝까지 간다.
    s = ask_once(budget=30_000, data_band="20~50GB", current_fee=69_000)
    assert "question" not in s
    assert len(s["results"]) == TOP_N
    assert (s["results"]["discounted_fee"] <= 30_000).all()
    assert s["report"] and len(s["report"]) > 20

    # 폼이 자유입력보다 세다.
    s = ask_once("5만원까지 쓸게요", budget=10_000, data_band="5GB 미만")
    assert s["query"]["budget"] == 10_000, s["query"]

    # 0건 경로: 조건을 풀고, 무엇을 풀었는지 남긴다.
    s = ask_once(budget=5_000, data_band="5GB 미만", voice_unlimited=True,
                 sms_unlimited=True, mvno_ok=False)
    assert s["dropped"], s
    assert s["report"]

    # 아무리 풀어도 안 되면 얼마면 되는지 말한다.
    s = ask_once(budget=1_000, data_band="50~100GB", mvno_ok=False)
    assert s["results"].empty
    assert "원까지 올리면" in s["report"], s["report"]

    print("check ok")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        state = ask_once(" ".join(sys.argv[1:]))
        if state.get("question"):
            print("[되물음]", state["question"])
        else:
            cols = ["plan_name", "carrier_type", "discounted_fee", "data_gb"]
            if "savings" in state["results"]:
                cols.append("savings")
            print(state["results"][cols].to_string(index=False))
            if state.get("dropped"):
                print("\n[푼 조건]", ", ".join(state["dropped"]))
            print("\n" + state["report"])
    else:
        check()
