"""① User Profiling Agent — 자연어 한 마디를 슬롯 dict로 바꾼다.

기획서 상 역할: "사용자 입력에서 통화량·데이터·문자량·연령·예산·선호 추출,
슬롯 부족하면 재질문"(docs/멀티에이전트_아키텍처.md).

**이 프로젝트에서 LLM이 필요한 유일한 지점이다.** 뒤쪽(Plan Matching,
Explanation)은 전부 규칙이라 키 없이 돌아간다. 여기만 자연어를 다루므로
LLM 호출을 `_call_llm` 하나로 몰아 두고, 키가 없으면 스텁으로 갈아끼워
전체 흐름을 그대로 테스트할 수 있게 한다.

**숫자를 지어내게 두면 안 된다.** "출퇴근에 영상 자주 봐요"에서 GB를 뽑는 건
추정이므로, 값과 함께 `data_usage_confidence`를 반드시 받는다. 그 값이
낮으면 Plan Matching이 후보 하한을 낮추고 여유분·QoS를 더 높게 친다
(scoring_agent.CONFIDENCE_HEADROOM). 확신한 값과 추정한 값을 같게 다루면
추정이 빗나갔을 때 사용자가 초과 요금을 맞는다.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from schema import BASE_DIR  # noqa: E402

# Windows 콘솔(cp949)에서 한글이 깨지지 않게 표준출력을 UTF-8로 돌린다.
# 매번 PYTHONIOENCODING을 붙이게 하면 실행 방법을 설명하기가 번거롭다.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

MODEL = "gpt-5"

# scoring_agent.filter_eligible이 실제로 읽는 키만 넣는다. 여기에 없는 슬롯을
# 뽑아 봐야 추천에 반영되지 않으므로, 물어놓고 못 쓰는 상황을 막는다.
SLOT_SPEC = """
budget_krw            정수. 월 납부액 상한(원). "3만원대"처럼 구간이면 상한값.
data_usage_gb         숫자. 월 데이터 사용량(GB).
data_usage_confidence "high"=사용자가 GB를 직접 말함 / "medium"=대략 말함 /
                      "low"=생활패턴("출퇴근에 영상")에서 추정.
data_unlimited_required  불리언. 무제한을 명시적으로 원할 때만 true.
voice_unlimited_required 불리언. 통화 무제한을 명시적으로 원할 때만 true.
preferred_network     "5G" 또는 "LTE".
user_age              정수. 나이.
current_carrier_type  "MNO"(통신3사) 또는 "MVNO"(알뜰폰). 지금 쓰는 것.
current_carrier       "KT" / "SKT" / "LGU+". 지금 쓰는 통신사.
target_carrier_type   "MNO" 또는 "MVNO". 옮겨 가고 싶은 쪽을 말했을 때만.
ott_preference        문자열 배열. 예: ["넷플릭스"].
price_sensitive       불리언. "싼 거 / 저렴한 / 최대한 아끼고 싶다"처럼
                      가격을 우선한다는 뜻을 밝혔을 때만 true.
"""

# 슬롯이 이만큼도 안 채워지면 추천이 아니라 카탈로그 구경이 된다.
# 실측: 아무 슬롯 없이 돌리면 후보가 1,094개고, 상위에 5만원짜리가 올라온다.
# 아키텍처 문서에도 "슬롯 부족하면 재질문"이 이 에이전트 역할로 적혀 있다.
REQUIRED_ANY = ("budget_krw", "data_usage_gb")

# 되물을 때 쓸 문구. 슬롯 이름을 그대로 보여줄 순 없다.
ASK = {
    "budget_krw": "월 통신비를 얼마까지 생각하고 계세요? (단말기 할부금 제외)",
    "data_usage_gb": "데이터를 월에 얼마나 쓰세요? 모르시면 평소 쓰는 모습으로 말씀해 주셔도 돼요.",
    "preferred_network": "지금 쓰시는 게 5G인가요, LTE인가요?",
    "data_unlimited_required": "데이터는 무제한이어야 할까요?",
    "voice_unlimited_required": "통화는 무제한이어야 할까요?",
    "user_age": "나이가 어떻게 되세요? 연령 전용 요금제가 더 쌀 수 있어요.",
    "current_carrier_type": "지금 통신3사를 쓰시나요, 알뜰폰을 쓰시나요?",
    "current_carrier": "지금 어느 통신사를 쓰고 계세요?",
    "target_carrier_type": "알뜰폰으로 옮기실 생각이세요, 통신3사를 유지하실 생각이세요?",
    "ott_preference": "넷플릭스처럼 꼭 끼고 싶은 구독 서비스가 있으세요?",
}

PROMPT = f"""너는 한국 휴대폰 요금제 상담의 슬롯 추출기다.
사용자 문장에서 아래 슬롯을 뽑아 JSON 하나만 출력한다.

{SLOT_SPEC}

규칙:
- 사용자가 말하지 않은 슬롯은 넣지 마라. 추측해서 채우지 마라.
- 단 data_usage_gb는 생활패턴 묘사에서 추정해도 된다. 그때는 반드시
  data_usage_confidence를 "low"로 함께 넣어라. 참고치:
  카톡·웹 위주 3GB / 가끔 영상 10GB / 출퇴근에 매일 영상 20GB /
  하루 종일 스트리밍 50GB 이상.
- "무제한"이라는 말이 나오면 data_unlimited_required를 true로.
- "통화를 많이 한다 / 통화 위주"는 voice_unlimited_required를 true로 넣어라.
  데이터도 마찬가지로 "데이터 많이 쓴다"만 있고 숫자가 없으면 추정치를 넣되
  confidence를 "low"로 하라.
- "50대", "30대", "스무살", "군인"처럼 나이를 짐작할 수 있는 표현이 나오면
  user_age에 대표값을 넣어라(50대->55, 30대->35, 군인->21). 연령 전용
  요금제가 더 쌀 수 있어서 이 값이 실제로 결과를 바꾼다.
- "비싸다 / 아끼고 싶다 / 저렴하게 / 싼 거" 같은 비용 불만은 금액이 아니므로
  budget_krw에 넣지 마라. 금액을 지어내면 안 된다. 대신 price_sensitive를
  true로 하고, "missing"의 맨 앞에 budget_krw를 넣어 얼마까지 생각하는지
  되묻게 하라.
- 추가로 "missing" 키에, 추천 품질을 위해 더 물어보면 좋을 슬롯 이름을
  중요한 순서로 최대 2개까지 배열로 넣어라.
- JSON 외에는 아무것도 출력하지 마라."""


def _load_key() -> str | None:
    """.env에서 키를 읽는다. python-dotenv가 있으면 그걸 쓴다."""
    if os.environ.get("OPENAI_API_KEY"):
        return os.environ["OPENAI_API_KEY"]
    try:
        from dotenv import load_dotenv
        load_dotenv(BASE_DIR / ".env")
    except ImportError:
        pass
    return os.environ.get("OPENAI_API_KEY")


def _call_llm(text: str) -> str:
    """LLM 호출을 여기 하나로 몰아 둔다. 테스트에서 이 함수만 바꿔치기하면
    키 없이도 아래 파이프라인 전체를 돌려볼 수 있다."""
    from openai import OpenAI

    client = OpenAI(api_key=_load_key())
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": text},
        ],
        response_format={"type": "json_object"},
    )
    return resp.choices[0].message.content


_ALLOWED = {
    "budget_krw", "data_usage_gb", "data_usage_confidence",
    "data_unlimited_required", "voice_unlimited_required",
    "preferred_network", "user_age", "current_carrier_type",
    "current_carrier", "target_carrier_type", "ott_preference",
    "price_sensitive",
}


def _clean(raw: dict) -> tuple[dict, list[str]]:
    """LLM이 준 값을 스코어링이 쓸 수 있는 형태로 검증한다.

    모델이 스펙에 없는 키나 엉뚱한 타입을 줄 수 있으므로 통과시키기 전에
    거른다. 여기서 안 거르면 filter_eligible이 조용히 무시하거나 터진다.
    """
    profile, dropped = {}, []
    for k, v in raw.items():
        if k == "missing":
            continue
        if k not in _ALLOWED or v is None:
            dropped.append(k)
            continue
        profile[k] = v

    # 추정인데 confidence가 빠지면 확신한 값으로 취급돼 후보가 잘못 좁아진다.
    if "data_usage_gb" in profile and "data_usage_confidence" not in profile:
        profile["data_usage_confidence"] = "low"

    if profile.get("current_carrier") and "current_carrier_type" not in profile:
        profile["current_carrier_type"] = "MNO"
    return profile, dropped


def profile_from_text(text: str, call=_call_llm) -> dict:
    """자연어 -> 슬롯 dict. `call`을 바꿔 끼우면 키 없이 테스트할 수 있다.

    슬롯이 REQUIRED_ANY만큼도 안 차면 `profiling_complete=False`로 두고
    질문을 돌려준다. 이 상태로 추천하면 후보가 1,000개가 넘어서 사실상
    무작위 5개를 보여주게 된다("잘 모르겠어요"로 실측).
    """
    raw = json.loads(call(text))
    profile, dropped = _clean(raw)

    missing = [m for m in raw.get("missing", []) if m in ASK and m not in profile]
    complete = any(k in profile for k in REQUIRED_ANY)
    if not complete:
        # 아무것도 못 받았으면 꼭 필요한 것부터 묻는다(모델이 고른 순서보다 우선).
        missing = [k for k in REQUIRED_ANY if k not in profile] + [
            m for m in missing if m not in REQUIRED_ANY
        ]

    return {
        "profile": profile,
        "profiling_complete": complete,
        "missing": missing,
        "questions": [ASK[m] for m in missing[:2]],
        "dropped": dropped,
        "user_input_raw": text,
    }


def demo():
    """실제 LLM으로 시나리오 몇 개를 돌려본다. 키가 없으면 건너뛴다."""
    scenarios = [
        "출퇴근할 때 영상을 자주 봐요. 월 2만원 안쪽으로 추천해주세요",
        "지금 KT 쓰는데 통신비가 너무 비싸요. 데이터는 한 15기가 정도 써요",
        "잘 모르겠어요 알아서 추천해주세요",
    ]
    if not _load_key():
        print("OPENAI_API_KEY 없음 - LLM 호출 건너뜀")
        return
    for s in scenarios:
        out = profile_from_text(s)
        print(f"입력: {s}")
        print(f"  슬롯: {out['profile']}")
        print(f"  더 물어볼 것: {out['missing']}")
        if out["dropped"]:
            print(f"  버린 키: {out['dropped']}")
        print()


if __name__ == "__main__":
    demo()
