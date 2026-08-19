"""그래프 실행 진입점.

    cd src && python -m multi_agent.orchestrator.main
"""
import sys

from .graph import app
from ..schemas import GraphState


def run(raw_input: str) -> GraphState:
    initial_state: GraphState = {
        "input_type": "chat",
        "raw_input": raw_input,
        "form_data": None,
        "profile": None,
        "candidates": [],
        "result": None,
        "report_text": None,
    }
    return app.invoke(initial_state)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    final_state = run("한달에 15기가 정도 쓰고 통화는 많이 안해요. 넷플릭스 결합되면 좋겠어요.")

    print("\n--- 최종 State ---")
    print("profile:", final_state["profile"])
    print("candidates 개수:", len(final_state["candidates"]))
    print("report_text:", final_state["report_text"])
