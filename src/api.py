"""추천 API (원문 5번 산출물의 백엔드).

엔드포인트는 하나다. 폼이든 채팅이든 같은 곳으로 들어와 `graph.ask_once()`를 탄다.

    uvicorn api:app --reload --app-dir src
    http://localhost:8000/docs
"""

import pandas as pd
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from graph import ask_once
from profile_input import CARRIERS, DATA_BANDS, USAGE_HINTS
from recommend import load_plans

app = FastAPI(title="요금제 추천 API", version="1.0")

# 개발 중에는 Vite(5173)가 다른 포트에서 뜬다. 배포 때는 실제 도메인만 남긴다.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 결과 카드에 실을 컬럼. 요금제 41개 컬럼을 통째로 내보내지 않는다.
CARD_COLUMNS = [
    "plan_id", "plan_name", "carrier_type", "host_mno", "mvno_brand",
    "discounted_fee", "data_gb", "data_unlimited", "data_throttle_speed",
    "daily_data_gb", "voice_unlimited", "sms_unlimited", "network_gen",
    "age_condition", "ott_options", "discount_period_months", "signup_channel",
    "value_score", "fair_price", "savings", "ott_matched", "benefit_monthly",
    "gift_benefit", "source_url",
]


class Ask(BaseModel):
    text: str = Field("", description="자유입력 문장. 비워도 된다")
    budget: int | None = None
    data_band: str | None = Field(None, description=f"{list(DATA_BANDS)} 중 하나")
    usage_hint: str | None = Field(None, description=f"{list(USAGE_HINTS)} 중 하나")
    voice_unlimited: bool | None = None
    sms_unlimited: bool | None = None
    mvno_ok: bool | None = None
    current_carrier: str | None = Field(None, description=f"{list(CARRIERS)} 중 하나")
    age: int | None = None
    ott_want: list[str] | None = None
    ott_required: bool | None = None
    current_fee: int | None = None


@app.get("/options")
def options() -> dict:
    """폼을 그릴 때 쓰는 선택지. 프런트에 상수를 복사해 두지 않기 위한 것."""
    plans = load_plans()
    otts = sorted({o.strip() for s in plans["ott_options"].dropna() for o in s.split("|")})
    return {"data_bands": list(DATA_BANDS), "usage_hints": list(USAGE_HINTS),
            "carriers": list(CARRIERS), "otts": otts, "plan_count": len(plans)}


@app.post("/recommend")
def recommend_api(ask: Ask) -> dict:
    """추천 1건. 필수가 비면 `question`이 채워져 온다(추천은 비어 있음)."""
    form = ask.model_dump(exclude={"text"}, exclude_none=True)
    if "ott_want" in form:
        form["ott_want"] = tuple(form["ott_want"])

    state = ask_once(ask.text or "", **form)
    if state.get("question"):
        return {"question": state["question"], "plans": [], "report": state["report"]}

    df: pd.DataFrame = state["results"]
    cols = [c for c in CARD_COLUMNS if c in df.columns]
    return {
        "question": None,
        "query": state.get("query"),
        "plans": df[cols].where(pd.notna(df[cols]), None).to_dict("records"),
        "dropped": state.get("dropped", []),
        "note": state.get("note"),
        "need_budget": state.get("need_budget"),
        "report": state["report"],
    }


def check() -> None:
    from fastapi.testclient import TestClient

    c = TestClient(app)

    o = c.get("/options").json()
    assert o["plan_count"] > 2_000 and "무제한" in o["data_bands"] and "넷플릭스" in o["otts"]

    r = c.post("/recommend", json={"budget": 30_000, "data_band": "20~50GB",
                                   "current_fee": 69_000}).json()
    assert r["question"] is None and len(r["plans"]) == 5
    assert all(p["discounted_fee"] <= 30_000 for p in r["plans"])
    assert all(p["savings"] == 69_000 - p["discounted_fee"] for p in r["plans"])
    assert r["report"]
    # NaN이 그대로 나가면 프런트에서 JSON 파싱이 깨진다.
    assert "NaN" not in c.post("/recommend", json={"budget": 30_000,
                                                   "data_band": "무제한"}).text

    # 필수가 비면 되물음만 온다.
    q = c.post("/recommend", json={"text": "요금제 추천해줘"}).json()
    assert q["question"] and q["plans"] == []

    # 0건이면 푼 조건이 실려 온다.
    z = c.post("/recommend", json={"budget": 5_000, "data_band": "5GB 미만",
                                   "voice_unlimited": True, "sms_unlimited": True,
                                   "mvno_ok": False}).json()
    assert z["dropped"], z

    # 지금 쓰는 통신사를 주면 그 통신사 온라인 전용은 빠진다(기기변경 불가).
    skt = c.post("/recommend", json={"budget": 90_000, "data_band": "무제한",
                                     "mvno_ok": False, "current_carrier": "SKT"}).json()
    assert all(not (p["signup_channel"] == "SKT 공식몰" and p["plan_name"].count("다이렉트"))
               for p in skt["plans"]), skt["plans"]

    # 사은품 월 환산액이 실려 나간다. 점수에는 안 들어가고 표시용이다.
    gift = c.post("/recommend", json={"budget": 30_000, "data_band": "5~20GB"}).json()
    assert any(p.get("benefit_monthly") for p in gift["plans"]), gift["plans"][0]

    # 가입 채널이 카드마다 실려 나간다. 같은 요금제가 채널에 따라 값이 다르다.
    assert all(p["signup_channel"] for p in r["plans"]), r["plans"][0]

    # 잘못된 입력은 500이 아니라 되물음으로 돌아온다.
    bad = c.post("/recommend", json={"budget": -1, "data_band": "5~20GB"})
    assert bad.status_code == 200 and bad.json()["question"], bad.text

    print("check ok")


if __name__ == "__main__":
    check()
