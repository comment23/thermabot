// Schedule editor: pick a block, edit its start and type, preview runtime. Keyboard first.
(() => {
  const form = document.querySelector('[data-schedule-editor]');
  if (!form || !window.tb) return;
  const blocks = () => [...form.querySelectorAll('.week button.block')];
  function show(btn) {
    blocks().forEach((b) => b.setAttribute('aria-pressed', b === btn ? 'true' : 'false'));
    const kind = btn.dataset.kind;
    const index = btn.dataset.index;
    for (const box of form.querySelectorAll('.block-fields')) {
      box.hidden = !(box.dataset.kind === kind && box.dataset.index === index);
    }
  }
  form.addEventListener('click', (e) => {
    const btn = e.target.closest('.week button.block');
    if (btn) show(btn);
  });
  form.addEventListener('keydown', (e) => {
    const btn = e.target.closest('.week button.block');
    if (!btn) return;
    const all = blocks();
    const i = all.indexOf(btn);
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown') { e.preventDefault(); (all[i + 1] || all[0]).focus(); }
    if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') { e.preventDefault(); (all[i - 1] || all[all.length - 1]).focus(); }
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); show(btn); const field = form.querySelector('.block-fields:not([hidden]) input'); if (field) field.focus(); }
  });
  const first = form.querySelector('.week button.block');
  if (first) show(first);
  const previewBtn = form.querySelector('[data-testid=schedule-preview-btn]');
  if (previewBtn) previewBtn.addEventListener('click', async () => {
    const body = JSON.parse(form.dataset.settings);
    for (const el of form.querySelectorAll('[data-path]')) {
      const keys = el.dataset.path.split('.');
      let o = body;
      for (let i = 0; i < keys.length - 1; i++) o = o[keys[i]];
      o[keys[keys.length - 1]] = el.value;
    }
    const res = await window.tb.api('POST', 'api/schedule/preview', body);
    const line = form.querySelector('[data-testid=schedule-preview]');
    if (line) line.textContent = res.ok ? res.data.sentence : window.tb.detail(res);
  });
})();
