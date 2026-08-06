# _archive — PoC 범위 밖으로 옮긴 코드

2026-08-06, PoC를 **4개 에이전트**로 좁히면서 옮겼습니다.
지운 게 아니라 옮긴 것이고, `git mv`를 썼으므로 커밋 이력이 그대로 따라옵니다.

**PoC 범위**: Orchestrator / User Profiling / Data Retrieval / Plan Matching

## 무엇을 왜 옮겼나

### `agents/` — 제외된 에이전트

| 파일 | 사유 |
|---|---|
| `explanation_agent.py` | **Explanation & Report Agent** 본체. 추천 사유를 자연어 문장으로 만드는 역할이라 PoC 범위 밖 |
| `ask.py` | 위 에이전트를 부르던 CLI 진입점. 지금은 `src/agents/graph.py`가 대신함 |
| `live_graph.py` | 배치/실시간으로 그래프가 나뉘어 있을 때의 실시간 쪽. Data Retrieval이 읽기 전용이 되면서 배치 그래프가 사라져 통합했음 |

### `pipeline/` — 수집·갱신 파이프라인

Data Retrieval을 **읽기 전용**으로 줄이면서 런타임에서 빠졌습니다. 예전에는
이 에이전트가 데이터가 오래되면 `refresh_plans.py`를 subprocess로 돌렸는데,
그러면 사용자 요청 한 번에 크롤링이 딸려 갑니다.

| 파일 | 사유 |
|---|---|
| `crawl_kt.py` / `crawl_skt.py` / `crawl_lguplus.py` / `crawl_moyo.py` | 사이트별 수집·파싱 |
| `merge_plans.py` | 중간 CSV -> 최종 CSV 병합 + 수집 범위 필터 |
| `refresh_plans.py` | 위를 묶은 일일 갱신 파이프라인 |
| `schema_drift.py` | 사이트 개편으로 파싱이 조용히 깨졌는지 진단 |

> ⚠️ **`src/schema.py`는 안 옮겼습니다.** 경로 상수와 컬럼 정의를 담고 있어
> Data Retrieval / Plan Matching이 모두 의존합니다.

### `analysis/`, `notebooks/`

| 파일 | 사유 |
|---|---|
| `generate_synthetic_customers.py` | Usage Prediction / Segmentation용 합성 고객 생성기. 그 에이전트가 제외됐고, Plan Matching도 합성 데이터를 안 씀 |
| `eda_plans.ipynb` | 데이터 탐색용. 런타임 의존성이 아님 |

## 되살리는 법

```bash
# 예: 수집 파이프라인을 다시 쓰고 싶을 때
git mv _archive/pipeline/crawl_*.py _archive/pipeline/merge_plans.py \
       _archive/pipeline/refresh_plans.py src/
git mv _archive/pipeline/schema_drift.py src/agents/
pip install requests beautifulsoup4 selenium
python src/refresh_plans.py
```

데이터를 새로 받아야 할 때는 **배치로 따로** 돌리세요. 추천 그래프에
연결하면 안 됩니다(요청 한 번이 34분짜리가 됩니다).

## 옮기면서 같이 바꾼 것

- `src/agents/data_retrieval_agent.py` — 신선도 확인·재수집 트리거를 들어내고
  CSV 읽기 + 구조 검증만 남김
- `src/agents/state.py` — 제외된 에이전트의 필드 제거
  (`report_text`, `drift_*`, `data_refreshed`, `data_stale_aborted`).
  누가 채우는지 모르는 빈 칸을 남기면 이미 구현된 줄 오해하게 됩니다
- `src/agents/graph.py` — 4개 에이전트를 잇는 단일 그래프로 재작성
- `src/agents/scoring_agent.py` — CSV를 직접 읽지 않고 State의 `plans`를
  받도록 `plans` 인자 추가(`from_rows`로 문자열을 숫자·불리언으로 되살림)
- `requirements.txt` — 위 코드가 쓰던 패키지를 주석으로 내림
