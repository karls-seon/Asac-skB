import pytest

from app.evaluation.run_eval import METHODS, evaluate, make_grid
from app.schemas.evaluation import EvaluationResult


def test_evaluation_contract():
    try:
        grid = make_grid()
    except NotImplementedError as e:
        pytest.skip(f"아직 스텁: {e}")

    assert len(grid) >= 100, f"격자가 너무 작아 비율 지표가 흔들린다: {len(grid)}건"

    results = {}
    for method in METHODS:
        r = evaluate(method, grid)
        assert isinstance(r, EvaluationResult)
        assert 0 <= r.rule_violation_rate <= 1
        assert 0 <= r.coverage <= 1
        results[method] = r

    # 셋이 완전히 같은 답을 내면 비교 자체가 성립하지 않는다 - 필터를 공유했는지 의심
    assert any(v < 0.99 for r in results.values() for v in r.overlap_jaccard.values()), (
        "세 방식 결과가 사실상 동일하다 - 예산·데이터 필터를 공유하고 있지 않은지 확인"
    )
