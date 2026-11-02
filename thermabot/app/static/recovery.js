(() => {
  const $ = (s) => document.querySelector(s);
  const { api, detail, toast } = window.tb;
  let pending = null;
  const error = (text) => { $('#recovery-errors').textContent = text; };
  function invalidate() {
    pending = null;
    $('#import-preview').hidden = true;
    error('');
  }
  $('#config-file').addEventListener('change', invalidate);
  $('#config-import').addEventListener('submit', async (event) => {
    event.preventDefault();
    invalidate();
    const file = $('#config-file').files[0];
    if (!file || file.size > 1024 * 1024) { error('Choose a configuration file smaller than 1 MB.'); return; }
    try {
      const document = JSON.parse(await file.text());
      const result = await api('POST', 'api/config/preview', { document });
      if (!result.ok) { error(detail(result)); return; }
      pending = { document, expected_current: result.data.expected_current };
      $('#import-changes').textContent = 'Changed sections: ' +
        (result.data.changed_sections.join(', ') || 'none') +
        (result.data.restart_required ? '. Thermostat restart required.' : '.');
      $('#import-values').textContent = JSON.stringify(result.data.changes, null, 2);
      $('#import-preview').hidden = false;
    } catch (e) { error('Cannot preview configuration: ' + e.message); }
  });
  $('#config-apply').addEventListener('click', async () => {
    if (!pending) return;
    const button = $('#config-apply');
    button.disabled = true;
    error('');
    try {
      const result = await api('POST', 'api/config/import', pending);
      if (!result.ok) { error(detail(result)); return; }
      pending = null;
      $('#import-preview').hidden = true;
      toast('Configuration imported. Bot remains stopped.' +
        (result.data.restart_required ? ' Restart Thermabot to use the saved thermostat.' : ''));
      await status();
    } catch (e) { error('Import request failed; check configuration before retrying: ' + e.message); }
    finally { button.disabled = false; }
  });
  $('#backup-create').addEventListener('click', async () => {
    const button = $('#backup-create');
    button.disabled = true;
    error('');
    try {
      const result = await api('POST', 'api/backups');
      if (!result.ok) { error(detail(result)); return; }
      const link = document.createElement('a');
      link.href = 'api/backups/' + encodeURIComponent(result.data.filename);
      link.textContent = 'Download verified backup';
      $('#backup-result').replaceChildren(link);
      link.click();
      await status();
    } catch (e) { error('Backup request failed: ' + e.message); }
    finally { button.disabled = false; }
  });
  async function status() {
    const result = await api('GET', 'api/recovery');
    if (!result.ok) { error(detail(result)); return; }
    $('#automatic-backup').textContent = result.data.automatic_backup;
    $('#recovery-health').textContent = 'HA connection: ' + result.data.ha +
      '. Bot: ' + result.data.state + '. Heartbeat URL: ' +
      (result.data.heartbeat_configured ? 'configured (delivery not verified here).' : 'not configured.');
    $('#backup-list').replaceChildren();
    for (const backup of result.data.backups) {
      const item = document.createElement('li');
      const link = document.createElement('a');
      link.href = 'api/backups/' + encodeURIComponent(backup.filename);
      link.textContent = backup.filename + ' (' + Math.ceil(backup.bytes / 1024) + ' KB)';
      item.append(link);
      $('#backup-list').append(item);
    }
    if (!result.data.backups.length) $('#backup-list').textContent = 'No local backups yet.';
  }
  status().catch((e) => error('Cannot load recovery status: ' + e.message));
})();
