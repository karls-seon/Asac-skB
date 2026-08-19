from app.bootstrap import create_app


def test_recommendation_workflow_runs_end_to_end():
    graph = create_app()

    result = graph.invoke(
        {
            "user_query": "KT망 알뜰폰 중에서 월 3만원 이하 데이터 20GB 이상 추천해줘",
            "retry_count": 0,
            "max_retry": 2,
            "validation_errors": [],
            "validation_warnings": [],
        },
        config={"recursion_limit": 20},
    )

    assert result["intent"] == "PLAN_RECOMMENDATION"
    assert result["user_profile"].budget_krw == 30000
    assert result["user_profile"].preferred_carrier == "KT"
    assert result["validation_passed"] is True
    assert result["recommendations"]
    assert result["final_response"]


def test_missing_preferences_returns_followup():
    graph = create_app()

    result = graph.invoke(
        {
            "user_query": "요금제 추천해줘",
            "retry_count": 0,
            "max_retry": 2,
            "validation_errors": [],
            "validation_warnings": [],
        }
    )

    assert result["needs_user_input"] is True
    assert "원하는 월 예산" in result["final_response"]
