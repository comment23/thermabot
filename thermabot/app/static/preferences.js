// A draft can be applied only after its exact server-validated proposal is reviewed.
(() => {
  const form = document.querySelector('[data-simple-preferences]');
  if (!form) return;
  const ideal = form.elements.ideal_f, slider = form.elements.priority;
  const previewButton = form.querySelector('[data-testid=simple-preview]');
  const applyButton = form.querySelector('[data-testid=simple-apply]');
  const output = form.querySelector('[data-testid=simple-preview-result]');
  const errors = form.querySelector('.errors');
  const priorities = ['comfort', 'balanced', 'efficiency'];
  const flexibilities = JSON.parse(form.dataset.flexibility);
  let preview = null, revision = 0, busy = false, idealEdited = false;
  const units = form.dataset.units;
  const temperature = (f) => tb.temp(f, units);
  const range = (values) => values.map(temperature).join('–');
  const rooms = (block) => [...form.querySelectorAll(`input[name="${block}_rooms"]:checked`)].map((el) => el.value);
  const roomNames = (values) => values.map((v) => v.replaceAll('_', ' ')).join(', ');
  function body() {
    const value = !idealEdited ? Number(form.dataset.idealF)
      : units === 'C' ? Math.round((Number(ideal.value) * 9 / 5 + 32) * 2) / 2 : Number(ideal.value);
    return {
      ideal_f: value, priority: priorities[Number(slider.value)],
      home_rooms: rooms('home'), sleep_rooms: rooms('sleep'),
      confirmed_mappings: form.elements.confirmed_mappings.checked,
      fingerprint: form.dataset.fingerprint,
    };
  }
  function refresh() {
    const index = Number(slider.value);
    const label = priorities[index];
    slider.setAttribute('aria-valuetext', label[0].toUpperCase() + label.slice(1));
    const flexibility = tb.temp(flexibilities[label], units, true).replace(/^\+/, '');
    form.querySelector('[data-testid=priority-description]').textContent =
      `${label[0].toUpperCase() + label.slice(1)}: Home/Sleep reference band ±${flexibility} around your ideal. `
      + (index === 0 ? 'Closer comfort; less flexibility to avoid runtime.'
        : index === 1 ? 'Modest flexibility for comfort and runtime trade-offs.'
          : 'More temperature variation may reduce runtime; savings are not guaranteed.');
    previewButton.disabled = busy || !form.checkValidity() || !rooms('home').length || !rooms('sleep').length;
  }
  form.addEventListener('input', (event) => {
    if (event.target === ideal) idealEdited = true;
    revision++; preview = null;
    output.hidden = true; applyButton.hidden = true; applyButton.disabled = true;
    errors.textContent = '';
    refresh();
  });
  const paragraph = (text) => {
    const element = document.createElement('p');
    element.textContent = text;
    return element;
  };
  function render(data) {
    const heading = document.createElement('h3');
    heading.textContent = 'Review the recommendation';
    const previous = data.previous;
    const before = previous.weather_aware
      ? `Previously: Home ${range(previous.home)}; Sleep ${range(previous.sleep)}.`
      : `Previously: heating Home ${range(previous.heat_home)}, Sleep ${range(previous.heat_sleep)}; `
        + `cooling Home ${range(previous.cool_home)}, Sleep ${range(previous.cool_sleep)}.`;
    const fallback = data.automatic[0] !== data.home[0] || data.automatic[1] !== data.home[1]
      ? `Automatic-mode fallback widens to ${range(data.automatic)} to respect your thermostat's minimum deadband.`
      : `Automatic-mode fallback: ${range(data.automatic)}.`;
    output.replaceChildren(heading,
      paragraph(`Ideal reference: ${temperature(data.ideal_f)}. Priority: ${data.priority}.`),
      paragraph(before),
      paragraph(`Proposed shared Home/Sleep range: ${range(data.home)}. Weather-aware comfort will be enabled.`),
      paragraph(fallback),
      paragraph(`Home rooms: ${roomNames(data.home_rooms)}. Sleep rooms: ${roomNames(data.sleep_rooms)}. `
        + 'Their numeric weights become equal; custom Home/Sleep weights are replaced.'),
      paragraph(data.note),
      paragraph('Saving does not start Active control. If already Active, future guarded decisions use these preferences.'),
      paragraph(data.changed ? 'Review these changes before applying.' : 'These preferences already match the saved settings.'));
    output.hidden = false;
    applyButton.hidden = false; applyButton.disabled = !data.changed;
    applyButton.textContent = data.changed ? 'Apply reviewed preferences' : 'No changes';
  }
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const current = revision;
    busy = true; preview = null; errors.textContent = ''; applyButton.hidden = true; output.hidden = true;
    previewButton.textContent = 'Checking recommendation…'; refresh();
    try {
      const result = await tb.api('POST', 'api/settings/preferences/preview', body());
      if (current !== revision) return;
      if (!result.ok) { errors.textContent = tb.detail(result); tb.toast(errors.textContent, 'error'); return; }
      preview = result.data;
      render(preview);
    } catch (error) {
      errors.textContent = 'Could not load the recommendation: ' + error.message;
      tb.toast(errors.textContent, 'error');
    } finally {
      busy = false; previewButton.textContent = 'Preview recommendation'; refresh();
    }
  });
  applyButton.addEventListener('click', async () => {
    if (!preview || !preview.changed) return;
    const reviewed = preview.preview_fingerprint;
    const confirmed = await tb.ask({
      title: 'Apply comfort preferences',
      body: 'Replace Home/Sleep reference ranges and room weights with this reviewed recommendation? '
        + 'Safety, Away settings, schedule and operating mode stay unchanged. If already Active, future decisions use the new preferences.',
      confirm: 'Apply preferences',
    });
    if (!confirmed || !preview || preview.preview_fingerprint !== reviewed) return;
    busy = true; applyButton.disabled = true; errors.textContent = ''; refresh();
    try {
      const result = await tb.api('POST', 'api/settings/preferences/apply',
        { ...body(), preview_fingerprint: reviewed });
      if (!result.ok) { errors.textContent = tb.detail(result); tb.toast(errors.textContent, 'error'); return; }
      preview = null;
      tb.toast('Saved; replanning. Operating mode unchanged.', 'ok');
      setTimeout(() => location.reload(), 600);
    } catch (error) {
      errors.textContent = 'Could not save preferences: ' + error.message;
      tb.toast(errors.textContent, 'error');
    } finally {
      busy = false; applyButton.disabled = !preview; refresh();
    }
  });
  refresh();
})();
