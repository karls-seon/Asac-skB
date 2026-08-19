# 담당 코드 구분

## 민재 담당 — Orchestrator / State

### 추천 Workflow
- `app/schemas/state.py`
- `app/orchestrator/interfaces.py`
- `app/orchestrator/nodes.py`
- `app/orchestrator/routers.py`
- `app/orchestrator/workflow.py`
- `app/orchestrator/dependencies.py`
- `app/bootstrap.py`

### 요금제 갱신 Workflow
- `app/schemas/plan_update.py`
- `app/orchestrator/update_nodes.py`
- `app/orchestrator/update_workflow.py`
- `app/update_main.py`

민재는 실제 크롤링 로직을 작성하는 것이 아니라,
Plan Update Agent가 어떤 인터페이스로 호출되고 어떤 결과를 State에 남기며
Workflow가 어떻게 실행되는지 관리합니다.

## 민재 주도 + 팀 합의 공통 Schema
- `app/schemas/user_profile.py`
- `app/schemas/recommendation.py`
- `app/schemas/plan_update.py`

## 민재 담당이 아닌 코드

### User Analysis 담당자
- 최종 `UserAnalyzer` 구현
- 현재 `app/mocks/default_user_analysis.py`는 테스트용

### Recommendation 담당자
- 최종 `Recommender` 구현
- 현재 `app/mocks/default_recommender.py`는 테스트용

### Validation 담당자
- 최종 `Validator` 구현
- 현재 `app/mocks/default_validator.py`는 테스트용

### Response 담당자
- 최종 `Responder` 구현
- 현재 `app/mocks/default_responder.py`는 테스트용

### Plan Data / Update 담당자
- 실제 SKT / KT / LGU+ / 알뜰폰 크롤러
- 데이터 정규화
- 신규/변경/종료 요금제 판별
- DB upsert / inactive 처리
- 최종 `PlanUpdater.refresh()` 구현

### Repository
- `app/repositories/plan_repository.py`
- Plan Data 담당 영역이지만 Orchestrator와 연결되는 method signature는 공동 합의

## 두 Workflow의 관계

```text
[Plan Update Workflow]
Scheduler
   ↓
Plan Update Agent
   ↓
크롤링 / 정규화 / DB 갱신
   ↓
Plan DB
   ↑
Plan Search
   ↑
[Recommendation Workflow]
User → User Analysis → Search → Recommendation → Validation → Response
```

추천 요청마다 크롤링하지 않습니다.
Plan Update Workflow가 독립적으로 DB를 갱신하고,
Recommendation Workflow는 현재 DB를 조회합니다.
