// Ostinato's browser UI: tracks (step roll / staff, generate, undo, generative mode), settings forms generated from
// the defaults files, and the activity log. Talks only to the device server's /api/ui/* (same session as Claude's
// tools). No build step, no libraries.
"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const h = (tag, attrs = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat()) if (c != null) e.append(c.nodeType ? c : document.createTextNode(String(c)));
  return e;
};
const SVG = "http://www.w3.org/2000/svg";
const s = (tag, attrs = {}, ...kids) => {
  const e = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) e.setAttribute(k, v);
  for (const c of kids) if (c != null) e.append(c);
  return e;
};

// ---------------------------------------------------------------- the API
async function api(path, method = "GET", body) {
  const opt = { method, headers: {} };
  if (method !== "GET") {
    opt.headers["X-Ostinato"] = "1";
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body || {});
  }
  const r = await fetch(path, opt);
  if (r.status === 401) { location.href = "/"; throw new Error("not logged in"); }
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error((data && data.error) || data || r.statusText);
  return data;
}

function toast(msg, err = false) {
  const t = h("div", { class: "toast" + (err ? " err" : ""), text: msg });
  $("#toasts").append(t);
  setTimeout(() => t.remove(), err ? 7000 : 4000);
}

async function act(path, body, label) {
  try {
    const r = await api(path, "POST", body);
    if (r.text) toast(r.text);
    for (const w of r.warnings || []) toast(w);
    poll(true);
    return r;
  } catch (e) {
    toast(`${label || "Failed"}: ${e.message}`, true);
  }
}

// ---------------------------------------------------------------- state
const S = {
  state: null, logVersion: -1, tracksKey: "", views: {}, data: {}, lastLog: 0, setupsShown: false,
  algoChoice: {}, controlChoice: {}, actionsKeys: {}, collapsed: {},
};
try { S.collapsed = JSON.parse(localStorage.getItem("ostinato.collapsed") || "{}") || {}; } catch (e) { S.collapsed = {}; }

function can(cap) { return S.state && S.state.capabilities.includes(cap); }

async function poll(force = false) {
  let st;
  try {
    st = await api("/api/ui/state");
  } catch (e) {
    $("#dot").className = "dot";
    $("#device-text").textContent = "the device server does not answer (" + e.message + ")";
    return;
  }
  S.state = st;
  renderHeader(st);
  const key = JSON.stringify(st.tracks.map(t => [t.track, t.kind, t.instrument, t.key]));
  const changed = st.log_version !== S.logVersion;
  if (key !== S.tracksKey) { S.tracksKey = key; buildTracks(st); }
  else updateTrackChrome(st);
  if (changed || force) {
    S.logVersion = st.log_version;
    await Promise.all(st.tracks.map(t => loadTrack(t.track)));
    loadLog();
  }
}

function renderHeader(st) {
  const s0 = st.status || {};
  const dot = $("#dot");
  dot.className = "dot" + (s0.connected ? " on" : st.backend ? " wait" : "");
  let text;
  if (s0.connected) text = `${st.backend} · ${s0.firmware} · ${s0.port}`;
  else if (st.backend) text = `${st.backend} · not connected (${s0.sync || "released"})`;
  else text = `looking for a device… ${s0.scan ? "(" + s0.scan + ")" : ""}`;
  $("#device-text").textContent = text;
  renderTempo(st);
  const play = $("#btn-play");
  play.hidden = st.playing == null;
  play.textContent = st.playing ? "■ Stop" : "▶ Play";
  play.setAttribute("aria-pressed", !!st.playing);
  play.classList.toggle("on", !!st.playing);
  $("#btn-release").hidden = !s0.connected;
  $("#btn-connect").hidden = !!s0.connected;
  const notice = $("#notice");
  if (!st.tracks.length) {
    notice.hidden = false;
    notice.textContent = st.backend
      ? "The device is not connected or still being read. Reconnect takes it back (e.g. after its own editor)."
      : "No device detected yet. Plug it in by USB; the device server looks for it every few seconds.";
  } else notice.hidden = true;
}

// the tempo: a number field where the device lets us set it, else only shown. The field follows the device, except
// while it holds a number typed here that is not sent yet.
const bpmText = v => String(Math.round(v * 100) / 100);

function renderTempo(st) {
  const input = $("#tempo-input"), text = $("#tempo-text"), r = st.tempo_range;
  const editable = !!(r && st.status && st.status.connected);
  $("#tempo").hidden = !st.tempo;
  input.hidden = !editable;
  text.hidden = editable;
  $("#tempo").title = st.tempo_note || "";
  $("#tempo").classList.toggle("noted", !!st.tempo_note);
  if (!st.tempo) return;
  text.textContent = bpmText(st.tempo);
  if (editable) {
    input.min = r.min; input.max = r.max; input.step = r.step;
    input.title = `Tempo in BPM (${r.min} to ${r.max})` + (st.tempo_note ? ` · ${st.tempo_note}` : "");
  }
  if (!S.tempoDirty) input.value = bpmText(st.tempo);
}

let tempoTimer = 0;
function revertTempo() {
  clearTimeout(tempoTimer);
  S.tempoDirty = false;
  if (S.state && S.state.tempo) $("#tempo-input").value = bpmText(S.state.tempo);
}

async function sendTempo() {
  clearTimeout(tempoTimer);
  if (!S.tempoDirty || S.tempoSending) return;
  const input = $("#tempo-input"), sent = input.value;
  if (!sent || (S.state && Number(sent) === S.state.tempo)) return revertTempo();
  S.tempoSending = true;
  const r = await act("/api/ui/tempo", { bpm: Number(sent) }, "Tempo");
  S.tempoSending = false;
  if (input.value !== sent) { tempoTimer = setTimeout(sendTempo, 250); return; }  // (changed meanwhile)
  if (!r) return revertTempo();
  S.tempoDirty = false;
  input.value = bpmText(r.tempo);
}

// ---------------------------------------------------------------- tracks
function buildTracks(st) {
  const box = $("#tracks");
  box.replaceChildren();
  S.actionsKeys = {};
  for (const t of st.tracks) {
    if (!S.views[t.track]) S.views[t.track] = "roll";
    const collapsed = !!S.collapsed[t.track];
    const card = h("article", { class: "track " + t.kind + (collapsed ? " collapsed" : ""), id: "track-" + t.track },
      h("div", { class: "track-head" },
        h("button", { class: "fold", "aria-expanded": !collapsed, "aria-controls": "track-body-" + t.track,
          title: collapsed ? "Show this track" : "Hide this track", onclick: () => toggleTrack(t.track) },
          s("svg", { class: "tri", viewBox: "0 0 12 12", width: 12, height: 12, "aria-hidden": "true" },
            s("path", { d: "M1.5 3.5 L10.5 3.5 L6 9.5 Z", fill: "currentColor" }))),
        h("span", { class: "track-num", text: t.track }),
        can("sounds") ? soundSelect(t) : h("span", { class: "track-title", text: t.instrument || t.name }),
        h("span", { class: "tag", text: t.kind }),
        h("span", { class: "tag key", "data-role": "key", hidden: !t.key, text: t.key || "" }),
        h("span", { class: "tag", "data-role": "grid" }),
        h("span", { class: "tag", "data-role": "muted", hidden: true, text: "MUTED" }),
        h("span", { class: "spacer" }),
        h("div", { class: "seg", role: "group" },
          h("button", { "aria-pressed": S.views[t.track] === "roll", onclick: () => setView(t.track, "roll"), text: "Steps" }),
          h("button", { "aria-pressed": S.views[t.track] === "staff", onclick: () => setView(t.track, "staff"), text: "Staff" }))),
      h("div", { class: "track-body", id: "track-body-" + t.track },
        h("div", { class: "track-actions", "data-role": "actions" }),
        h("div", { class: "roll", "data-role": "roll" }),
        h("div", { class: "staff", "data-role": "staff", hidden: true })));
    box.append(card);
  }
  updateTrackChrome(st);
}

// fold / unfold a track (an accordion): remembered in this browser
function toggleTrack(track) {
  const card = $("#track-" + track);
  const collapsed = !card.classList.contains("collapsed");
  card.classList.toggle("collapsed", collapsed);
  const btn = card.querySelector(".fold");
  btn.setAttribute("aria-expanded", !collapsed);
  btn.title = collapsed ? "Show this track" : "Hide this track";
  if (collapsed) S.collapsed[track] = true; else delete S.collapsed[track];
  try { localStorage.setItem("ostinato.collapsed", JSON.stringify(S.collapsed)); } catch (e) { /* (not kept) */ }
  if (!collapsed) drawTrack(track);  // (the roll fits the card's width, unknown while hidden)
}

// the track's sound as a menu (presets grouped by engine; kits on the drum track); the list is fetched once per
// kind of track
function soundSelect(t) {
  const sel = h("select", { class: "track-title sound", "aria-label": `Sound of track ${t.track}`,
    title: "Choose this track's sound" }, h("option", { value: "", text: t.instrument || t.name, selected: true }));
  const cacheKey = t.kind === "drums" ? "drums" : "synth";
  S.sounds = S.sounds || {};
  const fill = list => {
    const groups = {};
    for (const x of list) (groups[x.group] = groups[x.group] || []).push(x.name);
    const cur = t.instrument || "";
    let found = false;
    const opts = Object.entries(groups).map(([g, names]) => h("optgroup", { label: g },
      ...names.map(n => {
        const on = !found && (cur === `${g} ${n}` || (g === "KIT" && cur.endsWith(` ${n}`)));
        found = found || on;
        return h("option", { value: JSON.stringify([g, n]), selected: on, text: n });
      })));
    sel.replaceChildren(...(found ? [] : [h("option", { value: "", text: cur || t.name, selected: true })]), ...opts);
  };
  (S.sounds[cacheKey] ||= api(`/api/ui/track/${t.track}/sounds`).then(r => r.sounds))
    .then(fill).catch(() => { delete S.sounds[cacheKey]; });
  sel.onchange = async () => {
    if (!sel.value) return;
    const [group, name] = JSON.parse(sel.value);
    await act(`/api/ui/track/${t.track}/sound`, { name, group }, "Sound");
  };
  return sel;
}

function setView(track, view) {
  S.views[track] = view;
  const card = $("#track-" + track);
  card.querySelectorAll(".seg button").forEach((b, i) => b.setAttribute("aria-pressed", (i === 0) === (view === "roll")));
  drawTrack(track);
}

async function loadTrack(track) {
  try {
    S.data[track] = await api(`/api/ui/track/${track}`);
  } catch (e) { S.data[track] = { error: e.message }; }
  drawTrack(track);
}

function drawTrack(track) {
  const card = $("#track-" + track);
  const d = S.data[track];
  if (!card || !d) return;
  const roll = card.querySelector('[data-role="roll"]');
  const staff = card.querySelector('[data-role="staff"]');
  const gtag = card.querySelector('[data-role="grid"]');
  gtag.textContent = d.grid ? `${d.grid.steps} × ${d.grid.name}` : `${d.length} beats`;
  if (d.error) { roll.replaceChildren(h("div", { class: "empty", text: d.error })); return; }
  if (S.views[track] === "staff") {
    roll.hidden = true;
    staff.hidden = false;
    const src = `/api/ui/track/${track}/staff.svg?v=${S.logVersion}`;
    const img = staff.querySelector("img");
    if (!img || img.dataset.src !== src) {
      const ni = h("img", { alt: `Track ${track} as notation`, "data-src": src });
      ni.onerror = () => staff.replaceChildren(h("div", { class: "loading", text: "Could not draw the staff." }));
      ni.src = src;
      if (!img) staff.replaceChildren(h("div", { class: "loading", text: "drawing…" }));
      ni.onload = () => staff.replaceChildren(ni);
    }
  } else {
    staff.hidden = true;
    roll.hidden = false;
    const width = roll.clientWidth - 32;  // (the padding)
    roll.replaceChildren(d.events.length ? drawRoll(d, width) : h("div", { class: "empty", text: "No notes in the pattern." }));
  }
}

const NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
const BLACK = [1, 3, 6, 8, 10];
const LEVEL_OPACITY = { ghost: 0.38, soft: 0.65, normal: 0.88, hard: 1 };

function drawRoll(d, width) {
  const drums = d.kind === "drums";
  const stepBeats = d.grid ? d.grid.step : 0.25;
  const steps = d.grid ? d.grid.steps : Math.max(1, Math.round(d.length / stepBeats));
  const perBeat = Math.abs(1 / stepBeats - Math.round(1 / stepBeats)) < 1e-6 ? Math.round(1 / stepBeats) : 4;
  const labelW = drums ? 96 : 48;
  // the steps fill the card (at least 12 px each: a long pattern scrolls sideways)
  const cellW = Math.max(12, Math.floor(((width || 1000) - labelW - 4) / steps));
  const named = Object.fromEntries(d.voices.map(v => [v.pitch, v.name]));
  let rows;
  if (drums) rows = d.voices.map(v => v.pitch).sort((a, b) => a - b);
  else {
    const ps = d.voices.map(v => v.pitch);
    let lo = Math.min(...ps) - 2, hi = Math.max(...ps) + 2;
    while (hi - lo < 12) { lo--; hi++; }
    rows = [];
    for (let p = hi; p >= lo; p--) rows.push(p);
  }
  const rowH = drums ? 20 : 13;
  const W = labelW + steps * cellW + 2, H = rows.length * rowH + 18;
  const svg = s("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": `Track ${d.track}: ${d.events.length} notes` });
  const rowOf = Object.fromEntries(rows.map((p, i) => [p, i]));
  const css = getComputedStyle(document.documentElement);
  const col = n => css.getPropertyValue(n).trim();
  // rows and labels
  rows.forEach((p, i) => {
    const y = 18 + i * rowH;
    if (!drums && BLACK.includes(((p % 12) + 12) % 12))
      svg.append(s("rect", { x: labelW, y, width: steps * cellW, height: rowH, fill: col("--black-key") }));
    const label = drums ? (named[p] || String(p)) : (named[p] || (p % 12 === 0 ? NAMES[p % 12] + (Math.floor(p / 12) - 1) : ""));
    if (label) svg.append(s("text", { x: labelW - 6, y: y + rowH * 0.72, "text-anchor": "end",
      "font-size": drums ? 11 : 10, fill: named[p] ? col("--ink") : col("--muted") }, document.createTextNode(label)));
    svg.append(s("line", { x1: labelW, x2: labelW + steps * cellW, y1: y + rowH, y2: y + rowH, stroke: col("--grid") }));
  });
  // steps, beats, bars
  for (let i = 0; i <= steps; i++) {
    const x = labelW + i * cellW;
    const bar = i % (perBeat * 4) === 0, beat = i % perBeat === 0;
    svg.append(s("line", { x1: x, x2: x, y1: 18, y2: H, stroke: bar ? col("--muted") : beat ? col("--beat") : col("--grid"),
      "stroke-width": bar ? 1.2 : 1 }));
    if (beat && i < steps) svg.append(s("text", { x: x + 3, y: 12, "font-size": 10, fill: col("--muted") },
      document.createTextNode(String(i + 1))));
  }
  // notes
  const fillCol = drums ? col("--drum") : col("--note");
  for (const e of d.events) {
    const st = e.step != null ? e.step : e.start / stepBeats;
    const len = drums ? 1 : Math.max(1, e.steps != null ? e.steps : e.length / stepBeats);
    for (const n of e.notes) {
      if (!(n.pitch in rowOf)) continue;
      const x = labelW + st * cellW + 1.5, y = 18 + rowOf[n.pitch] * rowH + 1.5;
      const w = Math.max(4, len * cellW - 3), hh = rowH - 3;
      const g = s("g", {});
      const r = s("rect", { x, y, width: w, height: hh, rx: 3,
        fill: e.muted ? "none" : fillCol, "fill-opacity": LEVEL_OPACITY[n.level] || 0.88,
        stroke: e.accent ? col("--accent") : e.muted ? fillCol : "none",
        "stroke-width": e.accent ? 2 : 1, "stroke-dasharray": e.muted ? "3 2" : null });
      g.append(r);
      for (let k = 1; k < (n.ratchet || 1); k++)
        g.append(s("line", { x1: x + (w * k) / n.ratchet, x2: x + (w * k) / n.ratchet, y1: y + 2, y2: y + hh - 2,
          stroke: col("--panel"), "stroke-width": 1.5 }));
      if (e.slide) g.append(s("path", { d: `M${x + w - 6},${y + hh} L${x + w},${y + 1}`, stroke: col("--accent"),
        "stroke-width": 1.6, fill: "none" }));
      const bits = [n.name, `step ${st + 1}`, !drums && len !== 1 ? `${len} steps` : null, n.level !== "normal" ? n.level : null,
        n.ratchet > 1 ? `×${n.ratchet}` : null, e.accent ? "accent" : null, e.slide ? "slide" : null, e.muted ? "muted" : null,
        n.velocity ? `vel ${n.velocity}` : null,
        ...Object.entries(e.extras || {}).map(([k, v]) => `${k} ${Array.isArray(v) ? v.join(", ") : v}`)];
      g.append(s("title", {}, document.createTextNode(bits.filter(Boolean).join(" · "))));
      svg.append(g);
    }
  }
  return svg;
}

function updateTrackChrome(st) {
  const armedBy = Object.fromEntries(st.armed.map(a => [a.track, a]));
  for (const t of st.tracks) {
    const card = $("#track-" + t.track);
    if (!card) continue;
    const ktag = card.querySelector('[data-role="key"]');
    ktag.hidden = !t.key;
    ktag.textContent = t.key || "";
    const mtag = card.querySelector('[data-role="muted"]');
    mtag.hidden = !t.muted;
    const box = card.querySelector('[data-role="actions"]');
    const armed = armedBy[t.track];
    const algos = st.algorithms.filter(a => a.kinds.includes(t.kind === "chords" ? "chords" : t.kind));
    const key = JSON.stringify([
      can("writer"), can("controls"), can("mixer"), t.kind, t.undo, t.muted,
      t.level && t.level.value, t.pan && t.pan.value,
      armed ? [armed.algorithm, armed.control, armed.kind, armed.runs, armed.depth, armed.last] : null,
      algos.map(a => [a.name, a.description, a.kinds]),
      st.controls.map(c => [c.name, c.kind, c.description]),
    ]);
    if (S.actionsKeys[t.track] !== key) {
      S.actionsKeys[t.track] = key;
      box.replaceChildren(...trackActions(t, armed, st));
    }
  }
}

// the mixer: a slider on the device's own scale; it is sent when let go (not while dragging)
function levelText(m, v) {
  if (m.db_zero == null || !m.db_step) return String(v);
  return v <= m.min ? "OFF" : `${((v - m.db_zero) * m.db_step).toFixed(1)}dB`;
}

function panText(m, v) {
  const pct = Math.round((v * 100) / Math.max(Math.abs(m.min), Math.abs(m.max)));
  return pct === 0 ? "C" : pct < 0 ? `L${-pct}` : `R${pct}`;
}

function mixSlider(t, what, label, fmt) {
  const m = t[what];
  const val = h("span", { class: "mix-value", text: fmt(m, m.value) });
  const input = h("input", { type: "range", min: m.min, max: m.max, step: 1, value: m.value,
    "aria-label": `${label} of track ${t.track}`, title: `${label}: ${m.text}` + (what === "pan" ? " (double-click: center)" : ""),
    oninput: e => (val.textContent = fmt(m, +e.target.value)),
    onchange: e => act(`/api/ui/track/${t.track}/mix`, { [what]: +e.target.value }, label),
    ondblclick: e => { if (what === "pan") { e.target.value = 0; e.target.dispatchEvent(new Event("change")); } } });
  return h("label", { class: "mix " + what }, h("span", { class: "mix-label", text: label }), input, val);
}

function trackActions(t, armed, st) {
  const out = [];
  if (can("mixer")) {
    out.push(h("button", {
      class: t.muted ? "primary" : "ghost",
      "aria-pressed": !!t.muted,
      title: t.muted ? "Unmute this track" : "Mute this track",
      onclick: () => act(`/api/ui/track/${t.track}/mute`, { muted: !t.muted }, t.muted ? "Unmute" : "Mute"),
    }, t.muted ? "Unmute" : "Mute"));
    if (t.level) out.push(mixSlider(t, "level", "Level", levelText));
    if (t.pan) out.push(mixSlider(t, "pan", "Pan", panText));
  }
  if (!can("writer")) {
    if (!out.length) return [h("span", { class: "empty", text: "This device cannot be written to." })];
    return out;
  }
  const algos = st.algorithms.filter(a => a.kinds.includes(t.kind === "chords" ? "chords" : t.kind));
  if (algos.length) {
    if (!S.algoChoice[t.track] || !algos.some(a => a.name === S.algoChoice[t.track])) S.algoChoice[t.track] = algos[0].name;
    const action = h("button", { class: "ghost", disabled: !!armed,
      onclick: () => act(`/api/ui/track/${t.track}/generate`, { algorithm: S.algoChoice[t.track] },
        S.algoChoice[t.track] === "random_initializer" ? "Initialize" : "Vary") });
    const updateAction = value => {
      const initialize = value === "random_initializer";
      action.textContent = initialize ? "Initialize" : "Vary";
      action.title = initialize ? "Replace the sequence with generated notes" : "Run the algorithm once";
    };
    const sel = h("select", { "aria-label": "Algorithm", onchange: e => {
      S.algoChoice[t.track] = e.target.value;
      updateAction(e.target.value);
    } },
      ...algos.map(a => h("option", { value: a.name, selected: a.name === S.algoChoice[t.track], title: a.description,
        text: a.name.replace(/_/g, " ") })));
    updateAction(S.algoChoice[t.track]);
    out.push(h("div", { class: "group" }, sel, action));
  }
  out.push(h("button", { class: "ghost", disabled: !t.undo, title: "Undo the last write to this track",
    onclick: () => act(`/api/ui/track/${t.track}/undo`, {}, "Undo") }, t.undo ? `Undo (${t.undo})` : "Undo"));
  out.push(h("button", { class: "ghost", disabled: !!armed,
    title: "Remove all notes of this track (Undo brings them back)",
    onclick: () => { if (confirm(`Clear all notes of track ${t.track}?`)) act(`/api/ui/track/${t.track}/clear`, {}, "Clear"); }
  }, "Clear"));
  if (can("controls") && algos.length) {
    out.push(h("span", { class: "sep" }));
    if (armed) {
      out.push(h("span", { class: "armed-chip" }, h("span", { class: "pulse" }),
        `${armed.algorithm.replace(/_/g, " ")} on ${armed.control}`));
      out.push(h("button", { class: "small", onclick: () => act(`/api/ui/track/${t.track}/disarm`, {}, "Disarm"),
        title: "Stop; keep the pattern as it is" }, "Disarm"));
      out.push(h("button", { class: "small", onclick: () => act(`/api/ui/track/${t.track}/disarm`, { restore: true }, "Restore"),
        title: "Stop and bring back the pattern from before arming" }, "Restore"));
      const how = armed.kind === "encoder" ? "turn it clockwise to vary, back to undo" : "each press runs once";
      out.push(h("div", { class: "armed-info", text:
        `${armed.runs} run(s), ${armed.depth} undoable · ${how}` + (armed.last && armed.last.length ? ` · last: ${armed.last.join("; ")}` : "") }));
    } else {
      const ctrls = st.controls;
      if (!S.controlChoice[t.track]) S.controlChoice[t.track] = (ctrls.find(c => c.kind === "encoder") || ctrls[0] || {}).name;
      out.push(h("div", { class: "group" },
        h("select", { "aria-label": "Control", onchange: e => (S.controlChoice[t.track] = e.target.value) },
          ...ctrls.map(c => h("option", { value: c.name, title: c.description, selected: c.name === S.controlChoice[t.track],
            text: `${c.name} (${c.kind})` }))),
        h("button", { class: "ghost", title: "Generative mode: the control drives the algorithm on the device",
          onclick: () => act(`/api/ui/track/${t.track}/arm`,
            { algorithm: S.algoChoice[t.track], control: S.controlChoice[t.track] }, "Arm") }, "Arm")));
    }
  }
  return out;
}

// ---------------------------------------------------------------- activity
async function loadLog() {
  try {
    const r = await api(`/api/ui/log?since=${S.lastLog}`);
    const ol = $("#log");
    for (const e of r.events) {
      const t = new Date(e.at * 1000).toLocaleTimeString();
      ol.prepend(h("li", { class: "src-" + e.source },
        h("span", { class: "t", text: t }), h("span", { class: "tr", text: e.track ? `track ${e.track}` : "" }),
        h("span", { class: "k", text: e.kind }), h("span", { text: e.text })));
    }
    while (ol.children.length > 300) ol.lastChild.remove();
    S.lastLog = r.version;
  } catch (e) { /* (shown by the header) */ }
}

// ---------------------------------------------------------------- saved states
async function loadStates() {
  const box = $("#states");
  let r;
  try { r = await api("/api/ui/states"); } catch (e) { box.replaceChildren(h("div", { class: "notice", text: e.message })); return; }
  const name = h("input", { type: "text", placeholder: "name, e.g. before gig", "aria-label": "State name" });
  const note = h("input", { type: "text", placeholder: "note (optional)", "aria-label": "Note", class: "wide" });
  const exact = h("input", { type: "checkbox", checked: true });
  const save = async () => {
    const n = name.value.trim();
    if (!n) { toast("Give the state a name", true); return; }
    if (r.states.some(x => x.name === n) && !confirm(`Replace the saved state "${n}"?`)) return;
    const res = await act("/api/ui/states/save", { name: n, note: note.value, exact: exact.checked, overwrite: true }, "Save");
    if (res) loadStates();
  };
  const saveCard = h("div", { class: "card states-save" },
    h("h3", { text: "Save the device now" }),
    h("div", { class: "row" }, name, note,
      h("label", { class: "check", title: "Also keep the device's own copy of its project (put back byte for byte)" },
        exact, " exact copy"),
      h("button", { class: "primary", onclick: save }, "Save")));
  const row = x => h("tr", {},
    h("td", {}, h("strong", { text: x.name }), x.exact ? h("span", { class: "tag", text: "exact" }) : null),
    h("td", { text: x.saved_at || "" }),
    h("td", { class: "small", text: x.tracks.join(" · ") }),
    h("td", { text: x.note }),
    h("td", { class: "actions" },
      h("button", { class: "small", onclick: async () => {
        if (await act("/api/ui/states/load", { name: x.name }, "Load")) loadStates(); } }, "Load"),
      h("button", { class: "small ghost", onclick: async () => {
        if (confirm(`Delete the saved state "${x.name}"? This removes its file.`) &&
            await act("/api/ui/states/delete", { name: x.name }, "Delete")) loadStates(); } }, "Delete")));
  const list = r.states.length
    ? h("table", { class: "states-list" },
        h("thead", {}, h("tr", {}, ...["Name", "Saved", "Tracks", "Note", ""].map(x => h("th", { text: x })))),
        h("tbody", {}, ...r.states.map(row)))
    : h("div", { class: "empty", text: `No saved states yet (they go to ${r.dir}).` });
  box.replaceChildren(saveCard, h("h3", { text: "Saved states" }), list, loadedCard(r.loaded));
}

function loadedCard(L) {
  if (!L) return h("div");
  const trackBoxes = L.tracks.map(t => h("input", { type: "checkbox", checked: true, value: t }));
  const partBoxes = L.parts.map(p => h("input", { type: "checkbox", checked: true, value: p }));
  const exact = h("input", { type: "checkbox", disabled: !L.exact });
  exact.onchange = () => [...trackBoxes, ...partBoxes].forEach(b => (b.disabled = exact.checked));
  const apply = async () => {
    const tracks = trackBoxes.filter(b => b.checked).map(b => +b.value);
    const parts = partBoxes.filter(b => b.checked).map(b => b.value);
    if (!exact.checked && (!tracks.length || !parts.length)) { toast("Choose at least one track and one part", true); return; }
    const what = exact.checked ? "the exact copy (every track, everything; the song must be stopped)"
      : `track(s) ${tracks.join(", ")}: ${parts.join(", ")}`;
    if (!confirm(`Put "${L.name}" onto the device: ${what}?\nThe device's state now is saved as "before apply" first.`)) return;
    if (await act("/api/ui/states/apply", exact.checked ? { exact: true } : { tracks, parts }, "Apply")) loadStates();
  };
  return h("div", { class: "card states-loaded" },
    h("h3", { text: `Loaded: ${L.name}` }),
    h("pre", { class: "summary", text: L.summary }),
    L.compare ? h("div", {}, h("h4", { text: "Compared with the device now" }),
      h("ul", { class: "compare" }, ...L.compare.map(x => h("li", { class: x.endsWith("as saved") ? "same" : "diff", text: x }))))
      : null,
    h("div", { class: "row" }, h("span", { class: "mix-label", text: "Tracks" }),
      ...trackBoxes.map(b => h("label", { class: "check" }, b, ` ${b.value}`))),
    h("div", { class: "row" }, h("span", { class: "mix-label", text: "Parts" }),
      ...partBoxes.map(b => h("label", { class: "check" }, b, ` ${b.value}`))),
    h("div", { class: "row" },
      h("label", { class: "check", title: L.exact ? "Put back the device's own copy, byte for byte" : "This state has no exact copy" },
        exact, " exact copy instead"),
      h("button", { class: "primary", onclick: apply }, "Apply to the device")));
}

// ---------------------------------------------------------------- settings
async function loadSetups() {
  const box = $("#setups");
  box.replaceChildren(h("div", { class: "empty", text: "loading…" }));
  let r;
  try { r = await api("/api/ui/setups"); } catch (e) { box.replaceChildren(h("div", { class: "notice", text: e.message })); return; }
  box.replaceChildren();
  const groups = {};
  for (const su of r.setups) (groups[su.group] = groups[su.group] || []).push(su);
  for (const [g, list] of Object.entries(groups)) {
    box.append(h("div", { class: "setup-group" }, h("h2", { text: g }), ...list.map(setupCard)));
  }
}

const clone = o => JSON.parse(JSON.stringify(o));
const humanize = k => k.replace(/_/g, " ");

function setupCard(su) {
  const draft = clone(su.values);
  const ui = su.schema._ui || {};
  const body = h("div", { class: "body" });
  const details = h("details", { class: "setup" },
    h("summary", {}, h("span", { class: "name", text: su.title }),
      h("span", { class: "src", text: su.exists ? "your settings" : "defaults" })),
    body);
  const fill = (values, notes) => {
    Object.assign(draft, clone(values));
    for (const k of Object.keys(draft)) if (!(k in values)) delete draft[k];
    body.replaceChildren(
      su.schema._doc ? h("p", { class: "doc", text: su.schema._doc.replace(/ Copy this file.*$/, "").replace(/ Keys starting with _ are comments\./, "") }) : null,
      ...fields(su.schema, draft, "", ui),
      h("div", { class: "foot" },
        h("button", { class: "primary", onclick: save }, "Save"),
        h("button", { class: "ghost", onclick: reset, title: "Remove your file: the defaults again" }, "Reset to defaults"),
        h("span", { class: "path", text: su.path }),
        notes && notes.length ? h("div", { class: "notes", text: notes.join(" · ") }) : null));
  };
  const save = async () => {
    try {
      const res = await api(`/api/ui/setups/${encodeURIComponent(su.id)}`, "PUT", { values: draft });
      su.exists = res.exists;
      details.querySelector(".src").textContent = "your settings";
      fill(res.values, res.notes);
      toast(res.notes.length ? `Saved, with notes: ${res.notes.join("; ")}` : `Saved ${su.title}`);
    } catch (e) { toast("Not saved: " + e.message, true); }
  };
  const reset = async () => {
    try {
      const res = await api(`/api/ui/setups/${encodeURIComponent(su.id)}`, "DELETE");
      details.querySelector(".src").textContent = "defaults";
      fill(res.values, res.notes);
      toast(`${su.title}: back to the defaults`);
    } catch (e) { toast(e.message, true); }
  };
  fill(su.values, su.notes);
  return details;
}

function rangeFor(ui, path) {
  const r = ui.ranges || {};
  if (r[path]) return r[path];
  const parent = path.split(".").slice(0, -1).join(".");
  return r[parent + ".*"];
}

function choicesFor(ui, path) {
  const c = (ui.choices || {})[path];
  if (!c) return null;
  const out = [];
  for (const x of c) {
    if (x === "#track") (S.state ? S.state.tracks : []).forEach(t => out.push(t.track));
    else out.push(x);
  }
  return out;
}

function numberControl(get, set, range, isInt) {
  const num = h("input", { type: "number", value: get(), step: range ? range[2] : isInt ? 1 : "any",
    min: range ? range[0] : null, max: range ? range[1] : null, style: "width:6.5em" });
  num.oninput = () => { const v = num.value === "" ? 0 : Number(num.value); set(v); if (rng) rng.value = v; };
  let rng = null;
  if (range) {
    rng = h("input", { type: "range", min: range[0], max: range[1], step: range[2], value: get() });
    rng.oninput = () => { set(Number(rng.value)); num.value = rng.value; };
  }
  return [rng, num].filter(Boolean);
}

function control(dv, getter, setter, path, ui) {
  const choices = choicesFor(ui, path);
  if (Array.isArray(dv)) {
    if (choices) {
      return [h("div", { class: "ctl" }, ...choices.map(c => {
        const cb = h("input", { type: "checkbox", checked: (getter() || []).includes(c) });
        cb.onchange = () => {
          const cur = new Set(getter() || []);
          cb.checked ? cur.add(c) : cur.delete(c);
          setter(choices.filter(x => cur.has(x)));
        };
        return h("label", {}, cb, " " + c);
      }))];
    }
    const t = h("input", { type: "text", value: (getter() || []).join(", ") });
    t.oninput = () => setter(t.value.split(",").map(x => x.trim()).filter(Boolean));
    return [t];
  }
  if (typeof dv === "boolean") {
    const cb = h("input", { type: "checkbox", checked: !!getter() });
    cb.onchange = () => setter(cb.checked);
    return [cb];
  }
  if (choices) {
    const sel = h("select", {}, ...choices.map(c => h("option", { value: String(c), selected: String(getter()) === String(c),
      text: typeof c === "number" ? `track ${c}` : c })));
    sel.onchange = () => { const c = choices.find(x => String(x) === sel.value); setter(c); };
    return [sel];
  }
  if (typeof dv === "number") return numberControl(getter, setter, rangeFor(ui, path), Number.isInteger(dv) && !rangeFor(ui, path));
  if (dv === null) {
    const t = h("input", { type: "number", value: getter() ?? "", style: "width:12em",
      placeholder: (ui.placeholders || {})[path] || "empty: random" });
    t.oninput = () => setter(t.value === "" ? null : Number(t.value));
    return [t];
  }
  const t = h("input", { type: "text", value: getter() ?? "" });
  t.oninput = () => setter(t.value);
  return [t];
}

function fields(schema, draft, prefix, ui) {
  const out = [];
  for (const [k, dv] of Object.entries(schema)) {
    if (k.startsWith("_")) continue;
    const path = prefix ? `${prefix}.${k}` : k;
    const help = schema["_" + k];
    let ctl;
    if (dv && typeof dv === "object" && !Array.isArray(dv) && dv._open) {
      ctl = openTable(draft, k, path, ui);
    } else if (dv && typeof dv === "object" && !Array.isArray(dv)) {
      ctl = h("fieldset", { class: "sub" }, ...subRows(dv, draft, k, path, ui, ""));
    } else {
      ctl = h("div", { class: "ctl" }, ...control(dv, () => draft[k], v => (draft[k] = v), path, ui));
    }
    out.push(h("div", { class: "field" }, h("label", { text: humanize(k) }), ctl,
      help ? h("div", { class: "help", text: help }) : null));
  }
  return out;
}

// rows for a nested table (deeper tables flatten into "approach below", "approach above"...)
function subRows(dv, parent, k, path, ui, label) {
  if (!parent[k] || typeof parent[k] !== "object") parent[k] = clone(dv);
  const sub = parent[k];
  return Object.entries(dv).filter(([kk]) => !kk.startsWith("_")).flatMap(([kk, ddv]) => {
    const name = (label ? label + " " : "") + humanize(kk);
    if (ddv && typeof ddv === "object" && !Array.isArray(ddv)) return subRows(ddv, sub, kk, `${path}.${kk}`, ui, name);
    return [h("div", { class: "row" }, h("span", { class: "k", text: name }),
      ...control(ddv, () => sub[kk], v => (sub[kk] = v), `${path}.${kk}`, ui))];
  });
}

function openTable(draft, k, path, ui) {
  const box = h("fieldset", { class: "sub" });
  const render = () => {
    const tbl = draft[k] || {};
    box.replaceChildren(...Object.keys(tbl).sort((a, b) => Number(a) - Number(b)).map(kk =>
      h("div", { class: "row" }, h("span", { class: "k", text: kk }),
        ...numberControl(() => tbl[kk], v => (tbl[kk] = v), rangeFor(ui, `${path}.${kk}`), false),
        h("button", { class: "small", title: "Remove", onclick: () => { delete tbl[kk]; render(); } }, "×"))),
      h("div", { class: "row" }, h("button", { class: "small", onclick: () => {
        const keys = Object.keys(tbl).map(Number).filter(n => !isNaN(n));
        tbl[String(keys.length ? Math.max(...keys) + 1 : 1)] = 0.1;
        draft[k] = tbl;
        render();
      } }, "+ add")));
  };
  render();
  return box;
}

// ---------------------------------------------------------------- tabs, buttons, start
document.querySelectorAll(".tabs button").forEach(b => b.addEventListener("click", () => {
  document.querySelectorAll(".tabs button").forEach(x => x.setAttribute("aria-selected", x === b));
  document.querySelectorAll(".tab").forEach(t => (t.hidden = t.id !== "tab-" + b.dataset.tab));
  if (b.dataset.tab === "settings" && !S.setupsShown) { S.setupsShown = true; loadSetups(); }
  if (b.dataset.tab === "states") loadStates();
}));
$("#btn-play").onclick = () => act("/api/ui/transport", { action: S.state && S.state.playing ? "stop" : "play" },
  "Play / stop");
// (the arrows and the spin buttons "change" at every step: sent when they pause)
$("#tempo-input").addEventListener("input", () => (S.tempoDirty = true));
$("#tempo-input").addEventListener("change", () => { clearTimeout(tempoTimer); tempoTimer = setTimeout(sendTempo, 250); });
$("#tempo-input").addEventListener("blur", sendTempo);
$("#tempo-input").addEventListener("keydown", e => {
  if (e.key === "Enter") sendTempo();
  else if (e.key === "Escape") { revertTempo(); e.target.select(); }
});
$("#btn-release").onclick = () => act("/api/ui/disconnect", {}, "Release");
$("#btn-connect").onclick = () => act("/api/ui/connect", {}, "Reconnect");

let resizeTimer = 0;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => S.state && S.state.tracks.forEach(t => S.views[t.track] === "roll" && drawTrack(t.track)), 150);
});

poll(true);
setInterval(poll, 900);
