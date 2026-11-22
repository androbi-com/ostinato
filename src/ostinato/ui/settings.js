// The Settings tab: a form for every setup, generated from the defaults files and their _ui hints.
import { api, toast } from "./api.js";
import { $, h } from "./dom.js";
import { state } from "./store.js";

export async function loadSetups() {
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
    if (x === "#track") (state.value ? state.value.tracks : []).forEach(t => out.push(t.track));
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

