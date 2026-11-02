// thermabot UI glue: CSRF on every non-GET, SSE with 30 s polling fallback, stop/start dialog,
// JSON forms/buttons that call the existing API only (D§10, D§11). No external requests.
(() => {
  const $ = (s, r = document) => r.querySelector(s);

  // ---- v0.6.0 theme: prefers-color-scheme unless the user picked one (localStorage) ----
  const THEME_KEY = 'thermabot-theme';
  const root = document.documentElement;
  function savedTheme() { try { return localStorage.getItem(THEME_KEY); } catch (_) { return null; } }
  function applyTheme(t) {
    if (t === 'light' || t === 'dark') root.dataset.theme = t; else delete root.dataset.theme;
  }
  applyTheme(savedTheme());
  function currentTheme() {
    return root.dataset.theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  }
  function toggleTheme() {
    const next = currentTheme() === 'dark' ? 'light' : 'dark';
    try { localStorage.setItem(THEME_KEY, next); } catch (_) { /* private mode: this page only */ }
    applyTheme(next);
    document.dispatchEvent(new CustomEvent('tb:theme', { detail: next }));
    labelToggle();
  }
  function labelToggle() {
    const b = $('[data-theme-toggle]');
    if (!b) return;
    const to = currentTheme() === 'dark' ? 'light' : 'dark';
    b.setAttribute('aria-label', 'Switch to ' + to + ' mode');
    b.title = 'Switch to ' + to + ' mode';
    b.dataset.theme = currentTheme();
  }
  if (window.matchMedia) {
    matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
      if (!root.dataset.theme) { document.dispatchEvent(new CustomEvent('tb:theme')); labelToggle(); }
    });
  }
  const csrf = () => ($('meta[name=csrf-token]') || {}).content || '';

  function toast(msg, kind) {
    const t = $('#toast');
    if (!t) return;
    t.textContent = msg;
    t.dataset.kind = kind || 'ok';
    t.hidden = false;
    clearTimeout(t._h);
    t._h = setTimeout(() => { t.hidden = true; }, 8000);
  }

  async function api(method, path, body) {
    let r;
    try {
      r = await fetch(path, {
        method, credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf() },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (_) {
      return { ok: false, status: 0, data: {
        detail: 'Cannot reach Thermabot. No change was confirmed; check status before retrying.',
      } };
    }
    let data = null;
    try { data = await r.json(); } catch (_) { data = null; }
    return { ok: r.ok, status: r.status, data };
  }

  function detail(res) {
    const d = res.data && res.data.detail;
    if (Array.isArray(d)) return d.map((e) => (e.loc || []).join('.') + ': ' + e.msg).join('\n');
    return d || ('HTTP ' + res.status);
  }

  window.tb = { api, toast, detail };
  document.addEventListener('htmx:configRequest', (e) => { e.detail.headers['X-CSRF-Token'] = csrf(); });

  // ---- live state: SSE, else poll /api/status every 30 s (FR-UI-2) ----
  function setState(s) {
    const b = $('#state-badge');
    if (!b || !s || !s.state) return;
    b.dataset.state = s.state;
    b.dataset.label = s.label || s.state;
    b.textContent = s.label || s.state;
    const warning = $('#ha-connection-warning');
    if (warning && s.ha_health) warning.hidden = s.ha_health !== 'down';
  }
  let polling = null;
  async function poll() {
    const r = await api('GET', 'api/status');
    if (r.ok) { setState(r.data); document.dispatchEvent(new CustomEvent('tb:status', { detail: r.data })); }
  }
  function startPolling() {
    if (polling) return;
    document.documentElement.dataset.live = 'poll';
    polling = setInterval(poll, 30000);
  }
  function startLive() {
    if (!$('#state-badge')) return;
    if (!window.EventSource || document.documentElement.dataset.nosse) { startPolling(); return; }
    const es = new EventSource('events');
    es.addEventListener('open', () => { document.documentElement.dataset.live = 'sse'; });
    es.addEventListener('state', (e) => setState(JSON.parse(e.data)));
    for (const ev of ['snapshot', 'plan', 'alert']) {
      es.addEventListener(ev, (e) => document.dispatchEvent(
        new CustomEvent('tb:' + ev, { detail: JSON.parse(e.data) })));
    }
    es.onerror = () => { es.close(); startPolling(); };
    window.addEventListener('pagehide', () => es.close());
  }

  function countdown() {
    const el = $('[data-expires]');
    if (!el) return;
    const tick = () => {
      const ms = new Date(el.dataset.expires) - Date.now();
      if (ms <= 0) { el.textContent = 'Override ended'; return; }
      const m = Math.ceil(ms / 60000);
      el.textContent = 'Override: ' + Math.floor(m / 60) + 'h ' + String(m % 60).padStart(2, '0') + 'm left';
    };
    tick();
    setInterval(tick, 30000);
  }

  // ---- stop/start dialog (FR-KILL-1/3; 'kill switch' internally): reads /api/status, then POST /api/kill ----
  function botLabel(on, mode) {
    if (!on) return 'Bot: Stopped';
    return mode === 'active' ? 'Bot: Controlling' : 'Bot: Watching (Shadow)';
  }
  function showBot(kill, mode) {
    const on = kill === 'on';
    const k = $('#kill-state');
    if (k) { k.dataset.on = kill; k.dataset.mode = mode || ''; k.textContent = botLabel(on, mode); }
    const b = $('#kill-open');
    if (b) {
      b.textContent = on ? 'Stop bot' : 'Start bot';
      b.className = on ? 'stop' : 'start';
      b.hidden = !on && b.dataset.canStart !== '1';
    }
  }
  async function openKill(chooseMode = false) {
    const dlg = $('#kill-dialog');
    const st = await api('GET', 'api/status');
    if (!st.ok) { toast(detail(st), 'error'); return; }
    const on = !chooseMode && st.data.kill_switch === 'on';
    $('#kill-off', dlg).hidden = !on;
    $('#kill-on', dlg).hidden = on;
    dlg.setAttribute('aria-labelledby', on ? 'kill-off-title' : 'kill-on-title');
    const off = st.data.snapshot && st.data.snapshot.hvac_mode === 'off';
    $('#kill-off-warning', dlg).hidden = !off;
    $('#kill-restore-choices', dlg).hidden = !off;
    dlg.showModal();
  }
  async function submitKill(body) {
    const dlg = $('#kill-dialog');
    const res = await api('POST', 'api/kill', body);
    dlg.close();
    if (!res.ok) { toast(detail(res), 'error'); return; }
    setState(res.data);
    showBot(res.data.kill_switch, res.data.run_mode);
    toast((res.data.kill_switch === 'on'
      ? (res.data.run_mode === 'active' ? 'Bot started: controlling' : 'Bot started: watching (Shadow)')
      : 'Bot stopped')
      + (res.data.restore ? ', restore: ' + res.data.restore : '')
      + (res.data.mirror_ok ? '' : ', HA mirror not confirmed'),
      res.data.restore === 'failed' ? 'error' : 'ok');
  }

  // ---- generic API forms/buttons: <form data-api="PUT api/settings"> ----
  function formJson(f) {
    if (f.dataset.jsonField) return JSON.parse(f.elements[f.dataset.jsonField].value);
    const out = {};
    for (const el of f.elements) {
      if (!el.name || el.disabled) continue;
      if (el.type === 'checkbox') { out[el.name] = el.checked; continue; }
      if (el.value === '') continue;
      out[el.name] = el.type === 'number' ? Number(el.value) : el.value;
    }
    return out;
  }
  async function send(el, spec, body) {
    const [method, path] = spec.split(' ');
    if (el.dataset.confirm && !confirm(el.dataset.confirm)) return;
    const res = await api(method, path, body);
    const errs = el.closest('form, section') && $('.errors', el.closest('form, section'));
    if (errs) errs.textContent = res.ok ? '' : detail(res);
    if (!res.ok) { toast(detail(res), 'error'); return; }
    toast(el.dataset.done || 'Saved', 'ok');
    if (el.dataset.reload !== undefined) setTimeout(() => location.reload(), 600);
  }

  document.addEventListener('submit', (e) => {
    const f = e.target;
    if (!f.dataset.api) return;
    e.preventDefault();
    let body;
    try { body = formJson(f); } catch (err) { toast('Invalid JSON: ' + err.message, 'error'); return; }
    send(f, f.dataset.api, body);
  });
  document.addEventListener('click', (e) => {
    if (e.target.closest('[data-theme-toggle]')) { e.preventDefault(); toggleTheme(); return; }
    const b = e.target.closest('button[data-api]');
    if (b) { e.preventDefault(); send(b, b.dataset.api, b.dataset.body ? JSON.parse(b.dataset.body) : undefined); return; }
    const k = e.target.closest('[data-kill]');
    if (!k) return;
    e.preventDefault();
    const a = k.dataset.kill;
    if (a === 'open') openKill();
    else if (a === 'mode') openKill(true);
    else if (a === 'cancel') $('#kill-dialog').close();
    else if (a === 'off-band') submitKill({ enabled: false, restore: 'band' });
    else if (a === 'off-leave') submitKill({ enabled: false, restore: 'leave' });
    else if (a === 'on-shadow') submitKill({ enabled: true, mode: 'shadow' });
    else if (a === 'on-active') submitKill({ enabled: true, mode: 'active' });
  });

  // forms that build their API path from a select (users page; no inline handlers)
  document.addEventListener('submit', (e) => {
    const f = e.target;
    if (f.dataset.apiBase && f.dataset.idFrom) f.dataset.api = f.dataset.apiBase + $(f.dataset.idFrom, f).value;
  }, true);

  // plan tracks: each segment's width is its step count (no inline styles in the HTML)
  function tracks() {
    for (const el of document.querySelectorAll('.track > [data-n]')) el.style.flex = el.dataset.n + ' 1 0';
  }

  document.addEventListener('DOMContentLoaded', () => { startLive(); countdown(); labelToggle(); tracks(); });
})();
