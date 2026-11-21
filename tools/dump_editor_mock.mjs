// SPDX-License-Identifier: GPL-3.0-only
// Dumps the SLOOP web editor's own mock device (editor.html, PROTO section) as request -> reply frames,
// used as the reference fixture for the Python simulator (tests/fixtures/editor_mock.json).
// usage: node tools/dump_editor_mock.mjs <path to sloop-fm1/web/editor.html> <out.json>
import { readFileSync, writeFileSync } from "node:fs";
import vm from "node:vm";
const [, , htmlPath, outPath] = process.argv;
const html = readFileSync(htmlPath, "utf8");
const proto = html.slice(html.indexOf("/*PROTO-BEGIN*/"), html.indexOf("/*PROTO-END*/"));
const E = vm.runInNewContext(proto + `;({ parse, req, Link, makeMockDevice, CMD })`,
  { setTimeout, clearTimeout, setInterval, clearInterval, console, TextEncoder, TextDecoder, Blob, Response, DecompressionStream });
const m = E.makeMockDevice({ demo: true });
const inp = [...m.access.inputs.values()][0], out = [...m.access.outputs.values()][0];
const link = new E.Link((d) => out.send(d), { timeout: 300 });
inp.onmidimessage = (e) => link.receive(e.data);
const replies = [];
const rq = async (r) => { const a = await link.request(r); replies.push({ cmd: r[0], args: r[1], reply: Array.from(a) }); return a; };
const info = E.parse[E.CMD.INFO](await rq(E.req.info()));
const sel0 = E.parse[E.CMD.TRACK](await rq(E.req.track())).sel;
const descs = {};
for (let t = 0; t < info.ntrk; t++) {
  await link.request(E.req.track(t));
  descs[t] = [];
  for (let i = 0; i < info.pcount; i++) descs[t].push(Array.from(await link.request(E.req.desc(0, i))));
}
await link.request(E.req.track(sel0));
for (let i = 0; i < info.gcount; i++) await rq(E.req.desc(1, i));
for (let e = 0; e < info.nengines; e++) await rq(E.req.names(e));
await rq(E.req.track());
await rq(E.req.dump());
for (let k = 0; k < 4; k++) await rq(E.req.project(2, k));
for (let t = 0; t < info.ntrk; t++) {
  await rq(E.req.trackDump(t));
  for (let i = 0; i < info.nstep; i++) await rq(E.req.trackStep(t, i));
  await rq(E.req.lockGet(t)); await rq(E.req.microGet(t)); await rq(E.req.fillGet(t));
}
for (let i = 0; i < info.nstep; i++) await rq(E.req.drumStep(i));
writeFileSync(outPath, JSON.stringify({ source: "sloop-fm1 web/editor.html makeMockDevice", descByTrack: descs, replies }));
console.log("ok", info.version, "proto", info.proto, replies.length, "replies");
process.exit(0);
