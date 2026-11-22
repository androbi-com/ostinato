// The Tracks tab: a card per track with its sound, mix, notes (piano roll or staff), algorithms and generative mode.
import { useEffect, useLayoutEffect, useRef, useState } from "preact/hooks";
import { signal, useSignal } from "@preact/signals";
import { api } from "./api.js";
import { html } from "./html.js";
import { drawRoll } from "./roll.js";
import {
  act, algoChoice, can, collapsed, controlChoice, isVirtual, notesVersion, released, setKey, state, toggleTrack, trackData,
  views,
} from "./store.js";

// the cards, or why there are none
export function Tracks() {
  const st = state.value;
  if (!st) return null;
  if (!st.tracks.length) {
    return html`
      <div class="notice">
        <p>${
          st.status.connected ? "The device is still being read."
          : isVirtual(st) ? "The virtual device stopped. Restart starts it again; its patterns are kept."
          : released(st) ? "The device is released. Reconnect takes it back (e.g. after its own editor)."
          : "No device detected yet. Plug it in by USB; the device server looks for it every few seconds."}</p>
        ${!st.status.connected && !isVirtual(st) && html`
          <p>No device at hand? The virtual device has four tracks of up to 64 steps and plays them on this computer with
            General MIDI sounds.</p>
          <button class="primary" onClick=${() => act("/api/ui/virtual", {}, "Virtual device")}>Use the virtual device</button>`}
      </div>`;
  }
  return st.tracks.map(t => html`<${TrackCard} key=${t.track} t=${t} st=${st} />`);
}

// one track: its head (sound, tags, view buttons) and its body (actions, notes)
function TrackCard({ t, st }) {
  const folded = !!collapsed.value[t.track];
  const view = views.value[t.track] || "roll";
  const data = trackData.value[t.track];
  const known = data && !data.error;
  // the Steps / Staff buttons
  const viewButton = (name, label) => html`
    <button aria-pressed=${view === name} onClick=${() => setKey(views, t.track, name)}>${label}</button>`;
  return html`
    <article class="track ${t.kind} ${folded ? "collapsed" : ""}" id=${`track-${t.track}`}>
      <!-- the head: fold, number, sound, tags, view -->
      <div class="track-head">
        <button class="fold" aria-expanded=${!folded} aria-controls=${`track-body-${t.track}`}
          title=${folded ? "Show this track" : "Hide this track"} onClick=${() => toggleTrack(t.track)}>
          <svg class="tri" viewBox="0 0 12 12" width="12" height="12" aria-hidden="true">
            <path d="M1.5 3.5 L10.5 3.5 L6 9.5 Z" fill="currentColor" /></svg></button>
        <span class="track-num">${t.track}</span>
        ${can("sounds") ? html`<${SoundSelect} t=${t} />` : html`<span class="track-title">${t.instrument || t.name}</span>`}
        <span class="tag">${t.kind}</span>
        ${t.key && html`<span class="tag key">${t.key}</span>`}
        ${known && html`<${Pattern} t=${t} st=${st} data=${data} />`}
        ${t.muted && html`<span class="tag">MUTED</span>`}
        <span class="spacer"></span>
        <div class="seg" role="group">${viewButton("roll", "Steps")}${viewButton("staff", "Staff")}</div>
      </div>
      <!-- the body: buttons, then the notes -->
      <div class="track-body" id=${`track-body-${t.track}`}>
        <div class="track-actions"><${TrackActions} t=${t} st=${st} /></div>
        ${!folded && (view === "staff" ? html`<${Staff} track=${t.track} />` : html`<${Roll} data=${data} />`)}
      </div>
    </article>`;
}

// The pattern's length (steps, step length) where the device can be written to, and its time signature.
function Pattern({ t, st, data }) {
  const armed = st.armed.some(a => a.track === t.track);
  const set = (what, body, label) => act(`/api/ui/track/${t.track}/${what}`, body, label);
  const [steps, , commitSteps] = usePending(data.grid ? data.grid.steps : 0, v => set("length", { steps: v }, "Steps"));
  const [div, , commitDiv] = usePending(data.grid ? data.grid.name : "", v => set("length", { div: v }, "Step length"));
  const [sig, , commitSig] = usePending(data.signature, v => set("signature", { signature: v }, "Time signature"));
  const lim = data.limits;
  // a number in the range is sent; anything else is put back
  const onSteps = e => {
    const v = Number(e.target.value);
    if (e.target.value !== "" && Number.isInteger(v) && v >= 1 && (!lim.max_steps || v <= lim.max_steps)) {
      if (v !== data.grid.steps) commitSteps(v);
    } else e.target.value = data.grid.steps;
  };
  const divs = lim ? (lim.grids.includes(div) ? lim.grids : [div, ...lim.grids]) : [];
  return html`
    <!-- the length -->
    ${!data.grid ? html`<span class="tag">${data.length} beats</span>`
      : !can("writer") || !lim ? html`<span class="tag">${`${data.grid.steps} × ${data.grid.name}`}</span>`
      : html`<span class="pattern">
          <input type="number" inputmode="numeric" min="1" max=${lim.max_steps} step="1" value=${steps} disabled=${armed}
            aria-label=${`Steps of track ${t.track}`} title="Steps in the pattern (shorter: the notes past the end go; Undo brings them back)"
            onChange=${onSteps} />
          <span>×</span>
          <select value=${div} disabled=${armed} aria-label=${`Step length of track ${t.track}`}
            title="Step length (the notes stay on their steps)"
            onChange=${e => commitDiv(e.target.value)}>
            ${divs.map(x => html`<option key=${x} value=${x}>${x}</option>`)}</select></span>`}
    <!-- the time signature -->
    ${data.meter && html`
      <select class="signature" value=${sig} aria-label=${`Time signature of track ${t.track}`}
        title="Time signature: bars of the staff and the steps (the notes do not change). auto: by the length; 6/8 is never guessed"
        onChange=${e => commitSig(e.target.value)}>
        <option value="auto">${sig === "auto" ? `auto (${data.meter.name})` : "auto"}</option>
        ${st.signatures.map(x => html`<option key=${x} value=${x}>${x}</option>`)}</select>`}`;
}

// What the user just set, shown until the server's answer is in: a poll meanwhile must not put the old value back.
// -> [the value to show, show this value, send it]
function usePending(value, send) {
  const pending = useSignal(undefined);
  const show = v => (pending.value = v);
  // show it now, send it, then follow the device again
  const commit = async v => {
    pending.value = v;
    await send(v);
    if (pending.value === v) pending.value = undefined;  // (else: changed again meanwhile)
  };
  return [pending.value === undefined ? value : pending.value, show, commit];
}

// ---------------------------------------------------------------- the notes
// the piano roll, drawn again only when the notes or the width change
function Roll({ data }) {
  const box = useRef(null);
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {  // (the roll fits the card's width)
    const measure = () => box.current && setWidth(Math.round(box.current.clientWidth));
    const ob = new ResizeObserver(measure);
    ob.observe(box.current);
    measure();
    return () => ob.disconnect();
  }, []);
  // draw it, or clear it when there is nothing to draw
  const drawn = !!(data && !data.error && data.events.length);
  useLayoutEffect(() => {
    if (!drawn) box.current.replaceChildren();
    else if (width) box.current.replaceChildren(drawRoll(data, width - 32));  // (the padding)
  }, [data, width, drawn]);
  // what to say instead of a roll
  const text = !data ? "" : data.error ? data.error : drawn ? "" : "No notes in the pattern.";
  return html`<div class="roll" ref=${box}></div>${text && html`<div class="empty">${text}</div>`}`;
}

// The staff, an image drawn by the server; the old one stays until the new one is there.
function Staff({ track }) {
  const box = useRef(null);
  const [note, setNote] = useState("drawing…");
  const src = `/api/ui/track/${track}/staff.svg?v=${notesVersion.value}`;
  useEffect(() => {
    let current = true;
    const img = new Image();
    img.alt = `Track ${track} as notation`;
    img.onload = () => { if (current) { box.current.replaceChildren(img); setNote(""); } };
    img.onerror = () => { if (current) { box.current.replaceChildren(); setNote("Could not draw the staff."); } };
    img.src = src;
    return () => { current = false; };
  }, [src]);
  return html`<div class="staff"><div ref=${box}></div>${note && html`<div class="loading">${note}</div>`}</div>`;
}

// ---------------------------------------------------------------- the sound
// The sounds a track can take (presets grouped by engine; kits on the drum track): fetched once per device and kind
// of track.
const sounds = signal({});
const asked = {};

// the sounds for this kind of track ([] until they are there; asked for once)
function loadSounds(t) {
  const kind = `${state.value.status.backend} ${t.kind === "drums" ? "drums" : "synth"}`;
  asked[kind] ||= api(`/api/ui/track/${t.track}/sounds`)
    .then(r => setKey(sounds, kind, r.sounds)).catch(() => { delete asked[kind]; });
  return sounds.value[kind] || [];
}

// the sound menu, grouped, with the track's sound chosen
function SoundSelect({ t }) {
  const [shown, , commit] = usePending(null, v => {
    const [group, name] = JSON.parse(v);
    return act(`/api/ui/track/${t.track}/sound`, { name, group }, "Sound");
  });
  const cur = t.instrument || "";
  // sort the sounds into their groups, and find the one the track has
  const groups = {};
  let found = "";
  for (const x of loadSounds(t)) {
    const value = JSON.stringify([x.group, x.name]);
    (groups[x.group] ||= []).push({ value, name: x.name });
    if (!found && (cur === `${x.group} ${x.name}` || cur === x.name || (x.group === "KIT" && cur.endsWith(` ${x.name}`)))) found = value;
  }
  return html`
    <select class="track-title sound" aria-label=${`Sound of track ${t.track}`} title="Choose this track's sound"
      value=${shown ?? found} onChange=${e => e.target.value && commit(e.target.value)}>
      ${!found && html`<option value="">${cur || t.name}</option>`}
      ${Object.entries(groups).map(([g, list]) => html`
        <optgroup key=${g} label=${g}>${list.map(x => html`<option key=${x.value} value=${x.value}>${x.name}</option>`)}</optgroup>`)}
    </select>`;
}

// ---------------------------------------------------------------- the mixer
// a level as text: dB where the device has a dB scale, else the number
function levelText(m, v) {
  if (m.db_zero == null || !m.db_step) return String(v);
  return v <= m.min ? "OFF" : `${((v - m.db_zero) * m.db_step).toFixed(1)}dB`;
}

// a pan as text: C, L30, R50
function panText(m, v) {
  const pct = Math.round((v * 100) / Math.max(Math.abs(m.min), Math.abs(m.max)));
  return pct === 0 ? "C" : pct < 0 ? `L${-pct}` : `R${pct}`;
}

// a slider on the device's own scale; it is sent when let go (not while dragging)
function MixSlider({ t, what, label, text }) {
  const m = t[what];
  const [value, show, commit] = usePending(m.value, v => act(`/api/ui/track/${t.track}/mix`, { [what]: v }, label));
  const letGo = e => { if (+e.target.value === m.value) show(undefined); };  // (back where it was: nothing is sent)
  return html`
    <label class="mix ${what}"><span class="mix-label">${label}</span>
      <input type="range" min=${m.min} max=${m.max} step="1" value=${value}
        aria-label=${`${label} of track ${t.track}`}
        title=${`${label}: ${m.text}` + (what === "pan" ? " (double-click: center)" : "")}
        onInput=${e => show(+e.target.value)} onChange=${e => commit(+e.target.value)}
        onPointerUp=${letGo} onBlur=${letGo}
        onDblClick=${() => what === "pan" && commit(0)} />
      <span class="mix-value">${text(m, value)}</span></label>`;
}

// ---------------------------------------------------------------- the actions
// melodic_swapper -> melodic swapper
const spaced = name => name.replace(/_/g, " ");

// the row of buttons: mix, vary, undo, clear, generative mode (as the device can)
function TrackActions({ t, st }) {
  const armed = st.armed.find(a => a.track === t.track);
  const post = (what, body, label) => act(`/api/ui/track/${t.track}/${what}`, body, label);
  // the algorithms for this kind of track, and the one chosen
  const algos = st.algorithms.filter(a => a.kinds.includes(t.kind));
  const algo = (algos.find(a => a.name === algoChoice.value[t.track]) || algos[0] || {}).name;
  return html`
    <!-- mute, level, pan -->
    ${can("mixer") && html`
      <button class=${t.muted ? "primary" : "ghost"} aria-pressed=${!!t.muted}
        title=${t.muted ? "Unmute this track" : "Mute this track"}
        onClick=${() => post("mute", { muted: !t.muted }, t.muted ? "Unmute" : "Mute")}>${t.muted ? "Unmute" : "Mute"}</button>
      ${t.level && html`<${MixSlider} t=${t} what="level" label="Level" text=${levelText} />`}
      ${t.pan && html`<${MixSlider} t=${t} what="pan" label="Pan" text=${panText} />`}`}
    ${!can("writer")
      ? !can("mixer") && html`<span class="empty">This device cannot be written to.</span>`
      : html`
        ${algo && html`<${Vary} t=${t} algos=${algos} algo=${algo} armed=${armed} post=${post} />`}
        <button class="ghost" disabled=${!t.undo} title="Undo the last write to this track"
          onClick=${() => post("undo", {}, "Undo")}>${t.undo ? `Undo (${t.undo})` : "Undo"}</button>
        <button class="ghost" disabled=${!!armed} title="Remove all notes of this track (Undo brings them back)"
          onClick=${() => confirm(`Clear all notes of track ${t.track}?`) && post("clear", {}, "Clear")}>Clear</button>
        <!-- generative mode -->
        ${can("controls") && algo && html`<span class="sep"></span>
          ${armed ? html`<${Armed} armed=${armed} post=${post} />`
                  : html`<${Arm} t=${t} st=${st} algo=${algo} post=${post} />`}`}`}`;
}

// choose an algorithm and run it once
function Vary({ t, algos, algo, armed, post }) {
  const initialize = algos.some(a => a.name === algo && a.initializes);
  return html`
    <div class="group">
      <select aria-label="Algorithm" value=${algo} onChange=${e => setKey(algoChoice, t.track, e.target.value)}>
        ${algos.map(a => html`<option key=${a.name} value=${a.name} title=${a.description}>${spaced(a.name)}</option>`)}
      </select>
      <button class="ghost" disabled=${!!armed}
        title=${initialize ? "Replace the sequence with generated notes" : "Run the algorithm once"}
        onClick=${() => post("generate", { algorithm: algo }, initialize ? "Initialize" : "Vary")}>
        ${initialize ? "Initialize" : "Vary"}</button>
    </div>`;
}

// generative mode: not armed
function Arm({ t, st, algo, post }) {
  const ctrls = st.controls;
  // the control chosen here, else the first knob
  const preferred = ctrls.find(c => c.name === controlChoice.value[t.track]) || ctrls.find(c => c.kind === "encoder") || ctrls[0];
  const control = (preferred || {}).name;
  return html`
    <div class="group">
      <select aria-label="Control" value=${control} onChange=${e => setKey(controlChoice, t.track, e.target.value)}>
        ${ctrls.map(c => html`<option key=${c.name} value=${c.name} title=${c.description}>${`${c.name} (${c.kind})`}</option>`)}
      </select>
      <button class="ghost" title="Generative mode: the control drives the algorithm on the device"
        onClick=${() => post("arm", { algorithm: algo, control }, "Arm")}>Arm</button>
    </div>`;
}

// generative mode: armed
function Armed({ armed, post }) {
  // how to use the control, and what the last run changed
  const how = armed.kind === "encoder" ? "turn it clockwise to vary, back to undo" : "each press runs once";
  const last = armed.last && armed.last.length ? ` · last: ${armed.last.join("; ")}` : "";
  return html`
    <span class="armed-chip"><span class="pulse"></span>${`${spaced(armed.algorithm)} on ${armed.control}`}</span>
    <button class="small" title="Stop; keep the pattern as it is" onClick=${() => post("disarm", {}, "Disarm")}>Disarm</button>
    <button class="small" title="Stop and bring back the pattern from before arming"
      onClick=${() => post("disarm", { restore: true }, "Restore")}>Restore</button>
    <div class="armed-info">${`${armed.runs} run(s), ${armed.depth} undoable · ${how}${last}`}</div>`;
}
