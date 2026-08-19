from __future__ import annotations

from pprint import pprint

from app.bootstrap import create_app


def run(query: str) -> dict:
    graph = create_app()

    initial_state = {
        "user_query": query,
        "retry_count": 0,
        "max_retry": 2,
        "candidate_plans": [],
        "recommendations": [],
        "validation_errors": [],
        "validation_warnings": [],
    }

    return graph.invoke(
        initial_state,
        config={"recursion_limit": 20},
    )


if __name__ == "__main__":
    examples = [
        "KT망 알뜰폰 중에서 월 3만원 이하, 데이터 20GB 이상 요금제 추천해줘",
        "5G 요금제 중 5만원 이하로 추천해줘",
        "요금제 추천해줘",
    ]

    for query in examples:
        print("=" * 80)
        print("USER:", query)
        result = run(query)
        print(result["final_response"])
        print()
