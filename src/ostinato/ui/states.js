// The States tab: save the device, the saved states, apply a loaded one.
import { api, toast } from "./api.js";
import { $, h } from "./dom.js";
import { act } from "./store.js";

export async function loadStates() {
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

