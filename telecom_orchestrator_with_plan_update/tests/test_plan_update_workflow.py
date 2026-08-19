from app.bootstrap import create_plan_update_app


def test_plan_update_workflow_runs_with_mock():
    graph = create_plan_update_app()
    result = graph.invoke(
        {
            "trigger": "MANUAL",
            "requested_sources": [],
        }
    )

    assert result["update_succeeded"] is True
    assert result["update_result"].crawled_plan_count > 0
    assert result["final_update_message"]
