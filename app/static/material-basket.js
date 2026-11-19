/* The server owns selection and visibility. Never persist filenames/tokens in localStorage. */
window.MaterialBasket = (() => {
  let deps, items = [], owner = null, generation = 0, requestSequence = 0, busy = false;
  const $ = s => document.querySelector(s);
  function button(id) {
    if (!deps?.user()?.id) return '';
    const present = items.some(item => Number(item.id) === Number(id));
    return `<button type="button" class="basket-add secondary" data-basket-add="${Number(id)}" aria-pressed="${present}">${present ? '已在素材篮' : '+ 素材篮'}</button>`;
  }
  function render() {
    $('#materialBasketCount').textContent = String(items.length);
    document.querySelectorAll('[data-basket-add]').forEach(el => {
      const present = items.some(item => Number(item.id) === Number(el.dataset.basketAdd));
      el.textContent = present ? '已在素材篮' : '+ 素材篮';
      el.setAttribute('aria-pressed', String(present)); el.disabled = busy;
    });
    const unavailable = items.filter(item => !item.available).length;
    $('#materialBasketStatus').textContent = `${items.length} / 200 个素材${unavailable ? `，${unavailable} 个不可用，请移除后交接` : ' · 已保存到当前账号，跨目录/搜索保留'}`;
    $('#materialBasketItems').innerHTML = items.length ? items.map(item => `<article class="basket-item"><div>${item.available ? `<button type="button" class="text-button" data-basket-preview="${item.id}">${deps.escape(item.name || '素材')}</button><small>${deps.escape(item.relative_path || '')} · ${deps.escape(item.kind || '')} · ${deps.bytes(item.size || 0)}</small>` : '<b>素材已失效或无权访问</b><small>不再显示原名称或路径</small>'}</div><button type="button" class="secondary" data-basket-remove="${item.id}" ${busy ? 'disabled' : ''}>移除</button></article>`).join('') : '<div class="empty-state compact"><b>素材篮还是空的</b><p>在素材库或搜索结果里点击“+ 素材篮”，切换目录后继续选。</p></div>';
    $('#materialBasketHandoff').disabled = busy || !items.length || unavailable > 0 || !$('#materialBasketProject').value;
    $('#materialBasketClear').disabled = busy || !items.length;
    $('#materialBasketRefresh').disabled = busy;
    $('#materialBasketProject').disabled = busy;
  }
  async function refresh(force = false) {
    if (busy && !force) return;
    const version = generation, sequence = ++requestSequence;
    const data = await deps.api('/api/material-basket');
    if (version !== generation || sequence !== requestSequence) return;
    items = data.items; render();
  }
  async function mutate(path, options) {
    if (busy) return;
    const version = generation; ++requestSequence; busy = true; render();
    try {
      const data = await deps.api(path, options);
      if (version !== generation) return;
      items = data.items; render();
    } finally { if (version === generation) {busy = false; render();} }
  }
  function userChanged() {
    if (!deps) return;
    const next = deps.user()?.id ?? null;
    if (owner === next) return;
    owner = next; generation++; items = []; busy = false;
    deps.close($('#materialBasketModal')); render();
    if (next) refresh().catch(() => {});
  }
  async function open() {
    if (!owner) return;
    deps.open($('#materialBasketModal'));
    $('#materialBasketResult').textContent = '';
    const version = generation;
    await refresh();
    const projects = await deps.api('/api/projects');
    if (version !== generation) return;
    const eligible = projects.filter(p => ['owner','manager','editor'].includes(p.access_role));
    const select = $('#materialBasketProject'), previous = select.value;
    select.innerHTML = '<option value="">选择有编辑权限的项目</option>' + eligible.map(p => `<option value="${p.id}">${deps.escape(p.name)}</option>`).join('');
    if (eligible.some(p => String(p.id) === previous)) select.value = previous;
    render();
  }
  function init(options) {
    deps = options;
    $('#materialBasketOpen').addEventListener('click', () => open().catch(e => deps.toast(e.message, true)));
    $('#materialBasketProject').addEventListener('change', () => { $('#materialBasketResult').textContent = ''; render(); });
    $('#materialBasketRefresh').addEventListener('click', () => refresh().catch(e => deps.toast(e.message,true)));
    $('#materialBasketClear').addEventListener('click', async () => {
      const version = generation;
      if (!await deps.confirm({title:'清空素材篮',body:'仅移除选中记录，不删除原文件。',confirmText:'清空'})) return;
      if (version !== generation || !owner) return;
      mutate('/api/material-basket',{method:'DELETE'}).catch(e => deps.toast(e.message,true));
    });
    $('#materialBasketHandoff').addEventListener('click', async () => {
      if (busy || !items.length) return;
      const projectId = Number($('#materialBasketProject').value), version = generation;
      if (!projectId) return;
      busy = true; render();
      try {
        const result = await deps.api('/api/material-basket/handoff',{method:'POST',body:JSON.stringify({project_id:projectId})});
        if (version !== generation) return;
        $('#materialBasketResult').innerHTML = `已加入 ${result.added} 个，已有 ${result.skipped} 个。原文件未移动，素材篮保留。 <button type="button" class="secondary" id="materialBasketOpenProject">进入项目继续审阅 / 交付</button>`;
        $('#materialBasketOpenProject').onclick = () => {deps.close($('#materialBasketModal')); deps.project(projectId);};
      } catch(e) {if(version===generation){deps.toast(e.message,true);await refresh(true).catch(()=>{});}}
      finally {if(version===generation){busy=false;render();}}
    });
    // Capture stops the card's existing preview/selection handlers from opening on add.
    document.addEventListener('click', event => {
      const add = event.target.closest('[data-basket-add]');
      const remove = event.target.closest('[data-basket-remove]');
      const preview = event.target.closest('[data-basket-preview]');
      if (!add && !remove && !preview) return;
      event.preventDefault(); event.stopImmediatePropagation();
      if (!owner || busy) return;
      if (preview) {deps.preview(Number(preview.dataset.basketPreview)); return;}
      if (add && items.some(i => Number(i.id) === Number(add.dataset.basketAdd))) {open().catch(e=>deps.toast(e.message,true));return;}
      const operation = add ? mutate('/api/material-basket',{method:'POST',body:JSON.stringify({file_ids:[Number(add.dataset.basketAdd)]})}) : mutate(`/api/material-basket/${Number(remove.dataset.basketRemove)}`,{method:'DELETE'});
      operation.catch(e => deps.toast(e.message,true));
    },true);
    document.addEventListener('dblclick', event => {if(event.target.closest('[data-basket-add]')) event.stopImmediatePropagation();},true);
    render();
  }
  return {init,button,userChanged,refresh};
})();
