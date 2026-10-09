/* fitdash phone app. Plain JS, no dependencies. Draws the model from /api/model; logs through /api/*. */
"use strict";

const $ = (sel) => document.querySelector(sel);
const view = $("#view");
const S = { m: null, loadedAt: 0, tab: "today", logDate: "today", log: null, busy: false };

/* ------------------------------------------------------------------ formatting */
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const hm = (min) => {
  if (min == null || isNaN(min)) return "—";
  const t = Math.round(min), h = Math.floor(t / 60), m = t % 60;
  return h ? `${h}h${String(m).padStart(2, "0")}` : `${m}m`;
};
const num = (v, d = 0) => (v == null || isNaN(v) ? "—" : Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }));
const pdate = (iso) => new Date(iso.slice(0, 10) + "T12:00:00");
const dow = (iso) => pdate(iso).toLocaleDateString(undefined, { weekday: "short" });
const mday = (iso) => pdate(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
const clock = (iso) => {
  if (!iso) return "—";
  let [h, m] = iso.slice(11, 16).split(":").map(Number);
  const ap = h >= 12 ? "pm" : "am"; h = h % 12 || 12;
  return `${h}:${String(m).padStart(2, "0")} ${ap}`;
};
const clockMin = (min) => {
  if (min == null) return "—";
  let t = Math.round(min) % 1440, h = Math.floor(t / 60), m = t % 60;
  const ap = h >= 12 ? "pm" : "am"; h = h % 12 || 12;
  return `${h}:${String(m).padStart(2, "0")} ${ap}`;
};
const minsOf = (iso) => { const [h, m] = iso.slice(11, 16).split(":").map(Number); return h * 60 + m; };
const hhmmMin = (s) => { if (!s) return null; const [h, m] = s.split(":").map(Number); return h * 60 + m; };

/* ------------------------------------------------------------------ colors */
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const C = {};
["green", "yellow", "red", "strain", "sleep", "deep", "rem", "light", "awake", "gray", "card2", "line", "text3"].forEach((k) => (C[k] = css("--" + k)));
const bandColor = (b) => ({ green: C.green, yellow: C.yellow, red: C.red }[b] || C.gray);
const recColor = (v) => (v == null ? C.gray : v >= 67 ? C.green : v >= 34 ? C.yellow : C.red);
const freshColor = (p) => (p == null ? C.gray : p >= 100 ? "#8e8e93" : p >= 90 ? "#4cf08a" : p >= 70 ? C.green : p >= 40 ? C.yellow : C.red);
const lvlIcon = { good: "✓", watch: "!", flag: "✕", none: "·" };

/* ------------------------------------------------------------------ charts (SVG strings) */
function ring(frac, color, value, unit, sub) {
  const r = 44, c = 2 * Math.PI * r, f = Math.max(0, Math.min(1, frac ?? 0));
  return `<div class="ring"><svg viewBox="0 0 110 110" aria-hidden="true">
    <circle cx="55" cy="55" r="${r}" fill="none" stroke="${C.card2}" stroke-width="9"/>
    ${f > 0 ? `<circle cx="55" cy="55" r="${r}" fill="none" stroke="${color}" stroke-width="9" stroke-linecap="round"
      stroke-dasharray="${(c * f).toFixed(1)} ${c.toFixed(1)}" transform="rotate(-90 55 55)"/>` : ""}</svg>
    <div class="v"><b class="num">${esc(value)}</b><span>${esc(unit)}</span></div></div>`;
}

function spark(vals, color, { w = 140, h = 30, lo, hi } = {}) {
  const pts = vals.map((v, i) => [i, v]).filter(([, v]) => v != null);
  if (pts.length < 2) return `<svg viewBox="0 0 ${w} ${h}"></svg>`;
  const ys = pts.map(([, v]) => v), mn = lo ?? Math.min(...ys), mx = hi ?? Math.max(...ys), span = mx - mn || 1;
  const X = (i) => (i / (vals.length - 1)) * (w - 6) + 3, Y = (v) => h - 4 - ((v - mn) / span) * (h - 8);
  const d = pts.map(([i, v], k) => `${k ? "L" : "M"}${X(i).toFixed(1)} ${Y(v).toFixed(1)}`).join("");
  const [li, lv] = pts[pts.length - 1];
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true"><path d="${d}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/><circle cx="${X(li)}" cy="${Y(lv)}" r="3" fill="${color}"/></svg>`;
}

function barsChart(vals, colorFn, { h = 90, labels = [], max } = {}) {
  const w = 320, n = vals.length, gap = 3, bw = (w - gap * (n - 1)) / n;
  const mx = max ?? Math.max(1, ...vals.filter((v) => v != null));
  let s = "";
  vals.forEach((v, i) => {
    const x = i * (bw + gap);
    if (v == null) { s += `<rect x="${x}" y="${h - 3}" width="${bw}" height="3" rx="1.5" fill="${C.line}"/>`; return; }
    const bh = Math.max(3, (v / mx) * (h - 4));
    s += `<rect x="${x.toFixed(1)}" y="${(h - bh).toFixed(1)}" width="${bw.toFixed(1)}" height="${bh.toFixed(1)}" rx="${Math.min(3, bw / 2)}" fill="${colorFn(v, i)}"/>`;
  });
  let lab = "";
  const anchor = (i) => (i === 0 ? "start" : i === n - 1 ? "end" : "middle");
  const lx = (i) => (i === 0 ? 0 : i === n - 1 ? w : i * (bw + gap) + bw / 2);
  labels.forEach(([i, t]) => (lab += `<text x="${lx(i).toFixed(1)}" y="${h + 13}" text-anchor="${anchor(i)}">${esc(t)}</text>`));
  return `<svg class="chart" viewBox="0 0 ${w} ${h + (labels.length ? 16 : 0)}" aria-hidden="true">${s}${lab}</svg>`;
}

function lineChart(vals, color, { base, h = 110, labels = [] } = {}) {
  const w = 320, pts = vals.map((v, i) => [i, v]).filter(([, v]) => v != null);
  if (pts.length < 2) return `<p class="empty">Not enough data yet.</p>`;
  let ys = pts.map(([, v]) => v);
  if (base) ys = ys.concat([base.mean - base.sd, base.mean + base.sd]);
  const mn = Math.min(...ys), mx = Math.max(...ys), pad = (mx - mn) * 0.12 || 1, lo = mn - pad, hi = mx + pad;
  const X = (i) => (i / (vals.length - 1)) * (w - 30) + 2, Y = (v) => 6 + (1 - (v - lo) / (hi - lo)) * (h - 12);
  let band = "";
  if (base) band = `<rect x="2" y="${Y(base.mean + base.sd)}" width="${w - 30}" height="${Math.max(1, Y(base.mean - base.sd) - Y(base.mean + base.sd))}" fill="${color}" opacity=".09"/><line x1="2" x2="${w - 28}" y1="${Y(base.mean)}" y2="${Y(base.mean)}" stroke="${color}" stroke-opacity=".35" stroke-dasharray="3 4"/>`;
  const d = pts.map(([i, v], k) => `${k ? "L" : "M"}${X(i).toFixed(1)} ${Y(v).toFixed(1)}`).join("");
  const [li, lv] = pts[pts.length - 1];
  const anchor = (i) => (i === 0 ? "start" : i === vals.length - 1 ? "end" : "middle");
  let lab = labels.map(([i, t]) => `<text x="${i === vals.length - 1 ? w - 28 : X(i)}" y="${h + 12}" text-anchor="${anchor(i)}">${esc(t)}</text>`).join("");
  return `<svg class="chart" viewBox="0 0 ${w} ${h + 16}" aria-hidden="true">${band}
    <text x="${w - 2}" y="${Y(hi - pad) + 4}" text-anchor="end">${Math.round(hi - pad)}</text>
    <text x="${w - 2}" y="${Y(lo + pad) + 4}" text-anchor="end">${Math.round(lo + pad)}</text>
    <path d="${d}" fill="none" stroke="${color}" stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round"/>
    <circle cx="${X(li)}" cy="${Y(lv)}" r="4" fill="${color}" stroke="#121316" stroke-width="2"/>${lab}</svg>`;
}

function hbar(label, value, frac, color, right, band) {
  const b = band ? `<span class="band" style="left:${band[0] * 100}%;width:${(band[1] - band[0]) * 100}%"></span>` : "";
  return `<div class="bar"><span class="l">${esc(label)}</span><span class="t">${b}<i style="width:${Math.max(0, Math.min(1, frac)) * 100}%;background:${color}"></i></span><span class="r num">${esc(right ?? value)}</span></div>`;
}

const dayLabels = (days, every = 7) => days.map((d, i) => [i, d]).filter(([i]) => (days.length - 1 - i) % every === 0).map(([i, d]) => [i, i === days.length - 1 ? "today" : mday(d)]);

/* ------------------------------------------------------------------ api */
async function api(path, body) {
  const opt = body ? { method: "POST", headers: { "Content-Type": "application/json", "X-Fitdash": "1" }, body: JSON.stringify(body) } : {};
  const r = await fetch(path, { cache: "no-store", ...opt });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}
function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove("show"), 2200);
}
async function load(force = false) {
  if (S.busy || (!force && S.m && Date.now() - S.loadedAt < 30000)) return;
  S.busy = true;
  try {
    S.m = await api("/api/model");
    S.loadedAt = Date.now();
    render();
  } catch (e) {
    if (!S.m) view.innerHTML = `<div class="card"><h2>Can't reach your Mac</h2><p class="sub">${esc(e.message)}</p><p class="muted">Is the Mac awake, and Tailscale connected on this phone?</p><button class="btn" id="retry">Try again</button></div>`;
    else toast("Couldn't refresh: " + e.message);
  } finally { S.busy = false; }
}

/* ------------------------------------------------------------------ header / sync */
function header() {
  const m = S.m, titles = { today: "Today", sleep: "Sleep", train: "Training", log: "Log", trends: "Trends" };
  $("#top-title").textContent = titles[S.tab];
  $("#top-eyebrow").textContent = m ? pdate(m.date).toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" }) : "fitdash";
  const btn = $("#sync-btn"), sync = (m && m.sync) || {};
  btn.classList.toggle("busy", !!sync.running);
  const nowMs = m && m.now ? new Date(m.now).getTime() : Date.now();
  const age = m && m.last_sync ? (nowMs - new Date(m.last_sync).getTime()) / 3.6e6 : null;
  btn.classList.toggle("stale", age != null && age > 6);
  $("#sync-label").textContent = sync.running ? "Syncing…" : m && m.last_sync ? "Synced " + clock(m.last_sync) : "Sync";
  document.querySelectorAll(".tabs a").forEach((a) => a.classList.toggle("on", a.dataset.tab === S.tab));
}
$("#sync-btn").addEventListener("click", async () => {
  try {
    const st = await api("/api/sync", {});
    if (S.m) S.m.sync = st; header();
    toast("Syncing with Google…");
    const poll = setInterval(async () => {
      const s = await api("/api/sync").catch(() => null);
      if (s && !s.running) {
        clearInterval(poll);
        toast(s.last && s.last.ok ? "Synced" : "Sync finished with a problem");
        await load(true);
      }
    }, 2500);
  } catch (e) { toast(e.message); }
});

/* ------------------------------------------------------------------ today */
function renderToday(m) {
  const rec = m.recovery || {}, st = m.strain || {}, sl = m.sleep || {}, sc = (sl.score || {}).score;
  const v = m.verdict || {}, [word, ...rest] = (v.headline || "").split(":");
  const co = m.coach || {}, note = co.note;
  const s = m.series || {}, b = m.baselines || {}, days = m.days || [];
  const last14 = (k) => (s[k] || []).slice(-14);
  const tile = (k, val, unit, d, vals, color, opts) => `<div class="tile"><div class="k">${k}</div><div class="val num">${val}<small>${unit}</small></div><div class="d">${d}</div>${spark(vals, color, opts)}</div>`;
  const avg = (k, f = 0) => (b[k] ? "avg " + num(b[k].mean, f) : "");
  const tn = sl.tonight || {}, tr = m.training || {}, sp = tr.split || {};
  const fresh = (sp.fresh || {})[sp.next];
  const att = m.attention || [];
  return `
  <section class="card">
    <div class="rings">
      <div>${ring((rec.score ?? 0) / 100, bandColor(rec.band), rec.score ?? "—", "%")}<div class="ring-label">Recovery</div><div class="ring-sub">${esc(rec.band || rec.reason || "")}</div></div>
      <div>${ring((st.day ?? 0) / 21, C.strain, st.day != null ? num(st.day, 1) : "—", "of 21")}<div class="ring-label">Strain</div><div class="ring-sub">${st.target ? `target ${num(st.target[0])}–${num(st.target[1])}` : ""}</div></div>
      <div>${ring((sc ?? 0) / 100, C.sleep, sc ?? "—", hm(sl.asleep))}<div class="ring-label">Sleep</div><div class="ring-sub">${esc((sl.score || {}).band || "")}</div></div>
    </div>
  </section>
  <section class="card verdict ${esc(v.level)}"><b>${esc(word)}</b><p>${esc(rest.join(":").trim())}</p></section>
  ${note || co.fallback ? `<section class="card coach"><div class="who"><div class="avatar">C</div><div><b>Coach</b><div class="muted">${note ? esc(({ morning: "Morning", activity: "After your workout", evening: "Closing the day", note: "Note" })[note.kind] || "Note") + " · " + clock(note.at) : "auto · no written note yet"}</div></div></div><p>${esc(note ? note.text : co.fallback)}</p></section>` : ""}
  ${priorityCard(m)}
  <div class="tiles">
    ${tile("HRV", num(last14("hrv").slice(-1)[0]), " ms", avg("hrv"), last14("hrv"), C.green)}
    ${tile("Resting HR", num(last14("rhr").slice(-1)[0]), " bpm", avg("rhr"), last14("rhr"), C.red)}
    ${tile("Sleep", hm(sl.asleep), "", sl.performance != null ? `${sl.performance}% of need` : "no night", last14("asleep"), C.sleep)}
    ${tile("Steps", num((m.activity || {}).steps), "", `goal ${num((m.config || {}).steps_goal || 10000)}`, last14("steps"), C.awake, { lo: 0 })}
  </div>
  <div style="height:12px"></div>
  ${att.length ? `<section class="card"><h2>Needs attention</h2><ul class="list">${att.map(([l, t]) => `<li><span class="ic ${l}">${lvlIcon[l]}</span><span>${esc(t)}</span></li>`).join("")}</ul></section>` : ""}
  <section class="card"><h2>Plan</h2><dl class="kv">
    <dt>Tonight</dt><dd>${tn.asleep_by != null ? `Asleep by <b>${clockMin(tn.asleep_by)}</b> for your ${hm((tn.need || {}).total)} need` : "—"}${m.experiment ? `<div class="muted">Experiment: lights out by ${esc(clockMin(hhmmMin(m.experiment.lights_out)))}</div>` : ""}</dd>
    <dt>Training</dt><dd>${sp.next ? `Next: <b>${esc(sp.next)}</b>${fresh != null ? ` · ${fresh}% fresh` : ""}` : "—"}<div class="muted">Gym ${tr.gym_week}/${tr.gym_goal} this week</div></dd>
    <dt>Sleep debt</dt><dd>${hm(tn.debt)}</dd>
  </dl></section>`;
}

const PR_GLYPH = { done: "●", partial: "◐", missed: "✕" }, PR_LABEL = { done: "done", partial: "some progress", missed: "not today" };
function priorityCard(m) {
  const pr = m.priorities || {}, e = pr.today || {}, items = e.items || [], wk = pr.week || {};
  const late = new Date(m.now || Date.now()).getHours() >= 19;
  const head = `<h2>Priorities ${wk.set_days ? `<small>${wk.done}/${wk.items} done this week · ${wk.streak}-day streak</small>` : ""}</h2>`;
  if (!items.length) return `<section class="card">${head}<p class="sub">What are today's top 3? Setting them is the most important minute of your day.</p><a class="btn" href="#log" style="display:inline-block;text-decoration:none;margin-top:6px">Set priorities</a></section>`;
  return `<section class="card">${head}<ul class="list">${items.map((i, k) => `<li><span class="ic ${i.status === "done" ? "good" : i.status === "partial" ? "watch" : i.status === "missed" ? "flag" : "muted"}">${PR_GLYPH[i.status] || "○"}</span><span style="flex:1">${esc(i.text)}${i.status ? ` <span class="muted">· ${PR_LABEL[i.status]}</span>` : ""}</span></li>`).join("")}</ul>
    ${late && items.some((i) => !i.status) ? `<a class="btn ghost" href="#log" style="display:inline-block;text-decoration:none;margin-top:10px">How did they go?</a>` : ""}
    ${e.reflection ? `<p class="muted" style="margin:10px 0 0">“${esc(e.reflection)}”</p>` : ""}</section>`;
}

/* ------------------------------------------------------------------ sleep */
function hypnogram(sl) {
  const tl = sl.timeline || [];
  if (!tl.length) return "";
  const w = 320, lanes = ["awake", "rem", "light", "deep"], lh = 18, total = Math.max(...tl.map((t) => t.end_min));
  const col = { awake: C.awake, rem: C.rem, light: C.light, deep: C.deep };
  let s = lanes.map((l, i) => `<rect x="0" y="${i * lh + 2}" width="${w}" height="${lh - 4}" rx="4" fill="${C.card2}"/>`).join("");
  tl.forEach((t) => {
    const i = lanes.indexOf(t.stage); if (i < 0) return;
    s += `<rect x="${((t.start_min / total) * w).toFixed(1)}" y="${i * lh + 2}" width="${Math.max(1.5, ((t.end_min - t.start_min) / total) * w).toFixed(1)}" height="${lh - 4}" rx="2" fill="${col[t.stage]}"/>`;
  });
  return `<svg class="chart" viewBox="0 0 ${w} ${lanes.length * lh + 16}" aria-hidden="true">${s}
    <text x="0" y="${lanes.length * lh + 13}">${clock(sl.start)}</text><text x="${w}" y="${lanes.length * lh + 13}" text-anchor="end">${clock(sl.end)}</text></svg>`;
}
function bedWake(timing, cfg) {
  const w = 320, rh = 16, left = 34, start = 21 * 60, span = 17 * 60;    // 9 pm → 2 pm next day
  const X = (min) => left + ((min - start) / span) * (w - left);
  const shift = (min) => (min < 14 * 60 ? min + 1440 : min);             // after midnight counts as "next day"
  let s = "", n = timing.length;
  timing.forEach((t, i) => {
    const y = i * rh;
    s += `<text x="0" y="${y + 11}">${esc(dow(t.date))}</text>`;
    if (!t.start) return;
    const a = shift(minsOf(t.start));
    let b = minsOf(t.end) + (t.end.slice(0, 10) > t.start.slice(0, 10) ? 1440 : 0);
    if (b <= a) b += 1440;
    const x1 = Math.max(left, X(a)), x2 = Math.min(w, X(b));
    s += `<rect x="${x1.toFixed(1)}" y="${y + 3}" width="${Math.max(2, x2 - x1).toFixed(1)}" height="${rh - 6}" rx="4" fill="${C.sleep}" opacity="${i === n - 1 ? 1 : 0.75}"/>`;
  });
  const mark = (hhmm, color) => { const x = hhmmMin(hhmm); if (x == null) return ""; const px = X(shift(x)); return `<line x1="${px}" x2="${px}" y1="0" y2="${n * rh}" stroke="${color}" stroke-width="1.5" stroke-dasharray="3 3"/>`; };
  const ticks = [[21, "9p"], [24, "12a"], [27, "3a"], [30, "6a"], [33, "9a"], [36, "12p"]].map(([h, t]) => `<text x="${X(h * 60)}" y="${n * rh + 12}" text-anchor="${h === 21 ? "start" : "middle"}">${t}</text>`).join("");
  return `<svg class="chart" viewBox="0 0 ${w} ${n * rh + 16}" aria-hidden="true">${s}${mark(cfg.bedtime_goal, C.green)}${mark(cfg.wake_anchor, "#c8c8cc")}${ticks}</svg>
    <div class="legend">${cfg.bedtime_goal ? `<span><i style="background:${C.green}"></i>${clockMin(hhmmMin(cfg.bedtime_goal))} bedtime goal</span>` : ""}${cfg.wake_anchor ? `<span><i style="background:#c8c8cc"></i>${clockMin(hhmmMin(cfg.wake_anchor))} wake</span>` : ""}</div>`;
}
function renderSleep(m) {
  const sl = m.sleep || {}, sc = sl.score || {}, comp = sc.components || {}, nd = sl.need || {}, tn = sl.tonight || {};
  const names = { sufficiency: "Hours vs need", efficiency: "Efficiency", consistency: "Consistency", stress: "Sleep stress" };
  const stg = sl.stages || {};
  const ex = m.experiment;
  if (!sl.has_night) return `<section class="card"><h2>Last night</h2><p class="empty">No sleep recorded for last night yet. Sync after your watch has synced to the Fitbit app.</p></section>`;
  return `
  <section class="card">
    <div class="between"><div class="head"><span class="big num">${sc.score ?? "—"}</span><span class="pill ${sc.band === "optimal" ? "good" : sc.band === "sufficient" ? "watch" : "flag"}">${esc(sc.band || "")}</span></div>
    <div class="sub num">${hm(sl.asleep)} asleep</div></div>
    <div class="muted">${clock(sl.start)} → ${clock(sl.end)} · ${sl.efficiency ?? "—"}% efficient · awake ${sl.awake_count ?? "—"}×</div>
    <div class="divider"></div>
    ${Object.keys(names).map((k) => { const c = comp[k] || {}; const v = c.score; return hbar(names[k], v, (v ?? 0) / 100, v == null ? C.gray : v >= 90 ? C.green : v >= 70 ? C.yellow : C.red, v ?? "—"); }).join("")}
    <p class="muted" style="margin:10px 0 0">WHOOP's four sleep components, with our own weights (50 / 20 / 15 / 15).</p>
  </section>
  <section class="card"><h2>Stages</h2>${hypnogram(sl)}
    <div class="legend"><span><i style="background:${C.deep}"></i>Deep ${hm(stg.deep)}</span><span><i style="background:${C.light}"></i>Light ${hm(stg.light)}</span><span><i style="background:${C.rem}"></i>REM ${hm(stg.rem)}</span><span><i style="background:${C.awake}"></i>Awake ${hm(stg.awake)}</span></div>
  </section>
  <section class="card"><h2>Sleep need <small>last night</small></h2>
    <div class="head"><span class="big num">${hm(nd.total)}</span></div>
    <dl class="kv"><dt>Base</dt><dd>${hm(nd.base)}</dd><dt>½ of debt</dt><dd>${hm(nd.debt_adj)}</dd><dt>Strain</dt><dd>${hm(nd.strain_adj)}</dd>${nd.nap_credit ? `<dt>Naps</dt><dd>−${hm(nd.nap_credit)}</dd>` : ""}</dl>
    <div class="divider"></div>
    <div class="between"><span class="sub">Debt going into tonight</span><b class="num">${hm(tn.debt)}</b></div>
    <div class="between"><span class="sub">Asleep by tonight</span><b class="num">${clockMin(tn.asleep_by)}</b></div>
  </section>
  <section class="card"><h2>Bed &amp; wake <small>14 nights</small></h2>${bedWake(sl.timing || [], m.config || {})}</section>
  ${ex ? `<section class="card"><h2>${esc(ex.name)}</h2>
    <div class="row" style="flex-wrap:wrap;gap:6px">${ex.nights.map((n) => `<span title="${esc(n.date)}" style="width:18px;height:18px;border-radius:5px;background:${n.state === "hit" ? C.green : n.state === "miss" ? C.red : C.card2}"></span>`).join("")}</div>
    <p class="muted" style="margin:10px 0 0">${ex.nights.filter((n) => n.state === "hit").length}/${ex.nights.filter((n) => n.state !== "upcoming").length} nights on target · lights out by ${esc(clockMin(hhmmMin(ex.lights_out)))}</p></section>` : ""}`;
}

/* ------------------------------------------------------------------ training */
const MUSCLE = { chest: "Chest", shoulders: "Shoulders", lats: "Back", biceps: "Biceps", triceps: "Triceps", abs: "Abs", quads: "Quads", hamstrings: "Hamstrings", glutes: "Glutes", calves: "Calves" };
function miniRing(p, color) {
  const r = 17, c = 2 * Math.PI * r;
  return `<svg viewBox="0 0 42 42" aria-hidden="true"><circle cx="21" cy="21" r="${r}" fill="none" stroke="${C.line}" stroke-width="5"/><circle cx="21" cy="21" r="${r}" fill="none" stroke="${color}" stroke-width="5" stroke-linecap="round" stroke-dasharray="${(c * p / 100).toFixed(1)} ${c.toFixed(1)}" transform="rotate(-90 21 21)"/><text x="21" y="25" text-anchor="middle" style="fill:#f5f5f7;font-size:11px;font-weight:700">${p}</text></svg>`;
}
function renderTrain(m) {
  const tr = m.training || {}, sp = tr.split || {}, mu = m.muscles || {}, lf = m.lifting || {}, wk = (m.workouts || {}).week || [];
  const rows = [...(mu.rows || [])].sort((a, b) => a.fresh - b.fresh);
  const ac = tr.acwr || {};
  const lo = (lf.target || [10, 20])[0], hi = (lf.target || [10, 20])[1];
  const byDay = {};
  wk.forEach((w) => (byDay[w.date] = byDay[w.date] || []).push(w));
  const imp = tr.impact_weeks || [];
  return `
  <section class="card"><h2>Split <small>gym ${tr.gym_week}/${tr.gym_goal} this week</small></h2>
    <div class="queue">${(sp.queue || []).map((d, i) => `<div class="${i === 0 ? "next" : ""}"><b>${esc(d)}</b><span>${i === 0 ? "next · " : ""}${(sp.fresh || {})[d] ?? "—"}% fresh</span></div>`).join("")}</div>
    ${sp.last_done ? `<p class="muted" style="margin:10px 0 0">Last: ${esc(sp.last_done)} on ${esc(mday(sp.last_done_date))}</p>` : ""}
  </section>
  <section class="card"><h2>Muscle freshness <small>least recovered first</small></h2>
    ${mu.has_strength ? `<div class="muscles">${rows.map((r) => `<div class="muscle">${miniRing(r.fresh, freshColor(r.fresh))}<div><b>${MUSCLE[r.muscle] || esc(r.muscle)}</b><span>${r.last ? "trained " + esc(dow(r.last)) : "fresh"}</span></div></div>`).join("")}</div>`
      : `<p class="empty">No strength sessions yet. Log one in the Log tab.</p>`}
  </section>
  <section class="card"><h2>Hard sets <small>last 7 days · target ${lo}–${hi}</small></h2>
    ${lf.has_log ? Object.keys(MUSCLE).map((k) => { const v = (lf.sets || {})[k] || 0; return hbar(MUSCLE[k], v, v / 25, v >= lo && v <= hi ? C.green : v < lo ? C.yellow : C.strain, num(v, v % 1 ? 1 : 0), [lo / 25, hi / 25]); }).join("") : `<p class="empty">Log sets in the Log tab to see weekly volume per muscle.</p>`}
  </section>
  ${(lf.lifts || []).length ? `<section class="card"><h2>Progress <small>best est. 1RM, 8 weeks</small></h2><ul class="list">${lf.lifts.slice(0, 8).map((r) => `<li style="align-items:center"><div style="flex:1"><b>${esc(r.exercise)}</b>${r.pr ? ' <span class="pill good">PR</span>' : ""}<div class="muted">${esc(r.last.top)} · ${esc(mday(r.last.date))}</div></div><div style="width:70px">${spark(r.points.map((p) => p.e1rm), C.strain, { w: 70, h: 26 })}</div><div style="width:64px;text-align:right"><b class="num">${num(r.last.e1rm)}</b> kg<div class="muted ${r.change_pct > 0 ? "good" : ""}">${r.change_pct == null ? "first" : (r.change_pct > 0 ? "+" : "") + r.change_pct + "%"}</div></div></li>`).join("")}</ul></section>` : ""}
  <section class="card"><h2>Workouts <small>last 7 days</small></h2>
    ${wk.length ? [...wk].reverse().map((w) => `<div class="wk"><span class="day">${esc(dow(w.date))}</span><div><b>${esc(w.label)}</b>${w.split ? ` <span class="muted">${esc(w.split)}</span>` : ""}<div class="muted">${clock(w.start)} · ${Math.round(w.minutes)} min${w.distance_km ? ` · ${num(w.distance_km, 1)} km` : ""}${w.avg_hr ? ` · ${Math.round(w.avg_hr)} bpm` : ""}</div></div><span class="s num">${w.strain != null ? num(w.strain, 1) : ""}</span></div>`).join("") : `<p class="empty">No workouts in the last 7 days.</p>`}
  </section>
  <section class="card"><h2>Training load</h2>
    <div class="between"><div><span class="big num">${ac.ratio != null ? num(ac.ratio, 2) : "—"}</span> <span class="pill ${ac.zone === "sweet spot" ? "good" : ac.zone === "high" ? "flag" : "watch"}">${esc(ac.zone || "")}</span></div><span class="muted">7-day ÷ 28-day load</span></div>
    <svg class="chart" viewBox="0 0 320 34" aria-hidden="true">
      <rect x="0" y="10" width="${0.8 / 2 * 320}" height="8" rx="4" fill="${C.card2}"/><rect x="${0.8 / 2 * 320}" y="10" width="${0.5 / 2 * 320}" height="8" fill="${C.green}" opacity=".7"/><rect x="${1.3 / 2 * 320}" y="10" width="${0.2 / 2 * 320}" height="8" fill="${C.yellow}" opacity=".7"/><rect x="${1.5 / 2 * 320}" y="10" width="${0.5 / 2 * 320}" height="8" rx="4" fill="${C.red}" opacity=".7"/>
      ${ac.ratio != null ? `<circle cx="${Math.min(2, ac.ratio) / 2 * 320}" cy="14" r="7" fill="#fff" stroke="#000" stroke-width="2"/>` : ""}
      <text x="${0.8 / 2 * 320}" y="32" text-anchor="middle">0.8</text><text x="${1.3 / 2 * 320}" y="32" text-anchor="middle">1.3</text><text x="${1.5 / 2 * 320}" y="32" text-anchor="middle">1.5</text></svg>
    ${imp.length ? `<div class="divider"></div><div class="between"><span class="sub">Running &amp; impact, min/week</span>${tr.impact_spike_recent ? `<span class="pill watch">spike</span>` : ""}</div>
      ${barsChart(imp.map((w) => w.minutes), (v, i) => (imp[i].spike ? C.yellow : imp[i].partial ? C.gray : C.green), { h: 70, labels: imp.map((w, i) => [i, i === imp.length - 1 ? "now" : mday(w.start)]).filter(([i]) => i % 2 === 1 || i === imp.length - 1) })}
      ${tr.impact_limit ? `<p class="muted" style="margin:6px 0 0">Keep this week under ${Math.round(tr.impact_limit)} min.</p>` : ""}` : ""}
  </section>`;
}

/* ------------------------------------------------------------------ log */
async function loadLog() {
  try { S.log = await api("/api/log?date=" + S.logDate); render(); }
  catch (e) { toast(e.message); }
}
function priorityEditor(L) {
  const p = L.priorities || { items: [] }, items = p.items || [];
  if (!items.length || S.editPriorities) {
    const v = (k) => esc((items[k] || {}).text || "");
    return `<section class="card"><h2>Top 3 priorities <small>${esc(mday(p.date || L.date))}</small></h2>
      <p class="muted" style="margin:0 0 10px">What would make today a win? The coach will check in on these.</p>
      ${[0, 1, 2].map((k) => `<input type="text" class="prio-in" data-k="${k}" placeholder="${k + 1}." value="${v(k)}" style="margin-bottom:8px" autocomplete="off">`).join("")}
      <div class="row" style="justify-content:flex-end;gap:8px">${S.editPriorities ? `<button class="btn ghost" id="prio-cancel">Cancel</button>` : ""}<button class="btn" id="prio-save">Save</button></div></section>`;
  }
  return `<section class="card"><h2>Top 3 priorities <small><button class="btn ghost" id="prio-edit" style="padding:4px 10px;font-size:12px">Edit</button></small></h2>
    ${items.map((i, k) => `<div class="habit" style="flex-wrap:wrap"><span class="name" style="flex:1 1 100%;margin-bottom:6px">${k + 1}. ${esc(i.text)}</span><div class="choice">
      ${[["done", "Done", "pos"], ["partial", "Some", "mid"], ["missed", "Not today", "neg"]].map(([st, lab, cls]) => `<button class="${cls} ${i.status === st ? "on" : ""}" data-prio="${k + 1}" data-st="${st}">${lab}</button>`).join("")}</div></div>`).join("")}
    <textarea id="prio-reflect" rows="2" placeholder="What helped, or what got in the way?" style="margin-top:10px">${esc(p.reflection || "")}</textarea>
    <div class="row" style="justify-content:flex-end;margin-top:8px"><button class="btn ghost" id="prio-reflect-save">Save reflection</button></div></section>`;
}
function renderLog() {
  const L = S.log;
  if (!L) { loadLog(); return `<div class="loading"><div class="spinner"></div></div>`; }
  const j = L.journal || { habits: {} }, groups = [[true, "TO DO", C.green, "Done", "Missed"], [false, "TO AVOID", C.red, "Avoided", "Slipped"], [null, "TRACKING", C.strain, "Yes", "No"]];
  const habitRow = (h, posLabel, negLabel) => {
    const v = j.habits[h.key];                       // true = did it
    const posVal = h.good === false ? false : true, negVal = !posVal;
    return `<div class="habit"><span class="name">${esc(h.label)}</span><div class="choice">
      <button class="pos ${v === posVal ? "on" : ""}" data-habit="${esc(h.key)}" data-val="${posVal}">${posLabel}</button>
      <button class="neg ${v === negVal ? "on" : ""}" data-habit="${esc(h.key)}" data-val="${negVal}">${negLabel}</button></div></div>`;
  };
  return `
  <div class="between" style="margin:0 2px 12px"><div class="seg">${["today", "yesterday"].map((d) => `<button data-day="${d}" class="${S.logDate === d ? "on" : ""}">${d[0].toUpperCase() + d.slice(1)}</button>`).join("")}</div><span class="muted">${esc(mday(L.date))}</span></div>
  ${priorityEditor(L)}
  <section class="card"><h2>Habits</h2>
    ${groups.map(([g, title, color, pos, neg]) => { const hs = L.habits.filter((h) => h.good === g); return hs.length ? `<div class="group-title" style="color:${color}">${title}</div>${hs.map((h) => habitRow(h, pos, neg)).join("")}` : ""; }).join("")}
    <div class="divider"></div>
    <textarea id="note" rows="3" placeholder="Note about the day (optional)">${esc(j.note || "")}</textarea>
    <div class="row" style="justify-content:flex-end;margin-top:8px"><button class="btn" id="save-note">Save note</button></div>
  </section>
  <section class="card"><h2>Lift log</h2>
    <input type="text" id="lift-text" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="bench 3x8@60 row 4x10@50">
    <div class="err" id="lift-err"></div>
    <div class="row" style="justify-content:space-between"><span class="muted">sets×reps@kg · 25lb for pounds · no @ = bodyweight</span><button class="btn" id="add-lift">Add</button></div>
    ${(L.recent_exercises || []).length ? `<div class="chips">${L.recent_exercises.map((e) => `<button data-ex="${esc(e)}">${esc(e)}</button>`).join("")}</div>` : ""}
    <div class="divider"></div>
    ${L.lifts.length ? L.lifts.map((e) => `<div class="entry"><span>${esc(e.text)}</span></div>`).join("") + `<div class="row" style="justify-content:space-between;margin-top:10px"><span class="muted">${Object.entries(L.sets).map(([k, v]) => `${MUSCLE[k] || k} ${v}`).join(" · ")}</span><button class="btn ghost" id="undo-lift">Undo last</button></div>` : `<p class="empty">Nothing logged ${S.logDate === "today" ? "today" : "yesterday"}.</p>`}
  </section>`;
}
view.addEventListener("click", async (ev) => {
  const t = ev.target.closest("button");
  if (!t) return;
  if (t.id === "retry") { load(true); return; }
  if (t.dataset.day) { S.logDate = t.dataset.day; S.log = null; S.editPriorities = false; render(); return; }
  if (t.dataset.habit) {
    const cur = (S.log.journal.habits || {})[t.dataset.habit], val = t.dataset.val === "true";
    const next = cur === val ? null : val;                     // tap the active one again to clear it
    try { S.log = await api("/api/journal", { date: S.logDate, habits: { [t.dataset.habit]: next } }); S.loadedAt = 0; render(); }
    catch (e) { toast(e.message); }
    return;
  }
  if (t.id === "prio-edit") { S.editPriorities = true; render(); return; }
  if (t.id === "prio-cancel") { S.editPriorities = false; render(); return; }
  if (t.id === "prio-save") {
    const items = [...document.querySelectorAll(".prio-in")].map((i) => i.value.trim()).filter(Boolean);
    if (!items.length) { toast("Add at least one priority"); return; }
    try { S.log = await api("/api/priorities", { date: S.logDate, items }); S.editPriorities = false; S.loadedAt = 0; toast("Priorities set"); render(); }
    catch (e) { toast(e.message); }
    return;
  }
  if (t.dataset.prio) {
    const cur = ((S.log.priorities.items || [])[t.dataset.prio - 1] || {}).status;
    const status = cur === t.dataset.st ? null : t.dataset.st;
    try { S.log = await api("/api/priorities", { date: S.logDate, index: Number(t.dataset.prio), status }); S.loadedAt = 0; render(); }
    catch (e) { toast(e.message); }
    return;
  }
  if (t.id === "prio-reflect-save") {
    try { S.log = await api("/api/priorities", { date: S.logDate, reflection: $("#prio-reflect").value }); toast("Saved"); render(); }
    catch (e) { toast(e.message); }
    return;
  }
  if (t.dataset.ex) { const i = $("#lift-text"); i.value = (i.value ? i.value.trimEnd() + " " : "") + t.dataset.ex + " "; i.focus(); return; }
  if (t.id === "save-note") {
    try { S.log = await api("/api/journal", { date: S.logDate, habits: {}, note: $("#note").value }); toast("Note saved"); render(); }
    catch (e) { toast(e.message); }
  }
  if (t.id === "add-lift") {
    const text = $("#lift-text").value.trim();
    if (!text) return;
    t.disabled = true;
    try { S.log = await api("/api/lift", { date: S.logDate, text }); toast("Logged " + S.log.added.join(", ")); S.loadedAt = 0; render(); }
    catch (e) { $("#lift-err").textContent = e.message; t.disabled = false; }
  }
  if (t.id === "undo-lift") {
    try { S.log = await api("/api/lift/undo", { date: S.logDate }); toast("Removed " + S.log.removed); S.loadedAt = 0; render(); }
    catch (e) { toast(e.message); }
  }
});
view.addEventListener("keydown", (ev) => { if (ev.key === "Enter" && ev.target.id === "lift-text") { ev.preventDefault(); $("#add-lift").click(); } });

/* ------------------------------------------------------------------ trends */
function renderTrends(m) {
  const s = m.series || {}, b = m.baselines || {}, days = m.days || [], n = Math.min(28, days.length);
  const d = days.slice(-n), last = (k) => (s[k] || []).slice(-n), labels = dayLabels(d);
  const wk = (m.week_review || {}).rows || [], ins = m.insights || {}, body = m.body || {};
  const fmtW = (r, v) => (v == null ? "—" : r.metric === "Asleep h" ? hm(v * 60) : r.metric === "Steps" ? num(v) : num(v, Math.abs(v) < 20 ? 1 : 0));
  const effects = (ins.effects || []).slice(0, 10);
  const wts = (body.weights || []).slice(-30);
  return `
  <section class="card"><h2>This week vs last</h2>
    ${wk.filter((r) => r.avg != null).map((r) => { const dlt = r.prev == null ? null : r.avg - r.prev; const good = dlt == null || r.higher_better == null || Math.abs(dlt) < 1e-9 ? "" : (dlt > 0) === r.higher_better ? "good" : "watch"; return `<div class="between" style="padding:7px 0;border-top:1px solid var(--line)"><span class="sub">${esc(r.metric)}</span><span><b class="num">${fmtW(r, r.avg)}</b> <span class="muted ${good}" style="display:inline-block;min-width:64px;text-align:right">${dlt == null ? "" : (dlt > 0 ? "▲ " : dlt < 0 ? "▼ " : "") + fmtW(r, Math.abs(dlt))}</span></span></div>`; }).join("")}
  </section>
  <section class="card"><h2>Recovery <small>28 days</small></h2>${barsChart(last("recovery"), (v) => recColor(v), { h: 90, labels, max: 100 })}</section>
  <section class="card"><h2>HRV <small>ms · band = your normal range</small></h2>${lineChart(last("hrv"), C.green, { base: b.hrv, labels })}</section>
  <section class="card"><h2>Resting heart rate <small>bpm</small></h2>${lineChart(last("rhr"), C.red, { base: b.rhr, labels })}</section>
  <section class="card"><h2>Strain <small>28 days</small></h2>${barsChart(last("strain"), () => C.strain, { h: 80, labels, max: 21 })}</section>
  <section class="card"><h2>Sleep <small>hours asleep</small></h2>${barsChart(last("asleep").map((v) => (v == null ? null : v / 60)), (v) => (v >= 7 ? C.sleep : C.deep), { h: 80, labels, max: 12 })}</section>
  <section class="card"><h2>What drives your recovery</h2>
    ${effects.length ? effects.map((e) => { const clear = e.strength !== "unclear", f = Math.min(1, Math.abs(e.diff) / 25), col = clear ? (e.diff > 0 ? C.green : C.red) : C.line; return `<div style="padding:8px 0;border-top:1px solid var(--line)"><div class="between"><span class="${clear ? "" : "muted"}" style="font-size:14px">${esc(e.label)}</span><b class="num ${clear ? (e.diff > 0 ? "good" : "flag") : "muted"}">${e.diff > 0 ? "+" : ""}${num(e.diff)}</b></div><div style="display:flex;height:6px;margin-top:6px"><div style="flex:1;display:flex;justify-content:flex-end">${e.diff < 0 ? `<i style="width:${f * 100}%;background:${col};border-radius:3px 0 0 3px"></i>` : ""}</div><div style="width:2px;background:#3a3a3f"></div><div style="flex:1">${e.diff > 0 ? `<i style="display:block;height:100%;width:${f * 100}%;background:${col};border-radius:0 3px 3px 0"></i>` : ""}</div></div><div class="muted">${esc(e.strength)} · ${e.n_yes} vs ${e.n_no} mornings</div></div>`; }).join("")
      + `<p class="muted" style="margin:10px 0 0">Average next-morning Recovery with vs without. Correlation in your own data, not proof.</p>`
      : `<p class="empty">Needs about ${ins.min_days || 21} mornings of data.</p>`}
  </section>
  ${wts.length >= 2 ? `<section class="card"><h2>Weight <small>${esc((body.trend || {}).status || "")}</small></h2><div class="head"><span class="big num">${num(body.latest_kg, 1)}<small>kg</small></span><span class="sub">${(body.trend || {}).kg_per_week != null ? ((body.trend.kg_per_week > 0 ? "+" : "") + num(body.trend.kg_per_week, 2) + " kg/wk") : ""}</span></div>${lineChart(wts.map((w) => w[1]), "#c27ce6", { labels: [[0, mday(wts[0][0])], [wts.length - 1, mday(wts[wts.length - 1][0])]] })}${body.days_since_weigh_in > 7 ? `<p class="muted" style="margin:6px 0 0">Last weigh-in ${body.days_since_weigh_in} days ago.</p>` : ""}</section>` : ""}
  <p class="foot">Scores are estimates on WHOOP-like scales (${esc(m.formula_version || "")}), not medical advice · <a href="/terminal">terminal view</a></p>`;
}

/* ------------------------------------------------------------------ router */
function render() {
  header();
  const m = S.m;
  if (!m) return;
  if (!m.has_data) { view.innerHTML = `<section class="card"><h2>No data yet</h2><p class="sub">${esc(m.empty_reason || "")}</p><p class="muted">Connect and import with the fitbit-local tools in Claude Code, then pull to refresh.</p></section>`; return; }
  const scrollKeep = S.lastTab === S.tab ? window.scrollY : 0;
  view.innerHTML = { today: renderToday, sleep: renderSleep, train: renderTrain, log: renderLog, trends: renderTrends }[S.tab](m);
  if (S.lastTab !== S.tab) window.scrollTo(0, 0); else window.scrollTo(0, scrollKeep);
  S.lastTab = S.tab;
}
function route() {
  const t = (location.hash || "#today").slice(1);
  S.tab = ["today", "sleep", "train", "log", "trends"].includes(t) ? t : "today";
  if (S.tab === "log") S.log = null;
  render();
  load();                         // refetches only if a log was written (loadedAt reset) or the data is 30 s old
}
window.addEventListener("hashchange", route);
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") load(); });
window.addEventListener("pageshow", () => load());
route();
load(true);
setInterval(() => { if (document.visibilityState === "visible") load(); }, 5 * 60 * 1000);
