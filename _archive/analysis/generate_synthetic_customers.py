"""합성 고객 데이터 생성 (연령세그먼트 + 이용형태세그먼트 S1~S6 기반).

팀 노션 설계를 코드로 옮긴 것. 이전 버전(generate_synthetic_users.py, 페르소나 6종
기반)은 이 설계로 대체됐다 - 팀에서 "S1~S6부터 진행하고 이후 세분화하자"고 정했다.

## 파일을 3개로 나눈 이유

**"통신사가 이미 갖고 있는 관측 데이터"와 "상담 시점에 사용자가 답하는 값"은
성격이 다른 데이터라 섞으면 안 된다.** ott_preference 같은 필드를 처음엔
고객 테이블에 그냥 넣었는데, 통신사가 사전에 "이 사람은 넷플릭스를 좋아한다"를
알 방법이 없다는 지적을 받고 나눴다. 이건 가짜 데이터라서 빼야 하는 게 아니라 -
기획서상 User Profiling Agent가 상담 시점에 실시간으로 수집하는 값이라, 원래도
"관측 데이터"가 아니라 "합성 데이터를 만드는 우리가 상담을 진행했다고 가정하고
만들어내는 설문 응답"이다. 그래서 별도 테이블로 뒀다(팀 노션의 "사용자 선호도
테이블 [사후데이터]"가 정확히 이 개념이었다).

  - `synthetic_customers.csv`      : 고객 1명 = 1행. **통신사가 실제로 갖고
                                      있을 법한 사실만** - 나이·이용형태세그먼트·
                                      현재 가입정보. (가입정보는 고객과 1대1이라
                                      따로 안 나눔)
  - `synthetic_usage_monthly.csv`  : 고객 1명 x 3개월 = 3행. 이용량(1대다 관계).
  - `synthetic_preferences.csv`    : 고객 1명 = 1행. **상담 시점 입력을
                                      시뮬레이션한 값** - OTT 선호, 다음 요금제
                                      통신사 선호, 필수 조건(데이터/음성/문자
                                      무제한 필요 여부), 약정 허용 여부, 현재
                                      요금제 만족도 등.

팀 설계에 있던 "3개월 요약 VIEW"/"적합도 VIEW"는 파일로 안 만든다. SQL VIEW가
저장되는 테이블이 아니라 조회 시점에 계산되는 것처럼, 이것도 나중에 분석/에이전트
코드에서 `usage_monthly.groupby('customer_id').agg(...)`로 그때그때 만들면 된다.

## 현재 가입 요금제를 어떻게 배정하는가 - 이게 전체 시나리오의 성패를 가른다

무작위로 배정하면 비현실적이고, 이용형태에 딱 맞게 배정하면 다들 적합 판정이
나와서 추천할 이유가 없어진다. 그래서 **의도적으로 배정 품질을 섞는다**
(`ASSIGNMENT_MIX`) - 60%는 대략 맞는 요금제, 20%는 일부러 부족한 요금제, 20%는
일부러 과한 요금제. 이래야 팀 설계의 적합도 판정(OVERPROVISIONED~
REPEATED_SHORTAGE)이 실제로 다양하게 나와서 검증이 된다.

이 배정에 쓰는 통신사 비율(`AGE_MNO_RATIO`)은 "선호"가 아니라 **연령대별 실제
시장 분포 가정**이다(연령 조건부 요금제가 EDA상 전량 MNO였다는 사실 반영) -
"지금 가입돼 있는 통신사가 어느 쪽에 더 몰려 있을까"를 정하는 것뿐이라
`synthetic_preferences.csv`의 "다음 요금제로 원하는 통신사"와는 다른 개념이다
(같은 사람이 지금은 MNO를 쓰지만 다음엔 MVNO로 갈아타고 싶어할 수 있다).

배정 의도는 `assignment_intent` 컬럼에 **정답 라벨로 남긴다.** 실제 서비스라면
알 수 없는 값이지만(우리가 왜 이 요금제를 줬는지는 생성 시점에만 아는 정보),
이 데이터는 적합도 판정 로직을 검증하기 위한 것이므로 정답을 같이 남겨야
"우리 판정 로직이 의도한 대로 GOOD_FIT/SHORTAGE/OVER를 잡아내는지" 확인할 수
있다. **적합도 판정이나 추천 로직 자체가 이 컬럼을 읽으면 안 된다** - 그러면
답을 미리 보고 채점하는 것과 같다. (단, `synthetic_preferences.csv`의
`current_plan_satisfaction`을 만들 때는 이 라벨을 참고해서 만든다 - "부족하게
배정된 사람은 만족도가 낮다"는 현실적인 상관관계를 주기 위한 것으로, 이건
데이터를 그럴듯하게 만드는 생성 로직일 뿐 추천 로직이 아니라서 문제없다.)

실행:
    python src/analysis/generate_synthetic_customers.py
    python src/analysis/generate_synthetic_customers.py --validate
"""
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from schema import BASE_DIR, final_path  # noqa: E402

SYNTHETIC_DIR = BASE_DIR / "data" / "synthetic"
CUSTOMERS_CSV = SYNTHETIC_DIR / "synthetic_customers.csv"
USAGE_CSV = SYNTHETIC_DIR / "synthetic_usage_monthly.csv"
PREFERENCES_CSV = SYNTHETIC_DIR / "synthetic_preferences.csv"

SEED = 42
N_CUSTOMERS = 3_500          # + 이용량 3행/고객 = 총 14,000행 ("1만 행 이상" 요건)
USAGE_MONTHS = ["2026-05", "2026-06", "2026-07"]
REFERENCE_DATE = date(2026, 8, 3)   # subscription_start_date 역산 기준(오늘)

# ---------------- ① 연령세그먼트 ----------------
# 경계는 임의로 정하지 않고 요금제 데이터의 실제 age_condition 정규화 기준
# (schema.normalize_age_condition)에 맞췄다 - 그래야 "가입 가능 요금제 필터링"이
# 실제 데이터와 어긋나지 않는다. population_weight는 실제 통신 가입자 구성을
# 참고한 가정이다(어린이는 본인 명의 개통이 드묾, 시니어는 절대 수는 적지 않되
# 청년/일반보다는 낮게 - 사용자 피드백 반영).
AGE_SEGMENTS = {
    "CHILD":   {"age_range": (6, 12),  "population_weight": 0.04},
    "TEEN":    {"age_range": (13, 18), "population_weight": 0.08},
    "YOUTH":   {"age_range": (19, 34), "population_weight": 0.28},
    "GENERAL": {"age_range": (35, 64), "population_weight": 0.42},
    "SENIOR":  {"age_range": (65, 85), "population_weight": 0.18},
}

# "현재 가입돼 있는 통신사"의 연령대별 시장 분포 가정 (선호도 아님 - 위 모듈
# docstring 참고). 연령 조건부 요금제(청년 덤·시니어 특화 등)가 EDA상 전량
# MNO였다 - 그 조건을 실제로 쓸 법한 세그먼트일수록 비율을 높였다. 키즈
# 요금제도 MNO 전용이라 CHILD도 높다.
AGE_MNO_MARKET_RATIO = {"CHILD": 0.75, "TEEN": 0.70, "YOUTH": 0.55, "GENERAL": 0.45, "SENIOR": 0.65}

# ---------------- ② 이용형태세그먼트 S1~S6 (팀 설계 그대로) ----------------
USAGE_SEGMENTS = {
    "S1_저사용절약형":        {"data_gb": (0.5, 8),   "voice_min": (30, 200),   "sms": (5, 80),   "tethering_gb": (0, 2)},
    "S2_일반균형형":          {"data_gb": (8, 30),    "voice_min": (100, 400),  "sms": (10, 150), "tethering_gb": (0, 8)},
    "S3_데이터헤비형":        {"data_gb": (40, 150),  "voice_min": (50, 350),   "sms": (5, 100),  "tethering_gb": (5, 30)},
    "S4_통화집중저데이터형":  {"data_gb": (1, 10),    "voice_min": (500, 2000), "sms": (30, 300), "tethering_gb": (0, 3)},
    "S5_통화집중균형형":      {"data_gb": (10, 40),   "voice_min": (500, 2000), "sms": (30, 300), "tethering_gb": (0, 10)},
    "S6_업무헤비형":          {"data_gb": (50, 200),  "voice_min": (500, 2000), "sms": (30, 500), "tethering_gb": (20, 100)},
}

# 나이대별로 그럴듯한 이용형태 확률(완전 무작위 조합 방지 - "8세 아이가
# 업무헤비형"은 안 나오게). 각 행의 합은 1.0.
AGE_USAGE_WEIGHTS = {
    "CHILD":   {"S1_저사용절약형": 0.70, "S2_일반균형형": 0.30},
    "TEEN":    {"S1_저사용절약형": 0.40, "S2_일반균형형": 0.50, "S3_데이터헤비형": 0.10},
    "YOUTH":   {"S1_저사용절약형": 0.15, "S2_일반균형형": 0.35, "S3_데이터헤비형": 0.35,
                "S4_통화집중저데이터형": 0.05, "S5_통화집중균형형": 0.05, "S6_업무헤비형": 0.05},
    "GENERAL": {"S1_저사용절약형": 0.10, "S2_일반균형형": 0.25, "S3_데이터헤비형": 0.15,
                "S4_통화집중저데이터형": 0.10, "S5_통화집중균형형": 0.15, "S6_업무헤비형": 0.25},
    "SENIOR":  {"S1_저사용절약형": 0.35, "S2_일반균형형": 0.10,
                "S4_통화집중저데이터형": 0.45, "S5_통화집중균형형": 0.10},
}

# ---------------- ③ 현재 가입 요금제 배정 의도 (검증용 정답 라벨) ----------------
# good: 이용형태에 대략 맞는 요금제 / shortage: 일부러 부족하게 / over: 일부러 과하게
ASSIGNMENT_MIX = {"good": 0.60, "shortage": 0.20, "over": 0.20}

UNLIMITED_SENTINEL = 9999.0  # data_unlimited=True인 요금제를 수치 비교에 넣기 위한 값

_AGE_UPPER_RE = re.compile(r"만\s*(\d+)\s*세\s*이하")
_AGE_LOWER_RE = re.compile(r"만\s*(\d+)\s*세\s*이상")


def _load_plans() -> pd.DataFrame:
    df = pd.read_csv(final_path("통신요금제_통합데이터_최종.csv"), encoding="utf-8-sig", dtype=str)
    df["data_unlimited"] = df["data_unlimited"] == "True"
    df["voice_unlimited"] = df["voice_unlimited"] == "True"
    df["effective_data_gb"] = np.where(
        df["data_unlimited"], UNLIMITED_SENTINEL, pd.to_numeric(df["data_gb"], errors="coerce"))
    df["discounted_fee"] = pd.to_numeric(df["discounted_fee"], errors="coerce")
    df["age_condition"] = df["age_condition"].fillna("")
    df["discount_type"] = df["discount_type"].fillna("")
    return df.dropna(subset=["effective_data_gb", "discounted_fee"])


def _eligible_plans(plans: pd.DataFrame, age: int, market_carrier: str) -> pd.DataFrame:
    """나이 자격 조건을 만족하는 요금제만 남긴다. 시장분포상 통신사 우선, 없으면 전체로 폴백.

    schema.normalize_age_condition이 만든 "만 N세 이하/이상" 표기만 해석한다.
    "현역병사"처럼 나이가 아닌 자격 조건은 일반 소비자 시나리오에 안 맞으므로
    자격 없음으로 취급해 제외한다(더 안전한 쪽으로).
    """
    def _allowed(cond: str) -> bool:
        if not cond:
            return True
        m = _AGE_UPPER_RE.search(cond)
        if m:
            return age <= int(m.group(1))
        m = _AGE_LOWER_RE.search(cond)
        if m:
            return age >= int(m.group(1))
        return False  # "현역병사" 등 나이 아닌 조건 - 일반 배정 대상에서 제외

    pool = plans[plans["age_condition"].map(_allowed)]
    narrowed = pool[pool["carrier_type"] == market_carrier]
    # 시장분포상 통신사에 자격 되는 요금제가 아예 없을 수 있다(예: CHILD가
    # MVNO로 뽑혔는데 키즈 요금제는 MNO뿐인 경우) - 그럴 땐 전체 자격 풀로 넘어간다.
    return narrowed if len(narrowed) else pool


def _pick_plan(rng: np.random.Generator, eligible: pd.DataFrame, need_gb: float, intent: str) -> pd.Series:
    """intent(good/shortage/over)에 맞는 데이터 제공량 구간에서 요금제 하나를 고른다.

    원하는 구간에 후보가 없으면(예: 이미 최소 요금제인데 더 낮은 shortage가 없음)
    조용히 포기하지 않고 **가장 가까운 대안**으로 물러난다 - 없는 데이터를
    강요하지 않으면서도 항상 하나는 배정한다.
    """
    if intent == "shortage":
        cand = eligible[eligible["effective_data_gb"] < need_gb]
        if cand.empty:
            return eligible.loc[eligible["effective_data_gb"].idxmin()]
    elif intent == "over":
        cand = eligible[eligible["effective_data_gb"] > need_gb * 3]
        if cand.empty:
            return eligible.loc[eligible["effective_data_gb"].idxmax()]
    else:  # good
        cand = eligible[eligible["effective_data_gb"].between(need_gb, need_gb * 1.8)]
        if cand.empty:
            # need_gb 이상 중 가장 가까운 것 - 그마저 없으면 전체 중 가장 큰 것
            above = eligible[eligible["effective_data_gb"] >= need_gb]
            cand = above if len(above) else eligible
    idx = rng.integers(len(cand))
    return cand.iloc[idx]


def generate_customers(rng: np.random.Generator, plans: pd.DataFrame) -> pd.DataFrame:
    """통신사가 실제로 갖고 있을 법한 사실만 담는다 - 나이·이용형태·현재 가입정보."""
    rows = []
    cid = 1
    for age_seg, spec in AGE_SEGMENTS.items():
        n = round(N_CUSTOMERS * spec["population_weight"])
        usage_names = list(AGE_USAGE_WEIGHTS[age_seg].keys())
        usage_probs = list(AGE_USAGE_WEIGHTS[age_seg].values())

        for _ in range(n):
            age = int(rng.integers(spec["age_range"][0], spec["age_range"][1] + 1))
            usage_seg = rng.choice(usage_names, p=usage_probs)
            market_carrier = "MNO" if rng.random() < AGE_MNO_MARKET_RATIO[age_seg] else "MVNO"

            u = USAGE_SEGMENTS[usage_seg]
            need_gb = (u["data_gb"][0] + u["data_gb"][1]) / 2  # 세그먼트 중간값 = "적정 필요량" 기준

            intent = rng.choice(list(ASSIGNMENT_MIX), p=list(ASSIGNMENT_MIX.values()))
            eligible = _eligible_plans(plans, age, market_carrier)
            plan = _pick_plan(rng, eligible, need_gb, intent)

            start_days_ago = int(rng.integers(30, 36 * 30))
            rows.append({
                "customer_id": f"CUS{cid:06d}",
                "age": age,
                "age_segment": age_seg,
                "usage_segment": usage_seg,
                "gender": rng.choice(["M", "F"]),
                "plan_code": plan["plan_id"],
                "subscription_start_date": (REFERENCE_DATE - timedelta(days=start_days_ago)).isoformat(),
                "discount_tf": bool(plan["discount_type"]),
                "actual_plan_charge_krw": int(plan["discounted_fee"]),
                # 검증용 정답 라벨 - 추천/적합도 판정 로직에서 참조 금지 (모듈 docstring 참고)
                "assignment_intent": intent,
            })
            cid += 1
    return pd.DataFrame(rows)


def generate_usage_monthly(rng: np.random.Generator, customers: pd.DataFrame) -> pd.DataFrame:
    """고객별 3개월 이용량. 매달 값이 달라야 "평균 vs 피크" 비교가 의미 있어진다.

    고객마다 세그먼트 구간 내에서 개인 기준값(baseline)을 하나 뽑고, 그 기준값을
    매달 ±25% 흔든다. 10%는 한 달만 크게 튀는(spike) 고객으로 만들어
    PEAK_SHORTAGE류 시나리오가 실제로 나오게 한다.
    """
    rows = []
    for _, cust in customers.iterrows():
        seg = USAGE_SEGMENTS[cust["usage_segment"]]
        baseline = {
            "data_usage_gb": rng.uniform(*seg["data_gb"]),
            "voice_usage_min": rng.uniform(*seg["voice_min"]),
            "sms_usage_count": rng.uniform(*seg["sms"]),
            "tethering_usage_gb": rng.uniform(*seg["tethering_gb"]),
        }
        spike_month = rng.integers(3) if rng.random() < 0.10 else None

        for i, month in enumerate(USAGE_MONTHS):
            jitter = rng.uniform(0.75, 1.25)
            spike = 1.8 if i == spike_month else 1.0
            rows.append({
                "customer_id": cust["customer_id"],
                "usage_month": month,
                "data_usage_gb": round(max(0.1, baseline["data_usage_gb"] * jitter * spike), 1),
                "voice_usage_min": round(max(0, baseline["voice_usage_min"] * jitter)),
                "sms_usage_count": round(max(0, baseline["sms_usage_count"] * jitter)),
                "tethering_usage_gb": round(max(0.0, baseline["tethering_usage_gb"] * jitter * spike), 1),
            })
    return pd.DataFrame(rows)


# ---------------- 상담 시점 입력 시뮬레이션 (synthetic_preferences.csv) ----------------
# 팀 노션의 "사용자 선호도 테이블 [사후데이터]"에 대응. User Profiling Agent가
# 실제로는 사용자와의 대화/폼에서 실시간으로 받을 값들이라, 통신사 관측 데이터가
# 아니라 "상담을 진행했다고 가정하고 우리가 만들어내는 응답"이다.
OTT_CHOICES = ("넷플릭스", "디즈니플러스", "없음")
AGE_OTT_PREF = {
    "CHILD":   (0.10, 0.40, 0.50),
    "TEEN":    (0.30, 0.25, 0.45),
    "YOUTH":   (0.45, 0.15, 0.40),
    "GENERAL": (0.30, 0.25, 0.45),
    "SENIOR":  (0.05, 0.05, 0.90),
}
# "다음 요금제로 원하는 통신사" - 지금 쓰는 통신사(시장분포로 배정됨)와 달리
# 상담 시점에 표현하는 희망이라 갈아탈 의향이 섞여 있다. 그래도 연령대별
# 성향(연령조건부 혜택을 쓸 법한 세그먼트일수록 MNO 선호)은 유지한다.
AGE_NEXT_MNO_PREF = {"CHILD": 0.70, "TEEN": 0.65, "YOUTH": 0.50, "GENERAL": 0.40, "SENIOR": 0.60}

NETWORK_GEN_CHOICES = ("5G", "LTE", "무관")
THROTTLE_SPEED_CHOICES = (1, 2, 5, 10)  # Mbps. "무관"은 별도 처리 안 하고 그냥 낮은 값 선호로 취급


def generate_preferences(rng: np.random.Generator, customers: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, cust in customers.iterrows():
        age_seg = cust["age_segment"]
        usage_seg = cust["usage_segment"]
        u = USAGE_SEGMENTS[usage_seg]

        # 필수조건 요구는 세그먼트의 상한이 얼마나 큰지에 비례하게 뒀다 - 헤비유저일수록
        # "무제한이어야 한다"고 답할 확률이 높다는 상식적 가정.
        data_unlimited_required = bool(rng.random() < min(0.9, u["data_gb"][1] / 200))
        voice_unlimited_required = bool(rng.random() < min(0.9, u["voice_min"][1] / 2200))
        sms_unlimited_required = bool(rng.random() < 0.3)  # 대부분 문자는 크게 신경 안 씀(EDA: 거의 무제한)

        next_mno_pref = rng.random() < AGE_NEXT_MNO_PREF[age_seg]

        # 만족도는 지금 요금제가 실제로 얼마나 안 맞게 배정됐는지(assignment_intent)에
        # 상관관계를 준다 - 부족하게 배정된 사람이 만족도가 높게 나오면 그게 더 비현실적이다.
        # (assignment_intent는 정답 라벨일 뿐 추천 로직에 노출 안 됨 - 모듈 docstring 참고)
        satisfaction_center = {"good": 4.0, "over": 3.0, "shortage": 2.0}[cust["assignment_intent"]]
        satisfaction = int(np.clip(round(rng.normal(satisfaction_center, 0.8)), 1, 5))

        rows.append({
            "customer_id": cust["customer_id"],
            "preferred_network_gen": rng.choice(NETWORK_GEN_CHOICES, p=(0.4, 0.3, 0.3)),
            "data_unlimited_required": data_unlimited_required,
            "voice_unlimited_required": voice_unlimited_required,
            "sms_unlimited_required": sms_unlimited_required,
            "ott_preference": rng.choice(OTT_CHOICES, p=AGE_OTT_PREF[age_seg]),
            "preferred_carrier_type": "MNO" if next_mno_pref else "MVNO",
            "contract_discount_ok": bool(rng.random() < 0.7),  # 약정 자체를 꺼리는 사람은 소수
            "current_plan_satisfaction": satisfaction,
            "min_throttle_speed_mbps": int(rng.choice(THROTTLE_SPEED_CHOICES)),
        })
    return pd.DataFrame(rows)


def validate(customers: pd.DataFrame, usage: pd.DataFrame, prefs: pd.DataFrame) -> None:
    print("\n=== 연령세그먼트 x 이용형태세그먼트 인원 ===")
    print(pd.crosstab(customers["age_segment"], customers["usage_segment"]).to_string())

    print("\n=== 요금제 배정 의도 분포 (검증용 정답 라벨) ===")
    print(customers["assignment_intent"].value_counts(normalize=True).round(2).to_string())

    print("\n=== 이용량 월별 변동 확인 (첫 고객) ===")
    first_id = customers["customer_id"].iloc[0]
    print(usage[usage["customer_id"] == first_id].to_string(index=False))

    print("\n=== 만족도 vs 배정의도 상관관계 확인 ===")
    merged = customers[["customer_id", "assignment_intent"]].merge(prefs, on="customer_id")
    print(merged.groupby("assignment_intent")["current_plan_satisfaction"].describe()[["mean", "std"]].to_string())

    print("\n=== 선호 테이블 요약 ===")
    for col in ["preferred_network_gen", "ott_preference", "preferred_carrier_type"]:
        print(f"\n{col}:", prefs[col].value_counts(normalize=True).round(2).to_dict())


def main() -> None:
    rng = np.random.default_rng(SEED)
    plans = _load_plans()

    customers = generate_customers(rng, plans)
    usage = generate_usage_monthly(rng, customers)
    prefs = generate_preferences(rng, customers)

    SYNTHETIC_DIR.mkdir(parents=True, exist_ok=True)
    customers.to_csv(CUSTOMERS_CSV, index=False, encoding="utf-8-sig")
    usage.to_csv(USAGE_CSV, index=False, encoding="utf-8-sig")
    prefs.to_csv(PREFERENCES_CSV, index=False, encoding="utf-8-sig")
    print(f"저장 완료: {CUSTOMERS_CSV.relative_to(BASE_DIR)} ({len(customers)}행)")
    print(f"저장 완료: {USAGE_CSV.relative_to(BASE_DIR)} ({len(usage)}행)")
    print(f"저장 완료: {PREFERENCES_CSV.relative_to(BASE_DIR)} ({len(prefs)}행)")
    print(f"총 행수: {len(customers) + len(usage) + len(prefs)}")

    if "--validate" in sys.argv:
        validate(customers, usage, prefs)


if __name__ == "__main__":
    main()
