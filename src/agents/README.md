src/agents/
├── schemas.py                 # 데이터 계약 (오케스트레이터 소유)
├── nodes/
│   ├── __init__.py
│   ├── profiling.py           # 담당자가 이 안만 채우면 됨
│   ├── retrieval.py
│   ├── matching.py
│   └── report.py
├── graph.py                   # 노드 연결/순서 (오케스트레이터 소유)
├── main.py                    # 실행 진입점
│
├── data_retrieval_agent.py    # 최종 CSV 읽기 전용 검증·조회 (retrieval 노드에서 재사용)
├── data_verify.py             # 최종본 표본을 원본 캐시와 대조
└── schema_drift.py            # 사이트 구조 변경으로 파서가 조용히 0건 낼 때 탐지
