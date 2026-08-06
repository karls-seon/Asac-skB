"""④ Explanation & Report Agent — 매칭 결과를 사람이 읽는 답변으로 바꾼다.

기획서 상 역할: "추천 사유를 자연어로 생성"(docs/멀티에이전트_아키텍처.md).
읽는 것은 Plan Matching이 내놓은 **사실**(candidate_plans / shortfall /
strength / relaxation)뿐이고, 카탈로그를 직접 보지 않는다.

**LLM을 안 쓴다.** 여기서 하는 일은 정해진 사실 코드를 정해진 문장으로 바꾸는
것뿐이라 규칙으로 충분하고, 규칙이면 결과가 매번 같아서 테스트할 수 있다.
숫자를 지어낼 위험도 없다 - 요금·용량을 LLM에 맡기면 환각이 그대로 추천이 된다.
말투를 다양하게 하거나 사용자 문장에 맞춰 답해야 할 때가 오면 그때 LLM을
얹되, **숫자는 여기서 만든 문장을 그대로 쓰게** 해야 한다.

문장 틀은 KT M모바일 AI 추천(airs.ktmmobile.com)을 세 시나리오로 직접
돌려보고 가져왔다(2026-08-06). 거기서 제일 쓸모 있었던 것:
  - "정확히 일치하는 요금제는 없습니다"를 **먼저** 말한다
  - 요금제마다 "조건과 다른 점"을 항목으로 적는다
  - 조건이 충돌하면 무엇을 풀면 되는지 되제안한다
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scoring_agent  # noqa: E402

# Windows 콘솔(cp949)에서 한글이 깨지지 않게 표준출력을 UTF-8로 돌린다.
# 매번 PYTHONIOENCODING을 붙이게 하면 실행 방법을 설명하기가 번거롭다.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _fmt(n) -> str:
    """15.0 -> "15", 4.5 -> "4.5", 26400 -> "26,400"."""
    return f"{n:,.10g}"


# 사실 코드 -> 문장. Plan Matching은 코드만 주고 문장은 여기서 만든다.
def _say_shortfall(f: dict) -> str:
    code = f["code"]
    if code == "data_short":
        return (f"데이터 {_fmt(f['actual_gb'])}GB로 "
                f"사용량 {_fmt(f['requested_gb'])}GB보다 부족")
    if code == "data_not_unlimited":
        return "데이터 무제한 아님"
    if code == "voice_not_unlimited":
        return "통화 무제한 아님"
    if code == "overage_risk":
        return "데이터를 다 쓰면 속도 제한이 아니라 초과 요금이 붙음"
    if code == "no_tethering":
        return "테더링(핫스팟) 불가"
    return code


def _say_strength(f: dict) -> str:
    code = f["code"]
    if code == "under_budget":
        return f"월 {_fmt(f['actual_krw'])}원으로 예산({_fmt(f['budget_krw'])}원)보다 저렴"
    if code == "data_unlimited":
        return "데이터 무제한"
    if code == "data_headroom":
        return f"데이터 {_fmt(f['actual_gb'])}GB로 사용량의 {f['ratio']:.1f}배 여유"
    if code == "voice_unlimited":
        return "통화 무제한"
    if code == "stable_price":
        return "프로모션이 없어 가격이 그대로 유지"
    return code


def _price_line(row) -> str:
    """월 납부액. 프로모션이면 언제 얼마로 오르는지까지 붙인다 - 갈아탈
    생각이 없는 사용자에겐 이게 실제로 낼 돈이다."""
    out = f"월 {_fmt(row['monthly_cost'])}원"
    if pd.notna(row.get("promo_ends_after")):
        out += (f" ({_fmt(row['promo_ends_after'])}개월 후 "
                f"{_fmt(row['price_after_promo'])}원)")
    return out


def ask_more(profiling: dict) -> str:
    """슬롯이 모자라 추천을 미룰 때의 답변. User Profiling이
    profiling_complete=False로 준 경우다."""
    lines = ["맞는 요금제를 찾으려면 두 가지만 알려주세요."]
    lines += [f"- {q}" for q in profiling["questions"]]
    return "\n".join(lines)


def report(result: dict, profiling: dict | None = None) -> str:
    """Plan Matching의 산출물(scoring_agent.match)을 답변 텍스트로.

    profiling을 같이 주면 아직 못 받은 슬롯을 끝에 되묻는다 - 추천을 주면서도
    "이걸 알려주시면 더 좁혀드릴 수 있다"고 말할 수 있어야 한다.
    """
    profile = result.get("profile", {})

    if result["candidate_count"] == 0:
        lines = ["요청하신 조건을 모두 만족하는 요금제가 없습니다."]
        budget = profile.get("budget_krw")
        min_cost = result.get("min_cost_krw")
        if budget and min_cost and min_cost > budget:
            lines.append(f"- 다른 조건을 그대로 두면 월 {_fmt(min_cost)}원부터 있습니다"
                         f"(지금 예산은 {_fmt(budget)}원).")
        for r in result["relaxation"][:3]:
            lines.append(f"- {r['label']} 조건을 빼면 {r['opens']}개가 후보에 들어옵니다.")
        if len(lines) == 1:
            lines.append("- 조건을 조금 완화해 주시면 다시 찾아드릴 수 있습니다.")
        return "\n".join(lines)

    total_exact = result["total_exact"]
    if total_exact == 0:
        head = ("요청하신 조건에 정확히 맞는 요금제는 없습니다. "
                "가장 가까운 것들을 어떤 점이 다른지와 함께 보여드릴게요.")
    else:
        head = (f"조건을 모두 만족하는 요금제가 {total_exact}개 있습니다. "
                f"그중 성격이 다른 것들로 추려서 보여드릴게요.")

    lines = [head, ""]
    for i, (_, row) in enumerate(result["candidates"].iterrows(), 1):
        lines.append(f"{i}. {row['plan_name']} | {_price_line(row)}")
        good = [_say_strength(f) for f in row["strength"]]
        bad = [_say_shortfall(f) for f in row["shortfall"]]
        if good:
            lines.append(f"   좋은 점: {' / '.join(good)}")
        if bad:
            lines.append(f"   확인하세요: {' / '.join(bad)}")

    if profiling and profiling.get("questions"):
        lines.append("")
        lines.append("아래를 알려주시면 더 좁혀드릴 수 있어요.")
        lines += [f"- {q}" for q in profiling["questions"]]
    return "\n".join(lines)


def respond(profiling: dict, top_n: int = 5) -> str:
    """User Profiling 결과 하나로 최종 답변까지. 슬롯이 모자라면 추천 대신
    되묻는다 - 이 판단은 User Profiling이 profiling_complete로 내려준다."""
    if not profiling.get("profiling_complete", True):
        return ask_more(profiling)
    result = scoring_agent.match(profiling["profile"], top_n=top_n)
    return report(result, profiling)


def explain(profile: dict, top_n: int = 5) -> str:
    """프로필 하나로 매칭부터 리포트까지. 데모/테스트용 단축 경로다."""
    return report(scoring_agent.match(profile, top_n=top_n))


def demo():
    cases = [
        ("조건 충족 가능", {"data_usage_gb": 10, "budget_krw": 25000,
                            "preferred_network": "5G"}),
        ("조건 충돌", {"data_usage_gb": 30, "budget_krw": 8000,
                       "preferred_network": "5G", "data_unlimited_required": True}),
    ]
    for label, profile in cases:
        print(f"=== {label} ===")
        print(explain(profile))
        print()

    ok = scoring_agent.match(cases[0][1])
    text = report(ok)
    assert "좋은 점" in text, "추천했는데 이유를 한 줄도 못 대고 있음"
    assert "code" not in text, f"사실 코드가 문장으로 안 바뀌고 새어 나감:\n{text}"

    bad = scoring_agent.match(cases[1][1])
    assert bad["candidate_count"] == 0, "충돌 프로필인데 후보가 남음"
    assert "없습니다" in report(bad), "후보가 없는데 그렇게 말하지 않음"
    print("자체 점검 통과.")


if __name__ == "__main__":
    demo()
