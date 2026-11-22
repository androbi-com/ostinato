// The header: the device, play / stop, the tempo, release / reconnect.
import { useRef } from "preact/hooks";
import { useSignal } from "@preact/signals";
import { html } from "./html.js";
import { act, released, serverError, state } from "./store.js";

// the line next to the dot: which device, or what the server is doing
function deviceText(st) {
  if (serverError.value) return `the device server does not answer (${serverError.value})`;
  if (!st) return "connecting to the device server…";
  const s = st.status || {};
  if (s.connected) return `${st.backend} · ${s.firmware} · ${s.port}`;
  if (released(st)) return `${st.backend} · released`;
  const why = st.backend ? s.sync : s.scan;
  return `looking for a device… ${why ? `(${why})` : ""}`;
}

// the whole header; drawn again with every poll
export function Header() {
  const st = state.value;
  const connected = !!(st && st.status && st.status.connected);
  // green: connected, amber: released, grey: none
  const dot = serverError.value || !st ? "" : connected ? "on" : released(st) ? "wait" : "";
  return html`
    <div class="brand"><span class="mark" aria-hidden="true">𝄆</span> Ostinato</div>
    <div class="device"><span class="dot ${dot}"></span><span>${deviceText(st)}</span></div>
    <div class="top-actions">
      <!-- play / stop -->
      ${st && st.playing != null && html`
        <button class="ghost play ${st.playing ? "on" : ""}" aria-pressed=${!!st.playing}
          title="Start / stop the device's playback"
          onClick=${() => act("/api/ui/transport", { action: st.playing ? "stop" : "play" }, "Play / stop")}>
          ${st.playing ? "■ Stop" : "▶ Play"}</button>`}
      <!-- the tempo -->
      ${st && st.tempo && html`<${Tempo} st=${st} editable=${!!st.tempo_range && connected} />`}
      <!-- release, or take back -->
      ${st && (connected
        ? html`<button class="ghost" title="Release the device, e.g. for its own editor"
            onClick=${() => act("/api/ui/disconnect", {}, "Release")}>Release</button>`
        : html`<button class="ghost" title="Take the device back"
            onClick=${() => act("/api/ui/connect", {}, "Reconnect")}>Reconnect</button>`)}
    </div>`;
}

// a tempo as text, at most two decimals
const bpmText = v => String(Math.round(v * 100) / 100);

// A number field where the device lets us set the tempo, else only shown. It follows the device, except while it
// holds a number typed here that is not sent yet (the draft).
function Tempo({ st, editable }) {
  const draft = useSignal(null);
  const timer = useRef(0);
  const sending = useRef(false);

  // send after a short pause: the arrows and spin buttons change at every step
  const later = () => { clearTimeout(timer.current); timer.current = setTimeout(send, 250); };
  // send the draft, unless it is empty or the tempo already
  const send = async () => {
    clearTimeout(timer.current);
    const sent = draft.value;
    if (sent === null || sending.current) return;
    if (sent === "" || Number(sent) === state.value.tempo) { draft.value = null; return; }
    sending.current = true;
    await act("/api/ui/tempo", { bpm: Number(sent) }, "Tempo");
    sending.current = false;
    if (draft.value === sent) draft.value = null;
    else if (draft.value !== null) later();  // (changed meanwhile)
  };
  // Enter sends now, Escape puts the device's tempo back
  const onKeyDown = e => {
    if (e.key === "Enter") send();
    else if (e.key === "Escape") {
      clearTimeout(timer.current);
      draft.value = null;
      e.target.value = bpmText(st.tempo);
      e.target.select();
    }
  };

  const note = st.tempo_note || "";
  const r = st.tempo_range;
  return html`
    <label class="tempo ${note ? "noted" : ""}" title=${note}><span>♩ =</span>
      ${editable
        ? html`<input type="number" inputmode="numeric" aria-label="Tempo in BPM"
            min=${r.min} max=${r.max} step=${r.step}
            title=${`Tempo in BPM (${r.min} to ${r.max})` + (note ? ` · ${note}` : "")}
            value=${draft.value ?? bpmText(st.tempo)}
            onInput=${e => (draft.value = e.target.value)} onChange=${later} onBlur=${send} onKeyDown=${onKeyDown} />`
        : html`<span>${bpmText(st.tempo)}</span>`}
    </label>`;
}
