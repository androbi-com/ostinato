// What the page knows, as signals: a component that reads one is drawn again when it changes. The server is polled
// (poll), the notes are loaded when its log moves on or the tracks change.
import { signal } from "@preact/signals";
import { api, toast } from "./api.js";

export const state = signal(null);       // the last /api/ui/state reply
export const serverError = signal("");   // why the last poll failed ("": it did not)
export const trackData = signal({});     // track number -> its /api/ui/track reply, or { error }
export const notesVersion = signal(0);   // the log version the notes were loaded at (the staff images')

// the user's choices on this page, by track number
export const views = signal({});         // "roll" (default) / "staff"
export const algoChoice = signal({});
export const controlChoice = signal({});
export const collapsed = signal(readCollapsed());  // (remembered in this browser)

// does the device have this capability ("mixer", "writer"...)?
export function can(cap) { return !!state.value && state.value.capabilities.includes(cap); }

// was the device given away on purpose (Release)? Else it is being looked for.
export function released(st) { return !!st.backend && st.status.sync === "disconnected"; }

// a signal holding an object: change one key
export function setKey(sig, key, value) { sig.value = { ...sig.value, [key]: value }; }

// the folded tracks, as this browser remembers them
function readCollapsed() {
  try { return JSON.parse(localStorage.getItem("ostinato.collapsed") || "{}") || {}; } catch (e) { return {}; }
}

// fold / unfold a track, and remember it
export function toggleTrack(track) {
  const next = { ...collapsed.value };
  if (next[track]) delete next[track]; else next[track] = true;
  collapsed.value = next;
  try { localStorage.setItem("ostinato.collapsed", JSON.stringify(next)); } catch (e) { /* (not kept) */ }
}

// ---------------------------------------------------------------- polling
let asked = 0, shown = 0;  // (replies can arrive out of order: an older one than the one shown is dropped)
let loadedFor = "";        // what the notes were last loaded for

// ask the server for its state; load the notes when something happened (or force)
export async function poll(force = false) {
  const n = ++asked;
  let st;
  try {
    st = await api("/api/ui/state");
  } catch (e) {
    if (n > shown) { shown = n; serverError.value = e.message; }
    return;
  }
  // an older reply than the one on the page: drop it
  if (n < shown) return;
  shown = n;
  serverError.value = "";
  state.value = st;
  // the notes are loaded again when the log moved on or the tracks are others
  const key = JSON.stringify([st.log_version, st.tracks.map(t => [t.track, t.kind, t.instrument, t.key])]);
  if (key !== loadedFor || force) {
    loadedFor = key;
    loadNotes(st);  // (not waited for)
  }
}

let loads = 0;
// load every track's notes at once
async function loadNotes(st) {
  const n = ++loads;
  const replies = await Promise.all(st.tracks.map(t =>
    api(`/api/ui/track/${t.track}`).catch(e => ({ error: e.message }))));
  if (n !== loads) return;  // (a newer load is on its way)
  const old = trackData.value, next = {};
  st.tracks.forEach((t, i) => {  // (unchanged notes stay the same object: not drawn again)
    const was = old[t.track];
    next[t.track] = was && JSON.stringify(was) === JSON.stringify(replies[i]) ? was : replies[i];
  });
  trackData.value = next;
  notesVersion.value = st.log_version;
}

// ---------------------------------------------------------------- actions
// Send an action, say what came of it, and wait until the page shows the new state. -> the reply, or undefined.
export async function act(path, body, label) {
  try {
    const r = await api(path, "POST", body);
    if (r.text) toast(r.text);
    for (const w of r.warnings || []) toast(w);
    await poll(true);
    return r;
  } catch (e) {
    toast(`${label || "Failed"}: ${e.message}`, true);
  }
}
