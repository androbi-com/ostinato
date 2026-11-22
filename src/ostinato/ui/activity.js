// The Activity tab: the session's change log, newest first.
import { api } from "./api.js";
import { $, h } from "./dom.js";

let last = 0;  // the newest entry shown

// fetch what is new and put it on top
export async function loadLog() {
  let r;
  try { r = await api(`/api/ui/log?since=${last}`); } catch (e) { return; }  // (shown by the header)
  const ol = $("#log");
  for (const e of r.events) {
    if (e.version <= last) continue;  // (a load at the same time brought it)
    last = e.version;
    ol.prepend(h("li", { class: "src-" + e.source },
      h("span", { class: "t", text: new Date(e.at * 1000).toLocaleTimeString() }),
      h("span", { class: "tr", text: e.track ? `track ${e.track}` : "" }),
      h("span", { class: "k", text: e.kind }), h("span", { text: e.text })));
  }
  // keep the newest 300
  while (ol.children.length > 300) ol.lastChild.remove();
  last = Math.max(last, r.version);
}
