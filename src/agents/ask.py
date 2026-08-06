"""요금제 추천 전체 흐름을 한 줄로 돌려보는 진입점.

    python src/agents/ask.py "출퇴근할 때 영상 자주 봐요. 월 2만원 안쪽으로"
    python src/agents/ask.py                 # 인자 없으면 대화형

흐름: 자연어 -> ① User Profiling(LLM) -> ③ Plan Matching(규칙)
      -> ④ Explanation(규칙) -> 답변

`--slots`를 붙이면 중간 산출물(뽑힌 슬롯, 후보 수)도 같이 보여준다.
LLM을 안 거치고 슬롯을 직접 넣어 보고 싶으면 explanation_agent.explain()을
쓰면 된다 - 그쪽은 API 키가 필요 없다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import explanation_agent  # noqa: E402
import user_agent  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def answer(text: str, show_slots: bool = False) -> str:
    profiling = user_agent.profile_from_text(text)
    out = []
    if show_slots:
        out.append(f"[슬롯] {profiling['profile']}")
        out.append(f"[충분한가] {profiling['profiling_complete']}")
        if profiling["dropped"]:
            out.append(f"[버린 키] {profiling['dropped']}")
        out.append("")
    out.append(explanation_agent.respond(profiling))
    return "\n".join(out)


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--slots"]
    show_slots = "--slots" in sys.argv

    if not user_agent._load_key():
        print("OPENAI_API_KEY가 없습니다. .env에 채워 주세요(.env.example 참고).")
        print("키 없이 확인하려면: python src/agents/explanation_agent.py")
        return 1

    if args:
        print(answer(" ".join(args), show_slots))
        return 0

    print('요금제 상담 (그만하려면 빈 줄 또는 Ctrl+C)')
    while True:
        try:
            text = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not text:
            return 0
        print()
        print(answer(text, show_slots))


if __name__ == "__main__":
    sys.exit(main())
