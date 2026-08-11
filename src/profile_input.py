"""사용자 입력 -> `recommend()` 인자.

원문 ② User Profiling Agent가 하는 일. 폼이 단일 진실이고, LLM은 자유입력란을
슬롯으로 바꿔 폼을 **채워 넣는** 보조다. LLM이 죽어도 서비스는 그대로 돈다.

필수는 예산과 데이터 둘뿐. 나머지는 비우면 필터를 통과한다.

    q = build_query(budget=30000, data_band="20~50GB")
    recommend(plans, **q)
"""

import json
import os
import re

from dotenv import load_dotenv

# 키를 .env에 두고 쓴다(src/agents/data_verify.py와 같은 방식). 안 읽으면 키가
# 있어도 LLM 단계가 조용히 빠져서, 채팅이 되는데 안 되는 것처럼 보인다.
load_dotenv()

# 구간 -> (필요 GB, 무제한 요구). 자기가 월 몇 GB 쓰는지 아는 사람이 드물어서
# 숫자 대신 구간으로 받는다. 구간의 위쪽을 요구량으로 잡는다 - 모자란 요금제를
# 추천하는 쪽이 남는 요금제를 추천하는 쪽보다 나쁘다.
DATA_BANDS: dict[str, tuple[float | None, bool]] = {
    "5GB 미만": (5.0, False),
    "5~20GB": (20.0, False),
    "20~50GB": (50.0, False),
    "50~100GB": (100.0, False),
    "무제한": (None, True),
}

# "모름"을 골랐을 때 대신 묻는 행동 질문. 조사에 데이터 사용량 GB 문항이 없어서
# 실측으로 눈금을 매길 수 없다.
# ponytail: 근거 없는 눈금이다. 통신사 사용량 통계가 생기면 그걸로 교체한다.
USAGE_HINTS: dict[str, str] = {
    "집·회사 와이파이만 쓴다": "5GB 미만",
    "가끔 영상을 본다": "5~20GB",
    "매일 영상을 본다": "20~50GB",
    "밖에서도 계속 본다": "무제한",
}

MAX_BUDGET = 200_000


class InputError(ValueError):
    """사용자에게 그대로 보여줄 수 있는 입력 오류."""


def _as_number(v, label: str) -> float | None:
    """숫자 또는 숫자로 읽히는 문자열을 숫자로. 비었으면 None, 아니면 InputError."""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        raise InputError(f"{label}을(를) 숫자로 입력해 주세요.")
    if isinstance(v, (int, float)):
        return v
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        raise InputError(f"{label}을(를) 숫자로 입력해 주세요.") from None


def build_query(
    *,
    budget: int,
    data_band: str | None = None,
    usage_hint: str | None = None,
    voice_unlimited: bool = False,
    sms_unlimited: bool = False,
    mvno_ok: bool = True,
    age: int | None = None,
    ott_want: tuple[str, ...] = (),
    ott_required: bool = False,
    current_fee: int | None = None,
) -> dict:
    """폼 입력을 검증하고 `recommend()`가 받는 dict로 바꾼다."""
    # 폼은 숫자를, LLM은 "30000" 같은 문자열을 준다. 둘 다 받는다.
    budget = _as_number(budget, "예산")
    age = _as_number(age, "나이")
    current_fee = _as_number(current_fee, "현재 요금")

    if budget is None or budget <= 0:
        raise InputError("예산을 1원 이상으로 입력해 주세요.")
    if budget > MAX_BUDGET:
        raise InputError(f"예산이 너무 큽니다. {MAX_BUDGET:,}원 이하로 입력해 주세요.")

    if data_band is None:
        if usage_hint not in USAGE_HINTS:
            raise InputError("데이터 사용량을 고르거나 '모름'에서 사용 습관을 골라 주세요.")
        data_band = USAGE_HINTS[usage_hint]
    if data_band not in DATA_BANDS:
        raise InputError(f"모르는 데이터 구간입니다: {data_band}")

    if age is not None and not (0 < age < 120):
        raise InputError("나이를 다시 확인해 주세요.")

    # 문자열을 그대로 tuple()에 넣으면 글자 단위로 쪼개진다("넷플릭스" -> 넷,플,릭,스).
    # 조용히 깨지는 대신 여기서 막는다.
    if isinstance(ott_want, str):
        raise InputError("원하는 OTT는 목록으로 주세요. 예: [\"넷플릭스\"]")

    # 지금 내는 돈. 신규 가입이면 비운다. 예산과 달리 필터에 쓰지 않고
    # "월 얼마 아낀다"를 계산하는 데만 쓴다.
    if current_fee is not None and not (0 < current_fee <= MAX_BUDGET):
        raise InputError(f"현재 요금을 1원 ~ {MAX_BUDGET:,}원 사이로 입력해 주세요.")

    data_gb, unlimited = DATA_BANDS[data_band]
    return {
        "budget": int(budget),
        "data_gb": data_gb,
        "data_unlimited": unlimited,
        "voice_unlimited": bool(voice_unlimited),
        "sms_unlimited": bool(sms_unlimited),
        "mvno_ok": bool(mvno_ok),
        "age": age,
        "ott_want": tuple(ott_want),
        # 결과 화면의 "이 OTT 있는 것만 보기" 토글. 기본은 꺼져 있다 - 켜면
        # 후보의 89%가 날아가므로 사용자가 알고 켜야 한다.
        "ott_required": bool(ott_required),
        "current_fee": int(current_fee) if current_fee is not None else None,
    }


SLOTS = {
    "budget": ("월 예산(원). 숫자만. '3만원대'처럼 자릿수로 말하면 그 구간의 "
               "위쪽을 쓴다 - 3만원대 -> 39000, 만원대 -> 19000."),
    # 구간 경계가 겹쳐서(20~50 / 50~100) "50GB"가 어느 쪽인지 모호하다. 위 구간을
    # 고르면 요구량이 100GB가 되어, 50GB면 충분한 사람에게 비싼 것만 남는다.
    "data_band": (
        f"데이터 사용량 구간. {list(DATA_BANDS)} 중 하나. 사용자가 숫자로 말하면, "
        "그 숫자를 담을 수 있는 **가장 작은** 구간을 고른다. 예: 50GB -> 20~50GB, "
        "51GB -> 50~100GB."
    ),
    "usage_hint": f"GB를 모른다고 할 때만. 사용 습관. {list(USAGE_HINTS)} 중 하나.",
    "voice_unlimited": "통화를 많이 해서 무제한이 필요하면 true.",
    "sms_unlimited": "문자를 많이 보내면 true.",
    "mvno_ok": "알뜰폰도 괜찮으면 true, 통신3사만 원하면 false.",
    "current_fee": "지금 매달 내는 통신요금(원). 신규 가입이면 생략.",
    "age": "나이(만). 말하지 않았으면 생략.",
    "ott_want": "원하는 OTT 이름 목록. 예: [\"넷플릭스\"]",
}


def extract_slots(text: str) -> dict:
    """자유입력 문장에서 폼 값을 뽑는다. 키가 없거나 실패하면 빈 dict.

    **폼을 덮어쓰지 않는다.** 호출한 쪽이 비어 있는 칸만 채우는 데 쓴다. 추천 자체는
    이 함수 없이도 성립해야 한다.
    """
    if not text or not text.strip() or not os.getenv("OPENAI_API_KEY"):
        return {}
    try:
        from openai import OpenAI

        rsp = OpenAI().chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content":
                    "요금제 상담 입력에서 아래 슬롯만 JSON으로 뽑아라. 문장에 없는 키는 "
                    "넣지 마라. 추측하지 마라.\n" + json.dumps(SLOTS, ensure_ascii=False)},
                {"role": "user", "content": text},
            ],
        )
        got = json.loads(rsp.choices[0].message.content)
    except Exception:
        return {}

    # 모델이 뭘 뱉든 우리가 아는 키·형식만 통과시킨다.
    out = {k: v for k, v in got.items() if k in SLOTS}
    if out.get("data_band") not in DATA_BANDS:
        out.pop("data_band", None)

    # 구간 경계가 겹쳐서("20~50GB" / "50~100GB") 모델이 "50GB"를 위 구간으로 보낸다.
    # 그러면 요구량이 100GB가 되어 50GB면 충분한 사람에게 비싼 것만 남는다.
    # 문장에 적힌 숫자로 다시 잡는다 - 그 숫자를 담는 가장 작은 구간이 맞다.
    said = _said_gb(text)
    if said is not None:
        out["data_band"] = band_for(said)
    elif out.get("data_band") is None and _said_data_unlimited(text):
        out["data_band"] = "무제한"

    # "3만원대"는 모델이 30,000으로 잡는데, 그러면 정작 3만원대 요금제가 다 잘린다.
    # 자릿수 표현은 우리가 계산한 값으로 덮는다. 그 밖에는 모델이 못 뽑았을 때만
    # 문장에서 읽는다 - "7만원 내는데 반으로"(-> 35,000)처럼 맥락을 본 답을 살린다.
    if _WON_BAND_RE.search(text) or out.get("budget") is None:
        won = _said_won(text)
        if won is not None:
            out["budget"] = won
    return out


_GB_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:gb|기가|G\b)", re.IGNORECASE)
_WON_RE = re.compile(r"(\d+(?:\.\d+)?)\s*만\s*원|(\d[\d,]{3,})\s*원")
# "3만원대" "만원대"처럼 자릿수로 말하는 표현. 예산은 상한선이라 그 구간의 위쪽을
# 잡아야 한다 - "3만원대"를 30,000으로 잡으면 정작 3만원대 요금제가 다 잘린다.
_WON_BAND_RE = re.compile(r"(\d+)?\s*만\s*원\s*대")


def _said_gb(text: str) -> float | None:
    """문장에서 사용자가 말한 데이터량. 없으면 None."""
    m = _GB_RE.search(text)
    return float(m.group(1)) if m else None


def _said_won(text: str) -> int | None:
    """문장에서 사용자가 말한 예산. "6만원" "40,000원" "3만원대" 전부. 없으면 None."""
    band = _WON_BAND_RE.search(text)
    if band:
        # "만원대" = 1만원대. 그 구간의 위쪽(만9천)을 상한으로 잡는다.
        return int(band.group(1) or 1) * 10_000 + 9_000

    m = _WON_RE.search(text)
    if not m:
        return None
    if m.group(1):
        return int(float(m.group(1)) * 10_000)
    return int(m.group(2).replace(",", ""))


def _said_data_unlimited(text: str) -> bool:
    """데이터를 무제한으로 달라고 했는가.

    "통화도 무제한"처럼 통화·문자를 가리키는 경우가 있어서 그냥 "무제한"만 보면
    안 된다. 바로 앞에 통화/문자/음성이 붙은 것은 데이터 얘기가 아니다.
    """
    return any(
        # "통화도 무제한"처럼 조사가 붙고 띄어쓰기가 오는 형태까지 잡는다.
        not re.search(r"(통화|문자|음성)\S{0,3}\s*$", text[:m.start()])
        for m in re.finditer("무제한", text)
    )


def band_for(gb: float) -> str:
    """그 사용량을 담을 수 있는 가장 작은 구간."""
    for name, (need, unlimited) in DATA_BANDS.items():
        if not unlimited and need is not None and gb <= need:
            return name
    return "무제한"


# --- 채팅: 슬롯이 다 찰 때까지 되묻는다 -------------------------------------

QUESTIONS = {
    "budget": "월 요금은 얼마까지 생각하세요? (예: 3만원)",
    "data": (
        "데이터는 얼마나 쓰세요?\n  "
        + " / ".join(DATA_BANDS)
        + "\n  모르시면 '모름'이라고 답해 주세요."
    ),
    "usage_hint": "그러면 평소 습관으로 가늠해 볼게요. 어느 쪽에 가까우세요?\n  " + " / ".join(USAGE_HINTS),
}


def missing_slots(slots: dict) -> list[str]:
    """아직 못 채운 필수 슬롯. 데이터는 구간이든 습관이든 하나만 있으면 된다."""
    need = []
    if slots.get("budget") is None:
        need.append("budget")
    if slots.get("data_band") is None:
        need.append("usage_hint" if slots.get("asked_data") else "data")
    return need


def next_question(slots: dict) -> str | None:
    """되물을 문장. 필수가 다 찼으면 None."""
    need = missing_slots(slots)
    return QUESTIONS[need[0]] if need else None


def update_slots(slots: dict, text: str, extract=extract_slots) -> dict:
    """사용자 발화 하나를 슬롯에 반영한다. 이미 채워진 값은 덮어쓰지 않는다.

    `extract`를 갈아끼울 수 있게 열어 둔 건 API 키 없이 검증하기 위해서다.
    """
    out = dict(slots)
    if out.get("data_band") is None and any(w in text for w in ("모름", "몰라", "모르"))\
            and not out.get("usage_hint"):
        # "모름"이라고 답한 순간부터는 구간 대신 습관을 묻는다.
        out["asked_data"] = True
    for k, v in extract(text).items():
        if out.get(k) is None:
            out[k] = v
    if out.get("usage_hint") in USAGE_HINTS and out.get("data_band") is None:
        out["data_band"] = USAGE_HINTS[out["usage_hint"]]
    return out


def to_query(slots: dict) -> dict:
    """모인 슬롯을 `recommend()` 인자로. 필수가 비면 InputError."""
    allowed = ("budget", "data_band", "usage_hint", "voice_unlimited",
               "sms_unlimited", "mvno_ok", "age", "ott_want", "ott_required", "current_fee")
    kw = {k: v for k, v in slots.items() if k in allowed and v is not None}
    # 필수 슬롯이 비었으면 크래시나 일반 안내문 대신 **다음에 물을 그 문장**을 준다.
    # "모르겠어"라고 답한 사람에게는 구간 목록이 아니라 습관 질문이 나가야 한다.
    ask = next_question(slots)
    if ask:
        raise InputError(ask)
    return build_query(**kw)


def check() -> None:
    q = build_query(budget=30_000, data_band="20~50GB")
    assert q["data_gb"] == 50.0 and q["data_unlimited"] is False
    assert q["mvno_ok"] is True and q["age"] is None
    assert q["current_fee"] is None and q["ott_required"] is False

    # 지금 내는 돈을 주면 그대로 실려 나가 절감액 계산에 쓰인다.
    assert build_query(budget=30_000, data_band="5~20GB", current_fee=69_000)["current_fee"] == 69_000

    # 무제한 구간은 GB가 아니라 플래그로 나간다.
    assert build_query(budget=50_000, data_band="무제한")["data_gb"] is None
    assert build_query(budget=50_000, data_band="무제한")["data_unlimited"] is True

    # "모름"은 행동 질문으로 구간을 메운다.
    assert build_query(budget=30_000, usage_hint="가끔 영상을 본다")["data_gb"] == 20.0

    for bad in (
        dict(budget=0, data_band="5~20GB"),
        dict(budget=30_000),                       # 데이터도 습관도 없음
        dict(budget=30_000, data_band="10GB쯤"),   # 모르는 구간
        dict(budget=30_000, data_band="5~20GB", age=999),
        dict(budget=10_000_000, data_band="5~20GB"),
        dict(budget=30_000, data_band="5~20GB", current_fee=-1),
        dict(budget=30_000, data_band="5~20GB", ott_want="넷플릭스"),   # 목록이어야 함
    ):
        try:
            build_query(**bad)
        except InputError:
            pass
        else:
            raise AssertionError(f"통과하면 안 되는 입력: {bad}")

    # 실제로 recommend()에 그대로 들어가는가.
    from recommend import load_plans, recommend

    got = recommend(load_plans(), **build_query(budget=30_000, data_band="5~20GB"))
    assert len(got) and (got["discounted_fee"] <= 30_000).all()

    # 키가 없으면 LLM 단계는 조용히 빠진다.
    assert extract_slots("") == {}

    # 문장에 적힌 GB는 그 값을 담는 가장 작은 구간으로 간다. 경계값이 핵심이다 -
    # 50GB가 "50~100GB"로 가면 요구량이 100GB가 되어 후보가 부당하게 좁아진다.
    for gb, want in [(3, "5GB 미만"), (5, "5GB 미만"), (20, "5~20GB"),
                     (21, "20~50GB"), (50, "20~50GB"), (51, "50~100GB"),
                     (100, "50~100GB"), (150, "무제한")]:
        assert band_for(gb) == want, (gb, band_for(gb))
    assert _said_gb("3만원대 50gb 요금제") == 50.0
    assert _said_gb("50 기가 정도") == 50.0
    assert _said_gb("예산만 3만원") is None

    # 금액도 문장에서 직접 읽는다. 모델이 "6만원"을 자주 흘린다.
    assert _said_won("알뜰폰 말고 통신3사로 6만원") == 60_000
    assert _said_won("40,000원 이하로") == 40_000
    assert _said_won("데이터 많은 거") is None

    # 자릿수 표현은 그 구간의 위쪽. 아래쪽을 잡으면 정작 그 구간이 다 잘린다.
    assert _said_won("만원대로") == 19_000
    assert _said_won("3만원대 요금제") == 39_000
    assert _said_won("5만원대면 좋겠어") == 59_000
    assert _said_won("3만원 이하") == 30_000   # '대'가 없으면 그대로

    # "통화도 무제한"은 데이터 무제한이 아니다.
    assert _said_data_unlimited("무제한 필요해 10만원까지") is True
    assert _said_data_unlimited("데이터 무제한으로") is True
    assert _said_data_unlimited("통화도 무제한 4만원") is False
    assert _said_data_unlimited("문자 무제한이면 좋겠어") is False
    assert _said_data_unlimited("3만원 20기가") is False

    # 되물을 문장은 상황에 맞아야 한다. "모르겠어"에는 습관 질문이 나간다.
    try:
        to_query({"budget": 30_000, "asked_data": True})
    except InputError as e:
        assert str(e) == QUESTIONS["usage_hint"], str(e)
    else:
        raise AssertionError("데이터 없이 통과했다")

    _check_chat()
    print("check ok")


def _check_chat() -> None:
    """되묻기 루프. 실제 LLM 대신 정해둔 답을 주는 추출기로 돌린다."""
    scripted = {
        "요금제 추천해줘": {},
        "3만원 정도": {"budget": 30_000},
        "모름": {},
        "매일 영상을 본다": {"usage_hint": "매일 영상을 본다"},
        "알뜰폰은 싫어": {"mvno_ok": False},
    }
    fake = lambda t: scripted.get(t, {})  # noqa: E731

    slots: dict = {}
    # 아무 정보 없는 첫 발화면 예산부터 되묻는다.
    slots = update_slots(slots, "요금제 추천해줘", fake)
    assert next_question(slots) == QUESTIONS["budget"]

    slots = update_slots(slots, "3만원 정도", fake)
    assert slots["budget"] == 30_000
    assert next_question(slots) == QUESTIONS["data"]

    # "모름"이라고 하면 구간 대신 습관을 묻는다.
    slots = update_slots(slots, "모름", fake)
    assert next_question(slots) == QUESTIONS["usage_hint"]

    slots = update_slots(slots, "매일 영상을 본다", fake)
    assert slots["data_band"] == "20~50GB"
    assert next_question(slots) is None, "필수가 다 찼는데 아직 묻고 있다"

    # 다 찬 뒤 들어온 선택값도 반영된다.
    slots = update_slots(slots, "알뜰폰은 싫어", fake)
    q = to_query(slots)
    assert q == {
        "budget": 30_000, "data_gb": 50.0, "data_unlimited": False,
        "voice_unlimited": False, "sms_unlimited": False,
        "mvno_ok": False, "age": None, "ott_want": (), "ott_required": False, "current_fee": None,
    }, q

    # 필수가 비면 추천으로 넘어가지 않는다. 빈 슬롯도 크래시가 아니라 InputError다.
    for bad in ({"budget": 30_000}, {}):
        try:
            to_query(bad)
        except InputError:
            pass
        else:
            raise AssertionError(f"필수 없이 통과했다: {bad}")

    # LLM이 숫자를 문자열로 준다. 폼의 int와 똑같이 받아야 한다.
    llm = lambda t: {"budget": "30000", "data_band": "20~50GB", "ott_want": ["넷플릭스"]}  # noqa: E731
    q = to_query(update_slots({}, "아무말", llm))
    assert q["budget"] == 30_000 and q["data_gb"] == 50.0, q
    assert q["ott_want"] == ("넷플릭스",), q


if __name__ == "__main__":
    check()
