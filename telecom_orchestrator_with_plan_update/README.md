# Team-ready 구조

```text
app/
├── orchestrator/       # ★ 민재 담당
├── schemas/            # ★ 공통 계약, 민재 주도 + 팀 합의
├── mocks/              # 최종 코드 아님
├── team_agents/        # 각 담당자 실제 Agent가 들어갈 곳
├── repositories/       # Plan Data 담당
├── bootstrap.py        # ★ 최종 Agent 연결
└── main.py             # 실행
```

자세한 담당 구분은 `OWNERSHIP.md`를 확인하세요.

---

# Telecom Multi-Agent Orchestrator

통신 3사 + 알뜰폰 요금제 데이터를 대상으로 하는 **LangGraph 기반 Multi-Agent Orchestrator 골격**입니다.

이 프로젝트의 목적은 Recommendation 알고리즘 자체를 Orchestrator에 섞는 것이 아니라,
각 팀원이 만든 Agent를 동일한 인터페이스로 연결할 수 있게 만드는 것입니다.

## 1. 핵심 아키텍처

```text
User
  |
  v
Intent Node
  |
  v
User Analysis Agent
  |
  v
Plan Repository Search
  |
  v
Recommendation Agent
  |
  v
Validation Agent
  |\
  | \ FAIL
  |  +-----------------------+
  |                          |
 PASS                        v
  |                    Plan Search
  v                          |
Response Agent <-------------+
  |
  v
END
```

별도 운영 흐름인 크롤링/DB 업데이트는 이 그래프에 섞지 않습니다.

```text
Scheduler -> Crawlers -> Normalize -> Plan DB Update
                                  |
                                  v
                           PlanRepository
```

## 2. 파일 구조

```text
app/
├── agents/
│   ├── interfaces.py
│   ├── default_intent.py
│   ├── default_user_analysis.py
│   ├── default_recommender.py
│   ├── default_validator.py
│   └── default_responder.py
├── schemas/
│   ├── user_profile.py
│   ├── recommendation.py
│   └── state.py
├── repositories/
│   └── plan_repository.py
├── services/
│   └── dependencies.py
├── graph/
│   ├── nodes.py
│   ├── routers.py
│   └── workflow.py
├── utils/
│   └── normalization.py
├── bootstrap.py
└── main.py
```

## 3. 역할 분리

### Pydantic Schema
Agent 사이에 주고받는 데이터 계약입니다.

- `UserProfile`
- `CandidatePlan`
- `RecommendationItem`
- `ValidationResult`

### State
`TelecomState`가 전체 Graph 실행 중 공유되는 상태를 정의합니다.

### Repository
CSV/DB 접근을 Agent 로직과 분리합니다.

향후 CSV -> PostgreSQL로 바뀌더라도 `PlanRepository`의 public interface만 유지하면
Graph는 그대로 사용할 수 있습니다.

### Agent Interface
`Protocol`로 다음 계약을 정의했습니다.

- `IntentClassifier.classify()`
- `UserAnalyzer.analyze()`
- `Recommender.recommend()`
- `Validator.validate()`
- `Responder.respond()`

### Nodes
Agent 또는 Repository를 실제 LangGraph Node로 감싸는 계층입니다.

### Routers
Orchestrator의 조건부 흐름만 담당합니다.

- Intent 분기
- 추가 질문 여부
- Validation PASS/FAIL
- Retry limit

### Workflow
Node와 Router를 연결하는 LangGraph 정의입니다.

## 4. 실행

```bash
python -m venv .venv
```

Windows:

```bash
.venv\Scripts\activate
```

설치:

```bash
pip install -r requirements.txt
```

실행:

```bash
python -m app.main
```

테스트:

```bash
pytest -q
```

## 5. 팀원 코드 교체 방법

예를 들어 추천 알고리즘 담당자가 아래 클래스를 구현했다고 가정합니다.

```python
class TeamRecommender:
    def recommend(
        self,
        profile,
        candidates,
        *,
        validation_errors=None,
        top_n=3,
    ):
        ...
```

`bootstrap.py`만 바꿉니다.

```python
return AppDependencies(
    repository=repository,
    intent_classifier=RuleBasedIntentClassifier(),
    user_analyzer=MyLLMUserAnalyzer(),
    recommender=TeamRecommender(),
    validator=TeamValidator(),
    responder=MyLLMResponseAgent(),
)
```

`workflow.py`, `routers.py`, `TelecomState`는 수정하지 않아도 됩니다.

## 6. 현재 baseline 구현의 의미

현재 포함된 Rule-based User Analyzer / Weighted Recommender / Template Responder는
최종 팀 알고리즘이 아닙니다.

목적은 다음 두 가지입니다.

1. 다른 팀원 개발이 끝나기 전에도 Orchestrator 전체 그래프를 실제 실행한다.
2. 팀원 구현이 완성되면 인터페이스 단위로 교체한다.

반면 `PlanRepository`, `State`, `Router`, `Workflow`, Validation retry 구조는
실제 프로젝트 기반으로 그대로 발전시킬 수 있도록 구성했습니다.

## 7. 사용자 입력 예시

```text
KT망 알뜰폰 중에서 월 3만원 이하,
데이터 20GB 이상 요금제 추천해줘
```

User Analysis 결과 예시:

```python
UserProfile(
    budget_krw=30000,
    monthly_data_gb=20,
    carrier_type="MVNO",
    preferred_carrier="KT",
)
```

## 8. Validation 범위

Validation은 다음을 검사합니다.

- plan_id가 실제 DB에 존재하는가
- 예산 조건을 충족하는가
- 데이터 요구량을 충족하는가
- 통신망 조건을 충족하는가
- MNO/MVNO 조건을 충족하는가
- LTE/5G 조건을 충족하는가
- 최소 QoS 조건을 충족하는가
- 통화/문자 무제한 조건을 충족하는가

**크롤링 시점이나 데이터 최신성으로 추천을 중단하는 검증은 넣지 않았습니다.**

## 9. Retry 정책

Validation이 실패하면:

```text
Validation FAIL
    ↓
Plan Search
    ↓
실패한 plan_id 제외
    ↓
Recommendation
    ↓
Validation
```

`max_retry`까지 반복하고 그래도 실패하면 Response Agent에서
조건 완화를 안내합니다.

이 방식은 동일한 잘못된 추천을 반복하는 문제를 줄입니다.

---

# Plan Update / Refresh Workflow 추가

이 버전은 사용자 추천 Workflow와 별도로 요금제 자동 갱신 Workflow를 포함합니다.

```text
Recommendation Workflow
User
  ↓
User Analysis
  ↓
Plan Search ───────────────┐
  ↓                        │
Recommendation            │
  ↓                        │
Validation                 │
  ↓                        │
Response                   │
                           │
                      Plan DB
                           ↑
                           │
Plan Update Workflow       │
Scheduler / Manual         │
  ↓                        │
Plan Update Agent          │
  ↓                        │
Crawling / Normalize       │
  ↓                        │
Insert / Update / Disable ─┘
```

## 현재 Plan Update Mock

`app/mocks/default_plan_updater.py`는 실제 웹 크롤링을 수행하지 않습니다.
현재 CSV를 다시 읽어 Plan Update Workflow 자체가 정상 연결되는지만 확인합니다.

최종 통합 시 Plan Data 담당자가 `PlanUpdater` 인터페이스를 만족하는
실제 크롤링/갱신 Agent를 구현하고 `bootstrap.py`에서 교체합니다.

## 수동 갱신 Workflow 테스트

```bash
python -m app.update_main
```

## 실제 팀 Plan Update Agent 연결 예시

```python
return PlanUpdateDependencies(
    repository=repository,
    plan_updater=TeamPlanUpdateAgent(),
)
```
