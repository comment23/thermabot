// v0.6.0 e2e: WCAG AA contrast of every visible text node (computed; no axe-core).
// Returns a list of failures: ratio, element and text.
() => {
  const lin = (c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
  const lum = (r, g, b) => 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
  const parse = (s) => { const m = s.match(/[\d.]+/g); return m ? m.map(Number) : [0, 0, 0, 0]; };
  function bg(el) {
    const stack = [];
    for (let e = el; e; e = e.parentElement) {
      const c = parse(getComputedStyle(e).backgroundColor);
      const a = c.length > 3 ? c[3] : 1;
      if (getComputedStyle(e).backgroundImage !== 'none' && e !== document.documentElement) return null;
      if (a > 0) { stack.push([c[0], c[1], c[2], a]); if (a >= 1) break; }
    }
    let out = [255, 255, 255];
    for (const [r, g, b, a] of stack.reverse()) out = [r * a + out[0] * (1 - a), g * a + out[1] * (1 - a), b * a + out[2] * (1 - a)];
    return out;
  }
  const bad = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const seen = new Set();
  while (walker.nextNode()) {
    const t = walker.currentNode;
    const el = t.parentElement;
    if (!t.textContent.trim() || seen.has(el)) continue;
    seen.add(el);
    if (el.closest('[hidden], dialog:not([open]), details:not([open]) > :not(summary), script, style, .uplot, option')) continue;
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    if (!r.width || !r.height || cs.visibility === 'hidden' || +cs.opacity === 0) continue;
    if (el.closest('[disabled], fieldset[disabled]')) continue;  // WCAG: disabled controls exempt
    const b = bg(el);
    if (!b) continue;
    const f = parse(cs.color);
    const a = f.length > 3 ? f[3] : 1;
    const fg = [f[0] * a + b[0] * (1 - a), f[1] * a + b[1] * (1 - a), f[2] * a + b[2] * (1 - a)];
    const L1 = lum(...fg), L2 = lum(...b);
    const ratio = (Math.max(L1, L2) + 0.05) / (Math.min(L1, L2) + 0.05);
    const px = parseFloat(cs.fontSize), w = parseInt(cs.fontWeight, 10);
    const large = px >= 24 || (px >= 18.66 && w >= 700);
    if (ratio < (large ? 3 : 4.5)) bad.push(`${ratio.toFixed(2)} ${el.tagName}.${el.className} "${t.textContent.trim().slice(0, 30)}"`);
  }
  return bad;
}
