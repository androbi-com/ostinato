// The Groove tab: the device's one groove and which tracks play it.
//   - the tracks' lengths, drawn to scale and editable here (steps × step length);
//   - "apply to all tracks": switched on by the user only, while every track's length fits the groove; a change of a length switches it off (the server does: the tracks that still fit keep the groove).
//     Off, each track has its own switch, usable only while its length fits the groove;
//   - the groove's steps: one slider per step (as the pan slider: the middle is straight, right is late, left
//     early), in whole 1/64 of a step, shown in % above; a double-click on the % puts it back to 0. They can be
//     moved only while every track has the same step length. A slider is sent when let go.
import { signal, useSignal } from "@preact/signals";
import { html } from "./html.js";
import { act, state, trackData } from "./store.js";

const RES = 64;
const RANGES = [12.5, 25, 50];  // the slider's reach, in % of a step (50: the most, -50 .. +48.4)
const range = signal(readRange());

function readRange() {
  const v = Number(localStorage.getItem("ostinato.grooveRange"));
  return RANGES.includes(v) ? v : 50;
}

function setRange(v) {
  range.value = v;
  try { localStorage.setItem("ostinato.grooveRange", String(v)); } catch (e) { /* (not kept) */ }
}

// 6 -> "+9.4%"
const pct = v => (v === 0 ? "0%" : `${v > 0 ? "+" : "−"}${+(Math.abs(v) * 100 / RES).toFixed(1)}%`);
const same = (a, b) => a.length === b.length && a.every((x, i) => x === b[i]);
const useGroove = (body, label) => act("/api/ui/groove/use", body, label);

export function Groove() {
  const st = state.value;
  if (!st) return null;
  if (!st.tracks.length) return html`<div class="notice"><p>No device yet.</p></div>`;
  if (!st.groove) {
    return html`<div class="notice"><p>This device cannot play timing offsets, so it cannot play a groove.</p></div>`;
  }
  return html`
    <${Lengths} st=${st} gs=${st.groove} />
    <${GrooveEditor} st=${st} gs=${st.groove} />`;
}

// ---------------------------------------------------------------- the tracks' lengths, and which play the groove
function Lengths({ st, gs }) {
  const g = gs.groove, use = gs.use;
  const max = Math.max(...gs.lengths.map(l => l.beats || 0), 1);
  const equal = !gs.blocked;
  return html`
    <div class="card groove-lengths">
      <div class="row">
        <strong>Track lengths</strong>
        <span class="tag ${equal ? "ok" : "warn"}">${equal ? `all ${gs.grid.name}` : "different step lengths"}</span>
        <span class="spacer"></span>
        <label class="check" title=${!g ? "There is no groove yet: move a slider below to make one"
            : use.all ? "Every track plays the groove (a change of a track's length switches this off)"
            : gs.fits_all ? "Every track plays the groove" : "Possible only while every track's length fits the groove"}>
          <input type="checkbox" checked=${!!g && use.all} disabled=${!g || (!use.all && !gs.fits_all)}
            onChange=${e => useGroove({ all: e.target.checked }, "Apply to all tracks")} />
          Apply groove to all tracks</label>
      </div>
      ${gs.lengths.map(l => html`<${LengthRow} key=${l.track} l=${l} st=${st} gs=${gs} max=${max} />`)}
      ${g && !use.all && html`<p class="hint">Each track plays the groove only where it is switched on; a track can be
        switched on while its length fits the groove (steps of ${stepName(gs, g)}, a multiple of ${g.values.length}).</p>`}
    </div>`;
}

// the name of the groove's step length, as the tracks call it
function stepName(gs, g) {
  const l = gs.lengths.find(x => x.step === g.step);
  return l ? l.name : `${g.step} beats`;
}

function LengthRow({ l, st, gs, max }) {
  const t = st.tracks.find(x => x.track === l.track) || {};
  const data = trackData.value[l.track];
  const lim = data && data.limits;
  const g = gs.groove, use = gs.use;
  const plays = gs.playing.includes(l.track);
  const set = (body, label) => act(`/api/ui/track/${l.track}/length`, body, label);
  const onSteps = e => {
    const v = Number(e.target.value);
    if (Number.isInteger(v) && v >= 1 && (!lim || !lim.max_steps || v <= lim.max_steps)) { if (v !== l.steps) set({ steps: v }, "Steps"); }
    else e.target.value = l.steps;
  };
  const divs = lim ? (lim.grids.includes(l.name) ? lim.grids : [l.name, ...lim.grids]) : [l.name];
  // the bar: as long as the pattern (to scale), one cell per step, the beats marked
  const perBeat = data && data.meter ? Math.max(1, Math.round(data.meter.beat / l.step)) : 4;
  const cell = i => html`<span key=${i} class=${i % perBeat === 0 ? "b" : ""}></span>`;
  // where the groove fits, the steps are grouped by its length: each group, underlined, is one turn of the groove
  const n = g && l.fits ? g.values.length : 0;
  let bar;
  if (n) {
    bar = [];
    for (let k = 0; k * n < l.steps; k++) {
      const cs = [];
      for (let i = k * n; i < Math.min((k + 1) * n, l.steps); i++) cs.push(cell(i));
      bar.push(html`<span key=${k} class="cycle ${k % 2 ? "odd" : ""}" style=${`flex:${cs.length}`}
        title=${`steps ${k * n + 1}–${k * n + cs.length}: the groove's turn ${k + 1} of ${l.steps / n}`}>${cs}</span>`);
    }
  } else {
    bar = Array.from({ length: l.steps || 0 }, (_, i) => cell(i));
  }
  return html`
    <div class="len-row ${plays ? "plays" : ""}">
      <span class="track-num ${t.kind === "drums" ? "drums" : ""}">${l.track}</span>
      <span class="len-name" title=${t.instrument || ""}>${t.instrument || t.name}</span>
      <div class="len-bar-box">
        <div class="len-bar ${n ? "cycles" : ""}" style=${`width:${(100 * (l.beats || 0)) / max}%`}>${bar}</div>
      </div>
      <span class="pattern">
        <input type="number" min="1" max=${lim ? lim.max_steps : 64} step="1" value=${l.steps}
          aria-label=${`Steps of track ${l.track}`} onChange=${onSteps} disabled=${!lim} />
        <span>×</span>
        <select value=${l.name} aria-label=${`Step length of track ${l.track}`} disabled=${!lim}
          onChange=${e => set({ div: e.target.value }, "Step length")}>
          ${divs.map(x => html`<option key=${x} value=${x}>${x}</option>`)}</select></span>
      <span class="len-groove">
        ${!g ? null : use.all
          ? html`<span class="tag ${plays ? "ok" : ""}">${plays ? "groove" : "straight"}</span>`
          : html`<label class="check" title=${l.fits ? "Play the groove on this track"
              : "Its length does not fit the groove (its step length, a multiple of its steps)"}>
              <input type="checkbox" checked=${use.tracks.includes(l.track)} disabled=${!l.fits && !use.tracks.includes(l.track)}
                onChange=${e => useGroove({ track: l.track, on: e.target.checked }, "Apply to track")} />
              apply groove</label>`}
      </span>
    </div>`;
}

// ---------------------------------------------------------------- the groove's steps
function GrooveEditor({ st, gs }) {
  const g = gs.groove;
  const editable = !gs.blocked;
  const steps = gs.grid ? gs.grid.steps : g ? g.values.length : 16;
  const server = g ? g.values : new Array(steps).fill(0);
  const draft = useSignal(null);  // what is set here and not yet on the device
  if (draft.value && same(draft.value, server)) draft.value = null;  // (the device has it now)
  const values = draft.value || server;
  const n = values.length;
  const reach = Math.round(range.value * RES / 100);
  const lo = -reach, hi = Math.min(reach, RES / 2 - 1);
  const divisors = Array.from({ length: Math.min(steps, 64) }, (_, i) => i + 1).filter(k => k >= 2 && steps % k === 0);

  const send = async next => {
    draft.value = next;
    await act("/api/ui/groove", { values: next, res: RES }, "Groove");
    if (draft.value === next) draft.value = null;  // (the device's answer is shown from now on)
  };
  const setStep = (i, v) => send(values.map((x, k) => (k === i ? v : x)));
  const show = (i, v) => (draft.value = values.map((x, k) => (k === i ? v : x)));
  const setLength = len => len !== n && send(Array.from({ length: len }, (_, i) => values[i % n]));  // (longer: repeats)

  // the steps where some track has notes (folded onto the groove's length)
  const notes = new Set();
  for (const t of st.tracks) {
    const d = trackData.value[t.track];
    for (const e of (d && d.events) || []) if (e.step != null && e.notes.length && !e.muted) notes.add(e.step % n);
  }
  const d1 = trackData.value[st.tracks[0].track];
  const perBeat = d1 && d1.meter && gs.grid ? Math.max(1, Math.round(d1.meter.beat / gs.grid.step)) : 4;
  const off = g && g.step && gs.grid && g.step !== gs.grid.step;

  return html`
    <div class="card groove ${editable ? "" : "locked"}">
      <div class="row">
        <strong>Groove</strong>
        <span class="tag">${g ? `${g.values.length} steps` : "straight"}</span>
        <label class="mix">Preset
          <select value=${gs.preset || ""} disabled=${!editable}
            title="A feel measured in recordings (GROOVES.md); greyed out: it does not go onto the tracks' steps"
            onChange=${e => act("/api/ui/groove", e.target.value ? { preset: e.target.value } : { custom: true },
              e.target.value ? "Preset" : "Custom")}>
            <option value="" disabled=${!g}>${g ? "Custom" : "Choose…"}</option>
            ${gs.presets.map(p => html`<option key=${p.name} value=${p.name} disabled=${!p.fits}
              title=${`${p.description}. ${p.source}`}>${`${p.label} (${p.compat})`}</option>`)}
          </select></label>
        <label class="mix">Steps
          <select value=${n} disabled=${!editable} onChange=${e => setLength(Number(e.target.value))}
            title=${`The groove's length: it divides every track's length (up to ${steps} steps), so it repeats evenly on each`}>
            ${(divisors.includes(n) ? divisors : [n, ...divisors]).map(k => html`<option key=${k} value=${k}>${k}</option>`)}
          </select></label>
        <label class="mix">Range
          <select value=${range.value} onChange=${e => setRange(Number(e.target.value))}
            title="How far a slider reaches either way, in % of a step">
            ${RANGES.map(r => html`<option key=${r} value=${r}>±${r}%</option>`)}</select></label>
        <span class="spacer"></span>
        <button class="ghost" disabled=${!g || !values.some(v => v !== 0)}
          title="Every step to 0% (as a double-click on each percentage); the groove's length and its tracks stay"
          onClick=${() => { draft.value = null; act("/api/ui/groove", { straight: true }, "Straight"); }}>Straight</button>
      </div>
      ${(() => {
        const p = gs.presets.find(x => x.name === gs.preset);
        return p && html`<p class="preset-note"><strong>${p.label}</strong>: ${p.description}. <em>${p.source}</em>.</p>`;
      })()}
      ${!editable && html`<p class="notes">The steps can be changed while every track has the same step length: make
        them equal above.</p>`}
      ${editable && off && html`<p class="notes">This groove was made for steps of ${g.step} beats; the tracks now have
        ${gs.grid.name} steps. Moving a slider makes it a groove for ${gs.grid.name}.</p>`}
      <div class="groove-grid">
        ${values.map((v, i) => html`
          <div key=${i} class="groove-step ${i % perBeat === 0 ? "beat" : ""} ${notes.has(i) ? "has" : ""}">
            <span class="groove-pct ${v > 0 ? "late" : v < 0 ? "early" : ""}" title=${editable ? "Double-click: 0" : ""}
              onDblClick=${() => editable && v !== 0 && setStep(i, 0)}>${pct(v)}</span>
            <input type="range" min=${lo} max=${hi} step="1" value=${Math.max(lo, Math.min(hi, v))} disabled=${!editable}
              aria-label=${`Groove step ${i + 1}: ${pct(v)}`}
              onInput=${e => show(i, +e.target.value)} onChange=${e => setStep(i, +e.target.value)} />
            <span class="groove-num">${i + 1}</span>
          </div>`)}
      </div>
    </div>`;
}
