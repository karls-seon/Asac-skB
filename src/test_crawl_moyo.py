"""crawl_moyo 파서 최소 점검. `python test_crawl_moyo.py`로 실행.

캐시(data/raw_cache/moyo/detail_37132.html)를 픽스처로 쓴다. 이 요금제는
사은품 4개 중 2개가 "펼쳐보기" 뒤에 숨어 있어서, DOM만 읽던 예전 파서는
2개만 뽑았다.
"""
from crawl_moyo import parse_detail, parse_flight_gifts, CACHE_DIR
import os


def test_hidden_gifts():
    with open(os.path.join(CACHE_DIR, "detail_37132.html"), encoding="utf-8") as f:
        html = f.read()

    gifts = parse_flight_gifts(html)
    assert len(gifts) == 4, gifts
    assert gifts["1866"][0] == "쿠팡유심 개통 시 쿠팡캐시 2만원", gifts["1866"]
    assert gifts["1866"][1].startswith("대상: 쿠팡 유심"), gifts["1866"]

    _, benefits, _, _ = parse_detail("37132", "모두다 맘껏 안심 2.5GB+")
    names = [b["benefit_name"] for b in benefits]
    assert len(names) == len(set(names)) == 4, names
    assert "3대 마트 + 네이버페이 포인트 2만원" in names, names
    # 숨은 사은품도 금액이 붙어야 한다(2만원 -> 20000).
    assert [b["benefit_value_won"] for b in benefits if b["benefit_name"] == names[2]] == [20000]

    # 유심 구매처가 다른 두 사은품은 동시에 못 받는다 - 합계 4만원이 아니라 2만원.
    by_condition = {}
    for b in benefits:
        by_condition.setdefault(b["benefit_condition"], []).append(b["benefit_value_won"] or 0)
    assert set(by_condition) == {"", "쿠팡유심", "KT바로유심"}, by_condition
    free = sum(by_condition[""])
    best = max(sum(v) for c, v in by_condition.items() if c)
    assert free == 180000 and best == 20000, (free, best)


if __name__ == "__main__":
    test_hidden_gifts()
    print("ok")
