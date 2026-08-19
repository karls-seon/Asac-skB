from __future__ import annotations

from pydantic import BaseModel


class EvaluationResult(BaseModel):
    """추천 방식 하나를 격자 입력으로 돌린 결과.

    정답 라벨이 없으므로 "어느 게 정확한가"가 아니라 **어디서 무너지는가**를 잰다.
    TelecomState에는 들어가지 않는다 - 평가는 사용자 요청마다 도는 것이 아니라
    오프라인 배치(`app/evaluation/run_eval.py`)로 돈다.
    """

    method: str                        # "segment" | "plan_cluster" | "filter_score"
    rule_violation_rate: float         # 예산 초과·데이터 부족 추천 비율
    coverage: float                    # 결과가 0건인 입력 비율
    overlap_jaccard: dict[str, float]  # 다른 method -> top_n 겹침
    median_recommended_fee: float
