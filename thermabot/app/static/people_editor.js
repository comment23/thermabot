// Shared presence mappings for setup and Settings. Drafts never create login accounts.
(() => {
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const slug = (s) => s.toLowerCase().replace(/[^a-z0-9_]+/g, '_').replace(/^_+|_+$/g, '');

  window.tbPeopleEditor = (root, settings, discovery = null) => {
    const $ = (s, r = root) => r.querySelector(s);
    const rows = () => Array.from(root.querySelectorAll('[data-testid=person-row]'));
    const included = (row) => $('[data-p=on]', row).checked;
    const nameOf = (row) => {
      const name = $('[data-p=name]', row).value.trim();
      return name === row.dataset.savedName ? name : slug(name);
    };
    let known = new Set();
    function empty() {
      $('[data-people-empty]').hidden = rows().some(included);
    }
    function person(name = '', cfg = {}, selected = true) {
      const row = document.createElement('article');
      row.className = 'presence-person';
      row.dataset.testid = 'person-row';
      row.dataset.savedName = name;
      row.innerHTML = `<div class="card-head">
        <label class="review-check"${selected ? ' hidden' : ''}><input type="checkbox" data-p="on" aria-label="use person"${selected ? ' checked' : ''}>
          <span>Include this person in home/away presence</span></label>
        <button type="button" class="secondary outline" data-person-remove>Remove person</button>
        </div><div class="grid-3">
        <label>Name<input data-p="name" aria-label="display name" value="${esc(name)}" autocomplete="off"></label>
        <label>Presence entity<input type="search" data-p="companion" aria-label="tracker" list="presence-entities"
          value="${esc(cfg.companion)}" placeholder="person or device_tracker entity"></label>
        <label>Preferred tracker (optional)<input type="search" data-p="life360" aria-label="preferred tracker"
          list="presence-entities" value="${esc(cfg.life360)}"></label></div>`;
      $('[data-people-rows]').appendChild(row);
      empty();
      return row;
    }
    function apply(body) {
      body.presence.people = Object.fromEntries(rows().filter(included).map((row) => [
        nameOf(row), {
          life360: $('[data-p=life360]', row).value.trim() || null,
          companion: $('[data-p=companion]', row).value.trim() || null,
        },
      ]));
    }
    function validate(body, requireKnown = false) {
      const bad = [];
      const names = new Set();
      for (const row of rows().filter(included)) {
        const name = nameOf(row);
        const trackers = ['life360', 'companion'].map((key) => $('[data-p=' + key + ']', row).value.trim()).filter(Boolean);
        if (!name) bad.push('Every person needs a name.');
        else if (names.has(name)) bad.push(`${name}: this name is already listed.`);
        names.add(name);
        if (body.presence.excluded.includes(name)) bad.push(`${name} is excluded from presence; choose another name or update the excluded list.`);
        if (!trackers.length) bad.push(`${name || 'Person'}: choose a tracker.`);
        for (const id of trackers) {
          if (!/^[a-z_]+\.[a-z0-9_]+$/.test(id)) bad.push(`${name}: enter a Home Assistant entity ID.`);
          else if (requireKnown && !known.has(id)) bad.push(`${name}: ${id} is not a Home Assistant entity.`);
        }
      }
      const message = bad.join('\n');
      $('[data-people-errors]').textContent = message;
      return message;
    }
    function load(data, suggestions = false) {
      known = new Set(data.all.map((e) => e.entity_id));
      const candidates = data.all.filter((e) => /^(person|device_tracker)\./.test(e.entity_id));
      $('#presence-entities').innerHTML = candidates.map((e) =>
        `<option value="${esc(e.entity_id)}" label="${esc(e.name || e.entity_id)}"></option>`).join('');
      if (suggestions) {
        const used = new Set(rows().flatMap((row) =>
          ['life360', 'companion'].map((key) => $('[data-p=' + key + ']', row).value)));
        for (const e of data.suggestions.people) {
          if (!used.has(e.entity_id)) person(slug(e.entity_id.split('.')[1]), { companion: e.entity_id }, false);
        }
      }
      $('[data-people-status]').textContent = `${candidates.length} presence suggestions loaded. Saved mappings are retained even when undiscovered.`;
    }
    async function refresh() {
      const button = $('[data-people-refresh]');
      button.disabled = true;
      $('[data-people-status]').textContent = 'Loading Home Assistant presence suggestions...';
      const res = await window.tb.api('GET', 'api/setup/discover?thermostat=' + encodeURIComponent(settings.thermostat.entity_id));
      button.disabled = false;
      if (res.ok) load(res.data);
      else {
        $('[data-people-status]').textContent = 'Discovery failed: ' + window.tb.detail(res) + '. Saved mappings have not changed; you can enter entity IDs manually.';
        window.tb.toast(window.tb.detail(res), 'error');
      }
    }
    root.addEventListener('change', empty);
    root.addEventListener('click', async (e) => {
      const button = e.target.closest('button');
      if (!button) return;
      if (button.hasAttribute('data-people-add')) {
        const row = person();
        $('[data-p=name]', row).focus();
        root.dispatchEvent(new Event('change', { bubbles: true }));
      } else if (button.hasAttribute('data-person-remove')) {
        const row = button.closest('[data-testid=person-row]');
        if (await window.tb.ask({
          title: 'Remove person',
          body: 'Remove this presence mapping? No Home Assistant entity or login account is deleted. With no people, the house always counts as occupied. Save to apply.',
          confirm: 'Remove person',
        })) {
          row.remove();
          root.dispatchEvent(new Event('change', { bubbles: true }));
        }
      } else if (button.hasAttribute('data-people-refresh')) refresh();
    });
    for (const [name, cfg] of Object.entries(settings.presence.people)) person(name, cfg);
    empty();
    if (discovery) load(discovery, true);
    else if (root.dataset.canDiscover === '1') refresh();
    return { apply, validate, load };
  };
})();
