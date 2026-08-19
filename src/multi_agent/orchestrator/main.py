from graph import app
from schemas import GraphState


if __name__ == "__main__":
    initial_state: GraphState = {
        "input_type": "chat",
        "raw_input": "한달에 15기가 정도 쓰고 통화는 많이 안해요. 넷플릭스 결합되면 좋겠어요.",
        "form_data": None,
        "profile": None,
        "candidates": [],
        "result": None,
        "report_text": None,
    }

    final_state = app.invoke(initial_state)

    print("\n--- 최종 State ---")
    print("profile:", final_state["profile"])
    print("candidates 개수:", len(final_state["candidates"]))
    print("report_text:", final_state["report_text"])
