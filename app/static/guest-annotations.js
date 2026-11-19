/* Public review drawing. Coordinates are relative to the visible media, not its letterbox. */
(() => {
  const sessions = new WeakMap();
  const ns = 'http://www.w3.org/2000/svg';
  const clamp = value => Math.max(0, Math.min(1, value));
  function mount(card, version, comments, canComment) {
    sessions.get(card)?.dispose();
    const stage = card.querySelector('.public-asset-stage');
    const media = stage.querySelector('video, img');
    if (!media) return;
    const svg = document.createElementNS(ns, 'svg');
    svg.classList.add('guest-annotation-layer');
    svg.setAttribute('viewBox', '0 0 1000 1000');
    svg.setAttribute('preserveAspectRatio', 'none');
    svg.setAttribute('aria-label', '画面圈改区域');
    stage.append(svg);
    const toolbar = document.createElement('div');
    toolbar.className = 'guest-annotation-tools';
    toolbar.innerHTML = `${canComment ? '<button type="button" data-guest-draw="start">画笔圈改</button><button type="button" data-guest-draw="undo" disabled>撤销一笔</button><button type="button" data-guest-draw="reset" disabled>清空</button><button type="button" data-guest-draw="cancel" disabled>取消圈改</button>' : ''}<span role="status" aria-live="polite"></span>`;
    stage.after(toolbar);
    let strokes = [], active = false, stroke = null, pointer = null, time = null, selected = null, busy = false;
    const status = toolbar.querySelector('[role=status]');
    const isVideo = media.tagName === 'VIDEO';
    const form = card.querySelector('.public-comment-form');
    function layout() {
      const box = media.getBoundingClientRect(), parent = stage.getBoundingClientRect();
      const width = media.videoWidth || media.naturalWidth || box.width;
      const height = media.videoHeight || media.naturalHeight || box.height;
      const scale = Math.min(box.width / width, box.height / height);
      const w = width * scale, h = height * scale;
      Object.assign(svg.style, { left: `${box.left - parent.left + (box.width - w) / 2}px`, top: `${box.top - parent.top + (box.height - h) / 2}px`, width: `${w}px`, height: `${h}px` });
    }
    function render() {
      svg.replaceChildren();
      const shown = active || strokes.length ? strokes : selected?.drawing || [];
      for (const item of shown) {
        const points = (item.points || []).filter(p => Number.isFinite(p.x) && Number.isFinite(p.y));
        if (!points.length) continue;
        const line = document.createElementNS(ns, 'polyline');
        line.setAttribute('points', points.map(p => `${clamp(p.x)*1000},${clamp(p.y)*1000}`).join(' '));
        line.setAttribute('stroke', /^#[0-9a-f]{6}$/i.test(item.color) ? item.color : '#ffcc57');
        svg.append(line);
      }
      svg.classList.toggle('drawing', active && !busy);
      toolbar.querySelectorAll('button').forEach(button => {
        const action = button.dataset.guestDraw;
        button.disabled = busy || (action === 'start' ? active : action === 'cancel' ? !active && !strokes.length : !strokes.length);
      });
      if (form) form.elements.time_start.readOnly = active || strokes.length > 0;
    }
    function cancel() {
      strokes = []; stroke = null; active = false; pointer = null; selected = null; time = null;
      status.textContent = ''; render();
    }
    function click(event) {
      const action = event.target.closest('[data-guest-draw]')?.dataset.guestDraw;
      if (!action || busy) return;
      if (action === 'start') {
        if ((isVideo && media.readyState < 2) || (!isVideo && !media.naturalWidth)) { status.textContent = isVideo ? '请先播放视频到需要圈改的画面，再点画笔圈改' : '画面尚未加载，请稍后重试'; return; }
        if (isVideo) media.pause();
        active = true; selected = null; time = isVideo ? media.currentTime : null;
        if (form) form.elements.time_start.value = time ?? '';
        status.textContent = isVideo ? `已暂停在 ${time.toFixed(2)} 秒，画完后填写意见并发布` : '在图片上圈改，填写意见后发布';
        layout();
      } else if (action === 'undo') strokes.pop();
      else if (action === 'reset') strokes = [];
      else cancel();
      render();
    }
    function point(event) {
      const rect = svg.getBoundingClientRect();
      return { x: clamp((event.clientX-rect.left)/rect.width), y: clamp((event.clientY-rect.top)/rect.height) };
    }
    function down(event) {
      if (!active || busy || pointer !== null || (event.pointerType === 'mouse' && event.button !== 0)) return;
      if (strokes.length >= 32 || strokes.reduce((n,s) => n+s.points.length,0) >= 2048) { status.textContent = '圈改较多，请先发布或撤销部分笔画'; return; }
      event.preventDefault(); pointer = event.pointerId; svg.setPointerCapture(pointer);
      stroke = { color: '#ffcc57', points: [point(event)] }; strokes.push(stroke); render();
    }
    function move(event) {
      if (!stroke || event.pointerId !== pointer) return;
      event.preventDefault();
      if (stroke.points.length < 256 && strokes.reduce((n,s) => n+s.points.length,0) < 2048) stroke.points.push(point(event));
      render();
    }
    function end(event) {
      if (event.pointerId !== pointer) return;
      if (event.type === 'pointercancel') strokes.pop();
      else if (stroke?.points.length === 1) stroke.points.push({...stroke.points[0]});
      stroke = null; pointer = null; render();
    }
    function mediaChanged() {
      if (active || strokes.length) {
        if (isVideo && Math.abs(media.currentTime - time) > .05) {
          media.pause(); media.currentTime = time;
          status.textContent = '圈改已绑定当前时间；请先发布或取消，再切换画面';
        }
        if (isVideo && !media.paused) media.pause();
      } else if (selected && isVideo && (!media.paused || Math.abs(media.currentTime - selected.time_start) > .1)) {
        selected = null; render();
      }
    }
    function replay(event) {
      const button = event.target.closest('[data-public-annotation]');
      if (!button) return;
      if (busy || active || strokes.length) { status.textContent = '请先发布或取消当前圈改'; return; }
      const comment = comments.find(item => String(item.id) === button.dataset.publicAnnotation);
      if (!comment || Number(comment.version_id) !== Number(version.id)) { status.textContent = '请先切换到这条意见对应的版本'; return; }
      if (isVideo) { media.pause(); media.currentTime = comment.time_start || 0; }
      selected = comment; status.textContent = '正在查看已保存的圈改'; layout(); render();
    }
    toolbar.addEventListener('click', click); card.addEventListener('click', replay);
    svg.addEventListener('pointerdown', down); svg.addEventListener('pointermove', move);
    svg.addEventListener('pointerup', end); svg.addEventListener('pointercancel', end);
    svg.addEventListener('lostpointercapture', () => { stroke = null; pointer = null; });
    ['play','seeking','timeupdate'].forEach(name => media.addEventListener(name, mediaChanged));
    ['load','loadedmetadata'].forEach(name => media.addEventListener(name, layout));
    const observer = new ResizeObserver(layout); observer.observe(stage); observer.observe(media);
    sessions.set(card, {
      payload: () => ({drawing: strokes, ...(strokes.length ? {time_start: time} : {})}),
      busy: value => { busy = value; render(); },
      hasDraft: () => active || strokes.length > 0,
      clear: cancel,
      dispose: () => { observer.disconnect(); toolbar.remove(); svg.remove(); card.removeEventListener('click', replay); ['play','seeking','timeupdate'].forEach(name => media.removeEventListener(name,mediaChanged)); ['load','loadedmetadata'].forEach(name => media.removeEventListener(name,layout)); sessions.delete(card); },
    });
    layout(); render();
  }
  window.GuestAnnotations = { mount, get: card => sessions.get(card), disposeAll: root => root.querySelectorAll('.public-asset').forEach(card => sessions.get(card)?.dispose()) };
})();
