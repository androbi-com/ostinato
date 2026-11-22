// The piano roll / drum grid of a track, drawn as SVG.
import { s } from "./dom.js";

const NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
const BLACK = [1, 3, 6, 8, 10];
const LEVEL_OPACITY = { ghost: 0.38, soft: 0.65, normal: 0.88, hard: 1 };

export function drawRoll(d, width) {
  const drums = d.kind === "drums";
  const stepBeats = d.grid ? d.grid.step : 0.25;
  const steps = d.grid ? d.grid.steps : Math.max(1, Math.round(d.length / stepBeats));
  const perBeat = Math.abs(1 / stepBeats - Math.round(1 / stepBeats)) < 1e-6 ? Math.round(1 / stepBeats) : 4;
  const labelW = drums ? 96 : 48;
  // the steps fill the card (at least 12 px each: a long pattern scrolls sideways)
  const cellW = Math.max(12, Math.floor(((width || 1000) - labelW - 4) / steps));
  const named = Object.fromEntries(d.voices.map(v => [v.pitch, v.name]));
  let rows;
  if (drums) rows = d.voices.map(v => v.pitch).sort((a, b) => a - b);
  else {
    const ps = d.voices.map(v => v.pitch);
    let lo = Math.min(...ps) - 2, hi = Math.max(...ps) + 2;
    while (hi - lo < 12) { lo--; hi++; }
    rows = [];
    for (let p = hi; p >= lo; p--) rows.push(p);
  }
  const rowH = drums ? 20 : 13;
  const W = labelW + steps * cellW + 2, H = rows.length * rowH + 18;
  const svg = s("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": `Track ${d.track}: ${d.events.length} notes` });
  const rowOf = Object.fromEntries(rows.map((p, i) => [p, i]));
  const css = getComputedStyle(document.documentElement);
  const col = n => css.getPropertyValue(n).trim();
  // rows and labels
  rows.forEach((p, i) => {
    const y = 18 + i * rowH;
    if (!drums && BLACK.includes(((p % 12) + 12) % 12))
      svg.append(s("rect", { x: labelW, y, width: steps * cellW, height: rowH, fill: col("--black-key") }));
    const label = drums ? (named[p] || String(p)) : (named[p] || (p % 12 === 0 ? NAMES[p % 12] + (Math.floor(p / 12) - 1) : ""));
    if (label) svg.append(s("text", { x: labelW - 6, y: y + rowH * 0.72, "text-anchor": "end",
      "font-size": drums ? 11 : 10, fill: named[p] ? col("--ink") : col("--muted") }, document.createTextNode(label)));
    svg.append(s("line", { x1: labelW, x2: labelW + steps * cellW, y1: y + rowH, y2: y + rowH, stroke: col("--grid") }));
  });
  // steps, beats, bars
  for (let i = 0; i <= steps; i++) {
    const x = labelW + i * cellW;
    const bar = i % (perBeat * 4) === 0, beat = i % perBeat === 0;
    svg.append(s("line", { x1: x, x2: x, y1: 18, y2: H, stroke: bar ? col("--muted") : beat ? col("--beat") : col("--grid"),
      "stroke-width": bar ? 1.2 : 1 }));
    if (beat && i < steps) svg.append(s("text", { x: x + 3, y: 12, "font-size": 10, fill: col("--muted") },
      document.createTextNode(String(i + 1))));
  }
  // notes
  const fillCol = drums ? col("--drum") : col("--note");
  for (const e of d.events) {
    const st = e.step != null ? e.step : e.start / stepBeats;
    const len = drums ? 1 : Math.max(1, e.steps != null ? e.steps : e.length / stepBeats);
    for (const n of e.notes) {
      if (!(n.pitch in rowOf)) continue;
      const x = labelW + st * cellW + 1.5, y = 18 + rowOf[n.pitch] * rowH + 1.5;
      const w = Math.max(4, len * cellW - 3), hh = rowH - 3;
      const g = s("g", {});
      const r = s("rect", { x, y, width: w, height: hh, rx: 3,
        fill: e.muted ? "none" : fillCol, "fill-opacity": LEVEL_OPACITY[n.level] || 0.88,
        stroke: e.accent ? col("--accent") : e.muted ? fillCol : "none",
        "stroke-width": e.accent ? 2 : 1, "stroke-dasharray": e.muted ? "3 2" : null });
      g.append(r);
      for (let k = 1; k < (n.ratchet || 1); k++)
        g.append(s("line", { x1: x + (w * k) / n.ratchet, x2: x + (w * k) / n.ratchet, y1: y + 2, y2: y + hh - 2,
          stroke: col("--panel"), "stroke-width": 1.5 }));
      if (e.slide) g.append(s("path", { d: `M${x + w - 6},${y + hh} L${x + w},${y + 1}`, stroke: col("--accent"),
        "stroke-width": 1.6, fill: "none" }));
      const bits = [n.name, `step ${st + 1}`, !drums && len !== 1 ? `${len} steps` : null, n.level !== "normal" ? n.level : null,
        n.ratchet > 1 ? `×${n.ratchet}` : null, e.accent ? "accent" : null, e.slide ? "slide" : null, e.muted ? "muted" : null,
        n.velocity ? `vel ${n.velocity}` : null,
        ...Object.entries(e.extras || {}).map(([k, v]) => `${k} ${Array.isArray(v) ? v.join(", ") : v}`)];
      g.append(s("title", {}, document.createTextNode(bits.filter(Boolean).join(" · "))));
      svg.append(g);
    }
  }
  return svg;
}

