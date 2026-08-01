"""LangGraph 그래프 정의. 지금은 Data Retrieval Agent 하나만 들어 있다.

LangGraph 기본 개념 3가지
  · State : 에이전트들이 공유하는 dict(TypedDict, state.AppState). 노드가
            return한 dict가 기존 state 위에 얹어진다(같은 키는 덮어쓰기).
  · Node  : "state -> 부분 state(dict)"를 돌려주는 평범한 함수 하나.
            add_node("이름", 함수)로 그래프에 등록한다.
  · Edge  : 어떤 노드 다음에 어떤 노드로 갈지 정하는 연결. START/END는
            LangGraph가 제공하는 시작/끝 표시(실제 노드가 아니다).

지금은 START -> data_retrieval -> END 로 끝나는 1노드 그래프다. 다음
에이전트(User Profiling 등)를 만들면 END 대신 그 노드로 이어붙이면 된다:
    builder.add_edge("data_retrieval", "user_profiling")
    builder.add_edge("user_profiling", END)

실행:
    python src/agents/graph.py

Windows 작업 스케줄러 등 사람이 안 보는 환경에서 돌리면 콘솔 출력은 아무도
안 본다. 그래서 매 실행 결과를 data/review/agent_run_log.jsonl에 한 줄씩
남긴다 - 자동 실행이 조용히 실패해도 이 파일만 보면 언제 무슨 일이 있었는지
알 수 있다.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from langgraph.graph import StateGraph, START, END

# 이 폴더를 경로에 넣어야 아래 import가 **어디서 실행하든** 동작한다.
# python이 스크립트를 직접 실행할 때만 스크립트 폴더를 자동으로 넣어주므로,
# 이게 없으면 나중에 오케스트레이터가 `from agents.graph import graph`처럼
# 불러올 때 깨진다.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from state import AppState  # noqa: E402
from data_retrieval_agent import data_retrieval_agent, REVIEW_DIR  # noqa: E402

builder = StateGraph(AppState)
builder.add_node("data_retrieval", data_retrieval_agent)
builder.add_edge(START, "data_retrieval")
builder.add_edge("data_retrieval", END)

graph = builder.compile()

RUN_LOG_PATH = REVIEW_DIR / "agent_run_log.jsonl"


def _log_run(result: dict) -> None:
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "plans": len(result["plans"]),
        "benefits": len(result["benefits"]),
        "data_as_of": result["data_as_of"],
        "refreshed": result["data_refreshed"],
        "stale_aborted": result["data_stale_aborted"],
        "validation_errors": result["data_validation_errors"],
    }
    # jsonl(줄마다 JSON 하나)로 쌓는다 - 매번 전체를 다시 읽고 쓸 필요 없이
    # append만 하면 되고, 스케줄러가 겹쳐 돌아도 한 줄씩은 안 깨진다.
    with open(RUN_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    # 빈 state({})로 시작. 나중에 오케스트레이터가 여기에 사용자 프로필 같은
    # 초기값을 넣어서 invoke하게 된다.
    result = graph.invoke({})
    _log_run(result)

    print(f"\nplans: {len(result['plans'])}행 / benefits: {len(result['benefits'])}행")
    print(f"data_as_of: {result['data_as_of'] or '(성공한 갱신 기록 없음)'}")
    print(f"refreshed: {result['data_refreshed']} / stale_aborted: {result['data_stale_aborted']}")
    print(f"validation_errors: {result['data_validation_errors'] or '없음'}")
    print(f"실행 기록: {RUN_LOG_PATH}")

    # 스케줄러/모니터링이 "실패"로 인식하도록 종료 코드를 0이 아니게 한다.
    # 재수집이 중단된 경우도 실패다 - 이전 CSV가 멀쩡해서 검증을 통과하더라도
    # "이번에 새 데이터를 못 받았다"는 사실은 알려야 한다.
    if result["data_validation_errors"] or result["data_stale_aborted"]:
        raise SystemExit(1)
