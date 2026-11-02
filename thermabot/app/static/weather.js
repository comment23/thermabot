// thermabot v0.4.0 weather tools (setup wizard + Settings). Vanilla JS, relative URLs only.
// Each [data-weather-tools] box names its provider/lat/lon inputs with CSS selectors
// (data-provider, data-lat, data-lon). 'Use HA home location' reads GET api/setup/ha-location
// (HA /api/config, read only); 'Test fetch' posts to api/setup/weather-test, which saves nothing.
(() => {
  const tb = window.tb;
  const timeZone = document.documentElement.dataset.timezone;
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const fToC = (f) => Math.round((f - 32) * 5 / 9 * 10) / 10;

  function bind(box) {
    const q = (sel) => document.querySelector(sel);
    const prov = () => q(box.dataset.provider);
    const lat = () => q(box.dataset.lat);
    const lon = () => q(box.dataset.lon);
    const out = box.querySelector('[data-testid=weather-result]');
    const units = () => (box.dataset.units === 'C' ? 'C' : 'F');
    const set = (el, v) => { el.value = v; el.dataset.edited = '1'; el.dispatchEvent(new Event('input', { bubbles: true })); };

    async function haLocation() {
      out.dataset.ok = '';
      out.textContent = 'Reading the Home Assistant home location.';
      const r = await tb.api('GET', 'api/setup/ha-location');
      if (!r.ok || !r.data.ok) {
        out.dataset.ok = 'no';
        out.textContent = 'Failed: ' + (r.ok ? r.data.error : tb.detail(r));
        return null;
      }
      set(lat(), r.data.lat);
      set(lon(), r.data.lon);
      out.dataset.ok = 'yes';
      out.textContent = `Home location${r.data.location_name ? ' of ' + r.data.location_name : ''}: ${r.data.lat}, ${r.data.lon}`;
      return r.data;
    }

    async function test() {
      let la = lat().value.trim();
      let lo = lon().value.trim();
      if (!la || !lo) {
        const h = await haLocation();
        if (!h) return;
        la = h.lat; lo = h.lon;
      }
      out.dataset.ok = '';
      out.textContent = 'Fetching the forecast.';
      const r = await tb.api('POST', 'api/setup/weather-test',
        { provider: prov().value, lat: Number(la), lon: Number(lo) });
      if (!r.ok) { out.dataset.ok = 'no'; out.textContent = 'Failed: ' + tb.detail(r); return; }
      const d = r.data;
      out.dataset.ok = d.ok ? 'yes' : 'no';
      if (!d.ok) { out.textContent = 'Failed: ' + d.error; return; }
      const u = units();
      const rows = d.preview.map((p) => {
        const t = new Date(p.t).toLocaleTimeString([], { timeZone, weekday: 'short', hour: '2-digit', minute: '2-digit' });
        const temp = u === 'C' ? fToC(p.temp_f) : Math.round(p.temp_f * 10) / 10;
        return `<tr><td>${esc(t)}</td><td data-label="\u00b0${u}">${temp}</td>`
          + `<td data-label="RH">${p.humidity == null ? '-' : Math.round(p.humidity) + '%'}</td>`
          + `<td data-label="Sky">${esc(p.conditions || '-')}</td></tr>`;
      }).join('');
      out.innerHTML = `OK: ${esc(d.provider)}, ${d.points} hourly points, ${d.hours} h ahead, ${d.ms} ms`
        + `<table class="cards" data-testid="weather-preview"><thead><tr><th>Time</th><th>\u00b0${u}</th>`
        + `<th>RH</th><th>Sky</th></tr></thead><tbody>${rows}</tbody></table>`;
    }

    box.addEventListener('click', (e) => {
      const b = e.target.closest('[data-weather]');
      if (!b) return;
      e.preventDefault();
      if (b.dataset.weather === 'ha-location') haLocation();
      else if (b.dataset.weather === 'test') test();
    });
  }

  for (const box of document.querySelectorAll('[data-weather-tools]')) bind(box);
})();
