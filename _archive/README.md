# _archive — 지금 파이프라인에서 빠진 코드

지운 게 아니라 옮긴 것이고, `git mv`를 썼으므로 커밋 이력이 그대로 따라옵니다.

## `notebooks/`

| 파일 | 사유 |
|---|---|
| `eda_plans.ipynb` | 데이터 탐색용. 런타임 의존성이 아님 |

## 여기 없는 것

**추천 에이전트**는 2026-08-10에 전부 **삭제**했습니다(`graph.py`, `state.py`,
`user_agent.py`, `scoring_agent.py`, `explanation_agent.py`, `ask.py`,
`live_graph.py`, `docs/에이전트_설계.md`, `docs/멀티에이전트_아키텍처.md`).
합성 고객 생성기(`analysis/generate_synthetic_customers.py`)와 `data/synthetic/`도
같은 날 지웠습니다. 세그먼트 설계를 처음부터 다시 하기로 해서, 옛 가정이 박힌
코드가 남아 있으면 새 설계를 끌어당기기 때문입니다. 필요하면 git 이력에서
되살릴 수 있습니다.

**수집 파이프라인**은 한때 `pipeline/`으로 옮겼다가 되돌렸습니다.
`crawl_*.py` / `merge_plans.py` / `refresh_plans.py` / `schema_drift.py`는 모두
`src/`에서 계속 돌아갑니다(README.md 참고). 데이터 갱신은 추천 흐름에 연결하지
말고 **배치로 따로** 돌리세요 — 요청 한 번이 34분짜리가 됩니다.
