# 통신사 요금제 비교 추천 프로젝트

SK브로드밴드 연계 프로젝트. AI 기반 맞춤형 통신사·요금제 비교 추천 서비스.

## 현재 상태

추천 로직·API·프론트엔드는 설계를 처음부터 다시 하기 위해 지웠다. 지금 레포에는
**데이터 수집 파이프라인**과 **세그먼트 실험**만 남아 있다.

## 데이터 수집

KT·SKT·LG U+·모요(알뜰폰) 요금제를 직접 크롤링한다(스마트초이스·공공데이터 API는
쓰지 않기로 함 — 사유는 `src/refresh_plans.py`, `src/crawl_*.py` 참고).

```bash
python src/refresh_plans.py              # 수집 -> 파싱 -> 병합 -> 검증까지 전부
python src/refresh_plans.py --parse-only # 캐시로 파싱·비교만 (재수집 없음)
```

파이프라인 단계: 수집(`crawl_kt.py` / `crawl_skt.py` / `crawl_lguplus.py` /
`crawl_moyo.py`) → 병합·필터(`merge_plans.py`) → 이전 최종본과 비교 →
`data/review/`에 리포트 기록 → `src/agents/data_verify.py`로 표본 대조.

- `src/schema.py` : 통합 스키마(요금제 1행 + 혜택 long-format 1행)
- `src/agents/data_retrieval_agent.py` : 최종 CSV를 읽기 전용으로 검증해 내려줌
- `src/agents/schema_drift.py` : 사이트 구조가 바뀌어 파서가 조용히 0건을 내는 상황 탐지
- `data/final/` : 최종 산출물 2종 (`통신요금제_통합데이터_최종.csv`, `_혜택상세_최종.csv`)
- `data/interim/`, `data/raw_cache/` : 사이트별 중간 CSV / 원본 캐시
- `data/review/` : 일일 갱신 리포트

## 세그먼트 실험

`src/make_synthetic_mvno.py`로 알뜰폰 요금제 가입자 수 비율 기반 합성 고객
4만 명을 만들고, `segment_ml_check.ipynb` / `segment_test.ipynb`에서 세그먼트
분류가 실제로 추천에 쓸 만한 신호를 주는지 검증한다.

```bash
python src/make_synthetic_mvno.py            # 합성 고객 생성
python src/make_synthetic_mvno.py --check    # 자체 검증만
```

근거 문서: `docs/합성데이터_생성근거.md`, `docs/컬럼_명세서.md`.

## 멀티에이전트

`telecom_orchestrator_with_plan_update/` — LangGraph 오케스트레이터. 담당 구분은
`OWNERSHIP.md`, 구조·계약 설명은 그 폴더의 `README.md` 참고.

추천 Workflow와 요금제 갱신 Workflow가 분리돼 있고, 각 에이전트는 `app/mocks/`의
baseline 구현으로 붙어 있다. 팀원 구현이 끝나면 `app/bootstrap.py`에서 교체한다.

```bash
cd telecom_orchestrator_with_plan_update
pip install -r requirements.txt
python -m app.main           # 추천 Workflow 1회 실행
python -m app.update_main    # 요금제 갱신 Workflow 1회 실행
pytest -q
```

요금제 데이터는 현재 그 폴더의 `data/plans.csv`, `data/benefits.csv`(2026-08-11
스냅샷 복사본)를 쓴다. `data/final/`의 일일 갱신본으로 바꾸려면
`app/repositories/plan_repository.py`에 넘기는 경로만 바꾸면 된다.

## 다음

추천 로직 구현 중(노드는 아직 스텁).
