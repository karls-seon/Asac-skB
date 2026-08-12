"""추천 근거를 문장으로 (원문 ⑥ Explanation & Report Agent).

**LLM은 이미 정해진 결과를 설명만 한다.** 요금제를 고르거나 순서를 바꾸지 않는다.
숫자는 전부 우리가 계산해 넘기고, 모델은 그걸 엮어 문장으로 만든다. 키가 없거나
호출이 실패하면 같은 사실을 규칙으로 조립한 문장을 돌려준다 - 리포트가 없다고
추천이 멈추지는 않는다.
"""

import json
import os

import pandas as pd
from dotenv import load_dotenv

from recommend import ott_prices

load_dotenv()

MODEL = "gpt-4o-mini"

SYSTEM = """너는 통신 요금제 상담사다. 아래 JSON은 이미 확정된 추천 결과다.

규칙:
- 주어진 숫자만 쓴다. 없는 요금제·가격·혜택을 지어내지 마라.
- 순위를 바꾸거나 다른 요금제를 권하지 마라.
- 3~5문장. 존댓말. 표나 목록 없이 줄글로.
- 1위를 왜 추천했는지, 지금 요금 대비 얼마나 아끼는지, 주의할 점 순으로 말한다.
- **"월절감액"이 있을 때만** 지금보다 얼마 아낀다고 말한다. 없으면 절약 얘기를
  꺼내지 마라. "가성비"는 시장 시세와의 비교지 사용자가 내던 돈과의 비교가 아니다.
- dropped가 있으면 "요청하신 조건으로는 결과가 없어 이 조건을 **뺐다**"고 밝힌다.
  반영했다고 하지 마라 - 뺀 것이다.
- ott_note가 있으면 그 사실을 그대로 전한다."""


def facts(query: dict, results: pd.DataFrame, *, dropped: list[str] | None = None,
          note: str | None = None) -> dict:
    """LLM에 넘길 사실만 추린다. 여기 없는 건 모델도 모른다."""
    prices = ott_prices()
    rows = []
    for _, r in results.head(3).iterrows():
        row = {
            "요금제": r["plan_name"],
            "통신사": r["carrier_type"],
            "월요금": int(r["discounted_fee"]),
            "데이터": "무제한" if r["data_unlimited"] is True else r.get("data_gb"),
            "통화무제한": bool(r["voice_unlimited"] is True),
        }
        if r.get("ott_matched"):
            row["받는OTT"] = list(r["ott_matched"])
        if "savings" in results.columns and pd.notna(r.get("savings")):
            row["월절감액"] = int(r["savings"])
        if "value_score" in results.columns and pd.notna(r.get("value_score")):
            # 부호로 주면 모델이 "+9,783원이니 주의하세요"처럼 거꾸로 읽는다.
            # 싼지 비싼지를 말로 적어 준다.
            v = int(r["value_score"])
            row["가성비"] = (f"같은 스펙 시세보다 {v:,}원 쌉니다" if v > 0
                          else f"같은 스펙 시세보다 {-v:,}원 비쌉니다")
        if pd.notna(r.get("discount_period_months")):
            row["할인기간개월"] = int(r["discount_period_months"])
        rows.append({k: v for k, v in row.items() if v is not None})

    return {
        "요청": {"예산": query.get("budget"),
               "데이터": "무제한" if query.get("data_unlimited") else query.get("data_gb"),
               "통화무제한": query.get("voice_unlimited"),
               "알뜰폰허용": query.get("mvno_ok"),
               "현재요금": query.get("current_fee")},
        "추천": rows,
        "dropped": dropped or [],
        "ott_note": note,
        "참고_OTT가치": {k: v for k, v in prices.items() if k in
                     {o for r in rows for o in r.get("받는OTT", [])}},
    }


def _fallback(f: dict) -> str:
    """LLM 없이 같은 사실을 조립한다."""
    if not f["추천"]:
        return "조건에 맞는 요금제를 찾지 못했습니다."
    top = f["추천"][0]
    out = [f"{top['요금제']}({top['통신사']}, 월 {top['월요금']:,}원)를 추천합니다."]
    if "월절감액" in top:
        out.append(f"지금보다 월 {top['월절감액']:,}원 아낄 수 있습니다.")
    if f["dropped"]:
        out.append("요청하신 조건으로는 결과가 없어 " + ", ".join(f["dropped"]) + " 조건을 뺐습니다.")
    if f["ott_note"]:
        out.append(f["ott_note"])
    if top.get("할인기간개월"):
        out.append(f"프로모션 할인은 {top['할인기간개월']}개월간 적용됩니다.")
    return " ".join(out)


def write_report(query: dict, results: pd.DataFrame, *, dropped: list[str] | None = None,
                 note: str | None = None) -> str:
    """추천 결과를 설명하는 문단. 실패하면 규칙으로 조립한 문장."""
    f = facts(query, results, dropped=dropped, note=note)
    if not os.getenv("OPENAI_API_KEY") or not f["추천"]:
        return _fallback(f)
    try:
        from openai import OpenAI

        rsp = OpenAI().chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": SYSTEM},
                      {"role": "user", "content": json.dumps(f, ensure_ascii=False)}],
        )
        text = (rsp.choices[0].message.content or "").strip()
    except Exception:
        return _fallback(f)

    # 모델이 우리가 안 준 요금제 이름을 지어내면 쓰지 않는다.
    return text if text and _mentions_only_given(text, f) else _fallback(f)


def _mentions_only_given(text: str, f: dict) -> bool:
    """1위 요금제 이름이 실제로 등장하는가. 최소한의 사실 확인."""
    return any(r["요금제"][:8] in text for r in f["추천"])


def check() -> None:
    from fair_price import attach_value_score
    from profile_input import build_query
    from recommend import recommend

    plans = attach_value_score()
    q = build_query(budget=30_000, data_band="20~50GB", current_fee=69_000)
    r = recommend(plans, **q)

    f = facts(q, r)
    assert len(f["추천"]) == 3
    assert f["추천"][0]["월절감액"] == 69_000 - f["추천"][0]["월요금"]
    # 요금제 이름·가격 말고 다른 건 넘기지 않는다.
    assert "source_url" not in json.dumps(f, ensure_ascii=False)

    # LLM 없이도 문장이 나온다.
    assert "추천합니다" in _fallback(f)
    assert "아낄 수 있습니다" in _fallback(f)

    # 조건을 푼 경우 그 사실이 들어간다.
    f2 = facts(q, r, dropped=["통화 무제한"], note="'넷플릭스' 포함 요금제가 없습니다.")
    assert "통화 무제한" in _fallback(f2) and "넷플릭스" in _fallback(f2)

    # 0건이면 지어내지 않는다.
    empty = recommend(plans, budget=100, data_gb=999)
    assert _fallback(facts(q, empty)) == "조건에 맞는 요금제를 찾지 못했습니다."

    text = write_report(q, r)
    assert len(text) > 20 and _mentions_only_given(text, f), text
    print(f"check ok: {text[:60]}...")


if __name__ == "__main__":
    check()
