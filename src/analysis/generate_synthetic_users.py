"""합성 유저(가입자) 데이터 생성.

실제 통신 가입자 이용패턴 데이터가 없어서(API 접근 불가 - docs/페르소나_설계.md 참고)
페르소나 가설을 파라미터화해 그럴듯한 유저 데이터를 만든다. 여기서 만든 데이터는
"정답"이 아니라 다음 phase(Usage Prediction/Segmentation Agent의 K-means/GMM 군집화)가
돌아갈 재료다 - 군집화 결과를 이 페르소나와 대조해 검증하는 게 다음 단계의 일이다.

페르소나별 파라미터(연령대, 데이터/통화/문자량 평균·표준편차, 예산, 통신사 선호 비율)는
docs/페르소나_설계.md의 표를 코드 상수로 그대로 옮긴 것이다. 표를 고치면 이 상수도 같이
고쳐야 한다.

실행:
    python src/analysis/generate_synthetic_users.py              # 생성
    python src/analysis/generate_synthetic_users.py --validate   # 생성 + 페르소나별 요약 통계 출력
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from schema import BASE_DIR  # noqa: E402

SYNTHETIC_DIR = BASE_DIR / "data" / "synthetic"
OUT_CSV = SYNTHETIC_DIR / "synthetic_users.csv"

SEED = 42
N_PER_PERSONA = 120

# docs/페르소나_설계.md의 표와 1:1 대응. mu/sigma는 정규분포 파라미터,
# age_range는 균등분포 구간(정수), mno_ratio는 preferred_carrier_type이 MNO일 확률.
PERSONAS = [
    {
        "name": "자취생 헤비유저", "age_range": (20, 29),
        "data_gb": (80, 40), "call_minutes": (150, 80), "sms_count": (20, 15),
        "budget_won": (75_000, 12_000), "mno_ratio": 0.85,
    },
    {
        "name": "가성비 알뜰폰 알뜰족", "age_range": (25, 55),
        "data_gb": (6, 3), "call_minutes": (100, 60), "sms_count": (10, 8),
        "budget_won": (9_000, 3_000), "mno_ratio": 0.05,
    },
    {
        "name": "청년 실속형", "age_range": (19, 34),
        "data_gb": (20, 10), "call_minutes": (180, 90), "sms_count": (20, 15),
        # mno_ratio: 노트북 EDA에서 연령 전용 요금제(청년 덤 등)가 전량 MNO로 확인돼
        # 원래 가정(0.50)보다 올림 - docs/페르소나_설계.md 참고.
        "budget_won": (25_000, 8_000), "mno_ratio": 0.70,
    },
    {
        "name": "청소년/키즈 보호자용", "age_range": (8, 18),
        "data_gb": (4, 2), "call_minutes": (60, 40), "sms_count": (30, 20),
        # mno_ratio: 위 청년 실속형과 같은 이유(연령 전용 요금제 전량 MNO)로 올림.
        "budget_won": (12_000, 4_000), "mno_ratio": 0.75,
    },
    {
        "name": "시니어 실속형", "age_range": (65, 85),
        "data_gb": (3, 2), "call_minutes": (200, 100), "sms_count": (15, 10),
        # mno_ratio: 위와 동일한 이유로 올림.
        "budget_won": (15_000, 6_000), "mno_ratio": 0.70,
    },
    {
        "name": "가족 결합 다회선 헤비유저", "age_range": (35, 55),
        "data_gb": (100, 40), "call_minutes": (250, 100), "sms_count": (15, 10),
        "budget_won": (88_000, 8_000), "mno_ratio": 0.90,
    },
]

# 각 필드의 물리적 하한. 정규분포는 음수도 뽑을 수 있어서 클리핑이 필요하다.
MIN_DATA_GB = 0.5
MIN_CALL_MINUTES = 0
MIN_SMS_COUNT = 0
MIN_BUDGET_WON = 3_000  # 최저가 요금제 수준 이하로는 안 내려가게


def _sample_persona(rng: np.random.Generator, persona: dict, start_id: int) -> pd.DataFrame:
    n = N_PER_PERSONA
    age = rng.integers(persona["age_range"][0], persona["age_range"][1] + 1, size=n)
    data_gb = np.clip(rng.normal(*persona["data_gb"], size=n), MIN_DATA_GB, None).round(1)
    call_minutes = np.clip(rng.normal(*persona["call_minutes"], size=n), MIN_CALL_MINUTES, None).round().astype(int)
    sms_count = np.clip(rng.normal(*persona["sms_count"], size=n), MIN_SMS_COUNT, None).round().astype(int)
    budget_won = np.clip(rng.normal(*persona["budget_won"], size=n), MIN_BUDGET_WON, None).round(-2).astype(int)
    is_mno = rng.random(size=n) < persona["mno_ratio"]
    preferred = np.where(is_mno, "MNO", "MVNO")

    return pd.DataFrame({
        "user_id": [f"U{start_id + i:05d}" for i in range(n)],
        "persona": persona["name"],
        "age": age,
        "monthly_call_minutes": call_minutes,
        "monthly_data_gb": data_gb,
        "monthly_sms_count": sms_count,
        "monthly_budget_won": budget_won,
        "preferred_carrier_type": preferred,
    })


def generate() -> pd.DataFrame:
    rng = np.random.default_rng(SEED)
    frames = []
    next_id = 1
    # PERSONAS 리스트 순서 그대로 순차 샘플링해야 시드 고정 시 항상 같은 결과가 나온다.
    for persona in PERSONAS:
        frames.append(_sample_persona(rng, persona, next_id))
        next_id += N_PER_PERSONA
    return pd.concat(frames, ignore_index=True)


def validate(df: pd.DataFrame) -> None:
    print("\n=== 페르소나별 요약 통계 ===")
    cols = ["age", "monthly_call_minutes", "monthly_data_gb", "monthly_sms_count", "monthly_budget_won"]
    for name, group in df.groupby("persona", sort=False):
        print(f"\n[{name}] n={len(group)}")
        print(group[cols].describe().loc[["mean", "std", "min", "max"]].round(1).to_string())
        print("preferred_carrier_type:", group["preferred_carrier_type"].value_counts().to_dict())


def main() -> None:
    df = generate()
    SYNTHETIC_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(f"저장 완료: {OUT_CSV.relative_to(BASE_DIR)} ({len(df)}행)")

    if "--validate" in sys.argv:
        validate(df)


if __name__ == "__main__":
    main()
