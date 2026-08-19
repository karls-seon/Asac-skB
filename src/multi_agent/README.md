# multi_agent — 팀원 1인 1에이전트

LangGraph 멀티에이전트. **폴더 하나 = 담당자 한 명**. 자기 폴더 안에서만 작업하면
서로 충돌하지 않는다.

## 담당표

| 폴더 | 에이전트 | 담당 | 진행 |
|---|---|---|---|
| `profiling/` | User Profiling — 채팅/폼 입력을 `UserProfile`로 | (배정 예정) | 스텁 |
| `matching/` | Plan Matching & Ranking — 필터 + 비용 + 스코어 | (배정 예정) | 스텁 |
| `report/` | Explanation & Report — 결과를 자연어로 | (배정 예정) | 스텁 |
| `evaluation/` | Evaluation — 추천 방식 3개 비교 지표 | (배정 예정) | 스텁 |
| `orchestrator/` | 그래프 조립 + 계약 관리 | (배정 예정) | 동작 |

과제 원문의 에이전트 7종 중 2종은 **폴더를 안 만든다**:

- **Data Retrieval**: 이미 `src/agents/data_retrieval_agent.py`(읽기 전용 CSV 공급)로
  있다. 그래프에서는 `orchestrator/retrieval_step.py`가 그걸 불러 `PlanCandidate`로
  바꿔 준다. 담당자 없이 오케스트레이터가 유지한다.
- **Usage Prediction·Segmentation**: 합성 고객·세그먼트 실험이
  `src/make_synthetic_mvno.py` + `segment_*.ipynb`에 있고, 아직 에이전트로 감쌀
  단계가 아니다. 결과가 정리되면 그때 폴더를 만든다.

## 규칙

1. **계약은 `schemas.py` 하나뿐.** 오케스트레이터가 소유한다. 필드를 추가·변경하고
   싶으면 팀 논의 후 이 파일만 고친다. 자기 폴더에서 임의로 dict 키를 늘리지 않는다.
2. **노드는 `GraphState`를 받아 `GraphState`를 돌려준다.** 다른 노드의 키는 읽기만
   하고 자기 출력 키만 쓴다(`profiling`→`profile`, `matching`→`result`,
   `report`→`report_text`).
3. **자기 폴더 밖 파일은 건드리지 않는다.** 그래프 순서·분기를 바꿔야 하면
   오케스트레이터에게 말한다.
4. LLM 프롬프트는 자기 폴더에 `prompt.py`로 둔다. 노드 코드에 긴 문자열을 박지 않는다.
5. **숫자는 LLM이 만들지 않는다.** 리포트에 나가는 가격·절감액은 `result`에 있는
   값을 그대로 주입한다.

## 실행

`src`를 패키지 루트로 잡고 모듈로 돌린다(경로 조작 코드가 필요 없다).

```bash
cd src
python -m multi_agent.orchestrator.main        # 그래프 1회 실행
python -m multi_agent.orchestrator.test_orchestrator   # 배선 + 데이터 매핑 점검
python -m multi_agent.profiling.test_profiling         # 각자 자기 폴더 점검
```

## 완료 조건 (에이전트별 공통)

- `node.py`에서 `NotImplementedError`가 사라진다.
- `test_<이름>.py`가 통과한다. 프레임워크 없이 `assert`만 쓴다(이 레포 관행 —
  `src/agents/data_verify.py --demo` 참고).
- 입력이 비었거나 앞 노드가 실패한 경우를 정하고 처리한다. 조용히 빈 값을 흘려보내면
  뒤 노드가 엉뚱한 결과를 만든다.
