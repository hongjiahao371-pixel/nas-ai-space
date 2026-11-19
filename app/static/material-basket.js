/* The server owns selection and visibility. Never persist filenames/tokens in localStorage. */
window.MaterialBasket = (() => {
  let deps, items = [], clips = [], clipDrafts = new Map(), owner = null, generation = 0, requestSequence = 0, busy = false;
  const $ = s => document.querySelector(s);
  function button(id) {
    if (!deps?.user()?.id) return '';
    const present = items.some(item => Number(item.id) === Number(id));
    return `<button type="button" class="basket-add secondary" data-basket-add="${Number(id)}" aria-pressed="${present}">${present ? '已在素材篮' : '+ 素材篮'}</button>`;
  }
  function render() {
    $('#materialBasketCount').textContent = String(items.length + clips.length);
    document.querySelectorAll('[data-basket-add]').forEach(el => {
      const present = items.some(item => Number(item.id) === Number(el.dataset.basketAdd));
      el.textContent = present ? '已在素材篮' : '+ 素材篮';
      el.setAttribute('aria-pressed', String(present)); el.disabled = busy;
    });
    const unavailable = [...items,...clips].filter(item => !item.available).length;
    $('#materialBasketStatus').textContent = `${items.length + clips.length} / 200 项（${clips.length} 个片段）${unavailable ? `，${unavailable} 个不可用，请移除后交接` : ' · 已保存到当前账号，跨目录/搜索保留'}`;
    if(clipDrafts.size) $('#materialBasketStatus').textContent += ' · 有未保存入出点，请保存或取消';
    $('#materialBasketItems').innerHTML = items.length ? items.map(item => `<article class="basket-item"><div>${item.available ? `<button type="button" class="text-button" data-basket-preview="${item.id}">${deps.escape(item.name || '素材')}</button><small>${deps.escape(item.relative_path || '')} · ${deps.escape(item.kind || '')} · ${deps.bytes(item.size || 0)}</small>` : '<b>素材已失效或无权访问</b><small>不再显示原名称或路径</small>'}</div><button type="button" class="secondary" data-basket-remove="${item.id}" ${busy ? 'disabled' : ''}>移除</button></article>`).join('') : '<div class="empty-state compact"><b>素材篮还是空的</b><p>在素材库或搜索结果里点击“+ 素材篮”，切换目录后继续选。</p></div>';
    $('#materialBasketClips').innerHTML = clips.length ? clips.map((clip,index) => `<article class="basket-clip" data-basket-clip="${clip.id}"><b>${index+1}. ${clip.available ? deps.escape(clip.name || '视频') : '视频已失效或无权访问'}</b><div class="form-row"><label>入点（秒）<input data-basket-start type="number" min="0" step="any" value="${clipDrafts.get(clip.id)?.start ?? clip.start_time}" ${busy || !clip.available ? 'disabled' : ''}></label><label>出点（秒）<input data-basket-end type="number" min="0" step="any" value="${clipDrafts.get(clip.id)?.end ?? clip.end_time}" ${busy || !clip.available ? 'disabled' : ''}></label></div><div class="basket-actions"><button type="button" data-basket-clip-action="save" ${busy || !clip.available ? 'disabled' : ''}>保存入出点</button><button type="button" data-basket-clip-action="cancel" ${!clipDrafts.has(clip.id) ? 'disabled' : ''}>取消修改</button><button type="button" data-basket-clip-action="up" ${busy || !index ? 'disabled' : ''}>上移</button><button type="button" data-basket-clip-action="down" ${busy || index===clips.length-1 ? 'disabled' : ''}>下移</button><button type="button" data-basket-clip-action="remove" ${busy ? 'disabled' : ''}>移除片段</button></div></article>`).join('') : '<p>从视频的“选片 / 导出”窗口保存入出点；同一视频可以保留多个片段。</p>';
    $('#materialBasketHandoff').disabled = busy || clipDrafts.size > 0 || !(items.length + clips.length) || unavailable > 0 || !$('#materialBasketProject').value;
    $('#materialBasketClear').disabled = busy || !(items.length + clips.length);
    $('#materialBasketRefresh').disabled = busy;
    $('#materialBasketProject').disabled = busy;
  }
  async function refresh(force = false) {
    if (busy && !force) return;
    const version = generation, sequence = ++requestSequence;
    const data = await deps.api('/api/material-basket');
    if (version !== generation || sequence !== requestSequence) return;
    items = data.items; clips = data.clips || []; render();
  }
  async function mutate(path, options) {
    if (busy) return;
    const version = generation; ++requestSequence; busy = true; render();
    try {
      const data = await deps.api(path, options);
      if (version !== generation) return;
      items = data.items; clips = data.clips || []; render();
    } finally { if (version === generation) {busy = false; render();} }
  }
  function userChanged() {
    if (!deps) return;
    const next = deps.user()?.id ?? null;
    if (owner === next) return;
    owner = next; generation++; items = []; clips = []; clipDrafts.clear(); busy = false;
    deps.close($('#materialBasketModal'));
    $('#materialBasketProject').replaceChildren(new Option('选择有编辑权限的项目', ''));
    $('#materialBasketResult').replaceChildren();
    render();
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
      clipDrafts.clear();
      mutate('/api/material-basket',{method:'DELETE'}).catch(e => deps.toast(e.message,true));
    });
    $('#materialBasketHandoff').addEventListener('click', async () => {
      if (busy || clipDrafts.size || !(items.length + clips.length)) return;
      const projectId = Number($('#materialBasketProject').value), version = generation;
      if (!projectId) return;
      busy = true; render();
      try {
        const result = await deps.api('/api/material-basket/handoff',{method:'POST',body:JSON.stringify({project_id:projectId})});
        if (version !== generation) return;
        $('#materialBasketResult').innerHTML = `已加入 ${result.added} 个，已有 ${result.skipped} 个。原文件未移动，素材篮保留。 <button type="button" class="secondary" id="materialBasketOpenProject">进入项目继续审阅 / 交付</button>`;
        if(result.clips?.length) {
          const launch=document.createElement('button'); launch.type='button'; launch.className='primary';launch.textContent=`将 ${result.clips.length} 段按当前顺序带入粗剪`;
          launch.onclick=async()=>{ launch.disabled=true;try{deps.close($('#materialBasketModal'));await deps.roughCut(projectId,result.clips);}catch(e){deps.toast(e.message,true);}finally{launch.disabled=false;} };
          $('#materialBasketResult').append(launch);
        }
        $('#materialBasketOpenProject').onclick = () => {deps.close($('#materialBasketModal')); deps.project(projectId);};
      } catch(e) {if(version===generation){deps.toast(e.message,true);await refresh(true).catch(()=>{});}}
      finally {if(version===generation){busy=false;render();}}
    });
    $('#materialBasketClips').addEventListener('input', event => {
      const row=event.target.closest('[data-basket-clip]');if(!row)return;
      clipDrafts.set(Number(row.dataset.basketClip),{start:row.querySelector('[data-basket-start]').value,end:row.querySelector('[data-basket-end]').value});
      $('#materialBasketHandoff').disabled=true;row.querySelector('[data-basket-clip-action=cancel]').disabled=false;
      $('#materialBasketStatus').textContent='入出点尚未保存，请保存或取消修改后再交接';
    });
    $('#materialBasketClips').addEventListener('click', async event => {
      const action=event.target.closest('[data-basket-clip-action]');if(!action||busy)return;
      const row=action.closest('[data-basket-clip]'),id=Number(row.dataset.basketClip),kind=action.dataset.basketClipAction;
      try{
        if(kind==='cancel'){clipDrafts.delete(id);render();return;}
        if(kind==='remove'){await mutate(`/api/material-basket/clips/${id}`,{method:'DELETE'});clipDrafts.delete(id);render();}
        else if(kind==='save'){
          const start=Number(row.querySelector('[data-basket-start]').value),end=Number(row.querySelector('[data-basket-end]').value);
          if(!Number.isFinite(start)||!Number.isFinite(end)||end<=start)throw new Error('请检查入出点');
          await mutate(`/api/material-basket/clips/${id}`,{method:'PATCH',body:JSON.stringify({start_time:start,end_time:end})});clipDrafts.delete(id);render();
        }else{
          const ids=clips.map(c=>c.id),i=ids.indexOf(id),j=i+(kind==='up'?-1:1);
          if(j<0||j>=ids.length)return;[ids[i],ids[j]]=[ids[j],ids[i]];
          await mutate('/api/material-basket/clips/order',{method:'PUT',body:JSON.stringify({clip_ids:ids})});
        }
        $('#materialBasketResult').replaceChildren();
      }catch(e){deps.toast(e.message,true);}
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
  async function addClip(fileId,start,end) {
    if(!owner)throw new Error('请先登录个人账号');
    if(busy)throw new Error('素材篮正在保存，请稍后再试');
    const version=generation;
    await mutate('/api/material-basket/clips',{method:'POST',body:JSON.stringify({file_id:fileId,start_time:start,end_time:end})});
    if(version!==generation)throw new Error('账号已切换，请重新打开选片窗口');
  }
  return {init,button,userChanged,refresh,addClip};
})();
