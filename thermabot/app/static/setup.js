// thermabot v0.3.0 setup wizard. Vanilla JS, relative URLs only (works under HA ingress).
// Reads HA through api/setup/* (GET only); the only write is POST api/setup/save at the end.
// Units: the settings object always holds °F; °C exists only in the inputs (see app/units.py).
(() => {
  const root = document.getElementById('setup');
  if (!root) return;
  const $ = (s, r = root) => r.querySelector(s);
  const $$ = (s, r = root) => Array.from(r.querySelectorAll(s));
  const tb = window.tb;
  const orig = JSON.parse(root.dataset.settings);
  const st = JSON.parse(root.dataset.settings); // working copy, °F
  const DEFAULT_BANDS = {
    heat: { home: [68, 71], sleep: [66, 69], away: [62, 66] },
    cool: { home: [73, 76], sleep: [71, 74], away: [78, 80] },
  };
  const SENSORS = [
    ['blower', 'Blower / fan running', 'binary_sensor or sensor, on/off'],
    ['compressor', 'Compressor speed', 'sensor, %'],
    ['outdoor', 'Outdoor temperature', 'sensor, °F/°C'],
    ['humidity', 'Indoor humidity (optional)', 'replaces the thermostat humidity'],
  ];
  let step = 1;
  let haOk = false;
  let disc = null;

  // ---- units (mirror of thermabot/app/units.py) ----
  const fToC = (f) => Math.round((f - 32) * 5 / 9 * 10) / 10;
  const cToF = (c) => Math.round((c * 9 / 5 + 32) * 2) / 2;
  const units = () => st.display.units;
  const show = (f) => (units() === 'C' ? fToC(f) : f);
  const climateTemp = (e) => {
    if (e.current_temperature == null) return '';
    const f = (e.temperature_unit || '').toUpperCase().includes('C')
      ? cToF(e.current_temperature) : e.current_temperature;
    return `${show(f)}°${units()}`;
  };
  const store = (v) => (units() === 'C' ? cToF(v) : v);
  window.tbUnits = { fToC, cToF };

  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const errors = (msg) => { $('[data-testid=setup-errors]').textContent = msg || ''; };
  const slug = (s) => String(s).toLowerCase().replace(/[^a-z0-9_]+/g, '_').replace(/^_+|_+$/g, '');

  // ---- navigation ----
  function go(n) {
    step = Math.max(1, Math.min(7, n));
    for (const s of $$('section[data-step]')) s.hidden = Number(s.dataset.step) !== step;
    for (const li of $$('.steps li')) {
      li.toggleAttribute('aria-current', Number(li.dataset.step) === step);
      li.classList.toggle('done', Number(li.dataset.step) < step);
    }
    $('[data-setup=back]').hidden = step === 1;
    $('[data-setup=next]').hidden = step === 7;
    errors('');
    if (step === 5) renderBands();
    if (step === 6) renderWeather();
    if (step === 7) renderReview();
    window.scrollTo(0, 0);
  }

  function validateStep() {
    if (step === 1 && !haOk) return 'Test the Home Assistant connection first.';
    if (step === 2 && !st.thermostat.entity_id) return 'Pick the thermostat.';
    if (step === 3) return readSensors();
    if (step === 4) return readPeople();
    if (step === 5) return readBands();
    if (step === 6) return readWeather();
    return '';
  }

  // ---- step 1: connection ----
  async function haTest() {
    const out = $('[data-testid=ha-result]');
    out.textContent = 'Testing…';
    out.dataset.ok = '';
    const r = await tb.api('GET', 'api/setup/ha-test');
    if (!r.ok) { out.textContent = tb.detail(r); out.dataset.ok = 'no'; return; }
    haOk = r.data.ok;
    out.dataset.ok = haOk ? 'yes' : 'no';
    out.textContent = haOk
      ? `OK: Home Assistant ${r.data.version || '?'}${r.data.location_name ? ' (' + r.data.location_name + ')' : ''}, ${r.data.ms} ms`
      : `Failed: ${r.data.error}`;
    if (haOk) { errors(''); await load(); }
  }

  async function load() {
    const r = await tb.api('GET', 'api/setup/discover?thermostat=' + encodeURIComponent(st.thermostat.entity_id || ''));
    if (!r.ok) { errors(tb.detail(r)); return; }
    disc = r.data;
    const h = await tb.api('GET', 'api/setup/home-context');
    renderClimate();
    renderSensors();
    renderRooms();
    renderPeople();
    renderHomeContext(h.ok ? h.data : null, h.ok ? '' : tb.detail(h));
  }

  // ---- step 2: thermostat ----
  function renderClimate() {
    const box = $('[data-testid=climate-list]');
    if (!disc.climate.length) { box.innerHTML = '<p>No climate.* entities found in Home Assistant.</p>'; return; }
    const capabilities = (e) => {
      const limits = e.min_temp != null || e.max_temp != null
        ? `limits ${e.min_temp ?? '—'}–${e.max_temp ?? '—'} ${e.temperature_unit || ''}` : '';
      const step = e.target_temp_step == null ? '' : `step ${e.target_temp_step}`;
      const target = e.target_temp_low != null || e.target_temp_high != null
        ? `heat/cool targets ${e.target_temp_low ?? '—'} / ${e.target_temp_high ?? '—'}`
        : e.target_temperature == null ? '' : `target ${e.target_temperature}`;
      return [limits, step, target].filter(Boolean).join(' · ');
    };
    box.innerHTML = disc.climate.map((e) => `
      <label class="pick" data-entity="${esc(e.entity_id)}">
        <input type="radio" name="tstat" value="${esc(e.entity_id)}"${e.entity_id === st.thermostat.entity_id ? ' checked' : ''}>
        <span><strong>${esc(e.name)}</strong> <code>${esc(e.entity_id)}</code><br>
        <small>state ${esc(e.state)}${e.current_temperature != null ? ', ' + climateTemp(e) : ''}
        · modes ${esc(e.hvac_modes.join(', ') || '—')} · fan ${esc(e.fan_modes.join(', ') || '—')}
        · ${esc(capabilities(e) || 'temperature control capabilities not reported')}
        ${e.preset_modes.length ? `· presets ${esc(e.preset_modes.join(', '))}` : ''}</small></span>
      </label>`).join('');
  }
  root.addEventListener('change', (e) => {
    errors('');
    if (e.target.name === 'tstat') {
      st.thermostat.entity_id = e.target.value;
      if (st.rooms.thermostat) st.rooms.thermostat.temp = e.target.value;
    }
    if (e.target.dataset.testid === 'units') {
      readBands(true);
      st.display.units = e.target.value;
      renderBands();
    }
  });

  // ---- step 3: sensors + rooms ----
  function datalist() {
    if ($('#dl-all')) return;
    const dl = document.createElement('datalist');
    dl.id = 'dl-all';
    dl.innerHTML = disc.all.map((e) => `<option value="${esc(e.entity_id)}">${esc(e.name)}</option>`).join('');
    root.appendChild(dl);
  }
  const conf = (c) => `${Math.round(c.confidence * 100)}%`;

  function renderSensors() {
    datalist();
    const fresh = !orig.thermostat.entity_id;
    $('[data-testid=sensor-fields]').innerHTML = SENSORS.map(([k, label, hint]) => {
      const sug = disc.suggestions[k] || [];
      const cur = st.sensors[k] || (fresh && sug[0] && sug[0].confidence >= 0.6 ? sug[0].entity_id : '');
      const chips = sug.slice(0, 3).map((c) => `<button type="button" class="chip-btn outline secondary" data-pick="${esc(c.entity_id)}"
        data-for="${k}" title="${esc(c.reason)}">${esc(c.entity_id)} <small>${conf(c)}</small></button>`).join('');
      return `<div class="sensor" data-sensor="${k}">
        <label>${esc(label)} <small>(${esc(hint)})</small>
          <input type="search" list="dl-all" data-testid="sensor-${k}" name="sensor-${k}" value="${esc(cur)}"
            placeholder="search entities… (empty = not used)" autocomplete="off"></label>
        <div class="chips" data-testid="suggest-${k}">${chips || '<small>no suggestion</small>'}</div></div>`;
    }).join('');
  }
  root.addEventListener('click', (e) => {
    const b = e.target.closest('[data-pick]');
    if (b) { e.preventDefault(); $(`[data-testid=sensor-${b.dataset.for}]`).value = b.dataset.pick; }
  });

  function roomRow(key, entity, floor, on, c) {
    return `<div class="room-row" data-testid="room-row">
      <input type="checkbox" aria-label="use room" ${on ? 'checked' : ''} data-r="on">
      <input data-r="key" aria-label="room name" value="${esc(key)}" placeholder="room">
      <input type="search" list="dl-all" data-r="temp" aria-label="temperature sensor" value="${esc(entity)}" placeholder="sensor.…">
      <input data-r="floor" aria-label="floor (owner confirmed)" value="${esc(floor)}" maxlength="64" placeholder="floor (confirm)">
      <small>${c ? esc(conf(c) + ' ' + c.reason) : ''}</small></div>`;
  }
  function renderRooms() {
    const used = new Set(Object.values(st.rooms).map((r) => r.temp));
    const rows = Object.entries(st.rooms).filter(([k]) => k !== 'thermostat')
      .map(([k, r]) => roomRow(k, r.temp, r.floor || '', true, null));
    for (const c of disc.suggestions.rooms) if (!used.has(c.entity_id)) rows.push(roomRow(c.room, c.entity_id, '', false, c));
    $('[data-testid=room-list]').innerHTML = rows.join('') || '<p><small>No temperature sensors found.</small></p>';
  }

  function candidateList(selector, items) {
    const box = $(selector);
    box.innerHTML = items.length ? items.map((c) => `<li><code>${esc(c.entity_id)}</code> — ${esc(c.name)}
      <small>${esc(c.state)}${c.device_class ? ` · ${esc(c.device_class)}` : ''}
      · ${conf(c)} · ${esc(c.reason)}</small></li>`).join('')
      : '<li><small>None discovered.</small></li>';
  }
  function renderHomeContext(ctx, error) {
    const location = $('[data-testid=home-location]');
    const sun = $('[data-testid=sun-position]');
    if (!ctx) {
      location.textContent = `Home context unavailable${error ? `: ${error}` : '.'}`;
      sun.textContent = '';
    } else {
      location.textContent = ctx.location_available
        ? `HA location: ${ctx.location_name || 'Home'} · ${ctx.latitude}, ${ctx.longitude}${ctx.time_zone ? ` · ${ctx.time_zone}` : ''}`
        : ctx.location_error;
      const p = ctx.sun;
      sun.textContent = p
        ? `Sun: ${p.state}${p.azimuth == null ? '' : ` · azimuth ${p.azimuth}°`}${p.elevation == null ? '' : ` · elevation ${p.elevation}°`}`
        : 'Sun position is not available from sun.sun.';
    }
    candidateList('[data-testid=fan-candidates]', disc.suggestions.fans);
    candidateList('[data-testid=cover-candidates]', disc.suggestions.covers);
    candidateList('[data-testid=heater-candidates]', disc.suggestions.supplemental_heat);
    renderOpeningRules();
  }

  function renderOpeningRules() {
    const policy = st.openings;
    $('[data-testid=openings-enabled]').checked = policy.enabled;
    $('[data-testid=opening-min-open]').value = policy.min_open_min;
    $('[data-testid=opening-recovery]').value = policy.recovery_min;
    $('[data-testid=opening-stale]').value = policy.stale_after_min;
    const candidates = new Map(disc.suggestions.openings.map((c) => [c.entity_id, c]));
    for (const [entity_id, cfg] of Object.entries(policy.sensors)) {
      if (!candidates.has(entity_id)) {
        candidates.set(entity_id, {
          entity_id, name: entity_id, state: 'not currently discovered', device_class: 'saved mapping',
        });
      }
    }
    const roles = [
      ['unknown', 'Confirm role…'], ['exterior', 'Exterior door'],
      ['garage_exterior', 'Garage exterior door'], ['garage_to_house', 'Garage-to-house door'],
    ];
    $('[data-testid=opening-rules]').innerHTML = Array.from(candidates.values()).map((c) => {
      const cfg = policy.sensors[c.entity_id] || {};
      return `<div class="sensor opening-rule" data-opening-entity="${esc(c.entity_id)}">
        <label><input type="checkbox" data-opening="use"${policy.sensors[c.entity_id] ? ' checked' : ''}>
          ${esc(c.name)} <code>${esc(c.entity_id)}</code> <small>${esc(c.state)}${c.device_class ? ` · ${esc(c.device_class)}` : ''}</small>
        </label>
        <label>Role<select data-opening="role">${roles.map(([v, n]) =>
          `<option value="${v}"${(cfg.role || 'unknown') === v ? ' selected' : ''}>${n}</option>`).join('')}</select></label>
        <label>Affected room keys, comma-separated
          <input data-opening="rooms" value="${esc((cfg.affected_rooms || []).join(', '))}" placeholder="living_room, kitchen"></label>
        <label>Garage path requires these garage-to-house contacts
          <input data-opening="requires" list="dl-all" value="${esc((cfg.required_openings || []).join(', '))}" placeholder="binary_sensor.garage_entry"></label>
        <label>HA state that means open<select data-opening="active">
          <option value="on"${(cfg.active_state || 'on') === 'on' ? ' selected' : ''}>on</option>
          <option value="off"${cfg.active_state === 'off' ? ' selected' : ''}>off</option>
        </select></label>
      </div>`;
    }).join('') || '<p><small>No door contacts discovered. Add a binary_sensor with device class door or garage_door in Home Assistant.</small></p>';
  }

  function readSensors() {
    const ids = new Set(disc.all.map((e) => e.entity_id));
    const bad = [];
    for (const [k] of SENSORS) {
      const v = $(`[data-testid=sensor-${k}]`).value.trim();
      if (v && !ids.has(v)) bad.push(`${k}: ${v} is not a Home Assistant entity`);
      st.sensors[k] = v;
    }
    const rooms = { thermostat: st.rooms.thermostat || { temp: st.thermostat.entity_id, stale_min: 10, dead_min: 10 } };
    rooms.thermostat.temp = st.thermostat.entity_id;
    for (const row of $$('[data-testid=room-row]')) {
      if (!$('[data-r=on]', row).checked) continue;
      const key = slug($('[data-r=key]', row).value);
      const temp = $('[data-r=temp]', row).value.trim();
      const floor = $('[data-r=floor]', row).value.trim();
      if (!key || !temp) { bad.push('every ticked room needs a name and a sensor'); continue; }
      if (rooms[key]) { bad.push(`room ${key} listed twice`); continue; }
      if (!ids.has(temp)) bad.push(`room ${key}: ${temp} is not a Home Assistant entity`);
      rooms[key] = { ...(orig.rooms[key] || {}), temp, floor };
    }
    st.rooms = rooms;
    // weights: drop rooms that are gone; a new room joins the Home block at weight 1
    for (const blk of ['home', 'sleep', 'away']) {
      const w = st.weights[blk] || {};
      for (const k of Object.keys(w)) if (!rooms[k]) delete w[k];
      if (!Object.keys(w).length) w.thermostat = 1;
      st.weights[blk] = w;
    }
    for (const k of Object.keys(rooms)) {
      if (!orig.rooms[k] && !Object.values(st.weights).some((w) => k in w)) st.weights.home[k] = 1;
    }
    return [bad.join('\n'), readOpenings()].filter(Boolean).join('\n');
  }

  function readOpenings() {
    const enabled = $('[data-testid=openings-enabled]').checked;
    const rules = {};
    const bad = [];
    for (const row of $$('.opening-rule')) {
      if (!$('[data-opening=use]', row).checked) continue;
      const id = row.dataset.openingEntity;
      const role = $('[data-opening=role]', row).value;
      const affected = $('[data-opening=rooms]', row).value.split(',').map((x) => x.trim()).filter(Boolean);
      const required = $('[data-opening=requires]', row).value.split(',').map((x) => x.trim()).filter(Boolean);
      if (role === 'unknown') bad.push(`${id}: confirm whether it is an exterior, garage, or garage-to-house contact`);
      if (!affected.length || affected.some((room) => !st.rooms[room]))
        bad.push(`${id}: map at least one configured affected room`);
      if (role === 'garage_exterior' && !required.length)
        bad.push(`${id}: a garage exterior door requires its garage-to-house contact`);
      if (role !== 'garage_exterior' && required.length)
        bad.push(`${id}: related contacts are only valid for a garage exterior door`);
      rules[id] = { role, affected_rooms: affected, required_openings: required,
        active_state: $('[data-opening=active]', row).value };
    }
    if (enabled && !Object.keys(rules).length) bad.push('Enable the assessment only after mapping at least one door contact');
    for (const [id, cfg] of Object.entries(rules)) {
      for (const required of cfg.required_openings) {
        if (!rules[required] || rules[required].role !== 'garage_to_house')
          bad.push(`${id}: ${required} must be selected and mapped as a garage-to-house contact`);
      }
    }
    const minOpen = Number($('[data-testid=opening-min-open]').value);
    const recovery = Number($('[data-testid=opening-recovery]').value);
    const stale = Number($('[data-testid=opening-stale]').value);
    if (!Number.isFinite(minOpen) || minOpen < 1 || minOpen > 1440)
      bad.push('Sustained-open threshold must be between 1 and 1440 minutes');
    if (!Number.isFinite(recovery) || recovery < 0 || recovery > 120)
      bad.push('Recovery delay must be between 0 and 120 minutes');
    if (!Number.isFinite(stale) || stale < 0 || stale > 1440)
      bad.push('Stale threshold must be 0 to disable or between 1 and 1440 minutes');
    st.openings = {
      enabled,
      min_open_min: minOpen,
      recovery_min: recovery,
      stale_after_min: stale,
      sensors: rules,
    };
    return bad.join('\n');
  }

  // ---- step 4: people ----
  function personRow(name, main, fallback, on, c) {
    return `<div class="person-row" data-testid="person-row">
      <input type="checkbox" aria-label="use person" ${on ? 'checked' : ''} data-p="on">
      <input data-p="name" aria-label="display name" value="${esc(name)}" placeholder="name">
      <input type="search" list="dl-all" data-p="companion" aria-label="tracker" value="${esc(fallback)}" placeholder="person.… / device_tracker.…">
      <input type="search" list="dl-all" data-p="life360" aria-label="preferred tracker" value="${esc(main)}" placeholder="preferred tracker (optional)">
      <small>${c ? esc(conf(c) + ' ' + c.reason) : ''}</small></div>`;
  }
  function renderPeople() {
    const used = new Set();
    const rows = Object.entries(st.presence.people).map(([n, p]) => {
      used.add(p.life360); used.add(p.companion);
      return personRow(n, p.life360 || '', p.companion || '', true, null);
    });
    for (const c of disc.suggestions.people) {
      if (used.has(c.entity_id)) continue;
      rows.push(personRow(slug(c.entity_id.split('.')[1]), '', c.entity_id, false, c));
    }
    $('[data-testid=people-list]').innerHTML = rows.join('') || '<p><small>No person or device_tracker entities found.</small></p>';
  }
  function readPeople() {
    const ids = new Set(disc.all.map((e) => e.entity_id));
    const people = {};
    const bad = [];
    for (const row of $$('[data-testid=person-row]')) {
      if (!$('[data-p=on]', row).checked) continue;
      const name = slug($('[data-p=name]', row).value);
      const life360 = $('[data-p=life360]', row).value.trim() || null;
      const companion = $('[data-p=companion]', row).value.trim() || null;
      if (!name) { bad.push('every ticked person needs a name'); continue; }
      if (people[name]) { bad.push(`${name} listed twice`); continue; }
      if (!life360 && !companion) { bad.push(`${name}: pick a tracker`); continue; }
      for (const t of [life360, companion]) if (t && !ids.has(t)) bad.push(`${name}: ${t} is not a Home Assistant entity`);
      if (st.presence.excluded.includes(name)) bad.push(`${name} is in presence.excluded; pick another name`);
      people[name] = { life360, companion };
    }
    st.presence.people = people;
    return bad.join('\n');
  }

  // ---- step 5: comfort bands ----
  function renderBands() {
    $('[data-testid=units]').value = units();
    $('[data-testid=deadband]').textContent = (units() === 'C'
      ? Math.round(st.writes.deadband_f * 5 / 9 * 10) / 10 : st.writes.deadband_f) + '°' + units();
    const rows = [];
    for (const s of ['heat', 'cool']) {
      for (const b of ['home', 'sleep', 'away']) {
        const [lo, hi] = st.bands[s][b];
        const inp = (i, v) => `<input type="number" step="${units() === 'C' ? 0.1 : 0.5}" inputmode="decimal"
          data-band="${s}.${b}.${i}" data-f="${v}" value="${show(v)}" aria-label="${s} ${b} ${i ? 'high' : 'low'}" data-testid="band-${s}-${b}-${i}">`;
        rows.push(`<tr><td>${s} · ${b}</td><td data-label="Low">${inp(0, lo)}</td><td data-label="High">${inp(1, hi)}</td></tr>`);
      }
    }
    $('[data-testid=bands]').innerHTML = `<table class="cards"><thead><tr><th>Band</th><th>Low °${units()}</th>
      <th>High °${units()}</th></tr></thead><tbody>${rows.join('')}</tbody></table>`;
  }
  root.addEventListener('input', (e) => {
    if (e.target.dataset.band) e.target.dataset.edited = '1';
    errors('');
  });

  function readBands(silent) {
    const inputs = $$('[data-band]');
    if (!inputs.length) return '';
    for (const i of inputs) {
      const [s, b, n] = i.dataset.band.split('.');
      const v = i.dataset.edited ? store(Number(i.value)) : Number(i.dataset.f);
      if (i.value === '' || Number.isNaN(v)) continue;
      st.bands[s][b][Number(n)] = v;
    }
    if (silent) return '';
    const bad = [];
    const db = st.writes.deadband_f;
    for (const i of inputs) i.removeAttribute('aria-invalid');
    const mark = (s, b, n) => $(`[data-band="${s}.${b}.${n}"]`).setAttribute('aria-invalid', 'true');
    for (const b of ['home', 'sleep', 'away']) {
      const [hl, hh] = st.bands.heat[b];
      const [cl, ch] = st.bands.cool[b];
      if (hl > hh) { bad.push(`heat ${b}: low is above high`); mark('heat', b, 0); }
      if (cl > ch) { bad.push(`cool ${b}: low is above high`); mark('cool', b, 0); }
      if (hh > cl) { bad.push(`${b}: heat high must not be above cool low`); mark('heat', b, 1); mark('cool', b, 0); }
      if (ch - hl < db) { bad.push(`${b}: cool high − heat low must be at least ${db}°F`); mark('cool', b, 1); mark('heat', b, 0); }
    }
    return bad.join('\n');
  }

  // ---- step 6: weather (v0.4.0) ----
  function renderWeather() {
    $('[data-testid=weather-provider]').value = st.weather.provider;
    $('[data-testid=weather-lat]').value = st.weather.lat == null ? '' : st.weather.lat;
    $('[data-testid=weather-lon]').value = st.weather.lon == null ? '' : st.weather.lon;
  }
  function readWeather() {
    const la = $('[data-testid=weather-lat]').value.trim();
    const lo = $('[data-testid=weather-lon]').value.trim();
    st.weather.provider = $('[data-testid=weather-provider]').value;
    if (!la && !lo) { st.weather.lat = null; st.weather.lon = null; return ''; }
    const a = Number(la);
    const o = Number(lo);
    if (!la || !lo || Number.isNaN(a) || Number.isNaN(o)) return 'Enter both latitude and longitude, or leave both empty.';
    if (a < -90 || a > 90) return 'Latitude must be between -90 and 90.';
    if (o < -180 || o > 180) return 'Longitude must be between -180 and 180.';
    st.weather.lat = a;
    st.weather.lon = o;
    return '';
  }

  // ---- step 7: review + save ----
  function renderReview() {
    const ppl = Object.entries(st.presence.people);
    const rooms = Object.entries(st.rooms);
    const band = (s, b) => st.bands[s][b].map(show).join('–');
    const row = (t, v, id) => `<dt>${esc(t)}</dt><dd${id ? ` data-testid="${id}"` : ''}>${v}</dd>`;
    $('[data-testid=review]').innerHTML = '<dl class="review" data-testid="review-table">'
      + row('Thermostat', esc(st.thermostat.entity_id), 'review-thermostat')
      + row('Weather', esc((st.weather.provider === 'nws' ? 'NWS' : 'Open-Meteo') + ', '
        + (st.weather.lat == null ? 'HA home location' : st.weather.lat + ', ' + st.weather.lon)), 'review-weather')
      + SENSORS.map(([k, l]) => row(l, esc(st.sensors[k] || '-'), 'review-' + k)).join('')
      + row('Rooms', rooms.map(([k, r]) => esc(k + ' = ' + r.temp)).join('<br>'), 'review-rooms')
      + row('People', ppl.length ? ppl.map(([n, p]) => esc(n + ' = ' + (p.life360 || p.companion))).join('<br>')
        : 'none (always occupied)', 'review-people')
      + ['home', 'sleep', 'away'].map((b) => row(b + ' bands',
        `heat ${band('heat', b)}, cool ${band('cool', b)} °${units()}`)).join('')
      + '</dl>';
    const changed = st.thermostat.entity_id !== root.dataset.running;
    $('[data-testid=restart-note]').hidden = !changed;
    $('[data-testid=restart-from]').textContent = root.dataset.running || '(none)';
    $('[data-testid=restart-to]').textContent = st.thermostat.entity_id;
  }

  async function save() {
    const r = await tb.api('POST', 'api/setup/save', st);
    if (!r.ok) { errors(tb.detail(r)); tb.toast(tb.detail(r), 'error'); return; }
    tb.toast(r.data.restart_required ? 'Saved. Restart thermabot to use the new thermostat.' : 'Saved', 'ok');
    $('[data-testid=setup-save]').disabled = true;
    $('[data-testid=setup-save]').textContent = 'Saved';
    root.dataset.saved = '1';
  }

  root.addEventListener('click', (e) => {
    const b = e.target.closest('[data-setup]');
    if (!b) return;
    e.preventDefault();
    const a = b.dataset.setup;
    if (a === 'ha-test') haTest();
    else if (a === 'back') go(step - 1);
    else if (a === 'next') { const m = validateStep(); if (m) errors(m); else go(step + 1); }
    else if (a === 'add-room') $('[data-testid=room-list]').insertAdjacentHTML('beforeend', roomRow('', '', true, null));
    else if (a === 'defaults') { st.bands = JSON.parse(JSON.stringify(DEFAULT_BANDS)); renderBands(); }
    else if (a === 'save') save();
  });

  go(1);
})();
