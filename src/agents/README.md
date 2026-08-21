데이터 품질·공급 도구.

```
src/agents/
├── data_retrieval_agent.py    # 최종 CSV 읽기 전용 검증·조회
├── data_verify.py             # 최종본 표본을 원본 캐시와 대조 (--demo로 자체 점검)
└── schema_drift.py            # 사이트 구조 변경으로 파서가 조용히 0건 낼 때 탐지
```
