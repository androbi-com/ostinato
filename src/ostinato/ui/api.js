// The device server's JSON API (/api/ui/*), and the short messages at the bottom of the page.
import { $, h } from "./dom.js";

// one request -> the reply's JSON (or text); throws with the server's error text
export async function api(path, method = "GET", body) {
  const opt = { method, headers: {} };
  // writes carry the header the server asks for (see ui_server.py)
  if (method !== "GET") {
    opt.headers["X-Ostinato"] = "1";
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body || {});
  }
  const r = await fetch(path, opt);
  // the login is gone (the device server was restarted): the start page says what to do
  if (r.status === 401) { location.href = "/"; throw new Error("not logged in"); }
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error((data && data.error) || data || r.statusText);
  return data;
}

// a short message at the bottom right; errors stay longer
export function toast(msg, err = false) {
  const t = h("div", { class: "toast" + (err ? " err" : ""), text: msg });
  $("#toasts").append(t);
  setTimeout(() => t.remove(), err ? 7000 : 4000);
}
