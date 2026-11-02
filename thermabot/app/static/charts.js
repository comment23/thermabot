// thermabot charts (v0.6.0): thin helpers over the vendored uPlot. Responsive (ResizeObserver),
// colours from the CSS design tokens (re-drawn when light/dark changes), a tooltip that also
// follows touch, and loading/empty/error states. Every chart has a <details> table fallback.
// Relative URLs only (HA ingress). No external requests.
(() => {
  const root = document.documentElement;
  const timeZone = root.dataset.timezone;
  const zoneParts = new Intl.DateTimeFormat('en-US', { timeZone, year: 'numeric', month: 'numeric',
    day: 'numeric', hour: 'numeric', minute: 'numeric', second: 'numeric', hourCycle: 'h23' });
  const css = (n) => getComputedStyle(root).getPropertyValue(n).trim();
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const json = (id) => JSON.parse(document.getElementById(id).textContent);

  function chartDate(ts) {
    const instant = new Date(ts * 1000);
    const p = Object.fromEntries(zoneParts.formatToParts(instant)
      .filter((x) => x.type !== 'literal').map((x) => [x.type, Number(x.value)]));
    const midnight = Date.UTC(p.year, p.month - 1, p.day);
    const civil = new Date(Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second,
      instant.getUTCMilliseconds()));
    const local = new Date(new Date(p.year, p.month - 1, p.day).getTime() + civil.getTime() - midnight);
    // uPlot uses local Date getters and local-midnight arithmetic; avoid the viewer's DST gaps.
    for (const field of ['FullYear', 'Month', 'Date', 'Day', 'Hours', 'Minutes', 'Seconds', 'Milliseconds'])
      local['get' + field] = () => civil['getUTC' + field]();
    return local;
  }

  function pal() {
    const p = {};
    for (const k of ['text', 'text-muted', 'line', 'heat', 'cool', 'fan', 'outdoor', 'band', 'accent', 'good', 'bad', 'card'])
      p[k.replace('-muted', 'Muted')] = css('--tb-' + k);
    p.rooms = ['#8b5cf6', '#0e9488', '#d9468f', '#b7791f', '#4f6bed'];
    return p;
  }
  function alpha(hex, a) {
    const m = /^#([0-9a-f]{6})$/i.exec(hex);
    if (!m) return hex;
    const n = parseInt(m[1], 16);
    return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`;
  }
  function axes(p, yLabel, extra) {
    const ax = { stroke: p.textMuted, grid: { stroke: alpha(p.line.length === 7 ? p.line : '#888888', 0.9), width: 1 },
      ticks: { stroke: p.line, width: 1 }, font: '12px system-ui, sans-serif' };
    return [{ ...ax }, { ...ax, label: yLabel, labelFont: '12px system-ui, sans-serif', size: 46 }, ...(extra || [])];
  }

  // tooltip: follows the mouse and touch (tap or drag sideways); legend values stay off
  function tooltip(rows) {
    let tip;
    return {
      hooks: {
        init: (u) => {
          tip = document.createElement('div');
          tip.className = 'chart-tip';
          tip.hidden = true;
          u.over.appendChild(tip);
          const touch = (e) => {
            const t = e.touches[0];
            if (!t) return;
            const r = u.over.getBoundingClientRect();
            u.setCursor({ left: t.clientX - r.left, top: t.clientY - r.top });
          };
          u.over.addEventListener('touchstart', touch, { passive: true });
          u.over.addEventListener('touchmove', touch, { passive: true });
          u.over.addEventListener('mouseleave', () => { tip.hidden = true; });
        },
        setCursor: (u) => {
          const i = u.cursor.idx;
          const r = i == null ? null : rows(u, i);
          if (!r) { tip.hidden = true; return; }
          tip.innerHTML = '<b>' + esc(r.title) + '</b>' + r.items.filter((x) => x.value != null)
            .map((x) => `<i style="background:${x.color}"></i>${esc(x.label)}: ${esc(x.value)}`).join('<br>');
          tip.hidden = false;
          const w = tip.offsetWidth, ow = u.over.clientWidth, left = u.cursor.left;
          tip.style.left = (left + 14 + w > ow ? Math.max(0, left - w - 14) : left + 14) + 'px';
        },
      },
    };
  }

  // build(width, palette) -> [opts, data]; re-built on theme change, resized on width change
  function mount(el, build) {
    let u = null;
    const height = () => (el.clientWidth < 600 ? 220 : 280);
    const make = () => {
      if (u) u.destroy();
      el.querySelectorAll('.chart-state').forEach((n) => n.remove());
      const [opts, data] = build(el.clientWidth || 320, pal());
      u = new uPlot({ width: el.clientWidth || 320, height: height(),
        tzDate: chartDate,
        legend: { live: false }, ...opts }, data, el);
      el.dataset.ready = '1';
    };
    make();
    let lastW = el.clientWidth;
    if (window.ResizeObserver) {
      new ResizeObserver(() => {
        const w = el.clientWidth;
        if (u && w > 0 && Math.abs(w - lastW) > 1) { lastW = w; u.setSize({ width: w, height: height() }); }
      }).observe(el);
    }
    document.addEventListener('tb:theme', make);
    return { destroy: () => { document.removeEventListener('tb:theme', make); if (u) u.destroy(); u = null; } };
  }

  function state(el, kind, msg) {
    el.querySelectorAll('.chart-state').forEach((n) => n.remove());
    const p = document.createElement('p');
    p.className = 'chart-state';
    p.dataset.state = kind;
    p.setAttribute('role', kind === 'error' ? 'alert' : 'status');
    p.textContent = msg;
    el.appendChild(p);
    el.dataset.state = kind;
    return p;
  }

  const fmtT = (s, long) => new Date(s * 1000).toLocaleString([], long
    ? { timeZone, weekday: 'short', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }
    : { timeZone, weekday: 'short', hour: '2-digit', minute: '2-digit' });
  const deg = (v, u) => (v == null ? null : (Math.round(v * 10) / 10) + '°' + u);
  const toUnits = (arr, u) => (u === 'C' ? arr.map((v) => (v == null ? null : Math.round((v - 32) * 50 / 9) / 10)) : arr);

  // ---- run shading (heat/cool/fan fraction per bucket), drawn under the lines -------------
  function shading(get) {
    return {
      hooks: {
        drawAxes: (u) => {
          const { t, step, runs } = get();
          const ctx = u.ctx, top = u.bbox.top, h = u.bbox.height;
          ctx.save();
          for (const [arr, color] of runs) {
            for (let i = 0; i < t.length; i++) {
              const f = arr[i];
              if (!f) continue;
              const x0 = u.valToPos(t[i], 'x', true), x1 = u.valToPos(t[i] + step, 'x', true);
              ctx.fillStyle = alpha(color, 0.08 + 0.32 * f);
              ctx.fillRect(x0, top, Math.max(1, x1 - x0), h);
            }
          }
          ctx.restore();
        },
      },
    };
  }

  // ---- dashboard: live history from GET api/history?range= --------------------------------
  function historyLive(card) {
    const el = card.querySelector('[data-chart]');
    const table = card.querySelector('[data-testid=history-table] tbody');
    const units = card.dataset.units || 'F';
    let chart = null, req = 0, last = null;
    const btns = card.querySelectorAll('[data-range]');

    function fillTable() {
      if (!last || !card.querySelector('details.fallback').open) return;
      const d = last, u = units, rows = [];
      for (let i = d.t.length - 1; i >= 0; i--) {
        if (!d.n[i]) continue;
        const pct = (x) => (x == null ? '-' : Math.round(x * 100) + '%');
        rows.push(`<tr><td>${esc(fmtT(d.t[i], true))}</td><td data-label="Indoor">${esc(deg(d.indoor[i], u) || '-')}</td>`
          + `<td data-label="Outdoor">${esc(deg(d.outdoor[i], u) || '-')}</td><td data-label="Heat to">${esc(deg(d.heat_sp[i], u) || '-')}</td>`
          + `<td data-label="Cool to">${esc(deg(d.cool_sp[i], u) || '-')}</td><td data-label="Heat">${pct(d.heat[i])}</td>`
          + `<td data-label="Cool">${pct(d.cool[i])}</td><td data-label="Fan">${pct(d.fan[i])}</td></tr>`);
      }
      table.innerHTML = rows.join('') || '<tr><td colspan="8">No samples in this range.</td></tr>';
    }
    card.querySelector('details.fallback').addEventListener('toggle', fillTable);

    async function load(rng) {
      const my = ++req;
      btns.forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.range === rng)));
      if (chart) { chart.destroy(); chart = null; }
      el.dataset.ready = '';
      el.dataset.range = rng;
      state(el, 'loading', 'Loading history…');
      let res;
      try { res = await window.tb.api('GET', 'api/history?range=' + encodeURIComponent(rng)); } catch (e) { res = { ok: false, status: 0 }; }
      if (my !== req) return;
      if (!res.ok) {
        const p = state(el, 'error', "Couldn't load the history (" + (res.status ? 'HTTP ' + res.status : 'network error') + '). ');
        const b = document.createElement('button');
        b.type = 'button'; b.className = 'secondary outline'; b.textContent = 'Retry';
        b.addEventListener('click', () => load(rng));
        p.appendChild(b);
        return;
      }
      const d = res.data;
      last = d;
      el.dataset.count = d.count;
      el.dataset.points = d.t.length;
      if (!d.count) {
        state(el, 'empty', 'No samples in this range yet. The bot records one per minute.');
        el.dataset.ready = '1';
        fillTable();
        return;
      }
      const T = { indoor: toUnits(d.indoor, units), outdoor: toUnits(d.outdoor, units),
        heat_sp: toUnits(d.heat_sp, units), cool_sp: toUnits(d.cool_sp, units) };
      const long = rng !== '24h';
      chart = mount(el, (w, p) => {
        const stepped = uPlot.paths.stepped({ align: 1 });
        const series = [{},
          { label: 'Indoor', stroke: p.text, width: 2 },
          { label: 'Outdoor', stroke: p.outdoor, width: 2 },
          { label: 'Heat to', stroke: p.heat, width: 1.5, dash: [6, 4], paths: stepped },
          { label: 'Cool to', stroke: p.cool, width: 1.5, dash: [6, 4], paths: stepped }];
        el.dataset.series = series.slice(1).map((s) => s.label).join(',');
        const runs = [[d.heat, p.heat], [d.cool, p.cool], [d.fan, p.fan]];
        return [{
          series, axes: axes(p, '°' + units), scales: { x: { time: true } },
          cursor: { drag: { x: false, y: false }, points: { size: 7 } },
          plugins: [shading(() => ({ t: d.t, step: d.step_s, runs })), tooltip((u, i) => ({
            title: fmtT(d.t[i], long),
            items: [
              { label: 'Indoor', color: p.text, value: deg(T.indoor[i], units) },
              { label: 'Outdoor', color: p.outdoor, value: deg(T.outdoor[i], units) },
              { label: 'Heat to', color: p.heat, value: deg(T.heat_sp[i], units) },
              { label: 'Cool to', color: p.cool, value: deg(T.cool_sp[i], units) },
              { label: 'Heating', color: p.heat, value: d.heat[i] ? Math.round(d.heat[i] * 100) + '%' : null },
              { label: 'Cooling', color: p.cool, value: d.cool[i] ? Math.round(d.cool[i] * 100) + '%' : null },
              { label: 'Fan', color: p.fan, value: d.fan[i] ? Math.round(d.fan[i] * 100) + '%' : null },
            ] }))],
        }, [d.t, T.indoor, T.outdoor, T.heat_sp, T.cool_sp]];
      });
      el.dataset.state = 'ready';
      fillTable();
    }
    btns.forEach((b) => b.addEventListener('click', () => load(b.dataset.range)));
    load(card.dataset.range || '24h');
  }

  // ---- P2 plan: predicted temperature, setpoints, rooms (estimated rooms dashed) -----------
  function plan(el) {
    const d = json(el.dataset.src);
    mount(el, (w, p) => {
      const stepped = uPlot.paths.stepped({ align: 1 });
      const series = [{}, { label: 'Thermostat (pred)', stroke: p.text, width: 2 },
        { label: 'Heat SP', stroke: p.heat, paths: stepped }, { label: 'Cool SP', stroke: p.cool, paths: stepped }];
      const cols = [d.t, d.pred_t, d.heat_sp, d.cool_sp];
      Object.entries(d.rooms).forEach(([k, v], n) => {
        const est = d.estimated.includes(k);
        series.push({ label: k + (est ? ' (est)' : ''), stroke: p.rooms[n % p.rooms.length], dash: est ? [6, 4] : undefined });
        cols.push(v);
      });
      el.dataset.series = series.length - 1;
      el.dataset.dashed = series.filter((s) => s.dash).length;
      return [{ series, axes: axes(p, '°F'), scales: { x: { time: true } },
        plugins: [tooltip((u, i) => ({ title: fmtT(d.t[i]), items: series.slice(1).map((s, k) => ({
          label: s.label, color: s.stroke, value: deg(cols[k + 1][i], 'F') })) }))] }, cols];
    });
  }

  // ---- P4 history page: daily heat/cool hours -------------------------------------------------
  function daily(el) {
    const d = json(el.dataset.src);
    mount(el, (w, p) => {
      const bars = uPlot.paths.bars({ size: [0.6, 40] });
      const series = [{}, { label: 'Heat h', stroke: p.heat, fill: alpha(p.heat, 0.5), paths: bars },
        { label: 'Cool h', stroke: p.cool, fill: alpha(p.cool, 0.5), paths: bars }];
      return [{ series, axes: axes(p, 'hours'), scales: { x: { time: true } },
        plugins: [tooltip((u, i) => ({ title: new Date(d.t[i] * 1000).toLocaleDateString([], { timeZone }), items: [
          { label: 'Heat', color: p.heat, value: d.heat[i].toFixed(1) + ' h' },
          { label: 'Cool', color: p.cool, value: d.cool[i].toFixed(1) + ' h' }] }))] }, [d.t, d.heat, d.cool]];
    });
  }

  // ---- Forecast: outdoor, expected indoor, setpoint band, runtime bars ----------------------
  function forecast(el) {
    const d = json(el.dataset.src);
    const u = d.units;
    el.dataset.bars = d.heat_min.concat(d.cool_min).filter((m) => m > 0).length;
    mount(el, (w, p) => {
      const bars = uPlot.paths.bars({ size: [0.7, 30] });
      const stepped = uPlot.paths.stepped({ align: 1 });
      const series = [{},
        { label: 'Outdoor', stroke: p.outdoor, width: 2.5 },
        { label: 'Indoor (expected)', stroke: p.text, width: 1.5, dash: [5, 4] },
        { label: 'Heat to', stroke: p.heat, width: 1, paths: stepped },
        { label: 'Cool to', stroke: p.cool, width: 1, paths: stepped },
        { label: 'Heat min', scale: 'min', stroke: p.heat, fill: alpha(p.heat, 0.55), paths: bars, points: { show: false } },
        { label: 'Cool min', scale: 'min', stroke: p.cool, fill: alpha(p.cool, 0.55), paths: bars, points: { show: false } }];
      el.dataset.series = series.slice(1).map((s) => s.label).join(',');
      return [{ series, bands: [{ series: [4, 3], fill: p.band }],
        scales: { x: { time: true }, min: { range: [0, 240] } },
        axes: axes(p, '°' + u, [{ scale: 'min', show: false }]),
        plugins: [tooltip((_, i) => ({ title: fmtT(d.t[i]), items: [
          { label: 'Outdoor', color: p.outdoor, value: deg(d.outdoor[i], u) },
          { label: 'Indoor', color: p.text, value: deg(d.indoor[i], u) },
          { label: 'Heat to', color: p.heat, value: deg(d.heat_sp[i], u) },
          { label: 'Cool to', color: p.cool, value: deg(d.cool_sp[i], u) },
          { label: 'Heat', color: p.heat, value: d.heat_min[i] ? Math.round(d.heat_min[i]) + ' min' : null },
          { label: 'Cool', color: p.cool, value: d.cool_min[i] ? Math.round(d.cool_min[i]) + ' min' : null }] }))] },
        [d.t, d.outdoor, d.indoor, d.heat_sp, d.cool_sp, d.heat_min, d.cool_min]];
    });
  }

  // ---- Efficiency: scatter + fitted curve + 95 % band ---------------------------------------
  function efficiency(el) {
    const d = json(el.dataset.src);
    const color = (p) => (d.season === 'heat' ? p.heat : p.cool);
    // one aligned x axis: point x's and band x's, sorted; nulls where a series has no value
    const xs = [...new Set(d.points.map((q) => q.x).concat(d.band.map((b) => b.x)))].sort((a, b) => a - b);
    const at = (rows, key) => { const m = new Map(rows.map((r) => [r.x, r[key]])); return xs.map((x) => (m.has(x) ? m.get(x) : null)); };
    const pts = at(d.points, 'y'), days = at(d.points, 'day');
    const fit = at(d.band, 'y'), lo = at(d.band, 'lo'), hi = at(d.band, 'hi');
    el.dataset.points = d.points.length;
    el.dataset.fit = d.band.length ? '1' : '0';
    mount(el, (w, p) => {
      const c = color(p);
      const series = [{}, { label: 'Day', stroke: c, fill: alpha(c, 0.75), paths: () => null, points: { show: true, size: 9, fill: alpha(c, 0.75) } }];
      const cols = [xs, pts];
      if (d.band.length) {
        series.push({ label: 'Fit', stroke: c, width: 2.5, spanGaps: true, points: { show: false } },
          { label: '95% low', stroke: 'transparent', spanGaps: true, points: { show: false } },
          { label: '95% high', stroke: 'transparent', spanGaps: true, points: { show: false } });
        cols.push(fit, lo, hi);
      }
      return [{ series, bands: d.band.length ? [{ series: [4, 3], fill: p.band }] : [],
        scales: { x: { time: false }, y: { range: (u, mn, mx) => [Math.min(0, mn), mx * 1.08 + 0.5] } },
        axes: [{ ...axes(p)[0], label: 'mean outdoor °F', labelFont: '12px system-ui, sans-serif', size: 48 }, axes(p, 'min / degree-day')[1]],
        legend: { show: false },
        plugins: [tooltip((u, i) => (pts[i] == null && fit[i] == null ? null : { title: (days[i] || '') + ' ' + xs[i] + ' °F',
          items: [{ label: 'Runtime', color: c, value: pts[i] == null ? null : pts[i].toFixed(1) + ' min/DD' },
            { label: 'Fit', color: c, value: fit[i] == null ? null : fit[i].toFixed(1) + ' (' + lo[i].toFixed(1) + '–' + hi[i].toFixed(1) + ')' }] }))] }, cols];
    });
  }

  // ---- Efficiency: simulated saved minutes per day + cumulative ------------------------------
  function savings(el) {
    const d = json(el.dataset.src);
    const pos = d.saved.map((v) => (v >= 0 ? v : null)), neg = d.saved.map((v) => (v < 0 ? v : null));
    el.dataset.bars = d.saved.length;
    mount(el, (w, p) => {
      const bars = uPlot.paths.bars({ size: [0.7, 40] });
      return [{ series: [{},
        { label: 'Saved', stroke: p.good, fill: alpha(p.good, 0.6), paths: bars, points: { show: false } },
        { label: 'Lost', stroke: p.bad, fill: alpha(p.bad, 0.6), paths: bars, points: { show: false } },
        { label: 'Cumulative', stroke: p.text, width: 2 }],
        axes: axes(p, 'min'), scales: { x: { time: true } },
        plugins: [tooltip((u, i) => ({ title: d.day[i] + ' (simulated)', items: [
          { label: 'Saved', color: d.saved[i] < 0 ? p.bad : p.good, value: Math.round(d.saved[i]) + ' min' },
          { label: 'Cumulative', color: p.text, value: Math.round(d.cum[i]) + ' min' }] }))] },
        [d.t, pos, neg, d.cum]];
    });
  }

  const kinds = { plan, daily, forecast, efficiency, savings };
  function init() {
    if (!window.uPlot) return;
    for (const el of document.querySelectorAll('[data-chart]')) {
      const k = kinds[el.dataset.chart];
      if (!k) continue;
      try { k(el); } catch (e) { state(el, 'error', 'The chart could not be drawn: ' + e.message); }
    }
    document.querySelectorAll('[data-history-card]').forEach(historyLive);
  }
  window.tbCharts = { mount, tooltip, state };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
