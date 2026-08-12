# AI 기반 맞춤형 통신사·요금제 비교 추천 서비스

통신 3사(KT / SKT / LG U+)와 알뜰폰 비교 사이트 모요(moyoplan.com)의 요금제·혜택을
매일 수집하고, 사용자 조건에 맞는 요금제를 추천합니다.

**왜 필요한가** — 모요 알뜰폰 2,224개를 전수 대조한 결과, *데이터가 같거나 많고
통화가 같거나 많으면서 더 싼* 대안이 **99.2%**에 존재합니다. 가입자 수(316만 건)로
가중하면 월 평균 **8,113원**이 새고 있습니다.

## 빠른 시작

```bash
pip install -r requirements.txt

python src/graph.py "3만원대 50기가 쓰는데 넷플릭스 있으면 좋겠어"   # CLI로 한 번
```

웹으로 띄우려면 두 개를 같이 실행합니다.

```bash
uvicorn api:app --reload --app-dir src   # 백엔드  http://localhost:8000/docs
cd web && npm install && npm run dev     # 프런트  http://localhost:5173
```

`.env`에 `OPENAI_API_KEY`가 있으면 자유입력 문장 해석과 추천 근거 설명이 켜집니다.
**없어도 추천은 그대로 동작합니다** — 그 두 단계만 건너뜁니다.

## 폴더 구조

```
├── src/
│   │  --- 수집 ---
│   ├── schema.py           통합 스키마 + 공용 파싱 헬퍼 + 경로 상수
│   ├── crawl_kt.py         사이트별 크롤러 (수집 + 파싱)
│   ├── crawl_skt.py
│   ├── crawl_lguplus.py
│   ├── crawl_moyo.py
│   ├── merge_plans.py      중간 CSV -> 최종 CSV 병합 (수집 범위 필터도 여기)
│   ├── refresh_plans.py    ★ 일일 갱신 (수집→병합→이전본과 비교→반영)
│   ├── agents/
│   │   ├── schema_drift.py         사이트 개편으로 파싱이 조용히 깨졌는지 진단
│   │   ├── data_verify.py          갱신분을 원문과 표본 대조 (LLM, 진단만)
│   │   └── data_retrieval_agent.py 최종 CSV 읽기 + 구조 검증 (읽기 전용)
│   │  --- 추천 ---
│   ├── profile_input.py    입력 검증 · 자유입력 슬롯 추출 · 되묻기
│   ├── recommend.py        ★ 필터 + 랭킹 + 0건 완화 + OTT 가점
│   ├── fair_price.py       스펙 -> 적정가 회귀 (동점을 가르는 가성비 점수)
│   ├── report.py           추천 근거 설명 (LLM, 실패하면 규칙 문장)
│   ├── graph.py            ★ LangGraph 워크플로 (진입점)
│   ├── api.py              FastAPI 엔드포인트
│   │  --- 실험 ---
│   ├── make_synthetic.py   합성 고객 4만 (방식 비교용)
│   └── compare_methods.py  세그먼트 / 요금제군집 / 필터랭킹 3방식 격자 비교
├── web/                    React 19 + Vite (프런트엔드)
├── data/
│   ├── raw_cache/<사이트>/   수집한 원본 HTML·JSON (재파싱용)
│   ├── interim/            사이트별 중간 CSV
│   ├── review/             갱신 리포트 + 방식 비교 결과
│   ├── external/           KISDI 원자료 (git에 없음 — 아래 참고)
│   ├── synthetic/          합성 고객 (git에 없음 — 스크립트가 다시 만듭니다)
│   └── final/              ★ 최종 CSV — 분석·모델링은 여기만 보면 됩니다
│       └── history/        갱신 직전 최종본 백업 (git에 없음 — 로컬 되돌리기용)
├── docs/
│   ├── 요구사항_정의서.md     타겟 사용자, 기능, 유즈케이스, 화면설계
│   ├── 에이전트_설계.md       노드 구성, 상태 스키마, LLM 제약
│   ├── 컬럼_명세서.md        컬럼 정의, 빈값의 의미, 알려진 한계 (합성 데이터 포함)
│   └── 수정이력.md           고친 데이터 오류와 그 이유
├── scenario_compare.ipynb  추천 방식 3개 시나리오 비교 + 세그먼트 방식 해부
└── input_coverage.ipynb    입력 12개가 합성 고객에 다 있는지 + 왕복 검증
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

`openai`/`python-dotenv`는 세 곳에서 씁니다 — `profile_input.py`(자유입력 해석),
`report.py`(추천 근거 설명), `src/agents/data_verify.py`(원문 대조 진단).
키가 없으면 **이 세 단계만** 건너뛰고 추천과 갱신은 그대로 동작합니다.

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
3. **`data/external/`(KISDI 원자료)도 git에 없다.** 한국미디어패널조사에 신청해서
   받은 자료라 재배포하지 않는다. 합성 고객의 나이·성별 분포에만 쓰이고, 없으면
   `make_synthetic.py`가 경고만 찍고 그 두 컬럼을 비운 채 나머지를 만든다.
   필요하면 [미디어통계포털](https://stat.kisdi.re.kr/)에서 직접 신청한 뒤
   `p__gender`·`p__byear`가 든 CSV를 `data/external/`에 두면 자동으로 집어 쓴다.
4. **`data/synthetic/`도 git에 없다.** 고정 시드라 `python src/make_synthetic.py`로
   같은 파일이 다시 만들어진다.

동작 확인 (네트워크 없이 최종 CSV만 읽어 구조 검증):
```bash
python src/agents/data_retrieval_agent.py
```

## 추천은 어떻게 나오는가

입력 8개 중 **필수는 예산과 데이터 둘뿐**입니다. 나머지는 비우면 필터를 통과합니다.

```
입력 → 필터(가격·예산·데이터·통화·문자·통신사·나이)
     → 점수(요금 − OTT 가점)
     → 정렬(점수 → 동점이면 가성비 점수 → 데이터 많은 순)
     → 중복 제거(같은 요금제의 연령·옵션 변형)
     → 상위 5개 + 절감액
```

요청당 **5.8ms**입니다. 앱이 뜰 때 요금제 적재와 회귀 학습에 4.3초가 한 번 듭니다.

**머신러닝은 한 곳에만 씁니다.** 스펙에서 적정가를 예측해(R² 0.934) *예측가 − 실제가*를
가성비 점수로 쓰고, **가격과 스펙이 같아 순서를 정할 근거가 없는 동점**을 가릅니다.
예산 3만원·20GB 조건에서 후보 408개 중 요금 동점이 279개인데, 이 점수로 가르면
14개만 남습니다.

조건에 맞는 요금제가 없으면 **조건을 하나씩 풀고 무엇을 풀었는지 함께 알려줍니다**
(OTT 필수 → 문자 → 통화 → 연령 → 알뜰폰 순). **예산과 데이터는 절대 풀지 않습니다** —
몰래 풀면 못 내는 돈을 추천하게 됩니다.

자세한 설계는 [docs/에이전트_설계.md](docs/에이전트_설계.md)를 보세요.

### 세 가지 방식을 비교한 결과

추천에는 정답이 없어 "정확도"를 잴 수 없습니다. 대신 **사용자가 말한 조건을 지키는지**를
격자 200칸으로 쟀습니다(`python src/compare_methods.py`).

| 방식 | 예산 초과 | 데이터 부족 | 통신사 위반 | 추천 요금 중앙값 |
|---|---|---|---|---|
| A. 세그먼트 분류 (합성 인물 군집) | 48.6% | 67.0% | 0% | 22,125원 |
| B. 요금제 군집 | 10.8% | 46.4% | 44.0% | 12,900원 |
| **C. 필터 + 랭킹** (채택) | **0%** | **0%** | **0%** | **8,200원** |

A는 세그먼트가 6개뿐이라 200칸의 서로 다른 요청이 6가지 답으로 뭉개집니다. B는 군집
축에 통신사가 없어 "알뜰폰 싫다"고 답한 100칸에서 88%가 알뜰폰입니다.

## 검증

각 파일이 자체 검증을 갖고 있습니다. 프레임워크 없이 `assert`만 씁니다.

```bash
python src/recommend.py       # 필터·정렬·중복제거·완화·OTT 가점
python src/fair_price.py      # 회귀 누수·성능·잔차
python src/profile_input.py   # 입력 검증·구간 경계·5턴 대화
python src/graph.py           # 되물음·정상·0건 경로
python src/api.py             # 엔드포인트 응답 형태
python src/report.py          # 사실만 전달하는지
python src/make_synthetic.py --check
```

## 알아둘 점

- **스키마를 바꾸면 4개 크롤러를 모두 다시 돌려야 합니다.** `merge_plans.py`가
  중간 CSV의 컬럼이 `schema.py`와 일치하는지 `assert`로 검사합니다.
- **폴더 구조를 바꾸려면** `schema.py`의 경로 상수(`BASE_DIR`, `DATA_DIR`,
  `RAW_CACHE_DIR`, `INTERIM_DIR`, `FINAL_DIR`)만 수정하면 됩니다. 크롤러는
  이 상수만 쓰므로 손댈 필요가 없습니다.
- `data/raw_cache/`는 500MB가 넘습니다. 버전 관리에 넣지 마세요.
