"""구조변경(schema drift) 진단 — 사이트가 바뀌어 파서가 못 읽게 된 곳을 찾는다.

## 왜 필요한가

2026-08-03에 KT가 요금표를 페이지 안 <script>에서 외부 JS 파일로 옮겼다.
크롤러는 그 파일을 안 받으니 주력 4개 상품이 통째로 0건이 됐는데, **에러가
하나도 안 났다** - HTTP 200, 파싱 예외 없음, 종료코드 0. 데이터만 조용히
사라졌다(docs/수정이력.md 35번). 원인을 찾는 데 든 시간의 대부분은 "무엇이
어디로 옮겨갔는지" 원본 HTML을 뒤지는 데 들었다. 그 부분을 자동화한다.

## 어떻게 찾는가 - 어제는 되던 소스가 오늘 0행이면 의심한다

크롤러는 상세 페이지를 상품 하나당 파일 하나로 캐시에 남긴다. 그러면
캐시 파일과 파싱 결과가 이렇게 대응된다.

    캐시 파일 detail_23450.html  ->  interim CSV의 plan_id 23450

여기서 **"캐시에 있는데 CSV에 없는 파일"을 그냥 세면 안 된다.** 수집 범위
밖이라 일부러 안 쓰는 캐시가 많기 때문이다(KT는 예전에 5개 탭을 받아뒀지만
지금은 3개만 쓰고, SKT는 147개를 받아 50개만 쓴다). 처음에 그렇게 만들었더니
KT 35개 중 27개가 "0행"으로 잡혀서 신호가 잡음에 묻혔다.

그래서 **직전에 성공했던 커버리지와 비교**한다. `parse_coverage.json`에
"마지막으로 멀쩡했을 때 어떤 소스에서 행이 나왔는지"를 남겨두고, 그때는
나왔는데 지금은 안 나오는 것만 회귀로 본다. 범위 밖 캐시는 애초에 기준선에도
없으므로 조용하다.

찾아낸 회귀 파일에서 **파서가 의존하는 앵커**(KT의 `tableHTML`, LGU+의
`plan-title` 등)가 아직 있는지까지 확인해 리포트에 함께 적는다. 앵커가
통째로 사라졌으면 "그게 있던 자리가 바뀌었다"는 강한 단서다.

## 하지 않는 것

**파서 코드를 고치지 않는다.** 진단 리포트만 남기고 반영 여부는 사람이
정한다. 이 프로젝트에서 이미 두 번, 몇 개 사례만 보고 일반화한 수정이 새
오류를 만들었다(수정이력 34번의 모요 가격 되돌림, 15번의 검증 스크립트 오탐).
자동 수정은 같은 실수를 더 빠르고 조용하게 저지른다.

실행:
    python src/agents/schema_drift.py               # 4개 사이트 전부 진단
    python src/agents/schema_drift.py kt moyo       # 지정한 사이트만
    python src/agents/schema_drift.py --baseline    # 지금 상태를 기준선으로 저장
"""
import csv
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from schema import BASE_DIR, cache_dir, interim_path  # noqa: E402

REVIEW_DIR = BASE_DIR / "data" / "review"
# "마지막으로 멀쩡했을 때"의 소스 커버리지. 이 파일이 진단의 기준선이다.
COVERAGE_PATH = REVIEW_DIR / "parse_coverage.json"

# 리포트에 실을 예시 개수. 전부 실으면 수백 줄이 되는데 원인은 대개 몇 개만
# 열어봐도 같다.
MAX_SAMPLES = 15


@dataclass
class SiteSpec:
    """사이트 하나의 캐시 <-> 파싱 결과 대응 규칙."""
    label: str                    # 가드 위반 메시지에 쓰이는 이름 (예: "KT")
    cache: str                    # data/raw_cache/<이름>
    interim: str                  # data/interim/<이름>
    detail_pattern: re.Pattern    # 상세 파일명 -> group(1)이 소스 키
    # 파서가 의존하는 앵커. (설명, 찾을 문자열). 회귀 파일에서 존재 여부를 센다.
    anchors: list[tuple[str, str]] = field(default_factory=list)

    def source_of_file(self, filename: str) -> str | None:
        m = self.detail_pattern.fullmatch(filename)
        return m.group(1) if m else None


# plan_id는 사이트 공통으로 "{소스코드}_{접미사}" 꼴이라(접미사는 연령/택1 전개용)
# 첫 '_' 앞만 떼면 캐시 파일의 소스 키가 된다. 모요는 숫자 id라 '_'가 없다.
def _source_of_plan(plan_id: str) -> str:
    return (plan_id or "").split("_")[0]


SITES = {
    "kt": SiteSpec(
        label="KT", cache="kt", interim="kt_plans.csv",
        detail_pattern=re.compile(r"(\d+)\.html"),
        anchors=[
            ("연령별 요금표(tableHTML)", "tableHTML"),
            ("외부 요금표 JS(js/data/*.js)", "/js/data/"),
            ("요금제 표(detail-list)", "detail-list"),
        ],
    ),
    "skt": SiteSpec(
        label="SKT", cache="skt", interim="skt_plans.csv",
        detail_pattern=re.compile(r"ledger_([A-Z0-9]+)\.json"),
        anchors=[
            ("요금 정보(basFeeInfo)", "basFeeInfo"),
            ("상품군명(prodGrpNm)", "prodGrpNm"),
            ("상품 필터 태그(prodFilterFlagList)", "prodFilterFlagList"),
        ],
    ),
    "lguplus": SiteSpec(
        label="LGU+", cache="lguplus", interim="lguplus_plans.csv",
        detail_pattern=re.compile(r"(?:age_\d+|unified|direct)_([A-Z0-9]+)\.html"),
        anchors=[
            ("요금제명(h2.plan-title)", "plan-title"),
            ("요금제 카드(plan-list__item)", "plan-list__item"),
            ("월정액(strong.price-summary)", "price-summary"),
        ],
    ),
    "moyo": SiteSpec(
        label="MVNO(모요)", cache="moyo", interim="moyo_plans.csv",
        detail_pattern=re.compile(r"detail_(\d+)\.html"),
        anchors=[
            ("요금제 링크(/plans/)", "/plans/"),
            ("사은품 링크(gift-group)", "/gift-group/"),
            ("월 납부액", "납부액"),
        ],
    ),
}


@dataclass
class Finding:
    site: str
    cached: int                 # 캐시에 있는 상세 파일 수
    current: set               # 지금 행이 나오는 소스
    baseline: set              # 기준선(마지막으로 멀쩡했을 때) 소스
    regressed: list[str]       # 기준선엔 있었는데 지금 0행 + 캐시는 남아있음
    anchor_hits: dict          # {앵커 설명: 회귀 파일 중 그 앵커가 있는 파일 수}

    @property
    def has_baseline(self) -> bool:
        return bool(self.baseline)

    @property
    def added(self) -> int:
        return len(self.current - self.baseline)


def _parsed_sources(spec: SiteSpec) -> set[str]:
    path = interim_path(spec.interim)
    if not Path(path).exists():
        return set()
    with open(path, encoding="utf-8-sig", newline="") as f:
        return {_source_of_plan(r.get("plan_id", "")) for r in csv.DictReader(f)}


def _cached_sources(spec: SiteSpec) -> dict:
    cdir = cache_dir(spec.cache)
    if not cdir.exists():
        return {}
    out = {}
    for path in cdir.iterdir():
        key = spec.source_of_file(path.name)
        if key:
            out[key] = path
    return out


def load_coverage() -> dict:
    if not COVERAGE_PATH.exists():
        return {}
    try:
        return json.loads(COVERAGE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # 기준선이 깨졌다고 진단이 죽으면, 정작 볼 게 있을 때 아무것도 못 남긴다.
        print("[schema_drift] 경고: 기준선 파일을 읽을 수 없어 무시합니다")
        return {}


def save_coverage(findings: list["Finding"]) -> None:
    data = load_coverage()
    for f in findings:
        data[f.site] = sorted(f.current)
    COVERAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    COVERAGE_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def inspect(site: str, coverage: dict) -> Finding | None:
    """한 사이트를 진단한다. 캐시가 아예 없으면 None."""
    spec = SITES[site]
    cached = _cached_sources(spec)
    if not cached:
        return None

    current = _parsed_sources(spec) & set(cached)
    baseline = set(coverage.get(site, []))
    # 캐시가 지워진 소스(단종되어 목록에서 사라진 것)는 회귀가 아니다.
    regressed = sorted((baseline - current) & set(cached))

    anchor_hits = {label: 0 for label, _ in spec.anchors}
    for key in regressed[:MAX_SAMPLES]:
        try:
            text = cached[key].read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for label, needle in spec.anchors:
            if needle in text:
                anchor_hits[label] += 1

    return Finding(site=site, cached=len(cached), current=current,
                   baseline=baseline, regressed=regressed, anchor_hits=anchor_hits)


def _render(findings: list[Finding], run_date: str) -> str:
    lines = [
        f"# 구조변경 진단 리포트 ({run_date})",
        "",
        "**직전에 멀쩡히 파싱되던 소스가 지금은 행을 하나도 못 내놓는 경우**를",
        "찾은 것입니다. 캐시 파일은 그대로 있는데 결과만 사라졌다면, 받아오기는",
        "됐는데 파싱이 실패했다는 뜻이라 사이트 구조가 바뀌었을 가능성이 높습니다.",
        "",
        "> 이 리포트는 **진단만** 합니다. 파서 수정과 반영은 사람이 판단하세요.",
        "",
        "| 사이트 | 캐시 | 기준선 | 현재 | **회귀** | 신규 |",
        "|---|---|---|---|---|---|",
    ]
    for f in findings:
        base = len(f.baseline) if f.has_baseline else "—"
        lines.append(
            f"| {SITES[f.site].label} | {f.cached} | {base} | {len(f.current)} | "
            f"**{len(f.regressed)}** | +{f.added} |"
        )
    lines.append("")

    for f in findings:
        if not f.regressed:
            continue
        spec = SITES[f.site]
        sampled = min(len(f.regressed), MAX_SAMPLES)
        lines += [
            f"## {spec.label} — 회귀 {len(f.regressed)}건",
            "",
            f"### 파서 앵커 확인 (회귀 파일 {sampled}개 표본)",
            "",
            "| 앵커 | 존재하는 파일 | 판정 |",
            "|---|---|---|",
        ]
        for label, hits in f.anchor_hits.items():
            if hits == 0:
                verdict = "**사라짐 — 유력 원인**"
            elif hits == sampled:
                verdict = "정상"
            else:
                verdict = "일부만 있음"
            lines.append(f"| {label} | {hits}/{sampled} | {verdict} |")

        lines += ["", "### 회귀한 소스", "", "```"]
        lines += f.regressed[:MAX_SAMPLES]
        if len(f.regressed) > MAX_SAMPLES:
            lines.append(f"... 외 {len(f.regressed) - MAX_SAMPLES}건")
        lines += [
            "```",
            "",
            "확인 방법: 위 소스의 캐시 파일을 열어 사라진 앵커가 어디로 갔는지 봅니다.",
            "",
            "```bash",
            f"ls data/raw_cache/{spec.cache}/",
            "```",
            "",
        ]
    return "\n".join(lines)


def diagnose(sites: list[str] | None = None, run_date: str | None = None,
             update_baseline: bool = True) -> tuple[Path | None, list[Finding]]:
    """지정한 사이트를 진단하고 회귀가 있으면 리포트를 남긴다.

    회귀가 없으면 리포트를 만들지 않는다 - 빈 파일이 매일 쌓이면 정작 볼 게
    있을 때 눈에 안 띈다. 대신 지금 커버리지를 기준선으로 갱신한다.

    회귀가 있을 때는 **기준선을 갱신하지 않는다.** 갱신해버리면 다음 실행부터
    망가진 상태가 "정상"이 돼서 같은 문제를 다시는 못 잡는다. 기준선은 어디까지나
    "마지막으로 멀쩡했던 상태"여야 한다.
    """
    sites = [s for s in (sites or SITES) if s in SITES]
    run_date = run_date or date.today().isoformat()
    coverage = load_coverage()

    findings = [f for f in (inspect(s, coverage) for s in sites) if f is not None]
    regressed = [f for f in findings if f.regressed]

    if not regressed:
        if update_baseline:
            save_coverage(findings)
        return None, findings

    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    path = REVIEW_DIR / f"drift_{run_date}.md"
    path.write_text(_render(findings, run_date), encoding="utf-8")
    return path, findings


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    only_baseline = "--baseline" in sys.argv

    if only_baseline:
        cov = load_coverage()
        found = [f for f in (inspect(s, cov) for s in (args or SITES)) if f]
        save_coverage(found)
        print(f"기준선 저장: {COVERAGE_PATH.relative_to(BASE_DIR)}")
        for f in found:
            print(f"  {SITES[f.site].label}: {len(f.current)}개 소스")
        raise SystemExit(0)

    report, findings = diagnose(args or None)
    for f in findings:
        base = len(f.baseline) if f.has_baseline else "기준선 없음"
        print(f"{SITES[f.site].label}: 캐시 {f.cached} / 현재 {len(f.current)} / "
              f"기준선 {base} / 회귀 {len(f.regressed)}")
    print(f"\n리포트: {report}" if report else "\n회귀 없음 - 리포트를 만들지 않았습니다.")
