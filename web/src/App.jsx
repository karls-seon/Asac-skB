import { useEffect, useState } from "react";

const won = (n) => (n == null ? "-" : `${Number(n).toLocaleString()}원`);

// "안 골랐다"(빈 문자열)와 "모르겠다"를 구분해야 한다. 전자는 문장으로 채우거나
// 서버가 되묻고, 후자는 습관 질문으로 넘어간다.
const UNKNOWN = "__unknown__";

function dataLabel(p) {
  if (p.data_unlimited) {
    return p.data_throttle_speed ? `무제한 (소진 후 ${p.data_throttle_speed})` : "무제한";
  }
  if (p.data_gb != null) return `${p.data_gb}GB`;
  if (p.daily_data_gb != null) return `매일 ${p.daily_data_gb}GB`;
  return "-";
}

export default function App() {
  const [opts, setOpts] = useState(null);
  // 기본값을 넣어 두면 사용자가 손대지 않은 값까지 서버로 가고, 폼이 자유입력보다
  // 우선이라 문장이 통째로 무시된다("9만원 무제한"이라고 써도 3만원·50GB가 됨).
  // 안 고른 칸은 비워 두고, 문장이 채우거나 서버가 되묻게 한다.
  const [form, setForm] = useState({
    budget: "",
    data_band: "",
    usage_hint: "",
    voice_unlimited: false,
    sms_unlimited: false,
    mvno_ok: true,
    current_carrier: "",
    age: "",
    ott_want: [],
    ott_required: false,
    current_fee: "",
  });
  const [text, setText] = useState("");
  const [res, setRes] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  useEffect(() => {
    fetch("/api/options")
      .then((r) => r.json())
      .then(setOpts)
      .catch(() => setErr("서버에 연결하지 못했습니다. uvicorn이 실행 중인지 확인해 주세요."));
  }, []);

  const set = (k, v) => setForm((f) => ({ ...f, [k]: v }));

  const toggleOtt = (name) =>
    setForm((f) => ({
      ...f,
      ott_want: f.ott_want.includes(name)
        ? f.ott_want.filter((o) => o !== name)
        : [...f.ott_want, name],
    }));

  async function submit(e) {
    e?.preventDefault();
    setBusy(true);
    setErr(null);
    // 안 고른 값은 보내지 않는다. 보내면 서버에서 자유입력 문장을 덮어써서
    // "9만원 무제한"이라고 써도 폼 기본값이 이긴다.
    const body = { text };
    for (const [k, v] of Object.entries(form)) {
      if (v === "" || v == null) continue;
      if (Array.isArray(v) && v.length === 0) continue;
      // 체크 안 한 상자는 "아니다"가 아니라 "말 안 했다"이다.
      if (v === false && k !== "mvno_ok") continue;
      // 알뜰폰만 반대다. 기본이 "허용"이라 그대로 보내면 문장의 "알뜰폰 말고"를
      // 덮어버린다. 사용자가 상자를 체크했을 때(=false)만 보낸다.
      if (k === "mvno_ok" && v === true) continue;
      // 데이터 구간을 "모르겠어요"로 둔 경우엔 습관 답만 보낸다.
      if (k === "data_band" && v === UNKNOWN) continue;
      body[k] = ["age", "current_fee", "budget"].includes(k) ? Number(v) : v;
    }
    try {
      const r = await fetch("/api/recommend", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      setRes(await r.json());
    } catch {
      setErr("추천을 받아오지 못했습니다. 잠시 후 다시 시도해 주세요.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="page">
      <header>
        <h1>요금제 추천</h1>
        <p className="sub">
          {opts ? `${opts.plan_count.toLocaleString()}개 요금제에서 조건에 맞는 5개를 찾습니다.` : "불러오는 중…"}
        </p>
      </header>

      <form onSubmit={submit} className="card">
        <label className="field">
          <span>말로 설명해도 됩니다</span>
          <input
            type="text"
            value={text}
            placeholder="예: 3만원대 50기가 쓰는데 넷플릭스 있으면 좋겠어"
            onChange={(e) => setText(e.target.value)}
          />
        </label>

        <div className="grid">
          <label className="field">
            <span>월 예산</span>
            <input type="number" min="1000" step="1000" value={form.budget}
                   placeholder="예: 30000"
                   onChange={(e) => set("budget", e.target.value)} />
          </label>

          <label className="field">
            <span>데이터 사용량</span>
            <select value={form.data_band} onChange={(e) => set("data_band", e.target.value)}>
              <option value="">고르지 않음</option>
              {opts?.data_bands.map((b) => <option key={b} value={b}>{b}</option>)}
              <option value={UNKNOWN}>모르겠어요</option>
            </select>
          </label>

          {form.data_band === UNKNOWN && (
            <label className="field span2">
              <span>평소 습관은 어느 쪽인가요?</span>
              <select value={form.usage_hint} onChange={(e) => set("usage_hint", e.target.value)}>
                <option value="">고르세요</option>
                {opts?.usage_hints.map((h) => <option key={h} value={h}>{h}</option>)}
              </select>
            </label>
          )}

          {/* 3사 온라인 전용(요고·너겟·다이렉트)은 같은 통신사에서 기기변경으로
              못 옮긴다. 지금 쓰는 곳을 알아야 못 가는 걸 안 보여줄 수 있다. */}
          <label className="field">
            <span>지금 쓰는 통신사 <em>선택</em></span>
            <select value={form.current_carrier}
                    onChange={(e) => set("current_carrier", e.target.value)}>
              <option value="">고르지 않음</option>
              {opts?.carriers.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          </label>

          <label className="field">
            <span>나이 <em>선택</em></span>
            <input type="number" min="1" max="119" value={form.age}
                   placeholder="연령 전용 요금제 확인용"
                   onChange={(e) => set("age", e.target.value)} />
          </label>

          <label className="field">
            <span>지금 내는 요금 <em>선택</em></span>
            <input type="number" min="0" step="1000" value={form.current_fee}
                   placeholder="입력하면 절감액을 보여줍니다"
                   onChange={(e) => set("current_fee", e.target.value)} />
          </label>
        </div>

        <div className="checks">
          <label><input type="checkbox" checked={form.voice_unlimited}
                        onChange={(e) => set("voice_unlimited", e.target.checked)} /> 통화를 많이 합니다</label>
          <label><input type="checkbox" checked={form.sms_unlimited}
                        onChange={(e) => set("sms_unlimited", e.target.checked)} /> 문자를 많이 보냅니다</label>
          <label><input type="checkbox" checked={!form.mvno_ok}
                        onChange={(e) => set("mvno_ok", !e.target.checked)} /> 알뜰폰은 빼고 보기</label>
        </div>

        <div className="field">
          <span>원하는 OTT <em>있으면 위로 올려 드립니다</em></span>
          <div className="chips">
            {opts?.otts.slice(0, 12).map((o) => (
              <button type="button" key={o}
                      className={form.ott_want.includes(o) ? "chip on" : "chip"}
                      onClick={() => toggleOtt(o)}>{o}</button>
            ))}
          </div>
          {form.ott_want.length > 0 && (
            <label className="inline">
              <input type="checkbox" checked={form.ott_required}
                     onChange={(e) => set("ott_required", e.target.checked)} />
              이 OTT가 있는 요금제만 보기
            </label>
          )}
        </div>

        <button className="go" disabled={busy}>{busy ? "찾는 중…" : "추천받기"}</button>
      </form>

      {err && <p className="alert">{err}</p>}

      {res?.question && (
        <div className="card ask">
          <h2>하나만 더 여쭐게요</h2>
          <p>{res.question}</p>
        </div>
      )}

      {res && !res.question && (
        <section className="results">
          {res.report && <p className="report">{res.report}</p>}

          {res.dropped?.length > 0 && (
            <p className="alert warn">
              요청하신 조건으로는 결과가 없어 <b>{res.dropped.join(", ")}</b> 조건을 뺐습니다.
            </p>
          )}
          {res.note && <p className="alert">{res.note}</p>}
          {res.need_budget && (
            <p className="alert warn">예산을 월 {won(res.need_budget)}까지 올리면 추천할 수 있습니다.</p>
          )}

          {res.plans.map((p, i) => (
            <article className="plan" key={p.plan_id}>
              <div className="rank">{i + 1}</div>
              <div className="body">
                <h3>{p.plan_name}</h3>
                <p className="who">
                  {p.carrier_type === "MVNO" ? `알뜰폰 · ${p.mvno_brand ?? ""} (${p.host_mno}망)` : `${p.host_mno}`}
                  {p.network_gen ? ` · ${p.network_gen}` : ""}
                  {p.age_condition && <span className="tag age">{p.age_condition}만 가입</span>}
                </p>
                {p.signup_channel && (
                  <p className="channel">
                    <span className={p.signup_channel === "모요" ? "tag via" : "tag direct"}>
                      {p.signup_channel}에서 가입
                    </span>
                    {/* 통신3사 요금제를 모요로 가입하는 경우. 같은 이름이라도
                        공식몰과 값이 다르다 (너겟49: 모요 7,000원 / 공식몰 49,000원). */}
                    {p.signup_channel === "모요" && p.carrier_type === "MNO" && (
                      <em>통신사 공식몰과 값이 다를 수 있습니다</em>
                    )}
                  </p>
                )}
                <ul className="specs">
                  <li>데이터 {dataLabel(p)}</li>
                  <li>통화 {p.voice_unlimited ? "무제한" : "기본"}</li>
                  <li>문자 {p.sms_unlimited ? "무제한" : "기본"}</li>
                  {p.ott_matched?.length > 0 && <li className="ott">{p.ott_matched.join(", ")} 제공</li>}
                </ul>
                {/* 사은품·페이백은 이미 이 가격에 반영돼 있다. 얼마짜리가
                    반영된 건지 보여주되, 요금에서 또 빼지는 않는다. */}
                {p.benefit_monthly > 0 && (
                  <p className="gift">
                    사은품·페이백 <b>월 {won(p.benefit_monthly)}</b> 상당이 위 요금에 반영됨
                    {p.gift_benefit && <em>{p.gift_benefit.split(" | ")[0]}</em>}
                  </p>
                )}
                {p.discount_period_months && (
                  <p className="caution">프로모션 할인 {p.discount_period_months}개월 — 이후 요금이 오를 수 있습니다</p>
                )}
              </div>
              <div className="price">
                <strong>{won(p.discounted_fee)}</strong>
                {p.savings != null && <span className="save">월 {won(p.savings)} 절약</span>}
                {p.value_score != null && (
                  <span className={p.value_score >= 0 ? "value good" : "value bad"}>
                    시세보다 {won(Math.abs(p.value_score))} {p.value_score >= 0 ? "쌈" : "비쌈"}
                  </span>
                )}
                <a href={p.source_url} target="_blank" rel="noreferrer">상세 보기</a>
              </div>
            </article>
          ))}
        </section>
      )}
    </div>
  );
}
