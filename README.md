# 통신사 요금제 비교 추천 프로젝트

SK브로드밴드 연계 프로젝트. AI 기반 맞춤형 통신사·요금제 비교 추천 서비스.

## 현재 상태

추천 로직·API·프론트엔드는 설계를 처음부터 다시 하기 위해 지웠다. 지금 레포에는
**데이터 수집 파이프라인**만 남아 있다.

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

컬럼 정의와 수집 범위는 `docs/컬럼_명세서.md` 참고.

## 다음

추천 로직과 멀티에이전트 오케스트레이터를 다시 설계한다.
