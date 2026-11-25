// Shared room editor for setup and Settings. Equipment assignments are inventory, never commands.
(() => {
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const title = (s) => String(s).replace(/_/g, ' ').replace(/\b[a-z]/g, (c) => c.toUpperCase());
  const slug = (s) => s.toLowerCase().replace(/[^a-z0-9_]+/g, '_').replace(/^_+|_+$/g, '');

  window.tbHomeEditor = (root, settings, discovery = null) => {
    const $ = (s, r = root) => r.querySelector(s);
    const rows = () => Array.from(root.querySelectorAll('[data-home-room]'));
    const tb = window.tb;
    const original = structuredClone(settings);
    const drafts = new Set();
    let entities = [];
    let temperatureIds = new Set();
    let adding = 'room';
    let opener = null;
    const dialog = $('[data-home-dialog]');
    const status = $('[data-testid=home-discovery-status]');
    const nameOf = (id) => entities.find((e) => e.entity_id === id)?.name || id;
    const included = (row) => $('[data-r=on]', row).checked;
    const floors = () => new Set([...drafts, ...rows().map((r) => $('[data-r=floor]', r).value.trim()).filter(Boolean)]);

    function options(current = '', temperature = false) {
      const list = entities.filter((e) => !temperature || temperatureIds.has(e.entity_id));
      if (current && !list.some((e) => e.entity_id === current)) {
        const known = entities.find((e) => e.entity_id === current);
        list.unshift(known || { entity_id: current, name: current + ' (saved; not discovered)' });
      }
      return '<option value="">Choose an entity...</option>' + list.map((e) =>
        `<option value="${esc(e.entity_id)}"${e.entity_id === current ? ' selected' : ''}>${esc(e.name)} - ${esc(e.entity_id)}</option>`).join('');
    }

    function map() {
      const groups = new Map(Array.from(floors()).map((f) => [f, []]));
      for (const row of rows().filter(included)) {
        const floor = $('[data-r=floor]', row).value.trim() || 'Unassigned floor';
        if (!groups.has(floor)) groups.set(floor, []);
        groups.get(floor).push(row);
      }
      $('[data-home-map]').innerHTML = Array.from(groups, ([floor, rooms]) =>
        `<section class="home-floor" data-home-floor="${esc(floor)}">
          <div class="card-head"><h4>${esc(floor)}</h4><small>${rooms.length} room${rooms.length === 1 ? '' : 's'}</small></div>
          <div class="home-floor-rooms">${rooms.map((r) =>
            `<button type="button" class="map-room" data-home-focus="${esc(r.dataset.homeRoom)}">
              <strong>${esc(title(r.dataset.homeRoom))}</strong>
              <small>${esc(nameOf($('[data-r=temp]', r).value) || 'Choose a temperature sensor')}</small>
              <span class="meta">${JSON.parse(r.dataset.devices).length} additional equipment</span>
            </button>`).join('') || '<p class="meta">Add a room to save this floor.</p>'}</div>
        </section>`).join('') || '<p class="empty">Add your first room to build your home overview.</p>';
      $('#home-floor-options').innerHTML = Array.from(floors(), (f) => `<option value="${esc(f)}"></option>`).join('');
    }

    function equipment(row) {
      const ids = JSON.parse(row.dataset.devices);
      $('[data-room-device-list]', row).innerHTML = ids.map((id) =>
        `<span class="equipment-chip">${esc(nameOf(id))}
          <button type="button" class="secondary outline" data-room-device-remove="${esc(id)}"
            aria-label="Remove ${esc(nameOf(id))} from ${esc(title(row.dataset.homeRoom))}">Remove</button></span>`).join('')
        || '<p class="meta">No additional equipment assigned.</p>';
    }

    function room(key, cfg, on = true) {
      const c = { tol: 0, stale_min: 60, dead_min: 360, aux_heat_detect: false, devices: [], floor: '', ...cfg };
      const weights = ['home', 'sleep', 'away'].map((block) => {
        const value = original.weights[block]?.[key] ?? (block === 'home' && !original.rooms[key] ? 1 : '');
        return `<label>${title(block)} importance<input type="number" min="0" step="any"
          data-r-weight="${block}" value="${value}" placeholder="Not used"></label>`;
      }).join('');
      const tolerance = settings.display.units === 'C' ? Math.round(c.tol * 5 / 9 * 10) / 10 : c.tol;
      const el = document.createElement('article');
      el.className = 'room-editor';
      el.dataset.homeRoom = key;
      el.dataset.testid = 'room-row';
      el.dataset.devices = JSON.stringify(c.devices);
      el.innerHTML = `<div class="card-head"><h4>${esc(title(key))}</h4>
        ${key === 'thermostat' ? '<span class="tag">Configured thermostat</span>' : `<button type="button" class="secondary outline" data-room-remove>Remove room</button>`}</div>
        <label class="review-check"${on ? ' hidden' : ''}><input type="checkbox" data-r="on"${on ? ' checked' : ''}${key === 'thermostat' ? ' disabled' : ''}>
          <span>Include ${esc(title(key))} in comfort monitoring</span></label>
        <input type="hidden" data-r="key" value="${esc(key)}">
        <div class="grid-2">
          <label>Temperature sensor<select data-r="temp"${key === 'thermostat' ? ' disabled' : ''}>${options(c.temp, true)}</select></label>
          <label>Floor<input data-r="floor" list="home-floor-options" maxlength="64" value="${esc(c.floor)}" placeholder="Choose or name a floor"></label>
        </div>
        <details data-room-devices><summary>Additional equipment (read only)</summary>
          <div data-room-device-list class="equipment-list"></div>
          <div class="equipment-picker"><label>Equipment<select data-room-device-picker>${options()}</select></label>
            <button type="button" class="secondary outline" data-room-device-add>Assign equipment</button></div>
          <small>Inventory only. This does not enable fan, cover or heater control, or door assessment.</small>
        </details>
        <details><summary>Comfort importance and sensor settings</summary>
          <p class="meta">Room key: <code>${esc(key)}</code>. Higher importance has more influence; empty means not used.</p>
          <div class="grid-3">${weights}</div>
          <div class="grid-3">
            <label>Tolerance (${settings.display.units === 'C' ? 'C' : 'F'} difference)<input type="number" min="0" step="any" data-r="tol" value="${tolerance}"></label>
            <label>Stale after (minutes)<input type="number" min="0" step="any" data-r="stale_min" value="${c.stale_min}"></label>
            <label>Unavailable after (minutes)<input type="number" min="0" step="any" data-r="dead_min" value="${c.dead_min}"></label>
          </div>
          <label class="review-check"><input type="checkbox" data-r="aux_heat_detect"${c.aux_heat_detect ? ' checked' : ''}><span>Observe auxiliary heat in this room</span></label>
        </details>
        <small class="field-error" data-error-for="rooms.${esc(key)}"></small>`;
      $('[data-home-rooms]').appendChild(el);
      equipment(el);
      return el;
    }

    function apply(body) {
      const rooms = {};
      const weights = structuredClone(original.weights);
      for (const row of rows().filter(included)) {
        const key = row.dataset.homeRoom;
        const base = original.rooms[key] || { tol: 0, stale_min: 60, dead_min: 360, aux_heat_detect: false };
        const cfg = { ...base, temp: $('[data-r=temp]', row).value, floor: $('[data-r=floor]', row).value.trim(),
          devices: JSON.parse(row.dataset.devices), aux_heat_detect: $('[data-r=aux_heat_detect]', row).checked };
        for (const k of ['tol', 'stale_min', 'dead_min']) {
          const input = $(`[data-r=${k}]`, row);
          cfg[k] = input.value.trim() === '' ? '' : Number(input.value);
          if (k === 'tol' && settings.display.units === 'C' && typeof cfg[k] === 'number')
            cfg[k] = input.dataset.edited ? Math.round(cfg[k] * 1.8 * 10) / 10 : base.tol;
        }
        rooms[key] = cfg;
        for (const input of row.querySelectorAll('[data-r-weight]')) {
          const block = input.dataset.rWeight;
          weights[block] ||= {};
          if (input.value.trim() === '') delete weights[block][key];
          else weights[block][key] = Number(input.value);
        }
      }
      for (const [block, values] of Object.entries(weights)) {
        for (const key of Object.keys(values)) if (!Object.hasOwn(rooms, key)) delete values[key];
        weights[block] = values;
      }
      body.rooms = rooms;
      body.weights = weights;
      return body;
    }

    function validate(body) {
      const bad = [];
      for (const [key, cfg] of Object.entries(body.rooms)) {
        if (key !== 'thermostat' && !cfg.temp) bad.push(`${title(key)}: choose a temperature sensor.`);
        if (!/^[a-zA-Z0-9 _-]{0,64}$/.test(cfg.floor)) bad.push(`${title(key)}: use letters, numbers, spaces or hyphens for the floor.`);
        if (![cfg.tol, cfg.stale_min, cfg.dead_min].every(Number.isFinite) || cfg.tol < 0 || cfg.stale_min < 0 || cfg.dead_min < cfg.stale_min)
          bad.push(`${title(key)}: enter valid sensor limits; unavailable must be at least stale.`);
      }
      const message = bad.join('\n');
      $('[data-home-errors]').textContent = message;
      return message;
    }

    function changed() {
      map();
      root.dispatchEvent(new Event('change', { bubbles: true }));
    }
    function setThermostat(id) {
      const row = rows().find((r) => r.dataset.homeRoom === 'thermostat');
      if (row) $('[data-r=temp]', row).innerHTML = options(id, true);
      map();
    }
    function load(data) {
      entities = [...data.all, ...data.climate];
      temperatureIds = new Set([
        ...entities.filter((e) => e.device_class === 'temperature' || /^(°?[FC]|[fc]ahrenheit|[Cc]elsius)$/.test(e.unit || '')).map((e) => e.entity_id),
        ...data.suggestions.rooms.map((e) => e.entity_id),
      ]);
      for (const row of rows()) {
        const select = $('[data-r=temp]', row);
        select.innerHTML = options(select.value, true);
        $('[data-room-device-picker]', row).innerHTML = options();
        equipment(row);
      }
      $('[data-home-new=temp]').innerHTML = options('', true);
      status.textContent = `${entities.length} Home Assistant entities loaded. Saved but undiscovered mappings are retained.`;
      map();
    }
    async function refresh() {
      const button = $('[data-home-refresh]');
      button.disabled = true;
      status.textContent = 'Loading Home Assistant entities...';
      const res = await tb.api('GET', 'api/setup/discover?thermostat=' + encodeURIComponent(settings.thermostat.entity_id));
      if (res.ok) load(res.data);
      else { status.textContent = 'Discovery failed: ' + tb.detail(res) + '. Saved mappings have not changed.'; tb.toast(tb.detail(res), 'error'); }
      button.disabled = false;
    }
    root.addEventListener('input', (e) => {
      if (e.target.dataset.r) e.target.dataset.edited = '1';
      if (e.target.dataset.r === 'floor') map();
    });
    root.addEventListener('change', (e) => {
      if (e.target.dataset.r) map();
    });
    root.addEventListener('click', async (e) => {
      const button = e.target.closest('button');
      if (!button) return;
      if (button.hasAttribute('data-home-refresh')) { await refresh(); return; }
      if (button.dataset.homeFocus) {
        const row = rows().find((r) => r.dataset.homeRoom === button.dataset.homeFocus);
        row.scrollIntoView({ block: 'center' });
        $('[data-r=floor]', row).focus();
      }
      if (button.dataset.homeAdd) {
        adding = button.dataset.homeAdd;
        opener = button;
        $('#home-dialog-title').textContent = adding === 'room' ? 'Add room' : 'Add floor';
        $('[data-home-room-fields]').hidden = adding !== 'room';
        $('[data-home-dialog-save]').textContent = adding === 'room' ? 'Add room' : 'Add floor';
        $('[data-home-dialog-hint]').textContent = adding === 'room' ? 'Changes stay in this form until you save.' : 'A floor is saved when at least one room is assigned to it.';
        $('[data-home-new=name]').value = '';
        $('[data-home-new=floor]').value = Array.from(drafts).at(-1) || '';
        $('[data-home-new=temp]').innerHTML = options('', true);
        $('[data-home-dialog-errors]').textContent = '';
        dialog.showModal();
        $('[data-home-new=name]').focus();
      }
      if (button.hasAttribute('data-home-dialog-cancel')) dialog.close();
      if (button.hasAttribute('data-home-dialog-save')) {
        const name = $('[data-home-new=name]').value.trim();
        const key = slug(name);
        const floor = $('[data-home-new=floor]').value.trim();
        const temp = $('[data-home-new=temp]').value;
        let error = !name ? 'Enter a name.' : '';
        if (adding === 'floor' && !/^[a-zA-Z0-9 _-]{1,64}$/.test(name)) error = 'Use letters, numbers, spaces or hyphens.';
        if (adding === 'floor' && floors().has(name)) error = 'This floor already exists.';
        if (adding === 'room' && (!key || rows().some((r) => r.dataset.homeRoom === key))) error = 'Choose a unique room name.';
        if (adding === 'room' && !temp) error = 'Choose a temperature sensor.';
        if (error) { $('[data-home-dialog-errors]').textContent = error; return; }
        if (adding === 'floor') drafts.add(name);
        else room(key, { temp, floor });
        dialog.close();
        changed();
      }
      const row = button.closest('[data-home-room]');
      if (!row) return;
      if (button.hasAttribute('data-room-remove')) {
        const key = row.dataset.homeRoom;
        const assigned = JSON.parse(root.dataset.assignedRooms || '[]');
        const opening = Object.values(original.openings.sensors).some((x) => x.affected_rooms.includes(key));
        if (assigned.includes(key) || opening) {
          tb.toast('Reassign users or door-contact room mappings before removing this room.', 'error');
          return;
        }
        if (await tb.ask({ title: 'Remove room', body: `Remove ${title(key)} from comfort monitoring? No Home Assistant entity is deleted. Save the form to apply.`, confirm: 'Remove room' })) {
          row.remove(); changed();
        }
      }
      if (button.hasAttribute('data-room-device-add') || button.hasAttribute('data-room-device-remove')) {
        const remove = button.dataset.roomDeviceRemove;
        const id = remove || $('[data-room-device-picker]', row).value;
        if (!id) { tb.toast('Choose equipment to assign.', 'error'); return; }
        const ids = new Set(JSON.parse(row.dataset.devices));
        if (remove) ids.delete(id); else ids.add(id);
        row.dataset.devices = JSON.stringify(Array.from(ids));
        equipment(row); changed();
      }
    });
    dialog.addEventListener('close', () => opener?.focus());
    for (const [key, cfg] of Object.entries(settings.rooms)) room(key, cfg);
    if (discovery) {
      const used = new Set(Object.values(settings.rooms).map((r) => r.temp));
      for (const candidate of discovery.suggestions.rooms) {
        if (!used.has(candidate.entity_id) && !rows().some((r) => r.dataset.homeRoom === candidate.room))
          room(candidate.room, { temp: candidate.entity_id }, false);
      }
    }
    map();
    if (discovery) load(discovery);
    else if (root.dataset.canDiscover === '1') refresh();
    else {
      entities = Object.values(settings.rooms).map((r) => ({ entity_id: r.temp, name: r.temp }));
      temperatureIds = new Set(entities.map((e) => e.entity_id));
      status.textContent = 'Discovery needs an Owner. Existing room mappings remain available.';
      $('[data-home-refresh]').hidden = true;
    }
    return { apply, validate, setThermostat };
  };
})();
