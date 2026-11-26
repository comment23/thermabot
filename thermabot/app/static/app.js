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
  const prefix = () => ($('meta[name=tb-root]') || {}).content || '';
  function abs(path) {
    if (!path || /^(https?:|data:)/.test(path)) return path;
    return prefix() + String(path).replace(/^\//, '');
  }
  function temp(f, units, delta) {
    if (f == null || f === '' || Number.isNaN(Number(f))) return '—';
    let v = Number(f);
    const c = units === 'C';
    if (c) v = delta ? Math.round(v * 5 / 9 * 10) / 10 : Math.round((v - 32) * 50 / 9) / 10;
    const body = c ? v.toFixed(1) : String(Math.round(v * 1000) / 1000).replace(/(\.\d*?)0+$/, '$1').replace(/\.$/, '');
    return (delta && v > 0 ? '+' : '') + body + '°' + (c ? 'C' : 'F');
  }

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
      r = await fetch(abs(path), {
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

  function ask(opts) {
    const dlg = $('#confirm-sheet');
    if (!dlg) return Promise.resolve(!opts || !opts.body || window.confirm(opts.body));
    return new Promise((resolve) => {
      const opener = document.activeElement;
      $('#confirm-title').textContent = opts.title || 'Confirm';
      $('#confirm-body').textContent = opts.body || '';
      const typed = opts.typed || '';
      const word = $('#confirm-typed-word');
      const label = $('#confirm-typed-label');
      const input = $('#confirm-typed');
      const yes = $('#confirm-yes');
      word.textContent = typed;
      label.hidden = !typed;
      input.value = '';
      yes.textContent = opts.confirm || 'Confirm';
      yes.disabled = !!typed;
      const onInput = () => { yes.disabled = typed && input.value !== typed; };
      input.oninput = onInput;
      function finish(ok) {
        dlg.removeEventListener('close', onClose);
        yes.onclick = null;
        $('#confirm-no').onclick = null;
        dlg.close();
        if (opener && opener.focus) opener.focus();
        resolve(ok);
      }
      function onClose() { finish(false); }
      yes.onclick = () => { if (!yes.disabled) { dlg.removeEventListener('close', onClose); finish(true); } };
      $('#confirm-no').onclick = () => finish(false);
      dlg.addEventListener('close', onClose);
      dlg.showModal();
      (typed ? input : yes).focus();
    });
  }
  window.tb = { api, toast, detail, temp, ask, abs };
  document.addEventListener('htmx:configRequest', (e) => { e.detail.headers['X-CSRF-Token'] = csrf(); });

  // ---- live state: SSE, else poll /api/status every 30 s (FR-UI-2) ----
  function modeText(s) {
    if (!s) return '';
    if (s.label === 'SETUP NEEDED') return 'Setup needed';
    if (s.state === 'DISABLED') return 'Stopped';
    const active = s.run_mode === 'active' || s.state === 'ACTIVE' || (s.label || '').indexOf('ACTIVE') === 0;
    return active ? 'Active' : 'Shadow';
  }
  let announced = '';
  function refreshSystemStatus() {
    const menu = $('[data-testid=system-menu]');
    if (!menu) return;
    const label = $('[data-testid=system-status-label]', menu);
    const warning = menu.querySelector('[data-health]:not([data-health="ok"]), [data-testid=sensors-pill]');
    const reconnecting = document.documentElement.dataset.live === 'poll';
    menu.dataset.live = reconnecting ? 'poll' : 'sse';
    label.textContent = warning ? 'Needs attention' : reconnecting ? 'Reconnecting' : 'System status';
    $('summary', menu).setAttribute('aria-label', 'System status' + (warning ? ': needs attention' : reconnecting ? ': reconnecting' : ''));
  }
  function setState(s) {
    if (s && (s.kill_switch === 'on' || s.kill_switch === 'off')) {
      updateKillButton(s.kill_switch, s.stop_retry_required);
    }
    if (s && Object.prototype.hasOwnProperty.call(s, 'stop_warning')) {
      const notice = $('#stop-handoff-warning');
      if (notice) {
        notice.hidden = !s.stop_warning;
        $('span', notice).textContent = s.stop_warning || '';
        if (Object.prototype.hasOwnProperty.call(s, 'stop_retry_required')) {
          const retry = $('[data-kill=retry-stop]', notice);
          const acknowledge = $('[data-notice=stop_handoff]', notice);
          if (retry) retry.hidden = !s.stop_retry_required;
          if (acknowledge) acknowledge.disabled = !!s.stop_retry_required;
        }
      }
    }
    if (s && Object.prototype.hasOwnProperty.call(s, 'restart_notice')) {
      const notice = $('#restart-shadow-notice');
      if (notice) {
        notice.hidden = !s.restart_notice;
        $('span', notice).textContent = s.restart_notice || '';
      }
    }
    const b = $('#state-badge');
    if (!b || !s || !s.state) return;
    const text = modeText(s);
    const prev = b.dataset.state;
    b.dataset.state = s.state;
    b.dataset.label = s.label || s.state;
    b.dataset.mode = text === 'Active' ? 'active' : (text === 'Stopped' || text === 'Setup needed' ? 'stopped' : 'shadow');
    if (s.state === 'DEGRADED') b.dataset.degraded = s.degraded_reason || b.dataset.degraded || 'yes';
    else delete b.dataset.degraded;
    const label = $('.mode-text', b);
    if (label) label.textContent = text; else b.textContent = text;
    for (const title of document.querySelectorAll('[data-lifecycle-title]')) {
      title.textContent = text === 'Setup needed' ? 'Thermabot needs setup'
        : text === 'Active' ? 'Thermabot is controlling'
        : text === 'Stopped' ? 'Thermabot is stopped' : 'Thermabot is watching';
    }
    for (const description of document.querySelectorAll('[data-lifecycle-description]')) {
      description.textContent = text === 'Active'
        ? 'Thermostat changes pass through the safety checks. Restarting returns to Watch.'
        : text === 'Stopped' || text === 'Setup needed'
        ? 'Start Watch to collect evidence. Starting never enables thermostat control.'
        : 'Watch makes recommendations without changing the thermostat.';
    }
    const readiness = $('[data-testid=readiness]');
    if (readiness && prev && prev !== s.state) {
      readiness.dataset.ready = 'no';
      readiness.dataset.readyWithoutWatch = 'no';
      for (const id of ['reviewed-shadow', 'reviewed-safety-net', 'watch-override']) { const el = $('#' + id); if (el) el.checked = false; }
      if ($('#watch-override-reason')) $('#watch-override-reason').value = '';
      if ($('#active-word')) $('#active-word').value = '';
      const summary = $('[data-testid=control-readiness-summary]');
      if (summary) summary.textContent = 'State changed. Refresh checks before approving Control.';
    }
    refreshActiveButton();
    const warning = $('#ha-connection-warning');
    if (warning && s.ha_health) warning.hidden = s.ha_health !== 'down';
    const ha = $('[data-testid=ha-chip]');
    if (ha && s.ha_health) {
      ha.dataset.health = s.ha_health;
      ha.lastChild.textContent = ' HA ' + (s.ha_health === 'down' ? 'down' : s.ha_health);
    }
    if (s.weather) {
      const weather = $('[data-testid=weather-chip]');
      if (weather) {
        weather.dataset.health = s.weather === 'ok' ? 'ok' : 'stale';
        weather.lastChild.textContent = s.weather === 'ok' ? 'Weather ok' : 'Weather stale';
      }
      const notice = $('[data-testid=weather-pill]');
      if (notice) notice.textContent = s.weather === 'ok' ? 'Weather ok' : 'Weather stale';
    }
    refreshSystemStatus();
    const live = $('#live-status');
    if (live && prev && prev !== s.state && announced !== text) {
      announced = text;
      live.textContent = 'Mode is ' + text;
    }
    const tag = $('#live-tag');
    if (tag) tag.textContent = document.documentElement.dataset.live === 'poll'
      ? 'updated just now · reconnecting' : 'live';
  }
  let polling = null;
  async function poll() {
    const r = await api('GET', 'api/status');
    if (r.ok) {
      setState(r.data);
      document.dispatchEvent(new CustomEvent('tb:status', { detail: r.data }));
      refreshHome('snapshot');
    }
  }
  function startPolling() {
    if (polling) return;
    document.documentElement.dataset.live = 'poll';
    refreshSystemStatus();
    polling = setInterval(poll, 30000);
  }
  function startLive() {
    if (!$('#state-badge')) return;
    if (!window.EventSource || document.documentElement.dataset.nosse) { startPolling(); return; }
    const es = new EventSource(abs('events'));
    es.addEventListener('open', () => { document.documentElement.dataset.live = 'sse'; refreshSystemStatus(); });
    es.addEventListener('state', (e) => setState(JSON.parse(e.data)));
    for (const ev of ['snapshot', 'plan', 'alert']) {
      es.addEventListener(ev, (e) => {
        document.dispatchEvent(new CustomEvent('tb:' + ev, { detail: JSON.parse(e.data) }));
        if (ev !== 'alert') refreshHome(ev);
      });
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
  function updateKillButton(kill, retry) {
    const on = kill === 'on';
    const b = $('#kill-open');
    if (b) {
      b.textContent = on ? 'Stop' : 'Start watching';
      b.className = on ? 'stop' : 'resume';
      b.dataset.kill = on ? 'open' : 'resume';
      b.hidden = !on && b.dataset.canStart !== '1';
      b.disabled = !on && !!retry;
    }
    const resume = $('[data-testid=safety-resume]');
    if (resume) resume.disabled = !!retry;
    for (const action of document.querySelectorAll('[data-watch-action]')) {
      action.textContent = on ? 'Stop thermabot' : 'Start watching';
      action.className = on ? 'stop' : 'resume';
      action.dataset.kill = on ? 'open' : 'resume';
      action.hidden = !on && action.dataset.canStart !== '1';
      action.disabled = !on && !!retry;
    }
  }
  async function openStop() {
    const dlg = $('#stop-sheet');
    const opener = document.activeElement;
    const st = await api('GET', 'api/status');
    if (!st.ok || !st.data) {
      $('#stop-explanation').textContent = 'Current status is unavailable. Stop will still be attempted.';
      $('#stop-keeps').textContent = '';
      const warning = $('#kill-off-warning');
      warning.hidden = false;
      warning.textContent = 'The handoff may not be confirmed. Check the thermostat after stopping.';
      dlg._opener = opener;
      dlg.showModal();
      return;
    }
    const snap = st.data.snapshot || {};
    const active = st.data.kill_switch === 'on' && st.data.run_mode === 'active';
    const band = st.data.stop_handoff || {};
    const u = st.data.display_units || 'F';
    const explanation = $('#stop-explanation');
    if (active && band.enabled) {
      explanation.textContent = 'Stopping Control will try the saved comfort band: heat '
        + temp(band.heat_f, u) + ' and cool ' + temp(band.cool_f, u)
        + '. Safety checks may block the handoff; the bot still stops. Check the thermostat if it is not confirmed.';
    } else if (active) {
      explanation.textContent = 'No stop comfort band is configured. The bot will stop without a confirmed handoff; check the thermostat.';
    } else {
      explanation.textContent = 'Stopping Watch will not change the thermostat.';
    }
    const keeps = $('#stop-keeps');
    if (keeps) {
      const heat = snap.heat_sp == null ? '' : 'Heat ' + temp(snap.heat_sp, u);
      const cool = snap.cool_sp == null ? '' : 'Cool ' + temp(snap.cool_sp, u);
      keeps.textContent = 'Current thermostat: ' + (snap.hvac_mode || 'mode unavailable')
        + (heat || cool ? ' · ' + [heat, cool].filter(Boolean).join(' · ') : '') + '.';
    }
    const off = snap.hvac_mode === 'off';
    const warning = $('#kill-off-warning');
    warning.hidden = !off;
    if (off) warning.textContent = active
      ? 'The thermostat is off. If the comfort handoff is not confirmed, it may remain off. Check the thermostat.'
      : 'The thermostat is off. Stopping Watch will leave it off. Check the thermostat.';
    dlg._opener = opener;
    dlg.showModal();
  }
  async function submitKill(body) {
    const dlg = $('#stop-sheet');
    const res = await api('POST', 'api/kill', body);
    if (dlg && dlg.open) dlg.close();
    if (!res.ok) { toast(detail(res), 'error'); await poll(); return; }
    setState(res.data);
    const notRunning = res.data.kill_switch === 'on' && res.data.state === 'DISABLED';
    let message = notRunning ? 'Start requested, but the bot is not running. Check status and Home Assistant.'
      : res.data.kill_switch === 'on'
      ? (res.data.run_mode === 'active' ? 'Bot started: controlling' : 'Bot started: watching (Shadow)')
      : 'Bot stopped';
    if (res.data.restore === 'sent') message += '. Comfort band confirmed.';
    else if (res.data.restore === 'not_needed') message += '. Comfort band already matched.';
    else if (res.data.restore === 'failed') message += '. Comfort handoff not confirmed; check the thermostat.';
    if (!res.data.mirror_ok) message += ' HA mirror not confirmed.';
    toast(message, notRunning || res.data.restore === 'failed' || !res.data.mirror_ok ? 'error' : 'ok');
  }
  async function acknowledgeNotice(button) {
    const res = await api('POST', 'api/notices/acknowledge', { kind: button.dataset.notice });
    if (!res.ok) { toast(detail(res), 'error'); return; }
    const section = button.closest('section');
    if (section) section.hidden = true;
    const refresh = button.dataset.notice === 'stop_handoff' && $('[data-testid=readiness]');
    toast(refresh ? 'Notice acknowledged. Reload this page to update readiness.' : 'Notice acknowledged', 'ok');
  }
  function watchOverrideReason() {
    return $('#watch-override')?.checked ? $('#watch-override-reason').value.trim() : null;
  }
  function controlReady() {
    const list = $('[data-testid=readiness]');
    const reason = watchOverrideReason();
    if (reason !== null && (reason.length < 10 || reason.length > 500)) return false;
    return !!(list && (list.dataset.ready === 'yes' ||
      (list.dataset.readyWithoutWatch === 'yes' && reason)));
  }
  function activeReviewed() {
    const word = $('#active-word');
    const shadow = $('#reviewed-shadow');
    const net = $('#reviewed-safety-net');
    return !!(word && word.value === 'ACTIVE' && shadow && shadow.checked
      && net && net.checked && controlReady()
      && $('#state-badge')?.dataset.mode !== 'active');
  }
  function refreshActiveButton() {
    const reason = $('#watch-override-reason');
    if (reason) reason.disabled = !$('#watch-override').checked;
    const blockers = $('[data-testid=control-blockers]');
    if (blockers) blockers.hidden = controlReady();
    const btn = $('[data-testid=kill-on-active]');
    if (btn) btn.disabled = !activeReviewed();
    const hint = $('#control-approval-hint');
    if (hint) {
      const ready = controlReady();
      const active = $('#state-badge')?.dataset.mode === 'active';
      hint.textContent = active ? 'Control is running. Use Stop thermabot to end control.'
        : !ready ? 'Resolve readiness blockers, or explain an explicit Watch-only exception, then refresh checks.'
        : activeReviewed() ? (watchOverrideReason()
          ? 'Watch exception selected for this start. Control can change the thermostat; your reason will be audited.'
          : 'Ready for your approval. Starting Control can change the thermostat.')
        : 'Complete both acknowledgements and type ACTIVE to enable Start control.';
    }
  }
  async function refreshReadiness(button) {
    button.disabled = true;
    const res = await api('GET', 'api/readiness');
    button.disabled = false;
    if (!res.ok) { toast(detail(res), 'error'); return; }
    const list = $('[data-testid=readiness]');
    list.dataset.ready = res.data.ready ? 'yes' : 'no';
    list.dataset.readyWithoutWatch = res.data.ready_without_watch ? 'yes' : 'no';
    for (const check of res.data.checks) {
      const row = list.querySelector(`[data-check="${CSS.escape(check.id)}"]`);
      if (!row) continue;
      row.dataset.ok = check.ok ? 'yes' : 'no';
      $('.check-result', row).textContent = check.ok ? 'Passed' : 'Needs attention';
      $('.meta', row).textContent = check.detail;
    }
    const blockers = $('[data-testid=control-blockers]');
    blockers.hidden = res.data.ready;
    $('ul', blockers).replaceChildren(...res.data.checks.filter((ch) => !ch.ok).map((ch) => {
      const li = document.createElement('li'); li.textContent = ch.label; return li;
    }));
    $('[data-testid=control-readiness-summary]').textContent =
      `${res.data.checks.filter((ch) => ch.ok).length} of ${res.data.checks.length} checks passed`;
    const next = $('[data-readiness-next-step]');
    const blocked = res.data.checks.find((ch) => !ch.ok);
    next.hidden = !blocked;
    next.textContent = blocked ? blocked.label + ': ' + blocked.detail : '';
    const first = $('[data-testid=first-write]');
    if (first) {
      first.hidden = !res.data.first_write;
      first.textContent = res.data.first_write ? 'Current planned target (may change): ' + res.data.first_write : '';
    }
    refreshActiveButton();
    toast('Readiness checks refreshed', 'ok');
  }
  function refreshHome(which) {
    if (!document.getElementById('home-now') || !window.htmx) return;
    const names = which === 'plan' ? ['next', 'timeline'] : ['now', 'rooms'];
    for (const n of names) {
      window.htmx.ajax('GET', abs('partials/' + n), { target: '#home-' + n, swap: 'outerHTML' });
    }
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
    if (el.dataset.confirm) {
      const ok = await ask({
        title: el.dataset.confirmTitle || 'Confirm',
        body: el.dataset.confirm,
        typed: el.dataset.confirmTyped || '',
        confirm: el.dataset.confirmTyped ? 'Delete' : 'Confirm',
      });
      if (!ok) return;
    }
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
    const refresh = e.target.closest('[data-readiness-refresh]');
    if (refresh) { e.preventDefault(); refreshReadiness(refresh); return; }
    if (e.target.closest('[data-theme-toggle]')) { e.preventDefault(); toggleTheme(); return; }
    const notice = e.target.closest('button[data-notice]');
    if (notice) { e.preventDefault(); acknowledgeNotice(notice); return; }
    const b = e.target.closest('button[data-api]');
    if (b) { e.preventDefault(); send(b, b.dataset.api, b.dataset.body ? JSON.parse(b.dataset.body) : undefined); return; }
    const k = e.target.closest('[data-kill]');
    if (!k) return;
    e.preventDefault();
    const a = k.dataset.kill;
    if (a === 'open') openStop();
    else if (a === 'resume' || a === 'on-shadow') submitKill({ enabled: true, mode: 'shadow' });
    else if (a === 'cancel') { const dlg = $('#stop-sheet'); if (dlg) dlg.close(); }
    else if (a === 'off-band') submitKill({ enabled: false, restore: 'band' });
    else if (a === 'off-leave') submitKill({ enabled: false, restore: 'leave' });
    else if (a === 'retry-stop') submitKill({ enabled: false });
    else if (a === 'on-active') {
      const word = $('#active-word');
      if (!activeReviewed()) { toast('Complete the Owner review and type ACTIVE', 'error'); return; }
      submitKill({
        enabled: true, mode: 'active', confirmation: word.value,
        reviewed_shadow: true, reviewed_safety_net: true,
        watch_override_reason: watchOverrideReason(),
      });
    }
  });
  document.addEventListener('input', (e) => {
    if (['active-word', 'reviewed-shadow', 'reviewed-safety-net', 'watch-override', 'watch-override-reason'].includes(e.target.id)) refreshActiveButton();
  });
  document.addEventListener('change', (e) => {
    if (['reviewed-shadow', 'reviewed-safety-net', 'watch-override'].includes(e.target.id)) refreshActiveButton();
  });
  document.addEventListener('close', (e) => {
    const dlg = e.target;
    if (dlg && dlg._opener && dlg._opener.focus) dlg._opener.focus();
  }, true);

  // forms that build their API path from a select (users page; no inline handlers)
  document.addEventListener('submit', (e) => {
    const f = e.target;
    if (f.dataset.apiBase && f.dataset.idFrom) f.dataset.api = f.dataset.apiBase + $(f.dataset.idFrom, f).value;
  }, true);

  // plan tracks: each segment's width is its step count (no inline styles in the HTML)
  function tracks() {
    for (const el of document.querySelectorAll('.track > [data-n]')) el.style.flex = el.dataset.n + ' 1 0';
  }

  function sheetGesture(dlg) {
    if (!dlg) return;
    let y0 = null;
    dlg.addEventListener('pointerdown', (e) => {
      if (e.target.closest('.sheet-handle')) y0 = e.clientY;
    });
    dlg.addEventListener('pointerup', (e) => {
      if (y0 != null && e.clientY - y0 > 48) dlg.close();
      y0 = null;
    });
  }
  document.addEventListener('DOMContentLoaded', () => {
    startLive(); countdown(); labelToggle(); tracks();
    refreshActiveButton();
    refreshSystemStatus();
    $('[data-testid=settings-nav] [aria-current=page]')?.scrollIntoView({ block: 'nearest', inline: 'center' });
    sheetGesture($('#stop-sheet'));
    sheetGesture($('#confirm-sheet'));
    const stop = $('#kill-open');
    const sheet = $('#stop-sheet');
    if (stop && sheet) stop.addEventListener('click', () => { sheet._opener = stop; });
  });
})();
