from __future__ import annotations

from app.bootstrap import create_plan_update_app


def run_update(sources: list[str] | None = None) -> dict:
    graph = create_plan_update_app()
    return graph.invoke(
        {
            "trigger": "MANUAL",
            "requested_sources": sources or [],
        }
    )


if __name__ == "__main__":
    result = run_update()
    print(result["final_update_message"])
