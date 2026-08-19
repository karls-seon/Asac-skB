"""최종 CSV 행을 `PlanCandidate`로 바꿔 그래프에 올린다.

**담당자 없는 노드다.** 과제 원문의 Data Retrieval 에이전트에 해당하지만 실제
조회는 이미 `src/agents/data_retrieval_agent.py`(읽기 전용, 검증 포함)가 한다.
여기서는 그 결과를 계약 타입으로 옮기고 프로필로 1차 축소만 한다 - 오케스트레이터가
유지하고, 추천 로직은 matching 담당자 몫이다.
"""

from agents.data_retrieval_agent import data_retrieval_agent

from ..schemas import GraphState, PlanCandidate


def _int(v) -> int | None:
    v = (v or "").strip()
    return int(float(v)) if v else None


def _float(v) -> float | None:
    v = (v or "").strip()
    return float(v) if v else None


def _bool(v) -> bool:
    return (v or "").strip().lower() == "true"


def _qos_mbps(v) -> float | None:
    """"400Kbps" -> 0.4, "3Mbps" -> 3.0. 빈 값이면 소진 후 차단(=None)."""
    v = (v or "").strip()
    if not v:
        return None
    num = _float("".join(c for c in v if c.isdigit() or c == "."))
    if num is None:
        return None
    return num / 1000 if "kbps" in v.lower() else num


def _benefits(row: dict) -> list[str]:
    out = []
    if _int(row.get("ott_option_count")):
        out.append("OTT")
    if (row.get("membership_grade") or "").strip():
        out.append("membership")
    if (row.get("tethering_support") or "") in ("quota", "within_data"):
        out.append("tethering")
    return out


def to_candidate(row: dict) -> PlanCandidate:
    """CSV 한 행 -> PlanCandidate.

    monthly_fee_normalized는 **24개월 기준 가중 평균**이다. 프로모션가가 N개월만
    적용되므로 정가끼리 비교하면 "6개월 0원" 요금제가 실제보다 비싸 보이고,
    프로모션가끼리 비교하면 반대로 싸 보인다.
    """
    original = _int(row.get("monthly_fee")) or 0
    promo = _int(row.get("discounted_fee"))
    months = _int(row.get("discount_period_months"))
    if promo is not None and promo != original and months:
        capped = min(months, 24)
        normalized = (promo * capped + original * (24 - capped)) / 24
    else:
        normalized = float(promo if promo is not None else original)

    return PlanCandidate(
        plan_id=row["plan_id"],
        brand=(row.get("mvno_brand") or row.get("host_mno") or "").strip(),
        # ponytail: 브랜드-법인 매핑 테이블이 아직 없다. 필요해지면 그때 만든다.
        legal_entity=None,
        host_mno=row["host_mno"],
        monthly_fee_original=original,
        monthly_fee_promo=promo if promo != original else None,
        discount_period_months=months,
        monthly_fee_normalized=normalized,
        data_gb=_float(row.get("data_gb")),
        data_unlimited=_bool(row.get("data_unlimited")),
        qos_speed_mbps=_qos_mbps(row.get("data_throttle_speed")),
        voice_minutes=_int(row.get("voice_minutes")),
        voice_unlimited=_bool(row.get("voice_unlimited")),
        sms_count=_int(row.get("sms_count")),
        sms_unlimited=_bool(row.get("sms_unlimited")),
        age_condition=(row.get("age_condition") or "").strip() or None,
        is_online_only=_bool(row.get("is_online_only")),
        # ponytail: 약정 개월 컬럼이 크롤링에 없다(모요는 전부 무약정). MNO 약정을
        # 다루게 되면 크롤러에 컬럼부터 추가해야 한다.
        contract_months=None,
        benefits=_benefits(row),
        dominated_by=None,
    )


def retrieval_step(state: GraphState) -> GraphState:
    data = data_retrieval_agent()
    if data["data_validation_errors"]:
        # 데이터가 깨졌으면 여기서 멈춘다. 뒤 노드가 빈 후보로 추천을 만들면
        # 사용자는 "맞는 요금제가 없다"는 잘못된 결론을 본다.
        raise RuntimeError(f"요금제 데이터 검증 실패: {data['data_validation_errors']}")

    profile = state.get("profile")
    rows = data["plans"]
    if profile and profile.preferred_carrier:
        rows = [r for r in rows if profile.preferred_carrier in (r["host_mno"], r["mvno_brand"])]

    # ponytail: 지금은 전량 로드(2,700행, 1초 미만). 느려지면 SQLite로 옮긴다.
    state["candidates"] = [to_candidate(r) for r in rows]
    return state
