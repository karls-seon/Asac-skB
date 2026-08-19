"""추천 방식 3개(세그먼트 분류 / 요금제 군집 / 필터 스코어링)를 같은 입력으로 돌려
비교한다. **Workflow Node가 아니다** - 사용자 요청마다 도는 것이 아니라 오프라인
배치다.

    python -m app.evaluation.run_eval

담당: (배정 예정)

## 왜 이 4개인가

정답 라벨이 없다. 우리가 라벨을 붙이면 ML은 우리 규칙의 근사치를 배울 뿐이라
"정확도"는 성능이 아니다. 그래서 **어디서 무너지는가**만 잰다.

- rule_violation_rate: 예산 초과·데이터 부족 추천 비율
- coverage: 결과가 0건인 입력 비율
- overlap_jaccard: 세 방식의 top_n 겹침 - 다 같으면 비교할 게 없다는 뜻
- median_recommended_fee: 추천 요금 중앙값 (monthly_fee_normalized 기준)

## 입력은 격자로 만든다

예산 x 데이터 x carrier_type x 통화 격자. 합성 고객을 재사용하면 세그먼트 방식이
자기가 학습한 분포에서 시험 보는 셈이라 비교가 기운다.

**세 방식이 예산·데이터 필터를 공유하지 않게 한다.** 공유하면 셋이 같은 후보 안에서
놀아 차이가 안 나고, "세그먼트 방식은 예산을 못 지킨다"가 드러나지 않는다.
즉 `PlanRepository.find_candidates()`를 세 방식이 그대로 같이 쓰면 안 된다.
"""
from __future__ import annotations

import sys

from app.schemas.evaluation import EvaluationResult

METHODS = ("segment", "plan_cluster", "filter_score")


def make_grid() -> list[dict]:
    """평가 입력 격자. 예산 x 데이터 x carrier_type x 통화."""
    raise NotImplementedError("격자 생성 미구현")


def evaluate(method: str, grid: list[dict]) -> EvaluationResult:
    """방식 하나를 격자 전체에 돌리고 지표 4개를 계산한다."""
    raise NotImplementedError(f"{method} 평가 미구현")


def main() -> None:
    grid = make_grid()
    for method in METHODS:
        print(evaluate(method, grid))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
