// thermabot v0.3.0 Settings forms. Each input carries the dotted JSON path of one Settings
// value; on submit the inputs are applied on top of the current settings and the whole object
// is PUT to api/settings (relative URL: ingress-safe). The server's pydantic model validates;
// 422 error locations are shown next to the matching field.
(() => {
  const form = document.querySelector('[data-settings-form]');
  if (!form) return;
  const tb = window.tb;
  const units = form.dataset.units;
  const homeRoot = form.querySelector('[data-home-editor]');
  const home = homeRoot ? window.tbHomeEditor(homeRoot, JSON.parse(form.dataset.settings)) : null;
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
    if (home) home.apply(body);
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
  if (form.dataset.entityPickers) {
    async function discoverFields() {
      const button = form.querySelector('[data-entity-refresh]');
      const status = form.querySelector('[data-entity-status]');
      button.disabled = true;
      status.textContent = 'Loading Home Assistant device and sensor suggestions...';
      const stored = JSON.parse(form.dataset.settings);
      const res = await tb.api('GET', 'api/setup/discover?thermostat=' + encodeURIComponent(stored.thermostat.entity_id));
      button.disabled = false;
      if (!res.ok) { status.textContent = tb.detail(res); tb.toast(tb.detail(res), 'error'); return; }
      for (const input of form.querySelectorAll('[data-path="thermostat.entity_id"], [data-path^="sensors."]')) {
        const thermostat = input.dataset.path === 'thermostat.entity_id';
        const kind = input.dataset.path.split('.')[1];
        const items = thermostat ? res.data.climate : res.data.suggestions[kind] || [];
        let list = form.querySelector('#entities-' + input.id);
        if (!list) { list = document.createElement('datalist'); list.id = 'entities-' + input.id; form.appendChild(list); }
        list.replaceChildren(...items.map((item) => {
          const option = document.createElement('option');
          option.value = item.entity_id;
          option.label = item.name || item.entity_id;
          return option;
        }));
        input.setAttribute('list', list.id);
        input.type = 'search';
        let choices = input.closest('label').querySelector('.entity-suggestions');
        if (!choices) { choices = document.createElement('span'); choices.className = 'entity-suggestions'; input.closest('label').appendChild(choices); }
        choices.replaceChildren(...items.slice(0, 4).map((item) => {
          const pick = document.createElement('button');
          pick.type = 'button'; pick.className = 'secondary outline';
          pick.textContent = item.name || item.entity_id;
          pick.title = item.entity_id + (item.reason ? ': ' + item.reason : '');
          pick.addEventListener('click', () => { input.value = item.entity_id; input.dispatchEvent(new Event('input', { bubbles: true })); });
          return pick;
        }));
      }
      status.textContent = 'Suggestions loaded. Choose a named device or type an entity ID. Nothing is selected or saved automatically.';
    }
    form.querySelector('[data-entity-refresh]').addEventListener('click', discoverFields);
    discoverFields();
  }

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
    if (home && home.validate(body)) return;
    const stored = JSON.parse(form.dataset.settings);
    if (same(body, stored)) { tb.toast('No changes', 'ok'); refresh(); return; }
    if (form.dataset.review === 'safety') {
      const lines = [];
      const walk = (a, b, path) => {
        if (a && b && typeof a === 'object') {
          for (const k of Object.keys(b)) walk(a[k], b[k], path ? path + '.' + k : k);
          return;
        }
        if (JSON.stringify(a) !== JSON.stringify(b)) lines.push(path + ': ' + JSON.stringify(a) + ' → ' + JSON.stringify(b));
      };
      walk(stored, body, '');
      const ok = await tb.ask({
        title: 'Save safety settings',
        body: lines.slice(0, 12).join('\n') || 'Save these safety settings.',
        typed: 'SAVE',
        confirm: 'Save',
      });
      if (!ok) return;
    }
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
