/* OmaCPAP dashboard. Vanilla JS + uPlot; all data comes from the local server. */
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];

  const S = {
    state: null, nights: [], summary: null, range: localStorage.getItem("omacpap.range") || "90",
    sort: { key: "date", dir: -1 }, selected: null, charts: [], themeSig: null, polling: null,
  };

  // ------------------------------------------------------------------ utils
  async function api(path, body) {
    const opt = body === undefined ? {} : {
      method: "POST", headers: { "Content-Type": "application/json", "X-OmaCPAP": "1" }, body: JSON.stringify(body),
    };
    const r = await fetch(path, opt);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    return data;
  }
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const num = (v, d = 1) => (v === null || v === undefined || Number.isNaN(v) ? "—" : Number(v).toFixed(d));
  const hm = (min) => {
    if (!min) return "0h 00m";
    const h = Math.floor(min / 60), m = Math.round(min % 60);
    return `${h}h ${String(m).padStart(2, "0")}m`;
  };
  const LOCALE = (() => { try { new Intl.DateTimeFormat(navigator.language); return navigator.language; } catch { return "en-US"; } })();
  const dateObj = (iso) => new Date(iso + "T12:00:00");
  const fmtDate = (iso, opts = { weekday: "short", month: "short", day: "numeric" }) => dateObj(iso).toLocaleDateString(LOCALE, opts);
  const ts = (iso) => dateObj(iso).getTime() / 1000;
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  function hex2rgb(h) { h = h.replace("#", ""); return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16)); }
  function mix(a, b, t) { // t = share of a
    try { const A = hex2rgb(a), B = hex2rgb(b); return `rgb(${A.map((v, i) => Math.round(v * t + B[i] * (1 - t))).join(",")})`; }
    catch { return a; }
  }
  const goalH = () => S.state?.settings?.usage_goal_hours ?? 4;
  const leakRef = () => S.state?.settings?.leak_reference ?? 24;
  const ahiRef = () => S.state?.settings?.ahi_reference ?? 5;

  function setStatus(text, err = false) {
    const el = $("#sync-status");
    el.textContent = text || "";
    el.classList.toggle("err", !!err);
    el.title = text || "";
  }
  function ago(iso) {
    if (!iso) return "never";
    const s = (Date.now() - new Date(iso).getTime()) / 1000;
    if (s < 90) return "just now";
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    return `${Math.round(s / 86400)} days ago`;
  }

  // ------------------------------------------------------------------ boot
  async function loadState() {
    S.state = await api("/api/state");
    if (S.themeSig && S.themeSig !== S.state.theme) reloadTheme();
    S.themeSig = S.state.theme;
    return S.state;
  }

  async function boot() {
    await loadState();
    $("#version-line").textContent = `OmaCPAP ${S.state.version}`;
    if (!S.state.configured || S.state.mfa_pending) {
      showConnect(S.state.mfa_pending);
    } else {
      await showApp();
    }
    if (S.state.job.running) watchJob();
    setInterval(async () => { if (!S.polling) { try { await loadState(); renderHeaderStatus(); } catch {} } }, 15000);
  }

  function showConnect(mfa = false) {
    $("#connect").hidden = false; $("#app").hidden = true;
    $(".ranges").hidden = true; $(".actions").hidden = true;
    $("#login-form").hidden = mfa; $("#mfa-form").hidden = !mfa;
    (mfa ? $("#mfa-form [name=code]") : $("#login-form [name=username]")).focus();
  }

  async function showApp() {
    $("#connect").hidden = true; $("#app").hidden = false;
    $(".ranges").hidden = false; $(".actions").hidden = false;
    await loadData();
  }

  async function loadData() {
    const [n, s] = await Promise.all([api("/api/nights"), api("/api/summary")]);
    S.nights = n.nights; S.summary = s;
    renderAll();
  }

  function renderHeaderStatus() {
    if (!S.state || S.state.job.running) return;
    const j = S.state.job;
    if (j.error) setStatus(j.error, true);
    else setStatus(`Synced ${ago(S.state.last_sync)}`);
  }

  // ------------------------------------------------------------------ render
  function rangeNights() {
    if (S.range === "all") return S.nights;
    return S.nights.slice(-Number(S.range));
  }

  function renderAll() {
    $$(".ranges button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.range === S.range)));
    $("#btn-report").href = `/report?days=${S.range === "all" ? S.nights.length || 90 : S.range}`;
    renderHeaderStatus();
    if (!S.nights.length) {
      $("#last-night").innerHTML = S.state.job.running
        ? "Downloading your myAir history…"
        : `No nights yet. <button id="first-sync" class="primary">Sync myAir now</button>`;
      $("#first-sync")?.addEventListener("click", () => startSync(false));
      return;
    }
    renderLastNight(); renderDevice(); renderStrip(); renderFacts(); renderCharts(); renderTable();
  }

  function renderLastNight() {
    const ln = S.summary.last_night;
    const el = $("#last-night");
    if (!ln) { el.textContent = "No recorded nights in myAir yet."; return; }
    const w30 = S.summary.windows["30d"] || {};
    const flags = [];
    if (ln.leak_lpm != null && ln.leak_lpm >= leakRef()) flags.push(`<span class="flag">leak was high</span>`);
    if (ln.ahi != null && ln.ahi >= ahiRef()) flags.push(`<span class="flag">AHI above ${ahiRef()}</span>`);
    const who = S.state.first_name ? `${esc(S.state.first_name)}, ` : "";
    el.innerHTML =
      `${who}${fmtDate(ln.date, { weekday: "long", month: "long", day: "numeric" })}: <b>${hm(ln.usage_min)}</b> on the mask, ` +
      `AHI <b>${num(ln.ahi)}</b>, leak <b>${num(ln.leak_lpm)}</b> L/min, ` +
      `${ln.mask_pairs ?? "—"} mask on/off, myAir score <b>${ln.sleep_score ?? "—"}</b>.` +
      (flags.length ? ` ${flags.join(", ")}.` : "") +
      ` <span class="dim">30-night average ${num(w30.usage_avg_h)} h, streak ${S.summary.streak_current} night${S.summary.streak_current === 1 ? "" : "s"} at ${goalH()}+ h.</span>`;
  }

  const waveOn = () => localStorage.getItem("omacpap.wave") !== "off";

  function renderDevice() {
    const v = S.state?.device_image;
    const img = $("#device-photo"), art = $("#device-art");
    if (v) {
      if (img.dataset.v !== v) { img.src = `/device-image?v=${v}`; img.dataset.v = v; }
      img.hidden = false; art.style.display = "none";
    } else {
      img.hidden = true; img.removeAttribute("src"); delete img.dataset.v; art.style.display = "";
    }
    const ln = S.summary?.last_night;
    $("#device-hours").textContent = ln ? `${((ln.usage_min || 0) / 60).toFixed(1)}h` : "--";
  }

  function level(min) {
    const h = (min || 0) / 60;
    if (h <= 0) return 0;
    if (h < 2) return 1;
    if (h < 4) return 2;
    if (h < 6.5) return 3;
    return 4;
  }

  function renderStrip() {
    const strip = $("#strip");
    const span = Math.max(371, S.range === "all" ? S.nights.length : Number(S.range));
    const days = S.nights.slice(-span);
    const first = dateObj(days[0].date);
    const pad = (first.getDay() + 6) % 7; // Monday-first rows
    const goal = goalH() * 60;
    const frag = document.createDocumentFragment();
    let lastMonth = null;
    days.forEach((n, i) => {
      const slot = i + pad, col = Math.floor(slot / 7) + 1, row = (slot % 7) + 2;
      const month = n.date.slice(0, 7);
      if (month !== lastMonth) { // label each month over the column where it begins
        const m = document.createElement("span");
        m.className = "month"; m.style.gridColumn = `${row === 2 ? col : col + 1} / span 4`;
        const withYear = lastMonth === null || month.endsWith("-01");
        m.textContent = dateObj(n.date).toLocaleDateString(LOCALE, withYear ? { month: "short", year: "2-digit" } : { month: "short" });
        if (i === 0 && Number(n.date.slice(8)) > 10) m.textContent = ""; // partial first month: no room for a label
        frag.append(m);
        lastMonth = month;
      }
      const b = document.createElement("button");
      b.className = `cell l${level(n.usage_min)}${(n.usage_min || 0) >= goal ? " goal" : ""}${S.selected === n.date ? " sel" : ""}`;
      b.style.gridColumn = col; b.style.gridRow = row;
      b.style.setProperty("--col", col); b.style.setProperty("--row", row - 2);
      b.dataset.date = n.date;
      b.title = `${fmtDate(n.date)} — ${n.missing ? "no data" : hm(n.usage_min) + (n.ahi != null ? `, AHI ${num(n.ahi)}` : "")}`;
      b.setAttribute("aria-label", b.title);
      frag.append(b);
    });
    strip.replaceChildren(frag);
    strip.classList.toggle("wave", waveOn());
    $("#legend-goal").textContent = `${goalH()}+ h`;
    const wrap = strip.parentElement;
    requestAnimationFrame(() => { wrap.scrollLeft = wrap.scrollWidth; });
  }

  function renderFacts() {
    const key = { "30": "30d", "90": "90d", "365": "365d", all: "all" }[S.range];
    const w = S.summary.windows[key] || S.summary.windows.all;
    $("#facts-title").textContent = S.range === "all" ? `All ${w.days} nights` : `Last ${w.days} nights`;
    const cls = (v, good, warn) => (v == null ? "" : v >= good ? "good" : v >= warn ? "warn" : "bad");
    const best = S.summary.best_30;
    const rows = [
      ["Nights at " + goalH() + "+ h", `${w.nights_goal}/${w.days}`, ""],
      ["Compliance", `${num(w.compliance_pct, 0)}%`, cls(w.compliance_pct, 70, 50)],
      ["Avg use, nights used", `${num(w.usage_avg_h)} h`, ""],
      ["Avg use, all nights", `${num(w.usage_avg_all_days_h)} h`, ""],
      ["AHI, usage-weighted", num(w.ahi_weighted, 2), w.ahi_weighted == null ? "" : w.ahi_weighted < ahiRef() ? "good" : "warn"],
      ["AHI, worst night", num(w.ahi_max), ""],
      ["Leak, median", `${num(w.leak_median)} <small>L/min</small>`, ""],
      [`Nights leak ≥ ${leakRef()}`, String(w.nights_leak_over_ref), w.nights_leak_over_ref ? "warn" : ""],
      ["Mask on/off, avg", num(w.mask_pairs_avg), ""],
      ["myAir score, avg", num(w.score_avg, 0), ""],
      ["Current streak", `${S.summary.streak_current} <small>night${S.summary.streak_current === 1 ? "" : "s"}</small>`, ""],
      ["Longest streak", `${S.summary.streak_longest} <small>nights</small>`, ""],
      ["Total time on CPAP", `${Math.round(w.usage_total_h).toLocaleString(LOCALE)} <small>h</small>`, ""],
    ];
    if (best) rows.push(["Best 30-night run", `${num(best.pct, 0)}%`, ""]);
    $("#facts").innerHTML = rows.map(([k, v, c]) => `<dt>${k}</dt><dd class="${c}">${v}</dd>`).join("");
    $("#insights").innerHTML = (S.summary.insights || []).map((i) => `<li data-kind="${esc(i.kind)}">${esc(i.text)}</li>`).join("")
      || `<li>Not enough nights yet.</li>`;
  }

  // ------------------------------------------------------------------ charts
  function palette() {
    const bg = css("--bg"), fg = css("--fg");
    return {
      bg, fg, accent: css("--accent"), good: css("--good"), warn: css("--warn"), bad: css("--bad"),
      info: css("--info"), violet: css("--violet"), grid: mix(fg, bg, 0.1), axis: mix(fg, bg, 0.55),
      dim: mix(css("--accent"), bg, 0.4),
    };
  }

  function refLine(value, color, label) {
    return (u) => {
      if (value == null) return;
      const y = u.valToPos(value, "y", true);
      if (y < u.bbox.top || y > u.bbox.top + u.bbox.height) return;
      const c = u.ctx;
      c.save(); c.strokeStyle = color; c.setLineDash([5, 4]); c.lineWidth = devicePixelRatio;
      c.beginPath(); c.moveTo(u.bbox.left, y); c.lineTo(u.bbox.left + u.bbox.width, y); c.stroke();
      c.setLineDash([]); c.fillStyle = color; c.font = `${10 * devicePixelRatio}px monospace`;
      c.textAlign = "right"; c.fillText(label, u.bbox.left + u.bbox.width - 4, y - 4 * devicePixelRatio);
      c.restore();
    };
  }

  function tooltipPlugin(fmt) {
    let tip;
    return {
      hooks: {
        init: (u) => { tip = document.createElement("div"); tip.className = "tip"; tip.hidden = true; u.over.append(tip); },
        setCursor: (u) => {
          const i = u.cursor.idx;
          if (i == null) { tip.hidden = true; return; }
          const html = fmt(i);
          if (!html) { tip.hidden = true; return; }
          tip.innerHTML = html; tip.hidden = false;
          tip.style.left = `${u.cursor.left}px`; tip.style.top = `${u.cursor.top}px`;
        },
      },
    };
  }

  function baseOpts(el, p, series, extra = {}) {
    const width = el.clientWidth || 500;
    const axis = { stroke: p.axis, grid: { stroke: p.grid, width: 1 }, ticks: { stroke: p.grid, width: 1 }, font: "11px monospace" };
    return {
      width, height: 210, padding: [8, 8, 0, 0], legend: { show: false },
      cursor: { points: { show: false }, drag: { x: true, y: false } },
      scales: { x: { time: true }, y: extra.yRange ? { range: extra.yRange } : {} },
      axes: [{ ...axis, space: 70 }, { ...axis, size: 42, ...(extra.yAxis || {}) }],
      series: [{}, ...series],
      hooks: extra.hooks || {},
      plugins: extra.plugins || [],
    };
  }

  function toWeeks(days) {
    const out = [];
    let cur = null;
    const avg = (arr) => (arr.length ? arr.reduce((a, b) => a + b, 0) / arr.length : null);
    const flush = () => {
      if (!cur) return;
      const used = cur.rows.filter((n) => (n.usage_min || 0) > 0);
      const pick = (k) => used.map((n) => n[k]).filter((v) => v != null);
      const wAhiDen = used.filter((n) => n.ahi != null).reduce((a, n) => a + n.usage_min, 0);
      out.push({
        date: cur.date, open: (used.at(-1) || cur.rows.at(-1)).date,
        usage_min: avg(cur.rows.map((n) => n.usage_min || 0)),
        ahi: wAhiDen ? used.filter((n) => n.ahi != null).reduce((a, n) => a + n.ahi * n.usage_min, 0) / wAhiDen : null,
        leak_lpm: avg(pick("leak_lpm")), mask_pairs: avg(pick("mask_pairs")),
        sleep_score: avg(pick("sleep_score")), usage_score: avg(pick("usage_score")),
        leak_score: avg(pick("leak_score")), ahi_score: avg(pick("ahi_score")), mask_score: avg(pick("mask_score")),
        missing: !used.length,
      });
    };
    for (const n of days) {
      const d = dateObj(n.date), monday = new Date(d); monday.setDate(d.getDate() - ((d.getDay() + 6) % 7));
      const key = monday.toISOString().slice(0, 10);
      if (!cur || cur.date !== key) { flush(); cur = { date: key, rows: [] }; }
      cur.rows.push(n);
    }
    flush();
    return out;
  }

  function renderCharts() {
    S.charts.forEach((c) => c.destroy()); S.charts = [];
    const daily = rangeNights();
    if (!daily.length) return;
    // Long ranges read better as weekly averages than as 1,000 slivers.
    const weekly = daily.length > 200;
    const nights = weekly ? toWeeks(daily) : daily;
    $$(".charts figcaption .agg").forEach((e) => e.remove());
    if (weekly) $$(".charts figcaption").forEach((fc) => fc.insertAdjacentHTML("beforeend", ` <span class="ref agg">· weekly averages</span>`));
    const p = palette();
    const x = nights.map((n) => ts(n.date));
    const goal = goalH();
    const bars = uPlot.paths.bars({ size: [0.72, 60], gap: 1 });
    const open = (i) => nights[i] && openNight(nights[i].open || nights[i].date);
    const clickHook = (u) => u.over.addEventListener("click", () => { if (u.cursor.idx != null) open(u.cursor.idx); });
    const dateTip = (i) => `<b>${weekly ? "Week of " : ""}${fmtDate(nights[i].date)}</b>`;

    // usage: two series so goal-met nights read at a glance
    const hrs = nights.map((n) => (n.usage_min || 0) / 60);
    $("#cap-goal").textContent = `(dashed: ${goal} h goal)`;
    const elU = $("#ch-usage");
    S.charts.push(new uPlot(baseOpts(elU, p, [
      { fill: p.accent, stroke: p.accent, paths: bars, points: { show: false } },
      { fill: p.dim, stroke: p.dim, paths: bars, points: { show: false } },
    ], {
      yAxis: { values: (u, v) => v.map((t) => t + "h") }, yRange: (u, lo, hi) => [0, Math.max(hi * 1.1, goal * 1.5)],
      hooks: { draw: [refLine(goal, p.good, `${goal} h`)], ready: [clickHook] },
      plugins: [tooltipPlugin((i) => `${dateTip(i)} ${nights[i].missing ? "no data" : hm(nights[i].usage_min)}`)],
    }), [x, hrs.map((h) => (h >= goal ? h : null)), hrs.map((h) => (h < goal && h > 0 ? h : null))], elU));

    // AHI
    const ahi = nights.map((n) => n.ahi ?? null);
    $("#cap-ahi").textContent = `(dashed: ${ahiRef()})`;
    const elA = $("#ch-ahi");
    S.charts.push(new uPlot(baseOpts(elA, p, [
      { stroke: p.violet, width: 1.5, spanGaps: false, points: { show: nights.length <= 120, size: 4, fill: p.violet } },
    ], {
      yRange: (u, lo, hi) => [0, Math.max(hi * 1.15, ahiRef() * 1.4)],
      hooks: { draw: [refLine(ahiRef(), p.warn, String(ahiRef()))], ready: [clickHook] },
      plugins: [tooltipPlugin((i) => ahi[i] == null ? null : `${dateTip(i)} AHI ${num(ahi[i])}`)],
    }), [x, ahi], elA));

    // leak
    const leak = nights.map((n) => n.leak_lpm ?? null);
    $("#cap-leak").textContent = `(dashed: ${leakRef()} L/min)`;
    const elL = $("#ch-leak");
    S.charts.push(new uPlot(baseOpts(elL, p, [
      { stroke: p.info, width: 1.5, fill: mix(css("--info"), p.bg, 0.15), points: { show: false } },
    ], {
      yRange: (u, lo, hi) => [0, Math.max(hi * 1.15, leakRef() * 1.2)],
      hooks: { draw: [refLine(leakRef(), p.bad, `${leakRef()} L/min`)], ready: [clickHook] },
      plugins: [tooltipPlugin((i) => leak[i] == null ? null : `${dateTip(i)} ${num(leak[i])} L/min`)],
    }), [x, leak], elL));

    // score: overlaid cumulative bars read as a stack (usage 70 · seal 20 · events 5 · on/off 5)
    const u_ = nights.map((n) => n.usage_score ?? null);
    const s_ = nights.map((n, i) => (u_[i] == null ? null : u_[i] + (n.leak_score ?? 0)));
    const e_ = nights.map((n, i) => (s_[i] == null ? null : s_[i] + (n.ahi_score ?? 0)));
    const t_ = nights.map((n, i) => (e_[i] == null ? null : e_[i] + (n.mask_score ?? 0)));
    const elS = $("#ch-score");
    S.charts.push(new uPlot(baseOpts(elS, p, [
      { fill: p.warn, stroke: p.warn, paths: bars, points: { show: false } },
      { fill: p.violet, stroke: p.violet, paths: bars, points: { show: false } },
      { fill: p.info, stroke: p.info, paths: bars, points: { show: false } },
      { fill: p.accent, stroke: p.accent, paths: bars, points: { show: false } },
    ], {
      yRange: [0, 100],
      hooks: { ready: [clickHook] },
      plugins: [tooltipPlugin((i) => {
        const n = nights[i];
        if (n.sleep_score == null) return null;
        const r = (v) => (v == null ? "—" : Math.round(v));
        return `${dateTip(i)} ${r(n.sleep_score)} = use ${r(n.usage_score)} + seal ${r(n.leak_score)} + events ${r(n.ahi_score)} + on/off ${r(n.mask_score)}`;
      })],
    }), [x, t_, e_, s_, u_], elS));
  }

  let resizeT;
  window.addEventListener("resize", () => { clearTimeout(resizeT); resizeT = setTimeout(() => S.nights.length && renderCharts(), 150); });

  // ------------------------------------------------------------------ table
  function renderTable() {
    const rows = rangeNights().filter((n) => !n.missing || n.note);
    const { key, dir } = S.sort;
    rows.sort((a, b) => {
      const va = a[key] ?? -Infinity, vb = b[key] ?? -Infinity;
      return (va > vb ? 1 : va < vb ? -1 : 0) * dir;
    });
    const goal = goalH() * 60;
    $("#nights tbody").innerHTML = rows.map((n) => `
      <tr data-date="${n.date}">
        <td>${fmtDate(n.date, { weekday: "short", year: "numeric", month: "short", day: "numeric" })}</td>
        <td class="${(n.usage_min || 0) < goal ? "low" : ""}">${hm(n.usage_min)}</td>
        <td class="${n.ahi == null ? "none" : n.ahi >= ahiRef() ? "hi" : ""}">${num(n.ahi)}</td>
        <td class="${n.leak_lpm == null ? "none" : n.leak_lpm >= leakRef() ? "hi" : ""}">${num(n.leak_lpm)}</td>
        <td>${n.mask_pairs ?? "—"}</td>
        <td>${n.sleep_score ?? "—"}</td>
        <td class="note">${esc(n.note || "")}</td>
      </tr>`).join("");
    $$("#nights th[data-sort]").forEach((th) =>
      th.setAttribute("aria-sort", th.dataset.sort === key ? (dir > 0 ? "ascending" : "descending") : "none"));
  }

  // ------------------------------------------------------------------ night drawer
  function openNight(date) {
    const idx = S.nights.findIndex((n) => n.date === date);
    if (idx < 0) return;
    const n = S.nights[idx];
    S.selected = date;
    $$(".strip .cell.sel").forEach((c) => c.classList.remove("sel"));
    $(`.strip .cell[data-date="${date}"]`)?.classList.add("sel");
    const w = S.summary.windows["30d"] || {};
    $("#night-title").textContent = fmtDate(date, { weekday: "long", year: "numeric", month: "long", day: "numeric" });
    const cmp = (v, avg, d = 1) => (v == null || avg == null ? "" : `30-night avg ${num(avg, d)}`);
    let html = n.missing ? `<p class="fine">myAir has no record for this night.</p>` : `
      <dl>
        <dt>Time on mask</dt><dd><b>${hm(n.usage_min)}</b></dd><dd class="cmp">${cmp(n.usage_min, w.usage_avg_h != null ? w.usage_avg_h * 60 : null, 0).replace(/[\d.]+$/, (m) => hm(+m))}</dd>
        <dt>AHI</dt><dd><b>${num(n.ahi)}</b></dd><dd class="cmp">${cmp(n.ahi, w.ahi_weighted, 2)}</dd>
        <dt>Mask leak</dt><dd><b>${num(n.leak_lpm)}</b> L/min</dd><dd class="cmp">${cmp(n.leak_lpm, w.leak_median)}</dd>
        <dt>Mask on/off</dt><dd><b>${n.mask_pairs ?? "—"}</b></dd><dd class="cmp">${cmp(n.mask_pairs, w.mask_pairs_avg)}</dd>
        <dt>myAir score</dt><dd><b>${n.sleep_score ?? "—"}</b>/100</dd><dd class="cmp">${cmp(n.sleep_score, w.score_avg, 0)}</dd>
      </dl>`;
    if (n.sleep_score != null) {
      const p = palette();
      const parts = [["Usage", n.usage_score, 70, p.accent], ["Mask seal", n.leak_score, 20, p.info],
                     ["Events", n.ahi_score, 5, p.violet], ["Mask on/off", n.mask_score, 5, p.warn]];
      html += `<div><div class="scorebar">${parts.map(([, v, , c]) => `<span style="width:${v || 0}%;background:${c}"></span>`).join("")}</div>
        <div class="scorekey">${parts.map(([k, v, max, c]) => `<i style="background:${c}"></i><span>${k}</span><span>${v ?? "—"} / ${max}</span>`).join("")}</div></div>`;
    }
    if (n.oai != null || n.press95 != null) {
      html += `<h3>From SD card</h3><dl>
        <dt>Obstructive apnea index</dt><dd>${num(n.oai)}</dd><dd></dd>
        <dt>Central apnea index</dt><dd>${num(n.cai)}</dd><dd></dd>
        <dt>Hypopnea index</dt><dd>${num(n.hi)}</dd><dd></dd>
        <dt>Pressure, median / 95%</dt><dd>${num(n.press50)} / ${num(n.press95)}</dd><dd class="cmp">cmH₂O</dd></dl>`;
    }
    if (n.extra) {
      html += `<h3>Extra myAir fields</h3><dl>${Object.entries(n.extra).map(([k, v]) =>
        `<dt>${esc(k)}</dt><dd>${esc(typeof v === "object" ? JSON.stringify(v) : v)}</dd><dd></dd>`).join("")}</dl>`;
    }
    $("#night-body").innerHTML = html;
    $("#night-note").value = n.note || "";
    openDrawer("#night");
  }

  function stepNight(delta) {
    if (!S.selected) return;
    const list = S.nights;
    let i = list.findIndex((n) => n.date === S.selected) + delta;
    while (i >= 0 && i < list.length && list[i].missing) i += delta;
    if (i >= 0 && i < list.length) openNight(list[i].date);
  }

  function openDrawer(sel) {
    $$(".drawer").forEach((d) => (d.hidden = d !== $(sel)));
    $("#scrim").hidden = false;
    $(sel).querySelector("button, textarea, input")?.focus({ preventScroll: true });
  }
  function closeDrawers() {
    $$(".drawer").forEach((d) => (d.hidden = true));
    $("#scrim").hidden = true;
    S.selected = null;
    $$(".strip .cell.sel").forEach((c) => c.classList.remove("sel"));
  }

  // ------------------------------------------------------------------ settings
  function openSettings() {
    const st = S.state;
    $("#acct-line").textContent = st.configured
      ? `Signed in as ${st.username} (${st.region === "EU" ? "Europe" : "Americas"}). ` +
        `${st.nights.count} nights stored, ${st.nights.first || "—"} to ${st.nights.last || "—"}. ` +
        (st.device ? `Device: ${st.device.name || "ResMed"}.` : "") +
        (st.has_password ? " Password saved in keyring." : " No saved password — nightly sync needs one.")
      : "Not connected.";
    $("#fields-line").textContent = st.extra_fields.length
      ? `Extra fields in every sync: ${st.extra_fields.join(", ")}`
      : "Only the standard myAir fields are being collected.";
    $("#wave-toggle").checked = waveOn();
    const f = $("#prefs");
    for (const [k, v] of Object.entries(st.settings)) f.elements[k].value = v;
    $("#sync-log").innerHTML = (st.syncs || []).map((s) =>
      `<li class="${s.status === "error" ? "error" : ""}">${new Date(s.started_at).toLocaleString(LOCALE)} — ${esc(s.status)}${s.message ? ": " + esc(s.message) : ""}</li>`).join("")
      || "<li>No syncs yet.</li>";
    openDrawer("#settings");
  }

  // ------------------------------------------------------------------ sync jobs
  async function startSync(full) {
    try {
      await api("/api/sync", { full });
      watchJob();
    } catch (e) { setStatus(e.message, true); if (/Connect again|isn't connected/.test(e.message)) showConnect(); }
  }

  function watchJob() {
    if (S.polling) return;
    $("#btn-sync").disabled = true;
    S.polling = setInterval(async () => {
      try {
        await loadState();
        const j = S.state.job;
        if (j.running) {
          setStatus(j.progress.at(-1) || "Working…");
          return;
        }
        clearInterval(S.polling); S.polling = null; $("#btn-sync").disabled = false;
        if (j.error) setStatus(j.error, true);
        else setStatus(j.result?.message || "Done");
        await loadData();
      } catch (e) { setStatus(e.message, true); }
    }, 900);
  }

  function reloadTheme() {
    const link = $("#theme-css");
    const next = link.cloneNode();
    next.href = `/theme.css?v=${Date.now()}`;
    next.onload = () => { link.remove(); if (S.nights.length) { renderCharts(); if (S.selected) openNight(S.selected); } };
    link.after(next);
  }

  // ------------------------------------------------------------------ events
  $("#login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target, btn = f.querySelector("button");
    $("#connect-error").textContent = ""; btn.disabled = true; btn.textContent = "Signing in…";
    try {
      const r = await api("/api/login", {
        username: f.username.value, password: f.password.value, region: f.region.value, remember: f.remember.checked,
      });
      f.password.value = "";
      if (r.status === "MFA_REQUIRED") { $("#mfa-msg").textContent = r.message; showConnect(true); }
      else { await loadState(); await showApp(); watchJob(); }
    } catch (err) { $("#connect-error").textContent = err.message; }
    finally { btn.disabled = false; btn.textContent = "Sign in and download history"; }
  });

  $("#mfa-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = e.target.querySelector("button");
    btn.disabled = true; $("#connect-error").textContent = "";
    try {
      await api("/api/mfa", { code: e.target.code.value });
      e.target.code.value = "";
      await loadState(); await showApp(); watchJob();
    } catch (err) { $("#connect-error").textContent = err.message; }
    finally { btn.disabled = false; }
  });

  $$(".ranges button").forEach((b) => b.addEventListener("click", () => setRange(b.dataset.range)));
  function setRange(r) { S.range = r; localStorage.setItem("omacpap.range", r); if (S.nights.length) renderAll(); }

  $("#btn-sync").addEventListener("click", () => startSync(false));
  $("#btn-settings").addEventListener("click", openSettings);
  $("#btn-full").addEventListener("click", () => { closeDrawers(); startSync(true); });
  $("#btn-probe").addEventListener("click", async () => {
    try { await api("/api/probe", {}); closeDrawers(); watchJob(); } catch (e) { setStatus(e.message, true); }
  });
  $("#btn-logout").addEventListener("click", async () => {
    if (!confirm("Sign out of myAir? Your downloaded nights stay on this computer.")) return;
    await api("/api/logout", {}); closeDrawers(); await loadState(); showConnect();
  });
  $("#wave-toggle").addEventListener("change", (e) => {
    localStorage.setItem("omacpap.wave", e.target.checked ? "on" : "off");
    $("#strip").classList.toggle("wave", e.target.checked);
  });
  $("#device-file").addEventListener("change", (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = async () => {
      try {
        await api("/api/device-image", { data: reader.result });
        await loadState(); renderDevice();
      } catch (err) { alert(err.message); }
      e.target.value = "";
    };
    reader.readAsDataURL(file);
  });
  $("#btn-device-reset").addEventListener("click", async () => {
    await api("/api/device-image", { clear: true });
    await loadState(); renderDevice();
  });
  $("#btn-sd").addEventListener("click", async () => {
    try { await api("/api/import-sd", { path: $("#sd-path").value || null }); closeDrawers(); watchJob(); }
    catch (e) { alert(e.message); }
  });
  $("#prefs").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target, body = {};
    for (const el of f.elements) if (el.name) body[el.name] = Number(el.value);
    await api("/api/settings", body);
    await loadState(); await loadData(); closeDrawers();
  });

  $("#strip").addEventListener("click", (e) => { const d = e.target.closest(".cell[data-date]")?.dataset.date; if (d) openNight(d); });
  $("#nights tbody").addEventListener("click", (e) => { const d = e.target.closest("tr")?.dataset.date; if (d) openNight(d); });
  $$("#nights th[data-sort]").forEach((th) => th.addEventListener("click", () => {
    const k = th.dataset.sort;
    S.sort = { key: k, dir: S.sort.key === k ? -S.sort.dir : -1 };
    renderTable();
  }));
  $("#night-save").addEventListener("click", async () => {
    const text = $("#night-note").value;
    await api("/api/note", { date: S.selected, text });
    const n = S.nights.find((x) => x.date === S.selected); if (n) n.note = text.trim() || null;
    renderTable();
    $("#night-save").textContent = "Saved";
    setTimeout(() => ($("#night-save").textContent = "Save note"), 1200);
  });
  $$("[data-close]").forEach((b) => b.addEventListener("click", closeDrawers));
  $("#scrim").addEventListener("click", closeDrawers);

  document.addEventListener("keydown", (e) => {
    if (e.target.matches("input, textarea, select")) { if (e.key === "Escape") e.target.blur(); return; }
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const map = { 1: "30", 2: "90", 3: "365", 4: "all" };
    if (map[e.key]) setRange(map[e.key]);
    else if (e.key === "s") startSync(false);
    else if (e.key === "r") window.open($("#btn-report").href, "_blank", "noopener");
    else if (e.key === ",") openSettings();
    else if (e.key === "Escape") closeDrawers();
    else if (e.key === "j") stepNight(-1);
    else if (e.key === "k") stepNight(1);
    else return;
    e.preventDefault();
  });

  boot().catch((e) => { document.body.insertAdjacentHTML("beforeend", `<p class="empty error">${esc(e.message)}</p>`); });
})();
