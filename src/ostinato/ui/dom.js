// DOM helpers: elements (h), SVG elements (s), a selector ($). For the parts of the page that build their DOM directly.

// the first element that matches a CSS selector
export const $ = (sel, el = document) => el.querySelector(sel);
// an HTML element: attributes ("text" is its text, "on..." a listener), then its children
export const h = (tag, attrs = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? "" : v);
  }
  // children: elements as they are, anything else as text
  for (const c of kids.flat()) if (c != null) e.append(c.nodeType ? c : document.createTextNode(String(c)));
  return e;
};
const SVG = "http://www.w3.org/2000/svg";
// an SVG element, the same way (no listeners)
export const s = (tag, attrs = {}, ...kids) => {
  const e = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) e.setAttribute(k, v);
  for (const c of kids) if (c != null) e.append(c);
  return e;
};

