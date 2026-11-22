// Ostinato's browser UI: the start of the page. Talks only to the device server's /api/ui/* (the same session as
// Claude's tools). ES modules, no build step; Preact and htm are in vendor/ (index.html maps their names).
//   store.js   what the page knows (signals), polling, actions      api.js   the HTTP API, toasts
//   header.js, tracks.js (Preact components), roll.js (the piano roll as SVG)
//   states.js, settings.js, activity.js (build their DOM with dom.js)
import { render } from "preact";
import { computed, effect } from "@preact/signals";
import { loadLog } from "./activity.js";
import { $ } from "./dom.js";
import { Header } from "./header.js";
import { html } from "./html.js";
import { loadSetups } from "./settings.js";
import { loadStates } from "./states.js";
import { poll, state } from "./store.js";
import { Tracks } from "./tracks.js";

render(html`<${Header} />`, $("#header"));
render(html`<${Tracks} />`, $("#tab-tracks"));

const logVersion = computed(() => (state.value ? state.value.log_version : 0));
effect(() => { if (logVersion.value) loadLog(); });

let setupsShown = false;
document.querySelectorAll(".tabs button").forEach(b => b.addEventListener("click", () => {
  document.querySelectorAll(".tabs button").forEach(x => x.setAttribute("aria-selected", x === b));
  document.querySelectorAll(".tab").forEach(t => (t.hidden = t.id !== "tab-" + b.dataset.tab));
  if (b.dataset.tab === "settings" && !setupsShown) { setupsShown = true; loadSetups(); }
  if (b.dataset.tab === "states") loadStates();
}));

poll(true);
setInterval(poll, 900);
