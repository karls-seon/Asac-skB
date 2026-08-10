# 통신 요금제 수집 파이프라인

통신 3사(KT / SKT / LG U+)와 알뜰폰 비교 사이트 모요(moyoplan.com)의 요금제·혜택을
하나의 스키마로 모읍니다.

## 폴더 구조

```
├── src/                    코드
│   ├── schema.py           통합 스키마 + 공용 파싱 헬퍼 + 경로 상수
│   ├── crawl_kt.py         사이트별 크롤러 (수집 + 파싱)
│   ├── crawl_skt.py
│   ├── crawl_lguplus.py
│   ├── crawl_moyo.py
│   ├── merge_plans.py      중간 CSV -> 최종 CSV 병합 (수집 범위 필터도 여기)
│   ├── refresh_plans.py    ★ 일일 갱신 (수집→병합→이전본과 비교→반영)
│   └── agents/
│       ├── schema_drift.py         사이트 개편으로 파싱이 조용히 깨졌는지 진단
│       ├── data_verify.py          갱신분을 원문과 표본 대조 (LLM, 진단만)
│       └── data_retrieval_agent.py 최종 CSV 읽기 + 구조 검증 (읽기 전용)
├── data/
│   ├── raw_cache/<사이트>/   수집한 원본 HTML·JSON (재파싱용)
│   ├── interim/            사이트별 중간 CSV
│   ├── review/             갱신 리포트 (변경 내역 CSV + 요약 JSON)
│   └── final/              ★ 최종 CSV — 분석·모델링은 여기만 보면 됩니다
│       └── history/        갱신 직전 최종본 백업 (날짜별)
└── docs/
    ├── 컬럼_명세서.md        컬럼 정의, 빈값의 의미, 알려진 한계
    └── 수정이력.md           고친 데이터 오류와 그 이유
```

## 최종 산출물

| 파일 | 내용 | 행 단위 |
|---|---|---|
| `data/final/통신요금제_통합데이터_최종.csv` | 요금제 | 요금제 1개 = 1행 |
| `data/final/통신요금제_혜택상세_최종.csv` | 혜택 | 요금제 1개 × 혜택 1개 = 1행 |

`plan_id`로 조인합니다. 인코딩은 `utf-8-sig`(Excel에서 바로 열림).

```python
import pandas as pd
plans = pd.read_csv("data/final/통신요금제_통합데이터_최종.csv", encoding="utf-8-sig")
benefits = pd.read_csv("data/final/통신요금제_혜택상세_최종.csv", encoding="utf-8-sig")
df = benefits.merge(plans, on="plan_id", how="left")
```

> **컬럼 의미를 반드시 [docs/컬럼_명세서.md](docs/컬럼_명세서.md) 에서 확인하세요.**
> 빈 칸이 "결측"이 아니라 "해당 없음"인 컬럼이 많고(예: MNO는 데이터 초과요금이
> 아예 존재하지 않음), "요금제 개수"는 행 수가 아니라 `nunique(base_plan_id)`로
> 세야 합니다.

## 실행

### 원본은 그대로 두고 다시 파싱만 (권장)

파싱 로직을 고쳤을 때 씁니다. **네트워크 요청이 없어** 빠르고 사이트에 부담이 없습니다.

```bash
python src/crawl_kt.py --parse-only
```

### 전체 재수집

`--parse-only`를 빼면 사이트에서 새로 받아옵니다. 모요는 상세페이지가 2,000개가
넘어서 시간이 걸립니다.

```bash
python src/crawl_kt.py
```

### 병합

4개 사이트를 모두 파싱한 뒤 실행합니다.

```bash
python src/merge_plans.py
```

경로는 `schema.py`가 자기 위치를 기준으로 잡으므로 **어느 디렉터리에서 실행해도**
같은 곳을 읽고 씁니다.

## 갱신 (일 1회)

요금제는 개편·신규출시·단종이 계속 일어나므로 주기적으로 다시 뽑아야 합니다.
아래 하나만 실행하면 수집→파싱→병합→**이전 결과와 비교**→반영까지 끝납니다.

```bash
python src/refresh_plans.py
```

무엇이 달라졌는지는 `data/review/`에 남습니다.

| 파일 | 용도 |
|---|---|
| `summary_YYYY-MM-DD.json` | 신규/변경/단종 건수와 가드 위반 여부. **이것만 보면 볼 게 있는지 판단 가능** |
| `changes_YYYY-MM-DD.csv` | 어떤 요금제의 어떤 값이 어떻게 바뀌었는지 + `source_url` |

- 직전 최종본은 `data/final/history/YYYY-MM-DD/`에 백업되므로 되돌릴 수 있습니다.
- **이상 징후가 있으면 최종 CSV를 갱신하지 않고 중단**합니다(exit code ≠ 0).
  전체 행 수 ±20% 초과 변동, 단종 10% 초과, 특정 통신사 0행, `plan_id` 중복,
  고아 혜택 — 파싱이 조용히 망가진 경우를 잡기 위한 장치입니다.
- 모요는 상세페이지가 2,200개가 넘어서 매번 다 받지 않습니다. 목록 카드에
  요금·데이터·음성·문자가 다 있으므로, **목록을 비교해 신규·변경분의 상세만**
  다시 받습니다(`data/raw_cache/moyo/list_snapshot.json`).

### 사이트 구조가 바뀌었는지 확인

사이트가 개편되면 **에러 없이 데이터만 조용히 사라집니다**(실제로 겪었습니다 —
`docs/수정이력.md` 35번). 갱신할 때마다 자동으로 진단하지만, 따로 돌릴 수도 있습니다.

```bash
python src/agents/schema_drift.py
```

직전에 멀쩡히 파싱되던 소스가 지금은 0행이면 `data/review/drift_YYYY-MM-DD.md`에
어떤 상품이 안 읽히는지 + 파서 앵커 중 무엇이 사라졌는지 적힙니다.
**진단만 하고 코드는 안 고칩니다** — 판단은 사람이 합니다.

> 기준선(`data/review/parse_coverage.json`)은 git에 없습니다. 새 컴퓨터에서는
> 크롤링을 한 번 돌린 뒤 `python src/agents/schema_drift.py --baseline`으로 만듭니다.

### 매일 자동 실행 등록 (Windows 작업 스케줄러)

관리자 PowerShell에서 한 번만 등록하면 됩니다. 시간은 원하는 대로 바꾸세요.

```bash
schtasks /create /tn "요금제 갱신" /tr "python C:\Users\NT551_11TH\Desktop\skB-claude\src\refresh_plans.py" /sc daily /st 05:00
```

## 필요한 패키지

```bash
pip install -r requirements.txt
```

`selenium`(+ Chrome)은 **LG U+ 수집에만** 필요합니다. LG U+ 목록 페이지가 SPA라
브라우저 없이는 카드가 보이지 않습니다. 나머지 3개 사이트와 모든 `--parse-only`
실행은 브라우저가 필요 없습니다.

`openai`/`python-dotenv`는 `src/agents/data_verify.py`(원문 대조 진단)에서만 씁니다.
키가 없으면 이 단계만 건너뛰고 갱신 자체는 정상 동작합니다.

## 새 컴퓨터에서 이어서 작업하기

```bash
git clone https://github.com/karls-seon/Asac-skB.git
cd Asac-skB
pip install -r requirements.txt
copy .env.example .env   # 실제 API 키는 이 파일에 직접 채운다 (git에 안 올라감)
```

**주의할 점 2가지**

1. **Python 3.10 이상 필요** (`X | None` 타입 힌트 문법 때문). 3.12 권장.
2. **`data/raw_cache/`는 git에 없다** (500MB+라 `.gitignore`됨). `data/final`·
   `interim`은 그대로 받아지지만, `--parse-only`로 빠르게 재파싱하고 싶다면
   raw_cache를 다른 방법으로(USB, 클라우드 등) 직접 옮겨야 한다. 없으면
   `python src/refresh_plans.py`(전체 재수집)부터 한 번 돌려서 새로 만들면 된다.

동작 확인 (네트워크 없이 최종 CSV만 읽어 구조 검증):
```bash
python src/agents/data_retrieval_agent.py
```

## 알아둘 점

- **스키마를 바꾸면 4개 크롤러를 모두 다시 돌려야 합니다.** `merge_plans.py`가
  중간 CSV의 컬럼이 `schema.py`와 일치하는지 `assert`로 검사합니다.
- **폴더 구조를 바꾸려면** `schema.py`의 경로 상수(`BASE_DIR`, `DATA_DIR`,
  `RAW_CACHE_DIR`, `INTERIM_DIR`, `FINAL_DIR`)만 수정하면 됩니다. 크롤러는
  이 상수만 쓰므로 손댈 필요가 없습니다.
- `data/raw_cache/`는 500MB가 넘습니다. 버전 관리에 넣지 마세요.
