# Team Agents

각 담당자가 완성한 실제 Agent 구현을 이 폴더에 넣습니다.

예시:

- `user_analysis_agent.py`
- `recommendation_agent.py`
- `validation_agent.py`
- `response_agent.py`
- `plan_update_agent.py`

각 구현은 `app/orchestrator/interfaces.py`에 정의된 Protocol을 만족해야 합니다.

특히 Plan Data 담당자는 다음 형태를 구현하면 됩니다.

```python
class TeamPlanUpdateAgent:
    def refresh(self, *, repository, sources=None) -> PlanUpdateResult:
        # 1. 통신 3사 / 알뜰폰 크롤링
        # 2. 수집 결과 정규화
        # 3. 기존 DB와 비교
        # 4. 신규/변경/종료 요금제 반영
        # 5. PlanUpdateResult 반환
        ...
```

현재 `app/mocks/` 아래 구현들은 Orchestrator 통합 테스트용입니다.
