// thermabot v0.3.0 Settings forms. Each input carries the dotted JSON path of one Settings
// value; on submit the inputs are applied on top of the current settings and the whole object
// is PUT to api/settings (relative URL: ingress-safe). The server's pydantic model validates;
// 422 error locations are shown next to the matching field.
(() => {
  const form = document.querySelector('[data-settings-form]');
  if (!form) return;
  const tb = window.tb;
  const units = form.dataset.units;
  // °C -> °F (mirror of app/units.py): absolute temps round to 0.5 °F, deltas to 0.1 °F
  const cToF = (c) => Math.round((c * 9 / 5 + 32) * 2) / 2;
  const dToF = (c) => Math.round(c * 9 / 5 * 10) / 10;

  function set(obj, path, v) {
    const keys = path.split('.');
    let o = obj;
    for (let i = 0; i < keys.length - 1; i++) o = o[Array.isArray(o) ? Number(keys[i]) : keys[i]];
    const last = keys[keys.length - 1];
    o[Array.isArray(o) ? Number(last) : last] = v;
  }
  function get(obj, path) {
    return path.split('.').reduce((o, k) => (o == null ? o : o[Array.isArray(o) ? Number(k) : k]), obj);
  }

  function value(el) {
    const k = el.dataset.kind;
    if (k === 'bool') return el.checked;
    if (k === 'list') return el.value.split(',').map((s) => s.trim()).filter(Boolean);
    if (k === 'text' || k === 'select') return el.value;
    if (el.value.trim() === '') return null;
    const n = Number(el.value);
    if (Number.isNaN(n)) return el.value;
    // untouched temperature: send the stored °F exactly (no unit round trip)
    if ((k === 'temp' || k === 'delta') && units === 'C') {
      if (!el.dataset.edited && el.dataset.f !== undefined) return Number(el.dataset.f);
      return k === 'temp' ? cToF(n) : dToF(n);
    }
    return n;
  }

  // v0.6.1: the body a save would send; Save is disabled ('No changes') while it equals the
  // stored settings, so a no-diff save never writes a row.
  function collect() {
    const body = JSON.parse(form.dataset.settings);
    for (const el of form.querySelectorAll('[data-path]')) {
      if (el.closest('fieldset[disabled]')) continue;
      const v = value(el);
      if (v === null && el.dataset.nullable) { set(body, el.dataset.path, null); continue; } // optional: empty = null
      if (v === null && get(body, el.dataset.path) !== null) continue; // emptied: keep old
      if (v === '' && get(body, el.dataset.path) === null) continue; // optional text stays null
      set(body, el.dataset.path, v);
    }
    return body;
  }
  const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  const saveBtn = form.querySelector('[data-testid=settings-save]');
  function refresh() {
    if (!saveBtn) return;
    const dirty = !same(collect(), JSON.parse(form.dataset.settings));
    saveBtn.disabled = !dirty;
    saveBtn.textContent = dirty ? 'Save' : 'No changes';
    saveBtn.dataset.dirty = dirty ? '1' : '0';
  }

  form.addEventListener('input', (e) => { if (e.target.dataset.path) e.target.dataset.edited = '1'; refresh(); });
  form.addEventListener('change', refresh);

  function showErrors(detail) {
    for (const el of form.querySelectorAll('[data-error-for]')) el.textContent = '';
    for (const el of form.querySelectorAll('[aria-invalid]')) el.removeAttribute('aria-invalid');
    if (!Array.isArray(detail)) return;
    for (const e of detail) {
      // pydantic loc: ['bands', 'heat', 'home'] or with model-validator noise; match the
      // longest field path that starts with the loc
      const loc = (e.loc || []).filter((x) => typeof x === 'number' || !/[[\]]/.test(x)).join('.');
      let el = null;
      for (let l = loc; l && !el; l = l.includes('.') ? l.slice(0, l.lastIndexOf('.')) : '') {
        el = form.querySelector(`[data-error-for="${CSS.escape(l)}"]`)
          || form.querySelector(`[data-error-for^="${CSS.escape(l)}."]`);
      }
      if (!el) continue;
      el.textContent = (el.textContent ? el.textContent + '; ' : '') + e.msg;
      const inp = form.querySelector(`[data-path="${CSS.escape(el.dataset.errorFor)}"]`);
      if (inp) inp.setAttribute('aria-invalid', 'true');
      const d = el.closest('details');
      if (d) d.open = true;
    }
  }

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    e.stopImmediatePropagation();
    const body = collect();
    if (same(body, JSON.parse(form.dataset.settings))) { tb.toast('No changes', 'ok'); refresh(); return; }
    const res = await tb.api('PUT', 'api/settings', body);
    const errs = form.querySelector('.errors');
    errs.textContent = res.ok ? '' : tb.detail(res);
    showErrors(res.ok ? null : res.data && res.data.detail);
    if (!res.ok) { tb.toast(tb.detail(res), 'error'); return; }
    tb.toast('Saved; replanning', 'ok');
    setTimeout(() => location.reload(), 600);
  }, true);
  refresh();
})();
