import { useEffect, useState } from "react";

const won = (n) => (n == null ? "-" : `${Number(n).toLocaleString()}원`);

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
  const [form, setForm] = useState({
    budget: 30000,
    data_band: "20~50GB",
    usage_hint: "",
    voice_unlimited: false,
    sms_unlimited: false,
    mvno_ok: true,
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
    // 빈 문자열은 "안 골랐다"는 뜻이라 보내지 않는다. 서버가 기본값을 쓴다.
    const body = { text };
    for (const [k, v] of Object.entries(form)) {
      if (v === "" || v == null) continue;
      if (Array.isArray(v) && v.length === 0) continue;
      body[k] = k === "age" || k === "current_fee" || k === "budget" ? Number(v) : v;
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
                   onChange={(e) => set("budget", e.target.value)} required />
          </label>

          <label className="field">
            <span>데이터 사용량</span>
            <select value={form.data_band} onChange={(e) => set("data_band", e.target.value)}>
              {opts?.data_bands.map((b) => <option key={b} value={b}>{b}</option>)}
              <option value="">모르겠어요</option>
            </select>
          </label>

          {form.data_band === "" && (
            <label className="field span2">
              <span>평소 습관은 어느 쪽인가요?</span>
              <select value={form.usage_hint} onChange={(e) => set("usage_hint", e.target.value)}>
                <option value="">고르세요</option>
                {opts?.usage_hints.map((h) => <option key={h} value={h}>{h}</option>)}
              </select>
            </label>
          )}

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
                  {p.age_condition ? ` · ${p.age_condition}` : ""}
                </p>
                <ul className="specs">
                  <li>데이터 {dataLabel(p)}</li>
                  <li>통화 {p.voice_unlimited ? "무제한" : "기본"}</li>
                  <li>문자 {p.sms_unlimited ? "무제한" : "기본"}</li>
                  {p.ott_matched?.length > 0 && <li className="ott">{p.ott_matched.join(", ")} 제공</li>}
                </ul>
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
