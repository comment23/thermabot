// Read-only simulations use the shared ingress-aware API and CSRF helper.
(() => {
  document.querySelectorAll('form[data-room-simulation]').forEach((form) => {
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const button = form.querySelector('button');
      const output = form.querySelector('[data-simulation-result]');
      const errors = form.querySelector('.errors');
      const fields = new FormData(form);
      const body = { scenario: fields.get('scenario'), hours: Number(fields.get('hours')) };
      if (fields.has('setback_f')) {
        body.setback_f = Number(fields.get('setback_f')) * (form.dataset.units === 'C' ? 9 / 5 : 1);
      }
      button.disabled = true;
      errors.textContent = '';
      output.textContent = 'Calculating simulation…';
      try {
        const result = await tb.api('POST', form.dataset.roomSimulation, body);
        if (!result.ok) {
          errors.textContent = tb.detail(result);
          output.textContent = '';
          return;
        }
        const data = result.data;
        if (!data.available) {
          output.textContent = 'Unavailable: ' + data.reason;
          return;
        }
        const heading = document.createElement('p');
        heading.className = 'banner';
        heading.textContent = 'SIMULATED — ' + (data.recommended
          ? 'Worth reviewing; no changes applied.' : 'No evidence to recommend this change.');
        const table = document.createElement('table');
        table.className = 'simulation-comparison';
        table.dataset.testid = 'simulation-comparison';
        const caption = table.createCaption();
        caption.textContent = `Baseline vs proposed · ${Number(body.hours)} hours + 4 hours recovery · SIMULATED`;
        const header = table.createTHead().insertRow();
        for (const text of ['Metric / scope', 'Baseline', 'Proposed']) {
          const cell = document.createElement('th');
          cell.scope = 'col';
          cell.textContent = text;
          header.appendChild(cell);
        }
        const rows = table.createTBody();
        const runtimeMax = Math.max(1, data.baseline.runtime_min, data.proposed.runtime_min);
        const metrics = [
          ['Central HVAC runtime', 'runtime_min', (v) => v.toFixed(1) + ' min', runtimeMax],
          ['Room within ideal band', 'comfort_pct', (v) => v.toFixed(1) + '%', 100],
          ['Projected room range', null, (v) => `${tb.temp(Math.round(v.low_f * 10) / 10, form.dataset.units)}–${tb.temp(Math.round(v.high_f * 10) / 10, form.dataset.units)}`],
          ['Estimated central HVAC cost', 'cost', (v) => v == null ? 'Not configured' : `${v.toFixed(2)} ${data.currency}`],
        ];
        for (const [label, key, format, max] of metrics) {
          const row = rows.insertRow();
          const name = document.createElement('th');
          name.scope = 'row';
          name.textContent = label;
          row.appendChild(name);
          for (const scenario of ['baseline', 'proposed']) {
            const value = key ? data[scenario][key] : data[scenario];
            const cell = row.insertCell();
            cell.dataset.label = scenario === 'baseline' ? 'Baseline' : 'Proposed';
            const text = document.createElement('span');
            text.textContent = format(value);
            cell.appendChild(text);
            if (max != null) {
              const bar = document.createElement('meter');
              bar.min = 0; bar.max = max; bar.value = value;
              bar.setAttribute('aria-label', `${scenario}: ${label}`);
              bar.textContent = text.textContent;
              cell.appendChild(bar);
            }
          }
        }
        const delta = document.createElement('p');
        delta.textContent = `Runtime difference: ${data.saved_min.toFixed(1)} min (positive means less runtime). `
          + (data.cost_saving == null ? 'Costs require configured tariffs and rated power.'
            : `Estimated cost difference: ${data.cost_saving.toFixed(2)} ${data.currency} (positive means lower cost).`);
        const safety = document.createElement('p');
        safety.textContent = data.proposed.safe ? 'Predictions meet the simulated safety bounds.'
          : 'Predictions breach safety bounds: not recommended.';
        const note = document.createElement('p');
        note.className = 'muted';
        note.textContent = data.note;
        output.replaceChildren(heading, table, delta, safety, note);
      } catch (error) {
        errors.textContent = 'Could not render the simulation result: ' + error.message;
        output.textContent = '';
        tb.toast(errors.textContent, 'error');
      } finally {
        button.disabled = false;
      }
    });
  });
})();
