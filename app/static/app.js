const state = {
  token: localStorage.getItem('nasAiToken') || '',
  authReady: false,
  publicMode: location.pathname.startsWith('/share/'),
  kind: '',
  system: null,
  thumbnailUrls: new Map(),
  timelineItems: [],
  timelineOffset: 0,
  organizerMode: 'duplicates',
  preciseSearch: true,
  currentFile: null,
  currentFileUrl: '',
  user: null,
  libraries: [],
  users: [],
  indexStatus: null,
  searchQuery: '',
  searchResults: [],
  searchOffset: 0,
  searchTotal: 0,
  searchHasMore: false,
  searchSequence: 0,
  libraryItems: [],
  libraryOffset: 0,
  libraryTotal: 0,
  peopleItems: [],
  peopleOffset: 0,
  eventsItems: [],
  eventsOffset: 0,
  currentConversationId: null,
  conversations: [],
  smartAlbums: [],
  currentSmartAlbumId: null,
  currentSearchFeedbackQuery: '',
  bootstrapRequired: false,
  projects: [],
  currentProjectId: null,
  currentProject: null,
  projectDetails: null,
  projectAssets: [],
  projectFolderId: null,
  assetLayout: 'grid',
  currentAsset: null,
  currentVersionId: null,
  lookPreviewEnabled: false,
  annotationStrokes: [],
  drawingActive: false,
  activeStroke: null,
  publicAccessCode: '',
  publicVersionIds: {},
};

const thumbnailQueue = [];
const thumbnailQueued = new Set();
const thumbnailInFlight = new Map();
let thumbnailActive = 0;
const THUMBNAIL_CONCURRENCY = 5;
const THUMBNAIL_CACHE_LIMIT = 160;

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = value => String(value ?? '').replace(/[&<>'"]/g, character => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
}[character]));

const iconPaths = {
  files: '<path d="M4 7.5h16v11H4z"/><path d="M7 4h10l2 3.5H5z"/>',
  check: '<circle cx="12" cy="12" r="9"/><path d="m8 12 2.5 2.5L16 9"/>',
  storage: '<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
  activity: '<path d="M4 12h3l2-6 4 12 2-6h5"/>',
  library: '<path d="M4 7.5h16v11H4z"/><path d="M7 4h10l2 3.5H5z"/>',
  cpu: '<rect x="5" y="5" width="14" height="14" rx="2"/><path d="M9 9h6v6H9M9 2v3m6-3v3M9 19v3m6-3v3M2 9h3m-3 6h3m14-6h3m-3 6h3"/>',
  gpu: '<path d="M4 7h16v10H4zM7 10h7v4H7zM17 10v4M8 20v-3m8 3v-3"/>',
  plan: '<path d="M5 5h6v6H5zm8 8h6v6h-6zM14 5h5v4M5 14v5h5"/>',
  model: '<path d="m12 3 1.5 5.5L19 10l-5.5 1.5L12 17l-1.5-5.5L5 10l5.5-1.5z"/><path d="m19 16 .7 2.3L22 19l-2.3.7L19 22l-.7-2.3L16 19l2.3-.7z"/>',
  vector: '<circle cx="6" cy="12" r="2"/><circle cx="18" cy="6" r="2"/><circle cx="18" cy="18" r="2"/><path d="m8 11 8-4m-8 6 8 4"/>',
  document: '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v5h5M9 12h6m-6 4h6"/>',
  audio: '<path d="M9 18V6l10-2v12"/><circle cx="6" cy="18" r="3"/><circle cx="16" cy="16" r="3"/>',
  archive: '<path d="M4 7h16v13H4zM3 3h18v4H3zM9 11h6"/>',
  search: '<circle cx="11" cy="11" r="6"/><path d="m16 16 4 4"/>',
  task: '<path d="M12 3a9 9 0 1 0 9 9"/><path d="M21 5v7h-7"/>',
  timeline: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  duplicate: '<rect x="4" y="4" width="11" height="11" rx="2"/><path d="M9 15v5h11V9h-5"/>',
  play: '<circle cx="12" cy="12" r="9"/><path d="m10 8 6 4-6 4z"/>',
  people: '<circle cx="9" cy="8" r="3"/><path d="M3.5 20v-2.5A4.5 4.5 0 0 1 8 13h2a4.5 4.5 0 0 1 4.5 4.5V20M16 5.5a3 3 0 0 1 0 5.8M17 14a4.5 4.5 0 0 1 3.5 4.4V20"/>',
  backup: '<path d="M5 4h12l2 2v14H5zM8 4v6h8V4M8 16h8"/>',
  user: '<circle cx="9" cy="8" r="3"/><path d="M3 20v-2a5 5 0 0 1 5-5h2a5 5 0 0 1 5 5v2M17 8h4m-2-2v4"/>',
  place: '<path d="M12 21s7-6.2 7-12A7 7 0 0 0 5 9c0 5.8 7 12 7 12Z"/><circle cx="12" cy="9" r="2.5"/>',
  event: '<path d="M5 4h14v16H5zM8 2v4m8-4v4M5 9h14"/><path d="m9 14 2 2 4-4"/>',
  recycle: '<path d="M5 7h14M9 7V4h6v3m-8 0 1 13h8l1-13M10 11v5m4-5v5"/>',
  star: '<path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9z"/>',
  album: '<path d="M4 6h16v13H4z"/><path d="m7 15 3-3 2 2 2-2 3 3M8 9h.01"/>',
};

const icon = name => `<svg viewBox="0 0 24 24" aria-hidden="true">${iconPaths[name] || iconPaths.files}</svg>`;

const fmtBytes = value => {
  const bytes = Number(value || 0);
  if (!bytes) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const index = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
  return `${(bytes / 1024 ** index).toFixed(index ? 1 : 0)} ${units[index]}`;
};

const fmtCount = value => Number(value || 0).toLocaleString('zh-CN');
const fmtPercent = value => {
  const number = Math.max(0, Math.min(100, Number(value || 0)));
  return `${number.toFixed(number >= 99 ? 2 : 1)}%`;
};
const fmtEta = value => {
  if (value === null || value === undefined || value === '') return '估算中';
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds < 0) return '估算中';
  if (seconds === 0) return '已完成';
  if (seconds < 3600) return `约 ${Math.max(1, Math.round(seconds / 60))} 分钟`;
  const hours = seconds / 3600;
  return `约 ${hours < 10 ? hours.toFixed(1) : Math.round(hours)} 小时`;
};
const fmtDuration = value => {
  const seconds = Math.max(0, Number(value || 0));
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`;
};
const fmtDate = value => value ? new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: 'short', day: 'numeric' }).format(new Date(value)) : '时间未知';
const taskTitle = task => ({
  scan_library: '扫描并索引媒体库',
  scan_only: '快速扫描媒体库',
  index_pending: '索引待处理文件',
  index_files: '重新索引文件',
  restore_file: '恢复并重建文件索引',
  upgrade_captions: '升级图片识别描述',
  analyze_duplicates: '分析重复文件',
  analyze_similar: '分析相似照片',
  analyze_people: '识别与聚类人物',
  analyze_places: '整理地点相册',
  analyze_events: '整理事件相册',
  repair_index: '修复部分 AI 索引',
  generate_proxy: '生成审阅代理媒体',
  generate_look_preview: '生成 LUT 审阅预览',
  collect_project_inbox: '收集 NAS 项目入库箱',
}[task.type] || task.type);
const taskStatus = status => ({ pending: '等待中', running: '处理中', completed: '已完成', failed: '失败', cancelled: '已取消' }[status] || status);
const stageLabel = status => ({
  ready: '完成',
  manual: '人工提供',
  pending: '待处理',
  error: '失败',
  missing: '缺失',
  blocked: '被阻塞',
  not_applicable: '不适用',
}[status] || status || '未知');

const toast = (message, error = false) => {
  const element = $('#toast');
  element.textContent = message;
  element.className = `toast show${error ? ' error' : ''}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { element.className = 'toast'; }, 3200);
};

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  if (options.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, { ...options, headers });
  let detail = `请求失败 (${response.status})`;
  if (!response.ok) {
    try { detail = (await response.json()).detail || detail; } catch {}
  }
  if (response.status === 401) {
    const credentialRequest = path === '/api/auth/login' || path === '/api/auth/bootstrap' || path.startsWith('/api/public/');
    if (!credentialRequest) {
      state.token = '';
      state.user = null;
      state.authReady = false;
      localStorage.removeItem('nasAiToken');
      $('#tokenForm [name=token]').value = '';
      applyRole();
      if (!state.publicMode) $('#tokenModal').classList.add('open');
    }
  }
  if (!response.ok) {
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }
  return response.status === 204 ? null : response.json();
}

const titles = {
  home: ['数据总览', '你的私有数据，只在本地流动'],
  search: ['智能搜索', '全文与语义融合检索'],
  ask: ['问你的资料', '回答均可追溯到原始文件'],
  library: ['资料库', '统一浏览所有文件与索引状态'],
  albums: ['相册与发现', '时间、人物、地点、事件与智能相册'],
  timeline: ['时间线', '按拍摄日期浏览照片与视频'],
  people: ['人物相册', '本地识别并聚类照片中的人物'],
  places: ['地点相册', '按 GPS 元数据在本机聚类'],
  events: ['事件相册', '按时间与位置自动整理'],
  libraries: ['媒体库', '管理索引来源'],
  tasks: ['任务中心', '查看本地处理管线'],
  organizer: ['智能整理', '发现重复与相似内容'],
  recycle: ['回收站', '恢复或永久清除重复文件'],
  hardware: ['算力调度', '自动适配 NAS 异构硬件'],
  users: ['用户与权限', '管理本地账号与媒体库授权'],
  operations: ['运维与备份', '数据库健康、存储与审计'],
  projects: ['项目', '素材、版本、协作与交付工作区'],
  project: ['项目工作区', '管理项目素材与审阅流程'],
  reviews: ['待我审阅', '跨项目处理尚未解决的意见'],
  deliveries: ['分享与交付', '管理安全外部审阅链接'],
};

function showView(view) {
  if (!titles[view]) return;
  if (state.publicMode) return;
  if (!state.authReady) {
    $('#tokenModal').classList.add('open');
    return;
  }
  const systemViews = new Set(['libraries', 'tasks', 'organizer', 'people', 'events', 'recycle', 'hardware', 'users', 'operations']);
  if (systemViews.has(view)) {
    $('#systemNav').hidden = false;
    $('#systemNavToggle').classList.add('expanded');
  }
  $$('.view').forEach(element => element.classList.toggle('active', element.id === `view-${view}`));
  $$('.nav-item').forEach(element => element.classList.toggle('active', element.dataset.view === view));
  $('#pageTitle').textContent = titles[view][0];
  $('#pageSubtitle').textContent = titles[view][1];
  $('.sidebar').classList.remove('open');
  if (view === 'home') {
    loadDashboard(true);
    loadTasks(true);
  }
  if (view === 'libraries') loadLibraries();
  if (view === 'library') loadLibraryFiles(true);
  if (view === 'albums') loadSmartAlbums();
  if (view === 'tasks') {
    loadTasks();
    if (isAdmin()) loadIndexStatus(true);
  }
  if (view === 'timeline') loadTimeline(true);
  if (view === 'people') loadPeople(true);
  if (view === 'places') loadPlaces();
  if (view === 'events') loadEvents(true);
  if (view === 'organizer') loadOrganizer();
  if (view === 'recycle') loadRecycle();
  if (view === 'hardware') loadSystem();
  if (view === 'users') loadUsers();
  if (view === 'operations') loadOperations();
  if (view === 'projects') loadProjects();
  if (view === 'project' && state.currentProjectId) loadProjectWorkspace(state.currentProjectId);
  if (view === 'reviews') loadAllReviewTasks();
  if (view === 'deliveries') loadDeliveries();
  if (view === 'ask') loadConversations();
  if (view === 'search') setTimeout(() => $('#mainSearchInput').focus(), 80);
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function statsCard(label, value, note, iconName) {
  return `<article class="stat-card"><span class="stat-icon">${icon(iconName)}</span><div class="stat-copy"><small>${esc(label)}</small><strong>${esc(value)}</strong><span>${esc(note)}</span></div></article>`;
}

async function loadDashboard(quiet = false) {
  try {
    const data = await api('/api/dashboard');
    const total = Number(data.files.total || 0);
    const ready = Number(data.files.ready || 0);
    const partial = Number(data.files.partial || 0);
    const semanticReady = Number(data.files.semantic_ready || 0);
    const indexing = data.indexing || {};
    const completion = Number.isFinite(Number(indexing.semantic_percent))
      ? Number(indexing.semantic_percent)
      : total ? semanticReady / total * 100 : 0;
    const terminal = Number(data.files.terminal_failures || indexing.terminal_failures || 0);
    const waiting = Number(data.files.retry_waiting || indexing.retry_waiting || 0);
    $('#dashboardStats').innerHTML = [
      statsCard('已发现文件', fmtCount(total), '当前所有媒体库', 'files'),
      statsCard('语义可检索', fmtCount(semanticReady), `${fmtPercent(completion)} 拥有有效内容向量`, 'check'),
      statsCard('部分完成', fmtCount(partial), terminal ? `${fmtCount(terminal)} 个需人工检查` : `${fmtCount(data.files.repairable)} 个可自动修复`, 'activity'),
      statsCard('数据规模', fmtBytes(data.files.bytes), data.active_tasks ? `${fmtCount(data.active_tasks)} 个任务运行中` : `${fmtCount(ready)} 个完整索引`, 'storage'),
    ].join('');
    $('#indexHealthText').textContent = fmtPercent(completion);
    $('#indexHealthBar').style.width = `${completion}%`;
    const active = indexing.active;
    const parts = [];
    if (active) {
      const done = Number(active.work_done || 0);
      const workTotal = Number(active.work_total || 0);
      parts.push(workTotal ? `当前批次 ${fmtCount(done)}/${fmtCount(workTotal)}` : '当前批次正在启动');
    } else if (indexing.status === 'complete') {
      parts.push('全库处理完成');
    } else if (indexing.status === 'degraded') {
      parts.push('全库处理完成，存在需人工检查的文件');
    } else if (indexing.status === 'backoff') {
      parts.push(`${fmtCount(waiting)} 个文件等待退避重试`);
    }
    if (Number(indexing.pending || 0)) parts.push(`待处理 ${fmtCount(indexing.pending)}`);
    if (Number(indexing.repairable || 0)) parts.push(`待修复 ${fmtCount(indexing.repairable)}`);
    if (terminal) parts.push(`人工检查 ${fmtCount(terminal)}`);
    const eta = indexing.runtime?.eta_seconds;
    if (eta !== null && eta !== undefined && Number(indexing.runtime?.remaining_items || 0)) parts.push(fmtEta(eta));
    const updated = indexing.updated_at ? new Date(indexing.updated_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '';
    $('#indexHealthMeta').textContent = `${parts.join(' · ') || '索引状态正常'}${updated ? ` · ${updated} 更新` : ''}`;
  } catch (error) {
    $('#indexHealthMeta').textContent = '实时状态暂时不可用，保留上次数据';
    if (!quiet) toast(error.message, true);
  }
}

function libraryRow(item) {
  return `<div class="library-row"><span class="library-icon">${icon('library')}</span><div><b>${esc(item.name)}</b><small>${esc(item.path)}</small></div><div class="library-meta"><strong>${fmtCount(item.file_count)} 个文件</strong><span>${fmtBytes(item.total_bytes)}</span></div></div>`;
}

async function loadLibraries() {
  try {
    const items = await api('/api/libraries');
    state.libraries = items;
    ['#searchLibrary', '#librarySource'].forEach(selector => {
      const select = $(selector);
      if (!select) return;
      const selected = select.value;
      select.innerHTML = `<option value="">全部媒体库</option>${items.map(item => `<option value="${item.id}">${esc(item.name)}</option>`).join('')}`;
      select.value = selected;
    });
    $('#homeLibraries').innerHTML = items.length
      ? items.slice(0, 4).map(libraryRow).join('')
      : '<div class="empty-state compact"><b>还没有媒体库</b><p>添加只读目录后即可开始索引。</p></div>';
    $('#libraryCards').innerHTML = items.length
      ? items.map(item => `<article class="library-card"><span class="card-leading">${icon('library')}</span><div><h3>${esc(item.name)}</h3><p>${esc(item.path)} · ${fmtCount(item.file_count)} 个文件 · ${fmtBytes(item.total_bytes)}</p><div class="progress"><span style="width:${item.last_scan_at ? '100' : '0'}%"></span></div></div><div class="card-actions"><button class="secondary" data-discover="${item.id}">快速扫描</button><button class="danger" data-delete-library="${item.id}">删除</button></div></article>`).join('')
      : '<div class="empty-state"><span class="empty-icon">' + icon('library') + '</span><b>添加第一个媒体库</b><p>建立索引后，你就可以搜索和询问本地资料。</p></div>';
  } catch (error) {
    toast(error.message, true);
  }
}

async function loadSearchFacets() {
  try {
    const [people, places, events, tags] = await Promise.all([
      api('/api/people?limit=200'),
      api('/api/places'),
      api('/api/events?limit=200'),
      state.user?.id ? api('/api/tags') : Promise.resolve([]),
    ]);
    const replaceOptions = (selector, label, items) => {
      const select = $(selector);
      const selected = select.value;
      select.innerHTML = `<option value="">${label}</option>${items.map(item => `<option value="${item.id ?? item.name}">${esc(item.name)} · ${fmtCount(item.file_count || 0)}</option>`).join('')}`;
      select.value = selected;
    };
    replaceOptions('#searchPerson', '全部人物', people.items || []);
    replaceOptions('#searchPlace', '全部地点', places.items || []);
    replaceOptions('#searchEvent', '全部事件', events.items || []);
    replaceOptions('#searchTag', '全部标签', tags || []);
    replaceOptions('#libraryTag', '全部标签', tags || []);
  } catch {}
}

function indexSummaryItem(label, value, note) {
  return `<div class="index-summary-item"><span>${esc(label)}</span><b>${esc(value)}</b><span>${esc(note)}</span></div>`;
}

async function loadIndexStatus(refreshForm = false, quiet = false) {
  if (!isAdmin()) return;
  try {
    const data = await api('/api/index/status');
    state.indexStatus = data;
    const kinds = Object.fromEntries(data.pending.kinds.map(item => [item.kind, item]));
    const available = Number(data.resources.available_memory_bytes || 0);
    const minimum = Number(data.resources.minimum_memory_bytes || 0);
    const overview = data.overview || {};
    const runtime = overview.runtime || {};
    const terminal = Number(data.stages.terminal_failures || 0);
    const retryWaiting = Number(data.stages.retry_waiting || 0);
    $('#indexControlSummary').innerHTML = [
      indexSummaryItem('语义覆盖', fmtPercent(overview.semantic_percent), `${fmtCount(overview.semantic_ready)} / ${fmtCount(overview.total)}`),
      indexSummaryItem('待索引', `${fmtCount(data.pending.total)} 个`, fmtBytes(data.pending.bytes)),
      indexSummaryItem('待修复', `${fmtCount(data.stages.repairable)} 个`, '仅重跑缺失阶段'),
      indexSummaryItem('退避等待', `${fmtCount(retryWaiting)} 个`, '按指数退避自动重试'),
      indexSummaryItem('人工检查', `${fmtCount(terminal)} 个`, terminal ? '已停止无效自动重试' : '当前无终止错误'),
      indexSummaryItem('预计剩余', fmtEta(runtime.eta_seconds), runtime.items_per_minute ? `${runtime.items_per_minute} 个/分钟` : '等待采样'),
      indexSummaryItem('图片', `${fmtCount(kinds.image?.count)} 个`, fmtBytes(kinds.image?.bytes)),
      indexSummaryItem('视频', `${fmtCount(kinds.video?.count)} 个`, fmtBytes(kinds.video?.bytes)),
      indexSummaryItem('可用内存', fmtBytes(available), minimum ? `保护线 ${fmtBytes(minimum)}` : '未设置保护线'),
      indexSummaryItem('Swap 剩余', fmtBytes(data.resources.free_swap_bytes), data.resources.minimum_free_swap_bytes ? `保护线 ${fmtBytes(data.resources.minimum_free_swap_bytes)}` : '未设置保护线'),
    ].join('');
    $('#repairCallout').classList.toggle('healthy', !Number(data.stages.repairable || 0) && !retryWaiting && !terminal);
    $('#repairCallout span').textContent = data.stages.repairable
      ? `${fmtCount(data.stages.repairable)} 个文件可立即修复；失败会自动退避，达到上限后停止。`
      : retryWaiting
        ? `${fmtCount(retryWaiting)} 个文件正在等待下一次退避重试。`
        : terminal
          ? `${fmtCount(terminal)} 个文件多次失败，已停止自动重试，请在资料库查看后手动重建。`
          : '当前没有需要修复的部分索引。';
    $('#repairIndex').disabled = !Number(data.stages.repairable || 0);
    const badge = $('#indexPolicyBadge');
    if (data.active) {
      badge.textContent = data.active.status === 'running' ? '正在索引' : '等待执行';
    } else if (overview.controller && !overview.controller.stale) {
      badge.textContent = {
        complete: '全库完成',
        degraded: '完成 · 有异常',
        waiting: '退避等待',
        recycling: '正在释放资源',
        error: '调度器异常',
      }[overview.controller.state] || '外部调度器在线';
    } else {
      badge.textContent = data.policy.enabled ? `自动 · ${data.policy.start_hour}:00–${data.policy.end_hour}:00` : '手动模式';
    }
    const librarySelect = $('#indexLibrary');
    const selected = librarySelect.value;
    librarySelect.innerHTML = '<option value="">全部媒体库</option>' + data.pending.libraries.map(item => `<option value="${item.id}">${esc(item.name)} · ${fmtCount(item.count)}</option>`).join('');
    librarySelect.value = selected;
    if (refreshForm) {
      const policyForm = $('#indexPolicyForm');
      policyForm.elements.enabled.checked = Boolean(data.policy.enabled);
      policyForm.elements.start_hour.value = String(data.policy.start_hour);
      policyForm.elements.end_hour.value = String(data.policy.end_hour);
      policyForm.elements.batch_size.value = data.policy.batch_size;
      const batchSelect = $('#indexBatchForm').elements.limit;
      if ([...batchSelect.options].some(option => Number(option.value) === Number(data.resources.default_batch_size))) {
        batchSelect.value = String(data.resources.default_batch_size);
      }
    }
  } catch (error) {
    if (!quiet) toast(error.message, true);
  }
}

function hardwareHead(iconName, eyebrow, title) {
  return `<div class="hardware-card-head"><span class="hardware-card-icon">${icon(iconName)}</span><div><span class="eyebrow">${esc(eyebrow)}</span><h2>${esc(title)}</h2></div></div>`;
}

function kv(key, value) {
  return `<div class="kv"><span>${esc(key)}</span><b>${esc(value)}</b></div>`;
}

function runtimeMetricsBody(metrics) {
  const gpu = metrics.gpus?.[0];
  return `${hardwareHead('activity', '实时遥测', '当前资源负载')}`
    + kv('CPU 1 分钟负载', `${metrics.cpu.load_percent}% · ${metrics.cpu.load_1m}`)
    + kv('可用内存', fmtBytes(metrics.memory.available_bytes))
    + kv('Swap 已用', `${fmtBytes(metrics.memory.swap_used_bytes)} / ${fmtBytes(metrics.memory.swap_total_bytes)}`)
    + kv('GPU 利用率', gpu ? `${gpu.utilization_percent}%` : '无 NVIDIA 实时数据')
    + kv('GPU 显存', gpu ? `${fmtBytes(gpu.memory_used_bytes)} / ${fmtBytes(gpu.memory_total_bytes)}` : '—')
    + kv('GPU 温度 / 功耗', gpu ? `${gpu.temperature_c}℃ · ${gpu.power_watts.toFixed(1)} W` : '—');
}

async function loadRuntimeMetrics() {
  const card = $('#runtimeMetrics');
  if (!card) return;
  try {
    card.innerHTML = runtimeMetricsBody(await api('/api/system/metrics'));
  } catch {}
}

async function loadSystem() {
  try {
    const data = await api('/api/system');
    state.system = data;
    const plan = data.hardware.plan;
    const accelerated = plan.inference_backend !== 'cpu';
    $('#accelBadge').textContent = accelerated ? '硬件加速已启用' : 'CPU 模式';
    $('#accelTitle').textContent = `${plan.inference_backend.toUpperCase()} · ${plan.media_backend.toUpperCase()}`;
    $('#accelDescription').textContent = plan.reasons.join('；');
    $('#accelMeter').style.width = accelerated ? '88%' : '38%';
    $('#accelChips').innerHTML = [
      `${data.hardware.logical_cpus} 线程`,
      `${fmtBytes(data.hardware.memory_bytes)} 内存`,
      `${data.configuration.indexing.index_workers} 个索引线程`,
    ].map(value => `<span class="chip">${esc(value)}</span>`).join('');
    const gpu = data.hardware.gpus.length
      ? data.hardware.gpus.map(item => `${item.vendor.toUpperCase()} · ${item.name}`).join('、')
      : '未检测到 GPU';
    $('#hardwareContent').innerHTML = `
      <article class="hardware-card" id="runtimeMetrics">${runtimeMetricsBody(data.metrics)}</article>
      <article class="hardware-card">${hardwareHead('cpu', '主机', 'NAS 计算资源')}${kv('处理器', data.hardware.cpu)}${kv('架构', data.hardware.arch)}${kv('逻辑核心', data.hardware.logical_cpus)}${kv('可用内存', fmtBytes(data.hardware.memory_bytes))}</article>
      <article class="hardware-card">${hardwareHead('gpu', '加速器', '图形与运行时')}${kv('GPU', gpu)}${kv('ONNX', data.hardware.onnx_providers.join(', ') || 'CPU / 未安装')}${kv('媒体后端', plan.media_backend.toUpperCase())}${kv('推理后端', plan.inference_backend.toUpperCase())}</article>
      <article class="hardware-card wide">${hardwareHead('plan', '调度策略', '自动执行计划')}<div class="plan-list">${plan.reasons.map(value => `<div class="plan-step">${esc(value)}</div>`).join('')}</div>${kv('任务进程', data.configuration.indexing.task_workers)}${kv('文件索引线程', data.configuration.indexing.index_workers)}${kv('媒体处理并发', plan.media_workers)}${kv('模型推理并发', plan.inference_workers)}${kv('内存保护线', fmtBytes(data.configuration.indexing.min_available_memory_bytes))}${kv('Swap 保护线', fmtBytes(data.configuration.indexing.min_free_swap_bytes))}</article>
      <article class="hardware-card">${hardwareHead('model', '本地模型', '推理服务')}${kv('服务状态', data.local_ai.configured ? (data.local_ai.reachable ? '已连接' : '连接失败') : '未配置')}${kv('向量模型', data.configuration.embedding_model || '未配置')}${kv('视觉模型', data.configuration.vision_model || '未配置')}${kv('问答模型', data.configuration.chat_model || '未配置')}</article>
      <article class="hardware-card">${hardwareHead('vector', '向量索引', 'Qdrant 数据库')}${kv('运行状态', data.vector_store.reachable ? '运行中' : '不可达')}${kv('检索结构', 'HNSW · On-disk')}${kv('数据位置', 'NAS 本地卷')}</article>`;
  } catch (error) {
    toast(error.message, true);
  }
}

async function authenticatedBlobUrl(path) {
  const headers = state.token ? { Authorization: `Bearer ${state.token}` } : {};
  const response = await fetch(path, { headers });
  if (response.status === 401) {
    state.token = '';
    state.user = null;
    state.authReady = false;
    localStorage.removeItem('nasAiToken');
    $('#tokenForm [name=token]').value = '';
    applyRole();
    if (!state.publicMode) $('#tokenModal').classList.add('open');
    const error = new Error('登录已失效，请重新登录');
    error.status = 401;
    throw error;
  }
  if (!response.ok) throw new Error(`文件请求失败 (${response.status})`);
  return URL.createObjectURL(await response.blob());
}

async function downloadAuthenticated(path, filename) {
  try {
    const url = await authenticatedBlobUrl(path);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  } catch (error) {
    toast(error.message, true);
  }
}

function clearThumbnailUrls() {
  state.thumbnailUrls.forEach(url => URL.revokeObjectURL(url));
  state.thumbnailUrls.clear();
}

function touchThumbnail(path, url) {
  state.thumbnailUrls.delete(path);
  state.thumbnailUrls.set(path, url);
  while (state.thumbnailUrls.size > THUMBNAIL_CACHE_LIMIT) {
    const [oldPath, oldUrl] = state.thumbnailUrls.entries().next().value;
    state.thumbnailUrls.delete(oldPath);
    URL.revokeObjectURL(oldUrl);
  }
}

async function runThumbnailQueue() {
  while (thumbnailActive < THUMBNAIL_CONCURRENCY && thumbnailQueue.length) {
    const image = thumbnailQueue.shift();
    const path = image?.dataset.thumbnail;
    if (!image || !path || !image.isConnected) {
      thumbnailQueued.delete(image);
      continue;
    }
    thumbnailActive += 1;
    thumbnailQueued.delete(image);
    image.classList.add('thumbnail-loading');
    Promise.resolve().then(async () => {
      let url = state.thumbnailUrls.get(path);
      if (!url) {
        let pending = thumbnailInFlight.get(path);
        if (!pending) {
          pending = authenticatedBlobUrl(path);
          thumbnailInFlight.set(path, pending);
        }
        try {
          url = await pending;
        } finally {
          thumbnailInFlight.delete(path);
        }
        touchThumbnail(path, url);
      } else {
        touchThumbnail(path, url);
      }
      if (image.isConnected && image.dataset.thumbnail === path) image.src = url;
    }).catch(() => {
      image.classList.add('thumbnail-error');
    }).finally(() => {
      image.classList.remove('thumbnail-loading');
      thumbnailActive -= 1;
      runThumbnailQueue();
    });
  }
}

function enqueueThumbnail(image) {
  if (!image || image.src || thumbnailQueued.has(image)) return;
  thumbnailQueued.add(image);
  thumbnailQueue.push(image);
  runThumbnailQueue();
}

const thumbnailObserver = 'IntersectionObserver' in window
  ? new IntersectionObserver(entries => {
      entries.forEach(entry => {
        if (!entry.isIntersecting) return;
        thumbnailObserver.unobserve(entry.target);
        enqueueThumbnail(entry.target);
      });
    }, { rootMargin: '320px 0px' })
  : null;

function loadResultThumbnails(root) {
  $$('img[data-thumbnail]', root).forEach(image => {
    if (image.src) return;
    if (thumbnailObserver) thumbnailObserver.observe(image);
    else enqueueThumbnail(image);
  });
}

function resultCard(item) {
  const visual = ['image', 'video'].includes(item.kind)
    ? `<img loading="lazy" data-thumbnail="/api/files/${item.id}/thumbnail" alt="${esc(item.name)}">`
    : icon({ document: 'document', audio: 'audio', archive: 'archive' }[item.kind] || 'files');
  const moment = item.match_time != null ? `<span class="moment-badge">${icon('play')}${fmtDuration(item.match_time)}</span>` : '';
  const confidence = item.confidence != null ? `<span class="match-tag">匹配 ${Math.round(Number(item.confidence) * 100)}%</span>` : '';
  const status = item.status ? `<span class="index-state ${esc(item.status)}">${item.status === 'ready' ? '完整' : item.status === 'partial' ? '部分' : item.status === 'pending' ? '待处理' : '失败'}</span>` : '';
  const matched = item.matched_terms?.length ? `<span class="match-terms">命中：${esc(item.matched_terms.join('、'))}</span>` : '';
  const favorite = item.favorite != null ? `<button class="card-favorite ${item.favorite ? 'active' : ''}" data-favorite-card="${item.id}" data-enabled="${item.favorite ? '1' : '0'}" aria-label="${item.favorite ? '取消收藏' : '收藏'}">${icon('star')}</button>` : '';
  const feedback = state.searchQuery
    ? `<span class="card-feedback"><button data-card-feedback="relevant" data-feedback-file="${item.id}" title="相关">✓</button><button data-card-feedback="irrelevant" data-feedback-file="${item.id}" title="不相关">×</button></span>`
    : '';
  const similar = `<button class="find-similar" data-find-similar="${item.id}" type="button">找相似</button>`;
  return `<article class="result-card" data-file="${item.id}" data-time="${item.match_time ?? ''}"><div class="result-thumb">${visual}${moment}${favorite}</div><div class="result-body"><h3>${esc(item.name)}</h3><p>${esc(item.snippet || item.caption || item.ai_caption || item.relative_path || item.path)}</p>${matched}<div class="result-meta"><span>${status}${esc(item.kind)} · ${fmtBytes(item.size)}</span><span>${similar}${confidence}<span class="source-tag">${esc((item.sources || []).join(' + '))}</span>${feedback}</span></div></div></article>`;
}

function searchFilterParams() {
  const values = {
    library_id: $('#searchLibrary').value,
    date_from: $('#searchDateFrom').value,
    date_to: $('#searchDateTo').value,
    person_id: $('#searchPerson').value,
    place_id: $('#searchPlace').value,
    event_id: $('#searchEvent').value,
    tag: $('#searchTag').value,
  };
  const params = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => {
    if (value) params.set(key, value);
  });
  if ($('#searchFavorite').checked) params.set('favorite', 'true');
  return params;
}

function currentSmartAlbumFilters() {
  return {
    library_id: $('#searchLibrary').value ? Number($('#searchLibrary').value) : null,
    date_from: $('#searchDateFrom').value,
    date_to: $('#searchDateTo').value,
    person_id: $('#searchPerson').value ? Number($('#searchPerson').value) : null,
    place_id: $('#searchPlace').value ? Number($('#searchPlace').value) : null,
    event_id: $('#searchEvent').value ? Number($('#searchEvent').value) : null,
    favorite: $('#searchFavorite').checked,
    tag: $('#searchTag').value,
  };
}

function renderSearchResults(data, append = false, phase = 'fast') {
  state.searchTotal = Number(data.total || 0);
  state.searchHasMore = Boolean(data.has_more);
  state.searchOffset = Number(data.offset || 0) + data.results.length;
  if (append) state.searchResults.push(...data.results);
  else state.searchResults = data.results;
  const label = phase === 'precise' && data.precise
    ? '全文 + 语义 + 精准重排'
    : data.semantic ? '全文 + 语义' : '全文索引';
  $('#searchSummary').innerHTML = `找到约 ${fmtCount(data.total)} 个候选 · 已显示 ${fmtCount(state.searchResults.length)} 个 · ${label}${phase === 'fast' && state.preciseSearch ? '<span class="refining"><i></i>正在后台精排首屏</span>' : ''}`;
  $('#searchResults').innerHTML = state.searchResults.length
    ? state.searchResults.map(resultCard).join('')
    : '<div class="empty-state"><span class="empty-icon">' + icon('search') + '</span><b>没有找到相关内容</b><p>可以换一种描述、调整筛选，或先修复部分索引。</p></div>';
  $('#searchMore').hidden = !state.searchHasMore;
  loadResultThumbnails($('#searchResults'));
}

async function runSearch(query, append = false) {
  const value = String(query || '').trim();
  if (!value) return;
  showView('search');
  $('#mainSearchInput').value = value;
  const sequence = ++state.searchSequence;
  const offset = append && value === state.searchQuery ? state.searchOffset : 0;
  if (!append) {
    state.searchQuery = value;
    state.currentSearchFeedbackQuery = value;
    state.searchResults = [];
    state.searchOffset = 0;
    $('#searchSummary').textContent = '正在本地检索…';
    $('#searchResults').innerHTML = '<div class="empty-state"><b>正在融合全文与语义结果…</b><p>快速结果会先显示，精准重排随后更新。</p></div>';
  }
  const params = searchFilterParams();
  params.set('q', value);
  params.set('kind', state.kind);
  params.set('limit', '20');
  params.set('offset', String(offset));
  params.set('precise', 'false');
  params.set('semantic', 'false');
  try {
    const fast = await api(`/api/search?${params}`);
    if (sequence !== state.searchSequence) return;
    renderSearchResults(fast, append, 'fast');
    if (state.preciseSearch && !append && fast.results.length) {
      params.set('precise', 'true');
      params.set('semantic', 'true');
      const precise = await api(`/api/search?${params}`);
      if (sequence !== state.searchSequence) return;
      renderSearchResults(precise, false, 'precise');
    }
  } catch (error) {
    if (sequence !== state.searchSequence) return;
    toast(error.message, true);
    $('#searchResults').innerHTML = '<div class="empty-state"><b>搜索失败</b><p>请稍后重试。</p></div>';
  }
}

async function runSimilarSearch(fileId, name) {
  showView('search');
  const sequence = ++state.searchSequence;
  state.searchQuery = '';
  state.currentSearchFeedbackQuery = '';
  state.searchResults = [];
  state.searchOffset = 0;
  $('#mainSearchInput').value = `相似于：${name}`;
  $('#searchSummary').textContent = '正在查找语义相近的内容…';
  $('#searchResults').innerHTML = '<div class="empty-state"><b>正在比较本地向量…</b><p>不会上传原文件或内容描述。</p></div>';
  try {
    const data = await api(`/api/files/${fileId}/similar?limit=20`);
    if (sequence !== state.searchSequence) return;
    state.searchTotal = Number(data.total || 0);
    state.searchResults = data.results || [];
    state.searchHasMore = false;
    $('#searchSummary').textContent = `找到 ${fmtCount(state.searchTotal)} 个与“${name}”语义相近的内容`;
    $('#searchResults').innerHTML = state.searchResults.length
      ? state.searchResults.map(resultCard).join('')
      : '<div class="empty-state"><b>暂时没有可比较的相似内容</b><p>该文件可能还没有完成向量索引。</p></div>';
    $('#searchMore').hidden = true;
    loadResultThumbnails($('#searchResults'));
  } catch (error) {
    if (sequence !== state.searchSequence) return;
    toast(error.message, true);
    $('#searchResults').innerHTML = '<div class="empty-state"><b>查找相似内容失败</b><p>请确认该文件已经完成向量索引。</p></div>';
  }
}

function libraryParams(offset = 0) {
  const params = new URLSearchParams({
    limit: '60',
    offset: String(offset),
    sort: $('#librarySort').value,
  });
  const values = {
    kind: $('#libraryKind').value,
    status: $('#libraryStatus').value,
    library_id: $('#librarySource').value,
    tag: $('#libraryTag').value,
  };
  Object.entries(values).forEach(([key, value]) => {
    if (value) params.set(key, value);
  });
  if ($('#libraryFavorite').checked) params.set('favorite', 'true');
  return params;
}

async function loadLibraryFiles(reset = false) {
  if (reset) {
    state.libraryItems = [];
    state.libraryOffset = 0;
  }
  $('#librarySummary').textContent = '正在读取资料库…';
  try {
    const data = await api(`/api/files?${libraryParams(state.libraryOffset)}`);
    state.libraryItems.push(...data.items);
    state.libraryOffset = state.libraryItems.length;
    state.libraryTotal = Number(data.total || 0);
    $('#librarySummary').textContent = `共 ${fmtCount(data.total)} 个文件 · 已显示 ${fmtCount(state.libraryItems.length)} 个`;
    $('#libraryResults').innerHTML = state.libraryItems.length
      ? state.libraryItems.map(resultCard).join('')
      : '<div class="empty-state"><span class="empty-icon">' + icon('library') + '</span><b>这个筛选下没有文件</b><p>可以调整类型、索引状态或标签。</p></div>';
    $('#libraryMore').hidden = state.libraryItems.length >= state.libraryTotal;
    loadResultThumbnails($('#libraryResults'));
  } catch (error) {
    toast(error.message, true);
    $('#libraryResults').innerHTML = '<div class="empty-state"><b>资料库加载失败</b></div>';
  }
}

function smartAlbumCard(album) {
  const filters = Object.entries(album.filters || {}).filter(([, value]) => value).length;
  return `<article class="smart-album-card" data-smart-album="${album.id}"><span>${icon('album')}</span><div><h3>${esc(album.name)}</h3><p>${esc(album.query)}</p><small>${album.kind ? esc(album.kind) : '全部类型'} · ${filters} 个筛选条件</small></div><button class="danger" data-delete-smart-album="${album.id}">删除</button></article>`;
}

async function loadSmartAlbums() {
  const list = $('#smartAlbumList');
  if (!state.user?.id) {
    list.innerHTML = '<div class="empty-state compact"><b>使用本地账号登录后可创建智能相册</b></div>';
    return;
  }
  try {
    state.smartAlbums = await api('/api/smart-albums');
    list.innerHTML = state.smartAlbums.length
      ? state.smartAlbums.map(smartAlbumCard).join('')
      : '<div class="empty-state compact"><b>还没有智能相册</b><p>在智能搜索中设置条件，然后点击“保存为智能相册”。</p></div>';
  } catch (error) {
    toast(error.message, true);
  }
}

async function openSmartAlbum(albumId) {
  try {
    const data = await api(`/api/smart-albums/${albumId}/items?limit=40`);
    state.currentSmartAlbumId = Number(albumId);
    const detail = $('#smartAlbumDetail');
    detail.hidden = false;
    detail.innerHTML = `<div class="person-detail-head"><button class="secondary" data-smart-album-back>返回智能相册</button><div><span class="eyebrow">动态结果</span><h2>${esc(data.album.name)}</h2><p>${esc(data.album.query)} · ${fmtCount(data.total)} 个候选</p></div><button class="secondary" data-use-smart-album="${data.album.id}">在搜索中打开</button></div><div class="results-grid">${data.results.map(resultCard).join('') || '<div class="empty-state compact"><b>当前没有匹配文件</b></div>'}</div>`;
    $('#smartAlbumList').hidden = true;
    loadResultThumbnails(detail);
  } catch (error) {
    toast(error.message, true);
  }
}

function resetConversation() {
  state.currentConversationId = null;
  $('#conversationSelect').value = '';
  $('#deleteConversation').hidden = true;
  $('#conversation').innerHTML = `<div class="assistant-message"><span class="avatar">${icon('model')}</span><div>你可以直接提问。我会先检索本地资料，再给出带来源的回答。</div></div>`;
}

function sourceButton(source, index) {
  const label = source.source_label ? ` · ${source.source_label}` : source.match_time != null ? ` · ${fmtDuration(source.match_time)}` : '';
  return `<button data-file="${source.id}" data-time="${source.match_time ?? ''}">[${index + 1}] ${esc(source.path || source.name)}${esc(label)}</button>`;
}

function renderConversationMessages(messages) {
  const avatar = `<span class="avatar">${icon('model')}</span>`;
  $('#conversation').innerHTML = messages.length
    ? messages.map(message => {
        if (message.role === 'user') return `<div class="user-message"><div>${esc(message.content)}</div></div>`;
        const citations = message.sources?.length
          ? `<div class="citation-list">${message.sources.map(sourceButton).join('')}</div>`
          : '';
        return `<div class="assistant-message">${avatar}<div>${esc(message.content).replace(/\n/g, '<br>')}${citations}</div></div>`;
      }).join('')
    : `<div class="assistant-message">${avatar}<div>这是一个新对话，可以继续提问。</div></div>`;
}

async function loadConversations() {
  if (!state.user?.id) return;
  try {
    state.conversations = await api('/api/conversations');
    $('#conversationSelect').innerHTML = '<option value="">新对话</option>' + state.conversations.map(item => `<option value="${item.id}">${esc(item.title)} · ${fmtCount(item.message_count)} 条</option>`).join('');
    if (state.currentConversationId) $('#conversationSelect').value = String(state.currentConversationId);
  } catch {}
}

async function openConversation(conversationId) {
  if (!conversationId) return resetConversation();
  try {
    const data = await api(`/api/conversations/${conversationId}`);
    state.currentConversationId = Number(conversationId);
    $('#conversationSelect').value = String(conversationId);
    $('#deleteConversation').hidden = false;
    renderConversationMessages(data.messages || []);
  } catch (error) {
    toast(error.message, true);
  }
}

function timelineCard(item) {
  const visual = ['image', 'video'].includes(item.kind)
    ? `<img loading="lazy" data-thumbnail="/api/files/${item.id}/thumbnail" alt="${esc(item.name)}">`
    : `<span class="timeline-file-icon">${icon({ document: 'document', audio: 'audio' }[item.kind] || 'files')}</span>`;
  const duration = item.duration ? `<span>${fmtDuration(item.duration)}</span>` : '';
  return `<article class="timeline-card" data-file="${item.id}"><div class="timeline-thumb">${visual}${duration}</div><div><b>${esc(item.name)}</b><small>${esc(item.kind)} · ${fmtBytes(item.size)}</small></div></article>`;
}

function renderTimeline() {
  const groups = new Map();
  state.timelineItems.forEach(item => {
    const key = item.timeline_date || '时间未知';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  });
  $('#timelineList').innerHTML = groups.size
    ? [...groups.entries()].map(([date, items]) => `<section class="timeline-day"><div class="timeline-day-head"><time>${esc(fmtDate(date))}</time><span>${fmtCount(items.length)} 项</span></div><div class="timeline-grid">${items.map(timelineCard).join('')}</div></section>`).join('')
    : '<div class="empty-state"><span class="empty-icon">' + icon('timeline') + '</span><b>这个时间范围没有文件</b><p>可以切换年份、月份或文件类型。</p></div>';
  loadResultThumbnails($('#timelineList'));
}

async function loadTimeline(reset = false) {
  if (reset) {
    state.timelineItems = [];
    state.timelineOffset = 0;
  }
  const params = new URLSearchParams({ limit: '120', offset: String(state.timelineOffset) });
  const year = $('#timelineYear').value;
  const month = $('#timelineMonth').value;
  const kind = $('#timelineKind').value;
  if (year) params.set('year', year);
  if (month) params.set('month', month);
  if (kind) params.set('kind', kind);
  try {
    const data = await api(`/api/timeline?${params}`);
    if (!$('#timelineYear').dataset.ready) {
      $('#timelineYear').insertAdjacentHTML('beforeend', data.years.map(item => `<option value="${item.year}">${item.year} 年 · ${fmtCount(item.count)}</option>`).join(''));
      $('#timelineYear').dataset.ready = '1';
    }
    state.timelineItems.push(...data.items);
    state.timelineOffset = state.timelineItems.length;
    $('#timelineSummary').textContent = `共 ${fmtCount(data.total)} 项 · 已显示 ${fmtCount(state.timelineItems.length)} 项`;
    $('#timelineMore').hidden = state.timelineItems.length >= data.total;
    renderTimeline();
  } catch (error) {
    toast(error.message, true);
    $('#timelineList').innerHTML = '<div class="empty-state"><b>时间线加载失败</b><p>请稍后重试。</p></div>';
  }
}

function organizerItem(item, mode) {
  const visual = ['image', 'video'].includes(item.kind)
    ? `<img loading="lazy" data-thumbnail="/api/files/${item.id}/thumbnail" alt="${esc(item.name)}">`
    : icon('files');
  const distance = item.distance != null ? `<span class="distance-tag">差异 ${item.distance}</span>` : '';
  const recycle = mode === 'duplicates' && isAdmin()
    ? item.recommended_keep
      ? '<span class="keep-tag">推荐保留</span>'
      : `<label class="duplicate-select"><input type="checkbox" data-duplicate-file="${item.id}" checked><span>批量处理</span></label><button class="recycle-action" data-recycle-file="${item.id}">${icon('recycle')}单独移入回收站</button>`
    : '';
  return `<article class="organizer-item" data-file="${item.id}"><div>${visual}</div><b>${esc(item.name)}</b><small>${fmtBytes(item.size)} · ${esc(item.relative_path)}</small>${distance}${recycle}</article>`;
}

function organizerGroup(group, index, mode) {
  const title = mode === 'duplicates' ? `重复组 ${index + 1}` : `相似组 ${index + 1}`;
  const note = mode === 'duplicates'
    ? `${fmtCount(group.member_count)} 个完全相同文件 · 可回收 ${fmtBytes(group.reclaimable_bytes)}`
    : `${fmtCount(group.member_count)} 张视觉相似照片`;
  return `<section class="organizer-group"><div class="organizer-group-head"><div><h3>${title}</h3><p>${note}</p></div><span>${mode === 'duplicates' ? '内容指纹一致' : '感知哈希相近'}</span></div><div class="organizer-items">${group.items.map(item => organizerItem(item, mode)).join('')}</div></section>`;
}

function updateDuplicateSelection() {
  $('#duplicateSelectedCount').textContent = fmtCount($$('[data-duplicate-file]:checked').length);
  $('#duplicateBulkToolbar').hidden = state.organizerMode !== 'duplicates' || !isAdmin();
}

async function loadOrganizer() {
  try {
    const [duplicates, similar] = await Promise.all([
      api('/api/organizer/duplicates?limit=30'),
      api('/api/organizer/similar?limit=30'),
    ]);
    $('#organizerOverview').innerHTML = [
      statsCard('重复文件组', fmtCount(duplicates.total), `预计可回收 ${fmtBytes(duplicates.reclaimable_bytes)}`, 'duplicate'),
      statsCard('相似照片组', fmtCount(similar.total), '仅展示候选，不自动删除', 'timeline'),
    ].join('');
    const data = state.organizerMode === 'duplicates' ? duplicates : similar;
    $('#organizerGroups').innerHTML = data.groups.length
      ? data.groups.map((group, index) => organizerGroup(group, index, state.organizerMode)).join('')
      : '<div class="empty-state"><span class="empty-icon">' + icon('duplicate') + '</span><b>还没有分析结果</b><p>点击右上角按钮启动可取消的本地分析任务。</p></div>';
    updateDuplicateSelection();
    loadResultThumbnails($('#organizerGroups'));
  } catch (error) {
    toast(error.message, true);
  }
}

function isAdmin() {
  return ['owner', 'admin'].includes(state.user?.role);
}

function applyRole() {
  const admin = isAdmin();
  $$('.admin-only').forEach(element => { element.hidden = !admin; });
  $$('.personal-only').forEach(element => { element.hidden = !state.user?.id; });
  $('#globalScan').hidden = !admin;
  const label = state.user?.display_name || '私有空间';
  $('.workspace-pill span').textContent = label;
  $('.workspace-pill i').textContent = state.user?.role?.toUpperCase() || 'LOCAL';
  $('#authTitle').textContent = state.user ? `已连接：${label}` : '连接私有空间';
  $('#authDescription').textContent = state.user
    ? `${state.user.username} · ${state.user.role === 'owner' ? '系统管理员' : state.user.role === 'admin' ? '管理员' : '普通成员'}`
    : '使用管理员创建的本地账号，或输入系统 API Token。';
  $('#logoutButton').hidden = !state.user;
}

function personCard(person) {
  const visual = person.cover_face_id
    ? `<img loading="lazy" data-thumbnail="/api/faces/${person.cover_face_id}/thumbnail" alt="${esc(person.name)}">`
    : `<span>${icon('people')}</span>`;
  const select = isAdmin() ? `<label class="card-select"><input type="checkbox" data-select-person="${person.id}"><span></span></label>` : '';
  return `<article class="person-card" data-person="${person.id}"><div class="person-cover">${visual}${select}</div><div><h3>${esc(person.name)}</h3><p>${fmtCount(person.file_count)} 张照片 · ${fmtCount(person.face_count)} 张人脸</p></div></article>`;
}

function updatePeopleSelection() {
  $('#peopleSelectedCount').textContent = fmtCount($$('[data-select-person]:checked').length);
}

async function loadPeople(reset = false) {
  if (reset) {
    state.peopleItems = [];
    state.peopleOffset = 0;
  }
  $('#personDetail').hidden = true;
  $('#peopleGrid').hidden = false;
  try {
    const data = await api(`/api/people?limit=60&offset=${state.peopleOffset}`);
    state.peopleItems.push(...data.items);
    state.peopleOffset = state.peopleItems.length;
    $('#peopleSummary').innerHTML = [
      statsCard('已聚类人物', fmtCount(data.total), '可随时重命名', 'people'),
      statsCard('已归类人脸', fmtCount(data.faces), '特征仅保存在本机', 'check'),
    ].join('');
    $('#peopleGrid').innerHTML = state.peopleItems.length
      ? state.peopleItems.map(personCard).join('')
      : '<div class="empty-state"><span class="empty-icon">' + icon('people') + '</span><b>还没有人物分组</b><p>管理员运行识别后，系统会自动聚类至少出现两次的人物。</p></div>';
    $('#peopleMore').hidden = !data.has_more;
    updatePeopleSelection();
    loadResultThumbnails($('#peopleGrid'));
  } catch (error) {
    toast(error.message, true);
  }
}

function curationFileCard(item, mode) {
  const visual = ['image', 'video'].includes(item.kind)
    ? `<img loading="lazy" data-thumbnail="/api/files/${item.id}/thumbnail" alt="${esc(item.name)}">`
    : icon('files');
  const value = mode === 'person' ? item.face_id : item.id;
  return `<article class="timeline-card curation-file" data-file="${item.id}"><div class="timeline-thumb">${visual}<label class="card-select admin-only"><input type="checkbox" data-curation-item="${value}"><span></span></label></div><div><b>${esc(item.name)}</b><small>${esc(item.kind)} · ${fmtBytes(item.size)}</small></div></article>`;
}

async function openPerson(personId) {
  try {
    const data = await api(`/api/people/${personId}`);
    $('#peopleGrid').hidden = true;
    const detail = $('#personDetail');
    detail.hidden = false;
    detail.dataset.personId = personId;
    detail.innerHTML = `<div class="person-detail-head"><button class="secondary" data-person-back>返回人物列表</button><div><span class="eyebrow">人物照片</span><h2>${esc(data.person.name)}</h2><p>${fmtCount(data.files.length)} 个文件</p></div>${isAdmin() ? `<div class="detail-actions"><button class="secondary" data-person-rename="${personId}" data-person-name="${esc(data.person.name)}">重命名</button><button class="secondary" id="personSetCover">设为封面</button><button class="secondary" id="personSplit">拆分所选</button></div>` : ''}</div><div class="timeline-grid">${data.files.map(item => curationFileCard(item, 'person')).join('')}</div>`;
    loadResultThumbnails(detail);
  } catch (error) {
    toast(error.message, true);
  }
}

function albumCard(item, type) {
  const visual = item.cover_file_id
    ? `<img loading="lazy" data-thumbnail="/api/files/${item.cover_file_id}/thumbnail" alt="${esc(item.name)}">`
    : `<span>${icon(type === 'place' ? 'place' : 'event')}</span>`;
  const detail = type === 'place'
    ? `${Number(item.latitude).toFixed(3)}, ${Number(item.longitude).toFixed(3)} · 半径 ${Math.max(0, Number(item.radius_m || 0) / 1000).toFixed(1)} km`
    : `${fmtDate(item.start_at)}${item.end_at && item.end_at !== item.start_at ? `－${fmtDate(item.end_at)}` : ''}`;
  const select = type === 'event' && isAdmin()
    ? `<label class="card-select"><input type="checkbox" data-select-event="${item.id}"><span></span></label>`
    : '';
  return `<article class="album-card" data-${type}="${item.id}"><div class="album-cover">${visual}${select}</div><div><h3>${esc(item.name)}</h3><p>${fmtCount(item.file_count)} 个文件</p><small>${esc(detail)}</small></div></article>`;
}

function renderPlaceMap(items) {
  if (!items.length) return '';
  const latitudes = items.map(item => Number(item.latitude));
  const longitudes = items.map(item => Number(item.longitude));
  const minLatitude = Math.min(...latitudes);
  const maxLatitude = Math.max(...latitudes);
  const minLongitude = Math.min(...longitudes);
  const maxLongitude = Math.max(...longitudes);
  const latitudeRange = Math.max(0.001, maxLatitude - minLatitude);
  const longitudeRange = Math.max(0.001, maxLongitude - minLongitude);
  const markers = items.slice(0, 100).map(item => {
    const left = 5 + (Number(item.longitude) - minLongitude) / longitudeRange * 90;
    const top = 95 - (Number(item.latitude) - minLatitude) / latitudeRange * 90;
    const size = Math.min(22, 8 + Math.log2(Number(item.file_count || 1)) * 2);
    return `<button style="left:${left}%;top:${top}%;width:${size}px;height:${size}px" data-place="${item.id}" title="${esc(item.name)} · ${fmtCount(item.file_count)} 项"></button>`;
  }).join('');
  return `<div class="place-map"><div class="place-map-grid"></div>${markers}<span>离线位置分布 · 不加载外部地图</span></div>`;
}

async function loadPlaces() {
  $('#placeDetail').hidden = true;
  $('#placesGrid').hidden = false;
  try {
    const data = await api('/api/places');
    $('#placesSummary').innerHTML = [
      statsCard('地点相册', fmtCount(data.total), `${fmtCount(data.files)} 个定位文件`, 'place'),
      renderPlaceMap(data.items),
    ].join('');
    $('#placesGrid').innerHTML = data.items.length
      ? data.items.map(item => albumCard(item, 'place')).join('')
      : '<div class="empty-state"><span class="empty-icon">' + icon('place') + '</span><b>还没有地点相册</b><p>运行地点分析后，会聚合带 GPS 元数据的照片和视频。</p></div>';
    await loadResultThumbnails($('#view-places'));
  } catch (error) { toast(error.message, true); }
}

async function openPlace(placeId) {
  try {
    const data = await api(`/api/places/${placeId}`);
    $('#placesGrid').hidden = true;
    const detail = $('#placeDetail');
    detail.hidden = false;
    detail.innerHTML = `<div class="person-detail-head"><button class="secondary" data-place-back>返回地点列表</button><div><span class="eyebrow">地点照片</span><h2>${esc(data.place.name)}</h2><p>${fmtCount(data.files.length)} 个文件 · ${Number(data.place.latitude).toFixed(4)}, ${Number(data.place.longitude).toFixed(4)}</p></div>${isAdmin() ? `<button class="secondary" data-place-rename="${placeId}" data-album-name="${esc(data.place.name)}">重命名</button>` : ''}</div><div class="timeline-grid">${data.files.map(timelineCard).join('')}</div>`;
    await loadResultThumbnails(detail);
  } catch (error) { toast(error.message, true); }
}

function updateEventSelection() {
  $('#eventsSelectedCount').textContent = fmtCount($$('[data-select-event]:checked').length);
}

async function loadEvents(reset = false) {
  if (reset) {
    state.eventsItems = [];
    state.eventsOffset = 0;
  }
  $('#eventDetail').hidden = true;
  $('#eventsGrid').hidden = false;
  try {
    const data = await api(`/api/events?limit=60&offset=${state.eventsOffset}`);
    state.eventsItems.push(...data.items);
    state.eventsOffset = state.eventsItems.length;
    $('#eventsSummary').innerHTML = [
      statsCard('自动事件', fmtCount(data.total), `${fmtCount(data.files)} 个媒体文件`, 'event'),
      statsCard('整理依据', '时间 + 位置', '间隔、距离与活动跨度', 'timeline'),
    ].join('');
    $('#eventsGrid').innerHTML = state.eventsItems.length
      ? state.eventsItems.map(item => albumCard(item, 'event')).join('')
      : '<div class="empty-state"><span class="empty-icon">' + icon('event') + '</span><b>还没有事件相册</b><p>运行事件分析后，会自动整理至少包含 3 个媒体文件的活动。</p></div>';
    $('#eventsMore').hidden = !data.has_more;
    updateEventSelection();
    loadResultThumbnails($('#view-events'));
  } catch (error) { toast(error.message, true); }
}

async function openEvent(eventId) {
  try {
    const data = await api(`/api/events/${eventId}`);
    $('#eventsGrid').hidden = true;
    const detail = $('#eventDetail');
    detail.hidden = false;
    detail.dataset.eventId = eventId;
    detail.innerHTML = `<div class="person-detail-head"><button class="secondary" data-event-back>返回事件列表</button><div><span class="eyebrow">自动事件</span><h2>${esc(data.event.name)}</h2><p>${fmtCount(data.files.length)} 个文件 · ${esc(fmtDate(data.event.start_at))}－${esc(fmtDate(data.event.end_at))}</p></div>${isAdmin() ? `<div class="detail-actions"><button class="secondary" data-event-rename="${eventId}" data-album-name="${esc(data.event.name)}">重命名</button><button class="secondary" id="eventSetCover">设为封面</button><button class="secondary" id="eventSplit">拆分所选</button></div>` : ''}</div><div class="timeline-grid">${data.files.map(item => curationFileCard(item, 'event')).join('')}</div>`;
    loadResultThumbnails(detail);
  } catch (error) { toast(error.message, true); }
}

async function loadRecycle() {
  if (!isAdmin()) return;
  try {
    const data = await api('/api/recycle');
    $('#recycleSummary').innerHTML = [
      statsCard('待处理文件', fmtCount(data.total), '均可恢复到原路径', 'recycle'),
      statsCard('占用空间', fmtBytes(data.bytes), '永久清除后释放', 'storage'),
    ].join('');
    $('#recycleList').innerHTML = data.items.length
      ? data.items.map(item => `<article class="recycle-row"><span class="card-leading">${icon('recycle')}</span><div><h3>${esc(item.name)}</h3><p>${fmtBytes(item.size)} · ${esc(item.relative_path)}</p><small>移入时间 ${esc(item.trashed_at)} · 操作者 ${esc(item.actor)}</small></div><div class="card-actions"><button class="secondary" data-restore-trash="${item.id}">恢复</button><button class="danger" data-purge-trash="${item.id}">永久清除</button></div></article>`).join('')
      : '<div class="empty-state"><span class="empty-icon">' + icon('recycle') + '</span><b>回收站为空</b><p>从重复文件分析中移入的文件会显示在这里。</p></div>';
  } catch (error) { toast(error.message, true); }
}

function userCard(user) {
  const privileged = ['owner', 'admin'].includes(user.role);
  const canEdit = user.role !== 'owner' || state.user?.role === 'owner';
  const libraries = privileged
    ? '全部媒体库'
    : user.library_ids.map(id => state.libraries.find(item => item.id === id)?.name).filter(Boolean).join('、') || '未授权媒体库';
  const roleLabel = user.role === 'owner' ? '系统所有者' : user.role === 'admin' ? '管理员' : '普通成员';
  return `<article class="user-card"><span class="card-leading">${icon('user')}</span><div><h3>${esc(user.display_name)} <small>@${esc(user.username)}</small></h3><p>${roleLabel} · ${esc(libraries)}</p><span class="user-state ${user.enabled ? 'enabled' : ''}">${user.enabled ? '已启用' : '已停用'}</span></div>${canEdit ? `<button class="secondary" data-edit-user="${user.id}">编辑</button>` : '<span class="project-state">受保护</span>'}</article>`;
}

async function loadUsers() {
  if (!isAdmin()) return;
  try {
    const [users, libraries] = await Promise.all([api('/api/users'), api('/api/libraries')]);
    state.users = users;
    state.libraries = libraries;
    $('#userList').innerHTML = users.length ? users.map(userCard).join('') : '<div class="empty-state"><b>还没有独立账号</b></div>';
  } catch (error) {
    toast(error.message, true);
  }
}

function openUserModal(user = null) {
  const form = $('#userForm');
  const owner = user?.role === 'owner';
  form.reset();
  form.elements.id.value = user?.id || '';
  form.elements.username.value = user?.username || '';
  form.elements.username.disabled = Boolean(user);
  form.elements.display_name.value = user?.display_name || '';
  form.elements.password.required = !user;
  form.elements.password.placeholder = user ? '留空则不修改' : '至少 8 位';
  form.elements.role.value = user?.role || 'member';
  form.elements.role.disabled = owner;
  form.elements.enabled.checked = user?.enabled !== 0;
  form.elements.enabled.disabled = owner;
  $('#userModalTitle').textContent = user ? '编辑用户' : '添加用户';
  $('#permissionList').innerHTML = state.libraries.map(library => `<label><input type="checkbox" name="library_ids" value="${library.id}" ${(owner || user?.library_ids.includes(library.id)) ? 'checked' : ''} ${owner ? 'disabled' : ''}><span>${esc(library.name)}</span><small>${esc(library.path)}</small></label>`).join('');
  $('#userModal').classList.add('open');
}

async function loadOperations() {
  if (!isAdmin()) return;
  try {
    const [data, audit, snapshots] = await Promise.all([
      api('/api/operations/status'),
      api('/api/audit?limit=100'),
      api('/api/operations/vector-snapshots'),
    ]);
    const healthy = data.database.quick_check === 'ok';
    $('#opsStatus').innerHTML = [
      statsCard('索引数据库', healthy ? '正常' : data.database.quick_check, fmtBytes(data.database.bytes), healthy ? 'check' : 'activity'),
      statsCard('应用备份', fmtCount(data.backups.count), data.backups.latest || '尚无备份', 'backup'),
      statsCard('目录监听', data.watcher.mode === 'hybrid' ? '混合监控' : data.watcher.mode === 'polling' ? '属性轮询' : '已关闭', `${fmtCount(data.watcher.watches)} 个 inotify 监听 · ${data.watcher.poll_seconds || 0} 秒校验`, 'activity'),
      statsCard('回收站', fmtCount(data.recycle.count), `${fmtBytes(data.recycle.bytes)} 可恢复`, 'recycle'),
    ].join('');
    const production = data.production || { ready: false, checks: [] };
    $('#productionBadge').textContent = production.ready ? '生产基线通过' : '存在阻塞项';
    $('#productionBadge').classList.toggle('warning', !production.ready);
    $('#productionChecks').innerHTML = production.checks.length
      ? production.checks.map(check => `<div class="production-check ${esc(check.level)}"><i></i><div><b>${esc(check.name)}</b><span>${esc(check.detail)}</span></div><small>${check.level === 'ok' ? '通过' : check.level === 'warning' ? '提醒' : '阻塞'}</small></div>`).join('')
      : '<div class="empty-state compact"><b>尚未取得生产检查结果</b></div>';
    const stages = data.indexing.stages;
    const stageCard = (name, values) => {
      const ready = Number(values.ready || 0) + Number(values.manual || 0) + Number(values.not_applicable || 0);
      const trouble = Number(values.error || 0) + Number(values.missing || 0) + Number(values.blocked || 0);
      const pending = Number(values.pending || 0);
      return `<div class="stage-health-card"><span>${esc(name)}</span><b>${fmtCount(ready)} 正常</b><div class="stage-stack"><i style="--stage:${Math.max(2, ready)}"></i><i class="warning" style="--stage:${Math.max(0, trouble)}"></i><i class="muted" style="--stage:${Math.max(0, pending)}"></i></div><small>${fmtCount(trouble)} 需修复 · ${fmtCount(pending)} 待处理</small></div>`;
    };
    $('#stageHealthGrid').innerHTML = [
      stageCard('视觉描述', stages.vision || {}),
      stageCard('语音转写', stages.transcription || {}),
      stageCard('语义向量', stages.embedding || {}),
      `<div class="stage-health-card repair-total"><span>自动修复队列</span><b>${fmtCount(stages.repairable)} 个</b><small>${fmtCount(stages.retry_waiting)} 个退避等待 · ${fmtCount(stages.terminal_failures)} 个人工检查</small></div>`,
    ].join('');
    $('#repairFromOperations').disabled = !Number(stages.repairable || 0);
    $('#snapshotList').innerHTML = snapshots.items.length
      ? snapshots.items.map(item => `<div class="snapshot-row"><span>${icon('vector')}</span><div><b>${esc(item.name)}</b><small>${fmtBytes(item.bytes)} · ${esc(item.created_at)}</small></div><button class="secondary" data-restore-snapshot="${esc(item.name)}">恢复</button><button class="danger" data-delete-snapshot="${esc(item.name)}">删除</button></div>`).join('')
      : '<div class="empty-state compact"><b>尚无向量快照</b><p>完成一批索引后创建，可用于 Qdrant 集合灾难恢复。</p></div>';
    $('#snapshotList').dataset.collection = snapshots.collection;
    $('#visionUpgradeStatus').innerHTML = `<span>当前描述版本 v${data.vision_captions.version}</span><b>${fmtCount(data.vision_captions.pending_upgrade)} 张旧版图片等待升级</b>`;
    $('#auditList').innerHTML = audit.length
      ? audit.map(item => `<div class="audit-row"><span>${esc(item.actor)}</span><b>${esc(item.action)}</b><small>${esc(item.target_type)} ${esc(item.target_id)}</small><time>${esc(item.created_at)}</time></div>`).join('')
      : '<div class="empty-state compact"><b>暂无操作记录</b></div>';
  } catch (error) {
    toast(error.message, true);
  }
}

function uploadOne(file, progress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/uploads');
    xhr.setRequestHeader('X-Filename', encodeURIComponent(file.name));
    if (state.token) xhr.setRequestHeader('Authorization', `Bearer ${state.token}`);
    xhr.upload.addEventListener('progress', event => {
      if (event.lengthComputable) progress(event.loaded / event.total);
    });
    xhr.addEventListener('load', () => {
      let payload = {};
      try { payload = JSON.parse(xhr.responseText || '{}'); } catch {}
      if (xhr.status >= 200 && xhr.status < 300) resolve(payload);
      else reject(new Error(payload.detail || `上传失败 (${xhr.status})`));
    });
    xhr.addEventListener('error', () => reject(new Error('上传连接中断')));
    xhr.send(file);
  });
}

function closeFileViewer() {
  $('#fileModal').classList.remove('open');
  state.currentFile = null;
  state.currentFileUrl = '';
  $('#filePreview').innerHTML = '';
}

async function openFileViewer(fileId, matchTime = null) {
  const modal = $('#fileModal');
  modal.classList.add('open');
  $('#filePreview').innerHTML = '<div class="viewer-loading">正在安全读取原文件…</div>';
  $('#fileName').textContent = '正在载入';
  $('#filePath').textContent = '';
  $('#fileFacts').innerHTML = '';
  $('#fileCaption').innerHTML = '';
  $('#fileTimeline').innerHTML = '';
  try {
    const [details, ticket] = await Promise.all([
      api(`/api/files/${fileId}`),
      api(`/api/files/${fileId}/ticket`, { method: 'POST' }),
    ]);
    state.currentFile = details;
    state.currentFileUrl = ticket.url;
    $('#fileKind').textContent = details.kind === 'image' ? '照片详情' : details.kind === 'video' ? '视频详情' : '文件详情';
    $('#fileName').textContent = details.name;
    $('#filePath').textContent = details.relative_path;
    $('#fileFacts').innerHTML = [
      ['大小', fmtBytes(details.size)],
      ['时间', fmtDate(details.captured_at || new Date(Number(details.mtime_ns) / 1e6).toISOString())],
      details.width ? ['尺寸', `${details.width} × ${details.height}`] : null,
      details.duration ? ['时长', fmtDuration(details.duration)] : null,
      ['整体索引', details.status === 'ready' ? '完整' : details.status === 'partial' ? '部分完成' : taskStatus(details.status)],
      ['视觉', stageLabel(details.vision_status)],
      ['语音', stageLabel(details.transcription_status)],
      ['向量', stageLabel(details.embedding_status)],
      Number(details.retry_count) ? ['重试次数', fmtCount(details.retry_count)] : null,
      details.next_retry_at ? ['下次重试', new Date(details.next_retry_at).toLocaleString('zh-CN')] : null,
      Number(details.terminal_error) ? ['自动重试', '已停止，需人工检查'] : null,
    ].filter(Boolean).map(item => `<div><span>${item[0]}</span><b>${esc(item[1])}</b></div>`).join('');
    $('#fileCaption').innerHTML = details.ai_caption
      ? `<h3>${details.manual_caption ? '人工内容描述' : 'AI 内容描述'}</h3><p>${esc(details.ai_caption).replace(/\n/g, '<br>')}</p>${details.vision_error ? `<small class="stage-error">${esc(details.vision_error)}</small>` : ''}`
      : `<p class="muted">该文件还没有内容描述。</p>${details.vision_error ? `<small class="stage-error">${esc(details.vision_error)}</small>` : ''}`;
    $('#manualCaption').value = details.manual_caption || '';
    $('#reindexFile').textContent = Number(details.terminal_error) ? '清除失败状态并手动重试' : '重新建立 AI 索引';
    $('#favoriteFile').classList.toggle('active', Boolean(details.favorite));
    $('#favoriteFile').textContent = details.favorite ? '★ 已收藏' : '☆ 收藏';
    $('#fileTags').value = (details.tags || []).join(', ');
    const timed = details.chunks.filter(chunk => chunk.start_time != null);
    const sourced = details.chunks.filter(chunk => chunk.source_label && chunk.start_time == null);
    $('#fileTimeline').innerHTML = [
      timed.length ? `<h3>音视频时间轴</h3>${timed.map(chunk => `<button data-seek="${chunk.start_time}"><span>${fmtDuration(chunk.start_time)}</span><b>${esc(chunk.source_label || '')}</b>${esc(chunk.content)}</button>`).join('')}` : '',
      sourced.length ? `<h3>文档来源</h3>${sourced.slice(0, 40).map(chunk => `<div class="source-chunk"><span>${esc(chunk.source_label)}</span><p>${esc(chunk.content)}</p></div>`).join('')}` : '',
    ].join('');
    if (details.kind === 'image') {
      $('#filePreview').innerHTML = `<img src="${esc(ticket.url)}" alt="${esc(details.name)}">`;
    } else if (details.kind === 'video') {
      $('#filePreview').innerHTML = `<video controls preload="metadata" src="${esc(ticket.url)}"></video>`;
      const video = $('#filePreview video');
      if (matchTime != null && Number.isFinite(Number(matchTime))) video.addEventListener('loadedmetadata', () => { video.currentTime = Number(matchTime); }, { once: true });
    } else if (details.kind === 'audio') {
      $('#filePreview').innerHTML = `<div class="audio-preview">${icon('audio')}<audio controls src="${esc(ticket.url)}"></audio></div>`;
    } else if (details.mime_type === 'application/pdf') {
      $('#filePreview').innerHTML = `<iframe src="${esc(ticket.url)}" title="${esc(details.name)}"></iframe>`;
    } else {
      $('#filePreview').innerHTML = `<div class="generic-preview">${icon({ document: 'document', archive: 'archive' }[details.kind] || 'files')}<p>${esc(details.name)}</p></div>`;
    }
  } catch (error) {
    toast(error.message, true);
    closeFileViewer();
  }
}

function renderHomeTasks(tasks) {
  const items = tasks.slice(0, 3);
  $('#homeTasks').innerHTML = items.length
    ? items.map(task => `<div class="home-task-row"><i class="${esc(task.status)}"></i><div><b>${esc(taskTitle(task))}</b><small>${esc(task.message || task.error || '等待执行')}</small></div><span>${esc(taskStatus(task.status))}</span></div>`).join('')
    : '<div class="home-task-row"><i></i><div><b>当前没有处理任务</b><small>扫描媒体库后，处理进度会显示在这里</small></div><span>空闲</span></div>';
}

async function loadTasks(quiet = false) {
  try {
    const tasks = await api('/api/tasks');
    const activeCount = tasks.filter(task => ['pending', 'running'].includes(task.status)).length;
    $('#taskCount').textContent = activeCount;
    renderHomeTasks(tasks);
    $('#taskList').innerHTML = tasks.length
      ? tasks.map(task => {
        const done = Number(task.work_done || 0);
        const total = Number(task.work_total || 0);
        const work = total ? ` · ${fmtCount(done)}/${fmtCount(total)}` : '';
        const heartbeat = task.heartbeat_at && task.status === 'running'
          ? ` · 心跳 ${new Date(task.heartbeat_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}`
          : '';
        return `<article class="task-card"><span class="card-leading">${icon('task')}</span><div><h3>${esc(taskTitle(task))}</h3><p>${esc(task.message || task.error || '等待执行')}${esc(work)}${esc(heartbeat)} · ${esc(task.created_at)}</p><div class="progress"><span style="width:${Math.round(task.progress * 100)}%"></span></div></div><div class="card-actions"><span class="task-status ${esc(task.status)}">${esc(taskStatus(task.status))}</span>${['pending', 'running'].includes(task.status) ? `<button class="danger" data-cancel="${task.id}">取消</button>` : ''}${['failed', 'cancelled'].includes(task.status) ? `<button class="secondary" data-retry="${task.id}">重试</button>` : ''}</div></article>`;
      }).join('')
      : '<div class="empty-state"><span class="empty-icon">' + icon('task') + '</span><b>暂无处理任务</b><p>扫描媒体库时，实时进度会显示在这里。</p></div>';
  } catch (error) {
    if (!quiet) toast(error.message, true);
  }
}

async function ask(question) {
  const conversation = $('#conversation');
  const avatar = `<span class="avatar">${icon('model')}</span>`;
  conversation.insertAdjacentHTML('beforeend', `<div class="user-message"><div>${esc(question)}</div></div><div class="assistant-message pending">${avatar}<div>正在检索本地资料并组织答案…</div></div>`);
  conversation.lastElementChild.scrollIntoView({ behavior: 'smooth' });
  try {
    const data = await api('/api/ask', {
      method: 'POST',
      body: JSON.stringify({ question, conversation_id: state.currentConversationId }),
    });
    if (data.conversation_id) {
      state.currentConversationId = Number(data.conversation_id);
      $('#deleteConversation').hidden = false;
      await loadConversations();
      $('#conversationSelect').value = String(state.currentConversationId);
    }
    const pending = $('.assistant-message.pending');
    pending.classList.remove('pending');
    const citations = data.sources.length
      ? `<div class="citation-list">${data.sources.slice(0, 8).map(sourceButton).join('')}</div>`
      : '';
    pending.lastElementChild.innerHTML = `${esc(data.answer).replace(/\n/g, '<br>')}${citations}`;
  } catch (error) {
    const pending = $('.assistant-message.pending');
    if (pending) {
      pending.classList.remove('pending');
      pending.lastElementChild.textContent = error.message;
    }
    toast(error.message, true);
  }
}

const projectRoleLabel = role => ({
  owner: '所有者', manager: '项目经理', editor: '编辑',
  reviewer: '审阅者', viewer: '查看者',
}[role] || role);

const assetStatusName = key => {
  const status = state.projectDetails?.statuses?.find(item => item.key === key);
  return status?.name || key || '待整理';
};

const assetStatusColor = key => {
  const status = state.projectDetails?.statuses?.find(item => item.key === key);
  return status?.color || '#7f8997';
};

const supportsCardThumbnail = item => (
  ['image', 'video'].includes(item.kind) || item.mime_type === 'application/pdf'
);

const assetCardPreview = item => (
  item.file_id && supportsCardThumbnail(item)
    ? `<img data-thumbnail="/api/files/${item.file_id}/thumbnail" alt="">`
    : `<span class="asset-file-icon">${icon(item.kind === 'audio' ? 'audio' : item.kind === 'archive' ? 'archive' : 'document')}</span>`
);

async function loadProjects(quiet = false) {
  try {
    const projects = await api('/api/projects');
    state.projects = projects;
    const assets = projects.reduce((sum, item) => sum + Number(item.asset_count || 0), 0);
    const memberSeats = projects.reduce((sum, item) => sum + Number(item.member_count || 0), 0);
    const openComments = projects.reduce((sum, item) => sum + Number(item.open_comment_count || 0), 0);
    $('#reviewCount').textContent = openComments;
    $('#projectOverview').innerHTML = [
      statsCard('活跃项目', fmtCount(projects.filter(item => item.status === 'active').length), `${fmtCount(projects.length)} 个项目空间`, 'library'),
      statsCard('项目素材', fmtCount(assets), '原文件保持只读引用', 'files'),
      statsCard('待处理审阅', fmtCount(openComments), openComments ? '需要团队继续处理' : '当前审阅已清空', 'activity'),
      statsCard('协作席位', fmtCount(memberSeats), '按项目累计成员角色', 'people'),
    ].join('');
    renderProjectGrid();
    const select = $('#assetTargetProject');
    if (select) {
      const selected = select.value;
      select.innerHTML = projects.map(item => `<option value="${item.id}">${esc(item.name)}</option>`).join('');
      if ([...select.options].some(option => option.value === selected)) select.value = selected;
    }
    return projects;
  } catch (error) {
    if (!quiet) toast(error.message, true);
    return [];
  }
}

function renderProjectGrid() {
  const projects = [...state.projects];
  if ($('#projectSort')?.value === 'name') {
    projects.sort((left, right) => String(left.name).localeCompare(String(right.name), 'zh-CN'));
  }
  $('#projectGrid').innerHTML = projects.length
    ? projects.map(item => `<article class="project-card" data-project="${item.id}" style="--project-color:${esc(item.color)}">
        <div class="project-card-head"><i class="project-color"></i><span class="project-state">${item.status === 'archived' ? '已归档' : esc(projectRoleLabel(item.access_role))}</span></div>
        <h3>${esc(item.name)}</h3><p>${esc(item.description || '尚未填写项目说明')}</p>
        <div class="project-card-foot"><div><span>素材</span><b>${fmtCount(item.asset_count)}</b></div><div><span>成员</span><b>${fmtCount(item.member_count)}</b></div><div><span>待审阅</span><b>${fmtCount(item.open_comment_count)}</b></div></div>
      </article>`).join('')
    : '<div class="empty-state"><b>还没有项目</b><p>创建第一个项目，把素材版本、审阅和交付统一起来。</p></div>';
}

function folderDepth(folder, folders, seen = new Set()) {
  if (!folder.parent_id || seen.has(folder.id)) return 0;
  seen.add(folder.id);
  const parent = folders.find(item => item.id === folder.parent_id);
  return parent ? Math.min(2, 1 + folderDepth(parent, folders, seen)) : 0;
}

function fillFolderSelect(selector, folders, selected = '') {
  const select = $(selector);
  if (!select) return;
  select.innerHTML = '<option value="">项目根目录</option>' + folders.map(folder => {
    const depth = folderDepth(folder, folders);
    return `<option value="${folder.id}">${'　'.repeat(depth)}${esc(folder.name)}</option>`;
  }).join('');
  select.value = selected == null ? '' : String(selected);
}

async function openProject(projectId) {
  state.currentProjectId = Number(projectId);
  state.projectFolderId = null;
  showView('project');
}

async function loadProjectWorkspace(projectId, quiet = false) {
  try {
    const [details, assets, tasks] = await Promise.all([
      api(`/api/projects/${projectId}`),
      api(`/api/projects/${projectId}/assets?limit=200`),
      api(`/api/projects/${projectId}/review-tasks`),
    ]);
    state.currentProjectId = Number(projectId);
    state.currentProject = details.project;
    state.projectDetails = details;
    state.projectAssets = assets.items;
    $('#projectTitle').textContent = details.project.name;
    $('#projectDescription').textContent = details.project.description || '项目素材、版本和审阅协作空间';
    $('#projectEyebrow').textContent = `${projectRoleLabel(details.access_role)} · ${details.project.status === 'archived' ? '已归档' : '进行中'}`;
    const canEdit = ['owner', 'manager', 'editor'].includes(details.access_role);
    const canManage = ['owner', 'manager'].includes(details.access_role);
    $('#addProjectAsset').hidden = !canEdit;
    $('#projectInboxButton').hidden = !canEdit;
    $('#createProjectFolder').hidden = !canEdit;
    $('#projectMembersButton').hidden = !canManage;
    $('#projectShareButton').hidden = !canManage;
    $('#allProjectAssetCount').textContent = fmtCount(details.project.asset_count);
    $('#projectStatusFilter').innerHTML = '<option value="">全部状态</option>' + details.statuses.map(item => `<option value="${esc(item.key)}">${esc(item.name)}</option>`).join('');
    $('#projectFolderTree').innerHTML = details.folders.map(folder => `<button class="folder-item" data-project-folder="${folder.id}" data-depth="${folderDepth(folder, details.folders)}">${esc(folder.name)}<i>${fmtCount(folder.asset_count)}</i></button>`).join('');
    $('#workflowSummary').innerHTML = details.statuses.map(status => {
      const count = assets.items.filter(item => item.status === status.key).length;
      return `<div class="workflow-row" style="--status-color:${esc(status.color)}"><i></i><span>${esc(status.name)}</span><b>${fmtCount(count)}</b></div>`;
    }).join('');
    fillFolderSelect('#folderForm [name=parent_id]', details.folders);
    fillFolderSelect('#assetTargetFolder', details.folders);
    renderProjectAssets(assets.items, assets.total);
    renderProjectReviewTasks(tasks.items);
  } catch (error) {
    if (!quiet) toast(error.message, true);
  }
}

async function loadProjectAssets() {
  if (!state.currentProjectId) return;
  const params = new URLSearchParams({ limit: 200 });
  if (state.projectFolderId != null) params.set('folder_id', state.projectFolderId);
  const status = $('#projectStatusFilter').value;
  const query = $('#projectAssetSearch [name=q]').value.trim();
  if (status) params.set('status', status);
  if (query) params.set('q', query);
  try {
    const data = await api(`/api/projects/${state.currentProjectId}/assets?${params}`);
    state.projectAssets = data.items;
    renderProjectAssets(data.items, data.total);
  } catch (error) {
    toast(error.message, true);
  }
}

function renderProjectAssets(items, total) {
  const folder = state.projectDetails?.folders?.find(item => item.id === Number(state.projectFolderId));
  $('#assetBoardTitle').textContent = folder?.name || '全部素材';
  $('#assetBoardMeta').textContent = `${fmtCount(total)} 个素材`;
  const grid = $('#projectAssets');
  grid.classList.toggle('list', state.assetLayout === 'list');
  grid.innerHTML = items.length
    ? items.map(item => `<article class="asset-card" data-asset="${item.id}">
        <div class="asset-card-preview">${assetCardPreview(item)}<span class="asset-kind">${esc(item.kind)}</span><span class="asset-version">V${fmtCount(item.version_number)}</span>${item.duration ? `<span class="asset-duration">${fmtDuration(item.duration)}</span>` : ''}</div>
        <div class="asset-card-body"><h3>${esc(item.title)}</h3><p>${esc(item.caption || item.file_name || '暂无内容描述')}</p><div class="asset-card-meta"><span class="asset-status-dot" style="--status-color:${assetStatusColor(item.status)}">${esc(assetStatusName(item.status))}</span><span>${item.open_comment_count ? `${fmtCount(item.open_comment_count)} 条待处理` : `${'★'.repeat(Number(item.rating || 0)) || '未评级'}`}</span></div></div>
      </article>`).join('')
    : '<div class="empty-state"><b>当前文件夹没有素材</b><p>从资料库添加素材，或切换筛选条件。</p></div>';
  loadResultThumbnails(grid);
}

function renderProjectReviewTasks(items) {
  $('#projectReviewTasks').innerHTML = items.length
    ? items.slice(0, 12).map(item => `<article class="review-task" data-review-asset="${item.asset_id}" data-review-time="${item.time_start ?? ''}"><b>${esc(item.asset_title)}</b><span>${esc(item.body)}</span><small>${item.time_start != null ? fmtDuration(item.time_start) : '整条素材'} · ${esc(item.author)}</small></article>`).join('')
    : '<div class="empty-state compact"><b>没有待处理意见</b><p>当前项目审阅已清空。</p></div>';
}

async function loadCurrentProjectReviewTasks(projectId = state.currentAsset?.project_id || state.currentProjectId) {
  if (!projectId) return;
  try {
    const data = await api(`/api/projects/${projectId}/review-tasks`);
    if (Number(state.currentProjectId) === Number(projectId) && $('#view-project').classList.contains('active')) {
      renderProjectReviewTasks(data.items);
    }
    if ($('#view-reviews').classList.contains('active')) await loadAllReviewTasks();
  } catch (error) { toast(error.message, true); }
}

async function loadAllReviewTasks() {
  try {
    const projects = state.projects.length ? state.projects : await loadProjects(true);
    const groups = await Promise.all(projects.map(async project => ({
      project,
      tasks: (await api(`/api/projects/${project.id}/review-tasks`)).items,
    })));
    const tasks = groups.flatMap(group => group.tasks.map(item => ({ ...item, project: group.project })));
    $('#reviewDashboard').innerHTML = [
      statsCard('待处理意见', fmtCount(tasks.length), `${fmtCount(groups.filter(group => group.tasks.length).length)} 个项目涉及`, 'activity'),
      statsCard('时间点意见', fmtCount(tasks.filter(item => item.time_start != null).length), '可直接跳转到画面', 'timeline'),
      statsCard('文字意见', fmtCount(tasks.filter(item => item.time_start == null).length), '整条素材层级', 'document'),
      statsCard('已清空项目', fmtCount(groups.filter(group => !group.tasks.length).length), '当前没有未解决意见', 'check'),
    ].join('');
    $('#reviewCount').textContent = tasks.length;
    $('#reviewInbox').innerHTML = tasks.length
      ? tasks.map(item => `<article class="review-inbox-row"><span>${icon('activity')}</span><div><b>${esc(item.asset_title)}</b><small>${esc(item.project.name)} · ${esc(item.author)} · ${esc(item.body)}</small></div><button class="secondary" data-review-asset="${item.asset_id}" data-review-time="${item.time_start ?? ''}">${item.time_start != null ? fmtDuration(item.time_start) : '打开审阅'}</button></article>`).join('')
      : '<div class="empty-state"><b>审阅队列已经清空</b><p>新的项目意见会自动汇总到这里。</p></div>';
  } catch (error) { toast(error.message, true); }
}

async function loadDeliveries() {
  try {
    const projects = state.projects.length ? state.projects : await loadProjects(true);
    const groups = await Promise.all(projects.map(async project => {
      const detail = await api(`/api/projects/${project.id}`);
      return { project, shares: detail.shares || [] };
    }));
    const shares = groups.flatMap(group => group.shares.map(item => ({ ...item, project: group.project })));
    $('#deliverySummary').innerHTML = [
      statsCard('分享链接', fmtCount(shares.length), `${fmtCount(shares.filter(item => item.enabled).length)} 个正在生效`, 'files'),
      statsCard('允许下载', fmtCount(shares.filter(item => item.can_download).length), '其余仅允许在线预览', 'storage'),
      statsCard('访问码保护', fmtCount(shares.filter(item => item.access_code_required).length), '外部访问二次校验', 'check'),
      statsCard('最近访问', fmtCount(shares.filter(item => item.last_access_at).length), '仅记录访问时间，不跟踪访客', 'activity'),
    ].join('');
    $('#deliveryList').innerHTML = shares.length
      ? shares.map(item => `<article class="delivery-row"><span>${icon('files')}</span><div><b>${esc(item.name)}</b><small>${esc(item.project.name)} · ${item.asset_title ? esc(item.asset_title) : '整个项目'} · ${item.expires_at ? `有效期至 ${fmtDate(item.expires_at)}` : '长期有效'}</small></div><div class="delivery-actions"><span class="user-state ${item.enabled ? 'enabled' : ''}">${item.enabled ? '已启用' : '已关闭'}</span><button class="secondary" data-open-project-delivery="${item.project_id}">管理</button></div></article>`).join('')
      : '<div class="empty-state"><b>还没有外部分享</b><p>进入项目后创建带权限和水印的审阅链接。</p></div>';
  } catch (error) { toast(error.message, true); }
}

function reviewMedia() {
  return $('#reviewCanvas video, #reviewCanvas audio');
}

function updateReviewTimecode() {
  const media = reviewMedia();
  const seconds = Number(media?.currentTime || 0);
  const frames = Math.floor(seconds * 25) % 25;
  const totalSeconds = Math.floor(seconds);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor(totalSeconds / 60) % 60;
  const secs = totalSeconds % 60;
  $('#reviewTimecode').textContent = [hours, minutes, secs, frames].map(value => String(value).padStart(2, '0')).join(':');
}

function renderAnnotations() {
  const layer = $('#annotationLayer');
  const strokes = [
    ...state.currentAsset.comments
      .filter(item => item.version_id == null || Number(item.version_id) === Number(state.currentVersionId))
      .flatMap(item => item.drawing || []),
    ...state.annotationStrokes,
  ];
  layer.innerHTML = strokes.map(stroke => {
    const points = (stroke.points || []).map(point => `${Number(point.x) * 1000},${Number(point.y) * 1000}`).join(' ');
    return points ? `<polyline points="${esc(points)}" fill="none" stroke="${esc(stroke.color || '#ffcc57')}" stroke-width="5" vector-effect="non-scaling-stroke"/>` : '';
  }).join('');
}

function renderReviewComments() {
  const comments = state.currentAsset.comments.filter(item => item.version_id == null || Number(item.version_id) === Number(state.currentVersionId));
  const canComment = ['owner', 'manager', 'editor', 'reviewer'].includes(state.currentAsset.access_role);
  $('#reviewComments').innerHTML = comments.length
    ? comments.map(item => `<article class="review-comment ${item.resolved ? 'resolved' : ''}" data-comment-time="${item.time_start ?? ''}"><div class="review-comment-head"><b>${esc(item.display_name || item.guest_name || '成员')}</b><span>${item.time_start != null ? fmtDuration(item.time_start) : '整条素材'}</span></div><p>${esc(item.body)}</p><div class="review-comment-foot"><span>${esc(item.visibility === 'external' ? '外部可见' : '团队内部')} · ${esc(item.created_at)}</span>${canComment ? `<button data-resolve-comment="${item.id}" data-resolved="${item.resolved ? '1' : '0'}">${item.resolved ? '重新打开' : '标记解决'}</button>` : ''}</div></article>`).join('')
    : '<div class="empty-state compact"><b>这个版本还没有审阅意见</b><p>播放视频或在画面上标注后发布第一条意见。</p></div>';
  renderAnnotations();
}

function updateReviewActions(version) {
  const canEdit = ['owner', 'manager', 'editor'].includes(state.currentAsset?.access_role);
  const proxyButton = $('#requestProxy');
  const proxyEligible = canEdit && ['video', 'audio'].includes(version.kind);
  proxyButton.hidden = !proxyEligible;
  proxyButton.disabled = version.proxy_status === 'processing';
  proxyButton.textContent = version.proxy_status === 'processing'
    ? '代理生成中'
    : version.proxy_status === 'ready'
      ? '重新生成代理'
      : version.proxy_status === 'error' ? '重试生成代理' : '生成代理';
  const lookButton = $('#applyLook');
  lookButton.hidden = !canEdit || !['image', 'video'].includes(version.kind);
  lookButton.disabled = version.look_status === 'processing';
  lookButton.textContent = version.look_status === 'processing' ? 'LUT 生成中' : 'LUT 预览';
}

async function loadReviewVersion(versionId, seekTime = null) {
  const version = state.currentAsset.versions.find(item => Number(item.id) === Number(versionId));
  if (!version) return;
  state.currentVersionId = Number(version.id);
  updateReviewActions(version);
  if (!version.look_path) state.lookPreviewEnabled = false;
  state.annotationStrokes = [];
  $('#reviewVersionSelect').value = String(version.id);
  $('#reviewCanvas').innerHTML = '<div class="viewer-loading">正在安全读取素材…</div><svg id="annotationLayer" viewBox="0 0 1000 1000" preserveAspectRatio="none"></svg><span class="review-watermark" id="reviewWatermark" hidden></span>';
  try {
    const variant = state.lookPreviewEnabled && version.look_path ? 'look' : 'best';
    const ticket = await api(`/api/asset-versions/${version.id}/ticket?variant=${variant}`, { method: 'POST' });
    if (/\.(obj|ply)$/i.test(version.file_name || '') && Number(version.size || 0) <= 80 * 1024 * 1024) {
      $('#reviewCanvas').insertAdjacentHTML('afterbegin', '<canvas class="model-review-canvas" aria-label="3D 模型预览"></canvas><span class="model-review-help">拖动旋转 · 滚轮缩放 · 本地 WebGL 渲染</span>');
      const { mountModelViewer } = await import('/assets/model-viewer.js?v=2');
      await mountModelViewer($('.model-review-canvas', $('#reviewCanvas')), ticket.url, version.file_name);
    } else if (version.kind === 'image') {
      $('#reviewCanvas').insertAdjacentHTML('afterbegin', `<img src="${esc(ticket.url)}" alt="${esc(version.file_name)}">`);
    } else if (version.kind === 'video') {
      $('#reviewCanvas').insertAdjacentHTML('afterbegin', `<video controls preload="metadata" src="${esc(ticket.url)}"></video>`);
    } else if (version.kind === 'audio') {
      $('#reviewCanvas').insertAdjacentHTML('afterbegin', `<audio controls src="${esc(ticket.url)}"></audio>`);
    } else if (version.mime_type === 'application/pdf') {
      $('#reviewCanvas').insertAdjacentHTML('afterbegin', `<iframe src="${esc(ticket.url)}" title="${esc(version.file_name)}"></iframe>`);
    } else {
      $('#reviewCanvas').insertAdjacentHTML('afterbegin', `<div class="generic-preview">${icon('document')}<p>${esc(version.file_name)}</p></div>`);
    }
    $('.viewer-loading', $('#reviewCanvas'))?.remove();
    const media = reviewMedia();
    if (media) {
      media.addEventListener('timeupdate', updateReviewTimecode);
      if (seekTime != null && Number.isFinite(Number(seekTime))) {
        media.addEventListener('loadedmetadata', () => { media.currentTime = Number(seekTime); }, { once: true });
      }
    }
    const artifacts = [];
    if (version.filmstrip_path) artifacts.push('filmstrip');
    if (version.waveform_path) artifacts.push('waveform');
    let artifactUrl = '';
    for (const variant of artifacts) {
      try {
        const artifact = await api(`/api/asset-versions/${version.id}/ticket?variant=${variant}`, { method: 'POST' });
        artifactUrl = artifact.url;
        break;
      } catch {}
    }
    $('#reviewFilmstrip').innerHTML = artifactUrl ? `<img src="${esc(artifactUrl)}" alt="媒体概览">` : '';
  } catch (error) {
    $('#reviewCanvas').innerHTML = `<div class="empty-state"><b>素材暂时无法预览</b><p>${esc(error.message)}</p></div><svg id="annotationLayer" viewBox="0 0 1000 1000" preserveAspectRatio="none"></svg>`;
  }
  $('#toggleLookPreview').hidden = !version.look_path;
  $('#toggleLookPreview').textContent = state.lookPreviewEnabled ? '查看原片' : `查看 ${version.look_name || 'LUT'}`;
  renderReviewComments();
}

async function openAssetReview(assetId, seekTime = null) {
  $('#assetReviewModal').classList.add('open');
  try {
    const asset = await api(`/api/assets/${assetId}`);
    if (!state.projectDetails || Number(state.projectDetails.project.id) !== Number(asset.project_id)) {
      state.projectDetails = await api(`/api/projects/${asset.project_id}`);
    }
    state.currentAsset = asset;
    $('#reviewProjectName').textContent = asset.project_name;
    $('#reviewAssetTitle').textContent = asset.title;
    $('#reviewAssetStatus').textContent = assetStatusName(asset.status);
    $('#reviewVersionSelect').innerHTML = asset.versions.map(version => `<option value="${version.id}">V${version.version_number} · ${esc(version.label || version.file_name)}</option>`).join('');
    const detailForm = $('#assetDetailForm');
    detailForm.elements.title.value = asset.title;
    detailForm.elements.description.value = asset.description || '';
    detailForm.elements.rating.value = String(asset.rating || 0);
    detailForm.elements.status.innerHTML = asset.statuses.map(item => `<option value="${esc(item.key)}">${esc(item.name)}</option>`).join('');
    detailForm.elements.status.value = asset.status;
    detailForm.elements.assignee_id.innerHTML = '<option value="">未分配</option>' + asset.members.map(item => `<option value="${item.id}">${esc(item.display_name)} · ${esc(projectRoleLabel(item.role))}</option>`).join('');
    detailForm.elements.assignee_id.value = asset.assignee_id || '';
    fillFolderSelect('#assetDetailForm [name=folder_id]', state.projectDetails?.folders || [], asset.folder_id);
    $('#versionForm').dataset.assetId = asset.id;
    const canEdit = ['owner', 'manager', 'editor'].includes(asset.access_role);
    $('#openVersionModal').hidden = !canEdit;
    $('#assetDetailForm button').hidden = !canEdit;
    $$('#assetDetailForm input, #assetDetailForm textarea, #assetDetailForm select').forEach(element => { element.disabled = !canEdit; });
    const canComment = ['owner', 'manager', 'editor', 'reviewer'].includes(asset.access_role);
    $('#reviewCommentForm').hidden = !canComment;
    $('#toggleDraw').hidden = !canComment;
    $('#clearDrawing').hidden = !canComment;
    state.drawingActive = false;
    $('#toggleDraw').classList.remove('active');
    await loadReviewVersion(asset.cover_version_id || asset.versions[0]?.id, seekTime);
    const qc = await api(`/api/assets/${asset.id}/qc`);
    $('#assetQc').innerHTML = qc.checks.map(item => `<div class="qc-check ${esc(item.level)}"><i></i><div><b>${esc(item.name)}</b><span>${esc(item.detail)}</span></div></div>`).join('');
  } catch (error) {
    toast(error.message, true);
    $('#assetReviewModal').classList.remove('open');
  }
}

async function openAssetPicker(fileId = null) {
  const projects = state.projects.length ? state.projects : await loadProjects(true);
  if (!projects.length) {
    toast('请先创建项目', true);
    return $('#projectModal').classList.add('open');
  }
  $('#assetTargetProject').value = state.currentProjectId && projects.some(item => item.id === state.currentProjectId)
    ? String(state.currentProjectId)
    : String(projects[0].id);
  await refreshAssetTargetFolders();
  $('#addAssetModal').classList.add('open');
  if (fileId) {
    const file = await api(`/api/files/${fileId}`);
    renderAssetPickerResults([file]);
  } else {
    await loadAssetPickerResults('');
  }
}

async function refreshAssetTargetFolders() {
  const projectId = Number($('#assetTargetProject').value);
  if (!projectId) return;
  const details = projectId === state.currentProjectId && state.projectDetails
    ? state.projectDetails
    : await api(`/api/projects/${projectId}`);
  fillFolderSelect('#assetTargetFolder', details.folders);
}

async function loadAssetPickerResults(query) {
  try {
    const data = query
      ? await api(`/api/search?q=${encodeURIComponent(query)}&limit=80&semantic=false`)
      : await api('/api/files?limit=80&sort=newest');
    renderAssetPickerResults(data.results || data.items || []);
  } catch (error) { toast(error.message, true); }
}

function renderAssetPickerResults(items) {
  $('#assetPickerResults').innerHTML = items.length
    ? items.map(item => `<article class="asset-picker-row">${assetCardPreview(item)}<div><b>${esc(item.name)}</b><small>${esc(item.relative_path || item.path || '')} · ${fmtBytes(item.size)}</small></div><button class="primary" data-add-picked-file="${item.id}">添加</button></article>`).join('')
    : '<div class="empty-state compact"><b>没有匹配文件</b></div>';
  loadResultThumbnails($('#assetPickerResults'));
}

async function loadVersionPickerResults(query = '') {
  try {
    const data = query
      ? await api(`/api/search?q=${encodeURIComponent(query)}&limit=60&semantic=false`)
      : await api('/api/files?limit=60&sort=newest');
    const items = data.results || data.items || [];
    $('#versionPickerResults').innerHTML = items.length
      ? items.map(item => `<article class="asset-picker-row">${assetCardPreview(item)}<div><b>${esc(item.name)}</b><small>${esc(item.relative_path || item.path || '')} · ${fmtBytes(item.size)}</small></div><button class="secondary" type="button" data-version-file="${item.id}" data-version-name="${esc(item.name)}">选择</button></article>`).join('')
      : '<div class="empty-state compact"><b>没有匹配文件</b></div>';
    loadResultThumbnails($('#versionPickerResults'));
  } catch (error) { toast(error.message, true); }
}

async function loadLookPickerResults(query = '') {
  try {
    const data = await api('/api/files?limit=200&sort=newest');
    const normalized = query.trim().toLocaleLowerCase('zh-CN');
    const items = (data.items || []).filter(item => (
      String(item.extension || '').toLowerCase() === '.cube'
      && (!normalized || `${item.name} ${item.relative_path || ''}`.toLocaleLowerCase('zh-CN').includes(normalized))
    ));
    $('#lookPickerResults').innerHTML = items.length
      ? items.map(item => `<article class="asset-picker-row"><span class="lut-file-icon">LUT</span><div><b>${esc(item.name)}</b><small>${esc(item.relative_path || item.path || '')} · ${fmtBytes(item.size)}</small></div><button class="secondary" type="button" data-look-file="${item.id}" data-look-name="${esc(item.name)}">选择</button></article>`).join('')
      : '<div class="empty-state compact"><b>没有找到 .cube LUT</b><p>可先上传 LUT 文件，再回到这里选择。</p></div>';
  } catch (error) { toast(error.message, true); }
}

async function openShareCreator(assetId = '') {
  if (!state.currentProjectId || !state.projectDetails) return;
  const select = $('#shareForm [name=asset_id]');
  select.innerHTML = '<option value="">整个项目</option>' + state.projectAssets.map(item => `<option value="${item.id}">${esc(item.title)}</option>`).join('');
  select.value = assetId ? String(assetId) : '';
  $('#shareCreated').hidden = true;
  $('#shareModal').classList.add('open');
}

async function openProjectInbox() {
  if (!state.currentProjectId) return;
  try {
    const data = await api(`/api/projects/${state.currentProjectId}/inbox`);
    $('#projectInboxPath').textContent = data.relative_path;
    $('#projectInboxStats').innerHTML = [
      `<div><span>已发现文件</span><b>${fmtCount(data.discovered_files)}</b></div>`,
      `<div><span>已加入项目</span><b>${fmtCount(data.collected_files)}</b></div>`,
      `<div><span>当前状态</span><b>${data.active_task ? '正在处理' : '等待投递'}</b></div>`,
    ].join('');
    $('#collectProjectInbox').disabled = Boolean(data.active_task);
    $('#collectProjectInbox').textContent = data.active_task ? '入库任务正在运行' : '扫描并收集入库文件';
    $('#projectInboxModal').classList.add('open');
  } catch (error) { toast(error.message, true); }
}

async function loadNotifications() {
  if (!state.user?.id) return;
  try {
    const data = await api('/api/notifications');
    $('#notificationCount').hidden = !data.unread;
    $('#notificationCount').textContent = data.unread;
    $('#notificationList').innerHTML = data.items.length
      ? data.items.map(item => `<article class="notification-row ${item.read_at ? '' : 'unread'}" data-notification="${item.id}" data-notification-target="${esc(item.target_type)}" data-notification-target-id="${esc(item.target_id)}"><b>${esc(item.title)}</b><span>${esc(item.body)}</span><small>${esc(item.created_at)}</small></article>`).join('')
      : '<div class="empty-state compact"><b>暂无协作通知</b></div>';
  } catch {}
}

function publicStage(version, assetId) {
  if (!version?.media_url) return '<div class="empty-state"><b>媒体暂时离线</b></div>';
  if (version.kind === 'image') return `<img src="${esc(version.media_url)}" alt="">`;
  if (version.kind === 'video') return `<video controls preload="metadata" src="${esc(version.media_url)}"></video>`;
  if (version.kind === 'audio') return `<audio controls src="${esc(version.media_url)}"></audio>`;
  if (version.mime_type === 'application/pdf') return `<iframe src="${esc(version.media_url)}" title="${esc(version.file_name)}"></iframe>`;
  return `<a class="secondary" href="${esc(version.media_url)}" target="_blank" rel="noopener">打开 ${esc(version.file_name)}</a>`;
}

function renderPublicShare(data) {
  $('#publicBrand').textContent = data.share.brand_name || 'NAS AI Space';
  $('#publicShareTitle').textContent = data.share.name;
  $('#publicShareDescription').textContent = data.share.project_description || data.share.project_name;
  $('#publicAssets').innerHTML = data.assets.map(asset => {
    const selectedId = state.publicVersionIds[asset.id];
    const current = asset.versions.find(version => Number(version.id) === Number(selectedId)) || asset.versions[0];
    if (current) state.publicVersionIds[asset.id] = Number(current.id);
    const comments = asset.comments || [];
    return `<article class="public-asset" data-public-asset="${asset.id}">
      <div class="public-asset-stage">${publicStage(current, asset.id)}${data.share.watermark_text ? `<span class="review-watermark">${esc(data.share.watermark_text)}</span>` : ''}</div>
      <div class="public-asset-info"><div><h2>${esc(asset.title)}</h2><p>${esc(asset.description || current?.caption || '')}</p><div class="public-version-row">${asset.versions.map(version => `<button data-public-version="${version.id}" data-public-asset-id="${asset.id}">V${version.version_number} · ${esc(version.label || version.file_name)}</button>`).join('')}${current?.download_url ? `<a href="${esc(current.download_url)}">下载原文件</a>` : ''}</div></div><div><div class="public-comment-list">${comments.length ? comments.map(comment => `<article class="public-comment"><b>${esc(comment.display_name || comment.guest_name || '访客')} · ${comment.time_start != null ? fmtDuration(comment.time_start) : '整条素材'}</b><p>${esc(comment.body)}</p></article>`).join('') : '<div class="empty-state compact"><b>还没有外部审阅意见</b></div>'}</div>${data.share.can_comment ? `<form class="public-comment-form" data-public-comment-form="${asset.id}"><input name="guest_name" required maxlength="80" placeholder="你的名字"><textarea name="body" rows="3" required maxlength="4000" placeholder="输入审阅意见"></textarea><input name="time_start" type="number" min="0" step="0.01" placeholder="视频时间点（秒，可选）"><button class="primary">发布意见</button></form>` : ''}</div></div>
    </article>`;
  }).join('');
  state.publicShareData = data;
}

async function loadPublicShare(accessCode = '') {
  const token = decodeURIComponent(location.pathname.split('/').filter(Boolean)[1] || '');
  const response = await fetch(`/api/public/shares/${encodeURIComponent(token)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ access_code: accessCode }),
  });
  if (response.status === 401) {
    $('#publicAccessForm').hidden = false;
    if (accessCode) toast('访问码错误', true);
    return;
  }
  if (!response.ok) {
    let detail = '分享不存在或已过期';
    try { detail = (await response.json()).detail || detail; } catch {}
    $('#publicAssets').innerHTML = `<div class="empty-state"><b>${esc(detail)}</b></div>`;
    return;
  }
  state.publicAccessCode = accessCode;
  $('#publicAccessForm').hidden = true;
  renderPublicShare(await response.json());
}

function bootPublicShare() {
  $('.app-shell').hidden = true;
  $('#publicShareShell').hidden = false;
  loadPublicShare('');
}

document.addEventListener('click', async event => {
  if (event.target.closest('#systemNavToggle')) {
    const nav = $('#systemNav');
    nav.hidden = !nav.hidden;
    $('#systemNavToggle').classList.toggle('expanded', !nav.hidden);
    return;
  }
  if (event.target.closest('.card-select, .duplicate-select')) return;
  const nav = event.target.closest('[data-view]');
  if (nav) return showView(nav.dataset.view);
  const go = event.target.closest('[data-go]');
  if (go) return showView(go.dataset.go);
  if (event.target.closest('#menuButton')) return $('.sidebar').classList.toggle('open');
  if (event.target.closest('#tokenButton')) {
    $('#tokenForm [name=token]').value = state.user?.auth_type === 'api_token' ? state.token : '';
    applyRole();
    return $('#tokenModal').classList.add('open');
  }
  if (event.target.closest('#uploadButton')) return $('#uploadModal').classList.add('open');
  if (event.target.closest('#showFavorites')) {
    $('#libraryFavorite').checked = true;
    return loadLibraryFiles(true);
  }
  if (event.target.closest('#clearSearchFilters')) {
    ['#searchLibrary', '#searchDateFrom', '#searchDateTo', '#searchPerson', '#searchPlace', '#searchEvent', '#searchTag'].forEach(selector => { $(selector).value = ''; });
    $('#searchFavorite').checked = false;
    if (state.searchQuery) runSearch(state.searchQuery);
    return;
  }
  if (event.target.closest('#saveSmartAlbum')) {
    if (!state.searchQuery) return toast('请先执行一次搜索', true);
    const name = prompt('智能相册名称', state.searchQuery.slice(0, 30));
    if (!name?.trim()) return;
    try {
      await api('/api/smart-albums', {
        method: 'POST',
        body: JSON.stringify({
          name: name.trim(),
          query: state.searchQuery,
          kind: state.kind,
          filters: currentSmartAlbumFilters(),
        }),
      });
      toast('智能相册已保存');
      loadSmartAlbums();
    } catch (error) { toast(error.message, true); }
    return;
  }
  const deleteSmartAlbum = event.target.closest('[data-delete-smart-album]');
  if (deleteSmartAlbum) {
    event.stopPropagation();
    if (!confirm('删除这个智能相册？不会删除任何原文件。')) return;
    try {
      await api(`/api/smart-albums/${deleteSmartAlbum.dataset.deleteSmartAlbum}`, { method: 'DELETE' });
      toast('智能相册已删除');
      loadSmartAlbums();
    } catch (error) { toast(error.message, true); }
    return;
  }
  const smartAlbum = event.target.closest('[data-smart-album]');
  if (smartAlbum) return openSmartAlbum(smartAlbum.dataset.smartAlbum);
  if (event.target.closest('[data-smart-album-back]')) {
    $('#smartAlbumDetail').hidden = true;
    $('#smartAlbumList').hidden = false;
    return;
  }
  const useSmartAlbum = event.target.closest('[data-use-smart-album]');
  if (useSmartAlbum) {
    const album = state.smartAlbums.find(item => item.id === Number(useSmartAlbum.dataset.useSmartAlbum));
    if (!album) return;
    state.kind = album.kind || '';
    $$('[data-kind]').forEach(element => element.classList.toggle('active', element.dataset.kind === state.kind));
    const filters = album.filters || {};
    $('#searchLibrary').value = filters.library_id || '';
    $('#searchDateFrom').value = filters.date_from || '';
    $('#searchDateTo').value = filters.date_to || '';
    $('#searchPerson').value = filters.person_id || '';
    $('#searchPlace').value = filters.place_id || '';
    $('#searchEvent').value = filters.event_id || '';
    $('#searchTag').value = filters.tag || '';
    $('#searchFavorite').checked = Boolean(filters.favorite);
    return runSearch(album.query);
  }
  if (event.target.closest('#newConversation')) {
    resetConversation();
    return;
  }
  if (event.target.closest('#deleteConversation') && state.currentConversationId) {
    if (!confirm('删除当前对话记录？')) return;
    try {
      await api(`/api/conversations/${state.currentConversationId}`, { method: 'DELETE' });
      resetConversation();
      await loadConversations();
      toast('对话已删除');
    } catch (error) { toast(error.message, true); }
    return;
  }
  if (event.target.closest('#repairIndex') || event.target.closest('#repairFromOperations')) {
    try {
      const result = await api('/api/index/repair', { method: 'POST', body: JSON.stringify({ limit: 50 }) });
      toast(result.existing ? '已有自动修复任务在运行' : '已加入 50 个部分索引修复任务');
      showView('tasks');
    } catch (error) { toast(error.message, true); }
    return;
  }
  if (event.target.closest('#mergePeople')) {
    const ids = $$('[data-select-person]:checked').map(input => Number(input.dataset.selectPerson));
    if (ids.length < 2) return toast('至少选择两个人物', true);
    try {
      await api('/api/people/merge', {
        method: 'POST',
        body: JSON.stringify({ target_id: ids[0], source_ids: ids.slice(1) }),
      });
      toast('人物已合并');
      loadPeople(true);
      loadSearchFacets();
    } catch (error) { toast(error.message, true); }
    return;
  }
  if (event.target.closest('#hidePeople')) {
    const ids = $$('[data-select-person]:checked').map(input => Number(input.dataset.selectPerson));
    if (!ids.length || !confirm(`隐藏选中的 ${ids.length} 个人物？`)) return;
    try {
      await Promise.all(ids.map(id => api(`/api/people/${id}`, { method: 'DELETE' })));
      toast('所选人物已隐藏');
      loadPeople(true);
      loadSearchFacets();
    } catch (error) { toast(error.message, true); }
    return;
  }
  if (event.target.closest('#mergeEvents')) {
    const ids = $$('[data-select-event]:checked').map(input => Number(input.dataset.selectEvent));
    if (ids.length < 2) return toast('至少选择两个事件', true);
    try {
      await api('/api/events/merge', {
        method: 'POST',
        body: JSON.stringify({ target_id: ids[0], source_ids: ids.slice(1) }),
      });
      toast('事件已合并');
      loadEvents(true);
      loadSearchFacets();
    } catch (error) { toast(error.message, true); }
    return;
  }
  if (event.target.closest('#hideEvents')) {
    const ids = $$('[data-select-event]:checked').map(input => Number(input.dataset.selectEvent));
    if (!ids.length || !confirm(`隐藏选中的 ${ids.length} 个事件？`)) return;
    try {
      await Promise.all(ids.map(id => api(`/api/events/${id}`, { method: 'DELETE' })));
      toast('所选事件已隐藏');
      loadEvents(true);
      loadSearchFacets();
    } catch (error) { toast(error.message, true); }
    return;
  }
  if (event.target.closest('#personSetCover')) {
    const selected = $('[data-curation-item]:checked', $('#personDetail'));
    if (!selected) return toast('请选择一张照片', true);
    try {
      await api(`/api/people/${$('#personDetail').dataset.personId}/cover`, {
        method: 'PUT',
        body: JSON.stringify({ item_id: Number(selected.dataset.curationItem) }),
      });
      toast('人物封面已更新');
    } catch (error) { toast(error.message, true); }
    return;
  }
  if (event.target.closest('#personSplit')) {
    const ids = $$('[data-curation-item]:checked', $('#personDetail')).map(input => Number(input.dataset.curationItem));
    if (!ids.length) return toast('请选择要拆分的人脸', true);
    const name = prompt('新人物名称', '待命名人物');
    if (name == null) return;
    try {
      await api(`/api/people/${$('#personDetail').dataset.personId}/split`, {
        method: 'POST',
        body: JSON.stringify({ face_ids: ids, name: name.trim() }),
      });
      toast('已拆分为新人物');
      loadPeople(true);
      loadSearchFacets();
    } catch (error) { toast(error.message, true); }
    return;
  }
  if (event.target.closest('#eventSetCover')) {
    const selected = $('[data-curation-item]:checked', $('#eventDetail'));
    if (!selected) return toast('请选择一张照片', true);
    try {
      await api(`/api/events/${$('#eventDetail').dataset.eventId}/cover`, {
        method: 'PUT',
        body: JSON.stringify({ item_id: Number(selected.dataset.curationItem) }),
      });
      toast('事件封面已更新');
    } catch (error) { toast(error.message, true); }
    return;
  }
  if (event.target.closest('#eventSplit')) {
    const ids = $$('[data-curation-item]:checked', $('#eventDetail')).map(input => Number(input.dataset.curationItem));
    if (!ids.length) return toast('请选择要拆分的文件', true);
    const name = prompt('新事件名称', '新事件');
    if (!name?.trim()) return;
    try {
      await api(`/api/events/${$('#eventDetail').dataset.eventId}/split`, {
        method: 'POST',
        body: JSON.stringify({ file_ids: ids, name: name.trim() }),
      });
      toast('已拆分为新事件');
      loadEvents(true);
      loadSearchFacets();
    } catch (error) { toast(error.message, true); }
    return;
  }
  if (event.target.closest('#bulkRecycleDuplicates')) {
    const ids = $$('[data-duplicate-file]:checked').map(input => Number(input.dataset.duplicateFile));
    if (!ids.length) return toast('没有选中可清理副本', true);
    if (!confirm(`将选中的 ${ids.length} 个重复副本移入可恢复回收站？`)) return;
    try {
      await api('/api/recycle', { method: 'POST', body: JSON.stringify({ file_ids: ids }) });
      toast(`${ids.length} 个重复副本已移入回收站`);
      loadOrganizer();
    } catch (error) { toast(error.message, true); }
    return;
  }
  if (event.target.closest('#analyzePeople')) {
    try {
      await api('/api/people/analyze', { method: 'POST' });
      toast('人物识别任务已加入队列');
      showView('tasks');
    } catch (error) { toast(error.message, true); }
    return;
  }
  if (event.target.closest('#analyzePlaces')) {
    try {
      await api('/api/places/analyze', { method: 'POST' });
      toast('地点相册分析已加入队列');
      showView('tasks');
    } catch (error) { toast(error.message, true); }
    return;
  }
  if (event.target.closest('#analyzeEvents')) {
    try {
      await api('/api/events/analyze', { method: 'POST' });
      toast('事件相册分析已加入队列');
      showView('tasks');
    } catch (error) { toast(error.message, true); }
    return;
  }
  if (event.target.closest('[data-person-back]')) return loadPeople(true);
  const person = event.target.closest('[data-person]');
  if (person) return openPerson(person.dataset.person);
  const rename = event.target.closest('[data-person-rename]');
  if (rename) {
    const name = prompt('人物名称', rename.dataset.personName);
    if (name?.trim()) {
      try {
        await api(`/api/people/${rename.dataset.personRename}`, { method: 'PUT', body: JSON.stringify({ name: name.trim() }) });
        toast('人物名称已保存');
        openPerson(rename.dataset.personRename);
      } catch (error) { toast(error.message, true); }
    }
    return;
  }
  if (event.target.closest('[data-place-back]')) return loadPlaces();
  const place = event.target.closest('[data-place]');
  if (place) return openPlace(place.dataset.place);
  const renamePlace = event.target.closest('[data-place-rename]');
  if (renamePlace) {
    const name = prompt('地点名称', renamePlace.dataset.albumName);
    if (name?.trim()) {
      try {
        await api(`/api/places/${renamePlace.dataset.placeRename}`, { method: 'PUT', body: JSON.stringify({ name: name.trim() }) });
        toast('地点名称已保存');
        openPlace(renamePlace.dataset.placeRename);
      } catch (error) { toast(error.message, true); }
    }
    return;
  }
  if (event.target.closest('[data-event-back]')) return loadEvents(true);
  const albumEvent = event.target.closest('[data-event]');
  if (albumEvent) return openEvent(albumEvent.dataset.event);
  const renameEvent = event.target.closest('[data-event-rename]');
  if (renameEvent) {
    const name = prompt('事件名称', renameEvent.dataset.albumName);
    if (name?.trim()) {
      try {
        await api(`/api/events/${renameEvent.dataset.eventRename}`, { method: 'PUT', body: JSON.stringify({ name: name.trim() }) });
        toast('事件名称已保存');
        openEvent(renameEvent.dataset.eventRename);
      } catch (error) { toast(error.message, true); }
    }
    return;
  }
  if (event.target.closest('#addUser')) return openUserModal();
  const editUser = event.target.closest('[data-edit-user]');
  if (editUser) return openUserModal(state.users.find(user => user.id === Number(editUser.dataset.editUser)));
  if (event.target.closest('#createBackup')) {
    try {
      const backup = await api('/api/operations/backups', { method: 'POST' });
      toast(`备份已完成：${backup.name}`);
      loadOperations();
    } catch (error) { toast(error.message, true); }
    return;
  }
  if (event.target.closest('#addLibrary')) return $('#libraryModal').classList.add('open');
  if (event.target.matches('[data-close]') || event.target.classList.contains('modal')) {
    const modal = event.target.closest('.modal');
    if (modal?.id === 'tokenModal' && state.bootstrapRequired) return;
    if (modal?.id === 'fileModal') closeFileViewer();
    else modal?.classList.remove('open');
    return;
  }
  const quickQuery = event.target.closest('[data-query]');
  if (quickQuery) return runSearch(quickQuery.dataset.query);
  const filter = event.target.closest('[data-kind]');
  if (filter) {
    state.kind = filter.dataset.kind;
    $$('[data-kind]').forEach(element => element.classList.toggle('active', element === filter));
    const query = $('#mainSearchInput').value;
    if (query) runSearch(query);
    return;
  }
  const discover = event.target.closest('[data-discover]');
  if (discover) {
    try {
      await api(`/api/libraries/${discover.dataset.discover}/discover`, { method: 'POST' });
      toast('快速扫描任务已加入队列');
      showView('tasks');
    } catch (error) { toast(error.message, true); }
    return;
  }
  const cancel = event.target.closest('[data-cancel]');
  if (cancel) {
    try {
      await api(`/api/tasks/${cancel.dataset.cancel}/cancel`, { method: 'POST' });
      toast('已请求取消');
      loadTasks();
    } catch (error) { toast(error.message, true); }
    return;
  }
  const retry = event.target.closest('[data-retry]');
  if (retry) {
    try {
      await api(`/api/tasks/${retry.dataset.retry}/retry`, { method: 'POST' });
      toast('任务已重新加入队列');
      loadTasks();
    } catch (error) { toast(error.message, true); }
    return;
  }
  const analysis = event.target.closest('[data-analyze]');
  if (analysis) {
    try {
      await api(`/api/organizer/analyze/${analysis.dataset.analyze}`, { method: 'POST' });
      toast(analysis.dataset.analyze === 'similar' ? '相似照片分析已加入队列' : '重复文件分析已加入队列');
      showView('tasks');
    } catch (error) { toast(error.message, true); }
    return;
  }
  const organizerMode = event.target.closest('[data-organizer-mode]');
  if (organizerMode) {
    state.organizerMode = organizerMode.dataset.organizerMode;
    $$('[data-organizer-mode]').forEach(element => element.classList.toggle('active', element === organizerMode));
    loadOrganizer();
    return;
  }
  const remove = event.target.closest('[data-delete-library]');
  if (remove && confirm('删除媒体库索引？不会删除原文件。')) {
    try {
      await api(`/api/libraries/${remove.dataset.deleteLibrary}`, { method: 'DELETE' });
      toast('媒体库已删除');
      loadLibraries();
      loadDashboard();
    } catch (error) { toast(error.message, true); }
    return;
  }
  const suggestion = event.target.closest('.suggestions button');
  if (suggestion) {
    showView('ask');
    ask(suggestion.textContent);
    return;
  }
  const seek = event.target.closest('[data-seek]');
  if (seek) {
    const media = $('#filePreview video, #filePreview audio');
    if (media) {
      media.currentTime = Number(seek.dataset.seek);
      media.play().catch(() => {});
    }
    return;
  }
  if (event.target.closest('#createVectorSnapshot')) {
    try {
      const snapshot = await api('/api/operations/vector-snapshots', { method: 'POST' });
      toast(`向量快照已创建：${snapshot.name}`);
      loadOperations();
    } catch (error) { toast(error.message, true); }
    return;
  }
  if (event.target.closest('#upgradeCaptions')) {
    try {
      const result = await api('/api/vision/upgrade', {
        method: 'POST',
        body: JSON.stringify({ limit: 50 }),
      });
      toast(result.existing ? '已有图片描述升级任务在运行' : '已加入 50 张图片描述升级任务');
      showView('tasks');
    } catch (error) { toast(error.message, true); }
    return;
  }
  const restoreSnapshot = event.target.closest('[data-restore-snapshot]');
  if (restoreSnapshot) {
    const collection = $('#snapshotList').dataset.collection;
    const confirmation = prompt(`恢复会覆盖当前向量集合。请输入 ${collection} 确认：`);
    if (confirmation !== collection) return;
    try {
      await api(`/api/operations/vector-snapshots/${encodeURIComponent(restoreSnapshot.dataset.restoreSnapshot)}/restore`, {
        method: 'POST',
        body: JSON.stringify({ confirm: confirmation }),
      });
      toast('向量快照已恢复');
      loadOperations();
    } catch (error) { toast(error.message, true); }
    return;
  }
  const deleteSnapshot = event.target.closest('[data-delete-snapshot]');
  if (deleteSnapshot && confirm('删除这个本地向量快照？')) {
    try {
      await api(`/api/operations/vector-snapshots/${encodeURIComponent(deleteSnapshot.dataset.deleteSnapshot)}`, { method: 'DELETE' });
      toast('向量快照已删除');
      loadOperations();
    } catch (error) { toast(error.message, true); }
    return;
  }
  if (event.target.closest('#refreshRecycle')) return loadRecycle();
  const recycleFile = event.target.closest('[data-recycle-file]');
  if (recycleFile) {
    if (!confirm('将这个重复副本移入可恢复回收站？系统会再次确认仍有保留副本。')) return;
    try {
      await api('/api/recycle', {
        method: 'POST',
        body: JSON.stringify({ file_ids: [Number(recycleFile.dataset.recycleFile)] }),
      });
      toast('重复副本已安全移入回收站');
      await loadOrganizer();
    } catch (error) { toast(error.message, true); }
    return;
  }
  const restoreTrash = event.target.closest('[data-restore-trash]');
  if (restoreTrash) {
    try {
      await api(`/api/recycle/${restoreTrash.dataset.restoreTrash}/restore`, { method: 'POST' });
      toast('文件已恢复，正在重新索引');
      loadRecycle();
    } catch (error) { toast(error.message, true); }
    return;
  }
  const purgeTrash = event.target.closest('[data-purge-trash]');
  if (purgeTrash && confirm('永久清除后无法恢复，确定继续？')) {
    try {
      await api(`/api/recycle/${purgeTrash.dataset.purgeTrash}`, { method: 'DELETE' });
      toast('文件已永久清除');
      loadRecycle();
    } catch (error) { toast(error.message, true); }
    return;
  }
  if (event.target.closest('#reindexFile') && state.currentFile) {
    try {
      await api(`/api/files/${state.currentFile.id}/reindex`, { method: 'POST' });
      toast('该文件已加入重新索引队列');
      closeFileViewer();
      showView('tasks');
    } catch (error) { toast(error.message, true); }
    return;
  }
  const favoriteCard = event.target.closest('[data-favorite-card]');
  if (favoriteCard) {
    event.stopPropagation();
    try {
      const enabled = favoriteCard.dataset.enabled !== '1';
      await api(`/api/files/${favoriteCard.dataset.favoriteCard}/favorite?enabled=${enabled}`, { method: 'PUT' });
      favoriteCard.dataset.enabled = enabled ? '1' : '0';
      favoriteCard.classList.toggle('active', enabled);
      toast(enabled ? '已加入收藏' : '已取消收藏');
    } catch (error) { toast(error.message, true); }
    return;
  }
  const cardFeedback = event.target.closest('[data-card-feedback]');
  if (cardFeedback) {
    event.stopPropagation();
    try {
      await api(`/api/files/${cardFeedback.dataset.feedbackFile}/feedback`, {
        method: 'POST',
        body: JSON.stringify({
          query: state.currentSearchFeedbackQuery,
          verdict: cardFeedback.dataset.cardFeedback,
          note: '',
        }),
      });
      toast(cardFeedback.dataset.cardFeedback === 'relevant' ? '已记录：结果相关' : '已记录：结果不相关');
    } catch (error) { toast(error.message, true); }
    return;
  }
  const findSimilar = event.target.closest('[data-find-similar]');
  if (findSimilar) {
    event.stopPropagation();
    const card = findSimilar.closest('[data-file]');
    return runSimilarSearch(findSimilar.dataset.findSimilar, card?.querySelector('h3')?.textContent || '当前文件');
  }
  if (event.target.closest('#favoriteFile') && state.currentFile) {
    try {
      const enabled = !Boolean(state.currentFile.favorite);
      await api(`/api/files/${state.currentFile.id}/favorite?enabled=${enabled}`, { method: 'PUT' });
      state.currentFile.favorite = enabled;
      $('#favoriteFile').classList.toggle('active', enabled);
      $('#favoriteFile').textContent = enabled ? '★ 已收藏' : '☆ 收藏';
      toast(enabled ? '已加入收藏' : '已取消收藏');
    } catch (error) { toast(error.message, true); }
    return;
  }
  if (event.target.closest('#saveFileTags') && state.currentFile) {
    const tags = $('#fileTags').value.split(/[,，]/).map(value => value.trim()).filter(Boolean);
    try {
      const data = await api(`/api/files/${state.currentFile.id}/tags`, {
        method: 'PUT',
        body: JSON.stringify({ tags }),
      });
      state.currentFile.tags = data.tags;
      $('#fileTags').value = data.tags.join(', ');
      toast('标签已保存');
      loadSearchFacets();
    } catch (error) { toast(error.message, true); }
    return;
  }
  if (event.target.closest('#saveManualCaption') && state.currentFile) {
    try {
      await api(`/api/files/${state.currentFile.id}/caption`, {
        method: 'PUT',
        body: JSON.stringify({ caption: $('#manualCaption').value.trim() }),
      });
      toast('人工描述已保存，正在重建向量索引');
      closeFileViewer();
      showView('tasks');
    } catch (error) { toast(error.message, true); }
    return;
  }
  const fileFeedback = event.target.closest('[data-feedback]');
  if (fileFeedback && state.currentFile) {
    try {
      await api(`/api/files/${state.currentFile.id}/feedback`, {
        method: 'POST',
        body: JSON.stringify({
          query: state.currentSearchFeedbackQuery,
          verdict: fileFeedback.dataset.feedback,
          note: '',
        }),
      });
      toast('反馈已记录，会用于后续排序和质量改进');
    } catch (error) { toast(error.message, true); }
    return;
  }
  if (event.target.closest('#openOriginal') && state.currentFileUrl) {
    window.open(state.currentFileUrl, '_blank', 'noopener');
    return;
  }
  const file = event.target.closest('[data-file]');
  if (file) {
    openFileViewer(file.dataset.file, file.dataset.time === '' ? null : Number(file.dataset.time));
  }
});

document.addEventListener('click', async event => {
  if (event.target.closest('#createProject')) return $('#projectModal').classList.add('open');
  const project = event.target.closest('[data-project]');
  if (project) return openProject(project.dataset.project);
  if (event.target.closest('#backToProjects')) return showView('projects');
  const folder = event.target.closest('[data-project-folder]');
  if (folder) {
    state.projectFolderId = folder.dataset.projectFolder === '' ? null : Number(folder.dataset.projectFolder);
    $$('#projectFolderTree .folder-item, [data-project-folder=""]').forEach(item => item.classList.toggle(
      'active',
      (item.dataset.projectFolder === '' ? null : Number(item.dataset.projectFolder)) === state.projectFolderId,
    ));
    return loadProjectAssets();
  }
  if (event.target.closest('#createProjectFolder')) {
    fillFolderSelect('#folderForm [name=parent_id]', state.projectDetails?.folders || []);
    return $('#folderModal').classList.add('open');
  }
  if (event.target.closest('#projectInboxButton')) return openProjectInbox();
  if (event.target.closest('#collectProjectInbox')) {
    try {
      const result = await api(`/api/projects/${state.currentProjectId}/inbox/collect`, { method: 'POST' });
      toast(result.existing ? '当前项目已有入库任务' : '入库扫描、AI 索引和项目收集已开始');
      await Promise.all([openProjectInbox(), loadTasks(true)]);
    } catch (error) { toast(error.message, true); }
    return;
  }
  if (event.target.closest('#projectMembersButton')) {
    const users = state.users.length ? state.users : await api('/api/users');
    state.users = users;
    const ownerId = Number(state.projectDetails?.project.owner_id || 0);
    const candidates = users.filter(item => item.enabled && Number(item.id) !== ownerId);
    const memberForm = $('#projectMemberForm');
    memberForm.hidden = !candidates.length;
    $('#projectMemberHint').hidden = Boolean(candidates.length);
    memberForm.elements.user_id.innerHTML = candidates.map(item => `<option value="${item.id}">${esc(item.display_name)} · ${esc(item.username)}</option>`).join('');
    $('#projectMemberList').innerHTML = (state.projectDetails?.members || []).map(item => {
      const owner = Number(item.id) === ownerId;
      const role = owner ? 'owner' : item.role;
      return `<article class="project-member-row"><span>${esc(item.display_name.slice(0, 1))}</span><div><b>${esc(item.display_name)}</b><small>${esc(item.username)} · ${esc(projectRoleLabel(role))}</small></div>${owner ? '<span class="project-state">所有者</span>' : `<button class="danger" data-remove-project-member="${item.id}">移除</button>`}</article>`;
    }).join('');
    return $('#projectMemberModal').classList.add('open');
  }
  const removeMember = event.target.closest('[data-remove-project-member]');
  if (removeMember) {
    if (!confirm('从当前项目移除该成员？')) return;
    try {
      await api(`/api/projects/${state.currentProjectId}/members/${removeMember.dataset.removeProjectMember}`, { method: 'DELETE' });
      await loadProjectWorkspace(state.currentProjectId);
      $('#projectMemberModal').classList.remove('open');
      toast('项目成员已移除');
    } catch (error) { toast(error.message, true); }
    return;
  }
  if (event.target.closest('#addProjectAsset')) return openAssetPicker();
  if (event.target.closest('#addFileToProject') && state.currentFile) {
    const fileId = state.currentFile.id;
    closeFileViewer();
    return openAssetPicker(fileId);
  }
  const picked = event.target.closest('[data-add-picked-file]');
  if (picked) {
    const projectId = Number($('#assetTargetProject').value);
    const folderId = $('#assetTargetFolder').value ? Number($('#assetTargetFolder').value) : null;
    try {
      await api(`/api/projects/${projectId}/assets`, {
        method: 'POST',
        body: JSON.stringify({ file_id: Number(picked.dataset.addPickedFile), folder_id: folderId, title: '' }),
      });
      picked.disabled = true;
      picked.textContent = '已添加';
      toast('素材已加入项目');
      if (projectId === state.currentProjectId) await loadProjectWorkspace(projectId, true);
    } catch (error) { toast(error.message, true); }
    return;
  }
  const asset = event.target.closest('[data-asset]');
  if (asset) return openAssetReview(asset.dataset.asset);
  const layout = event.target.closest('[data-asset-layout]');
  if (layout) {
    state.assetLayout = layout.dataset.assetLayout;
    $$('[data-asset-layout]').forEach(button => button.classList.toggle('active', button === layout));
    return renderProjectAssets(state.projectAssets, state.projectAssets.length);
  }
  const review = event.target.closest('[data-review-asset]');
  if (review) return openAssetReview(review.dataset.reviewAsset, review.dataset.reviewTime === '' ? null : Number(review.dataset.reviewTime));
  if (event.target.closest('#closeAssetReview')) {
    $('#assetReviewModal').classList.remove('open');
    state.currentAsset = null;
    return;
  }
  if (event.target.closest('#openVersionModal')) {
    const form = $('#versionForm');
    form.reset();
    form.elements.file_id.value = '';
    $('#versionFileSelection').textContent = '尚未选择版本文件';
    $('#versionModal').classList.add('open');
    return loadVersionPickerResults('');
  }
  if (event.target.closest('#applyLook')) {
    const form = $('#lookForm');
    form.reset();
    form.elements.lut_file_id.value = '';
    $('#lookFileSelection').textContent = '尚未选择 LUT';
    $('#lookModal').classList.add('open');
    return loadLookPickerResults('');
  }
  if (event.target.closest('#toggleLookPreview') && state.currentVersionId) {
    state.lookPreviewEnabled = !state.lookPreviewEnabled;
    return loadReviewVersion(state.currentVersionId);
  }
  const versionFile = event.target.closest('[data-version-file]');
  if (versionFile) {
    $('#versionForm').elements.file_id.value = versionFile.dataset.versionFile;
    $('#versionFileSelection').innerHTML = `<b>已选择</b><span>${esc(versionFile.dataset.versionName)}</span>`;
    $$('#versionPickerResults [data-version-file]').forEach(button => {
      button.classList.toggle('primary', button === versionFile);
      button.classList.toggle('secondary', button !== versionFile);
      button.textContent = button === versionFile ? '已选择' : '选择';
    });
    return;
  }
  const lookFile = event.target.closest('[data-look-file]');
  if (lookFile) {
    $('#lookForm').elements.lut_file_id.value = lookFile.dataset.lookFile;
    $('#lookFileSelection').innerHTML = `<b>已选择</b><span>${esc(lookFile.dataset.lookName)}</span>`;
    $$('#lookPickerResults [data-look-file]').forEach(button => {
      button.classList.toggle('primary', button === lookFile);
      button.classList.toggle('secondary', button !== lookFile);
      button.textContent = button === lookFile ? '已选择' : '选择';
    });
    return;
  }
  if (event.target.closest('#requestProxy') && state.currentVersionId) {
    try {
      const result = await api(`/api/asset-versions/${state.currentVersionId}/proxy`, { method: 'POST' });
      toast(result.existing ? '代理任务已经在运行' : '代理媒体已加入硬件加速队列');
      const version = state.currentAsset.versions.find(item => Number(item.id) === Number(state.currentVersionId));
      if (version) {
        version.proxy_status = 'processing';
        updateReviewActions(version);
      }
    } catch (error) { toast(error.message, true); }
    return;
  }
  const reviewTab = event.target.closest('[data-review-tab]');
  if (reviewTab) {
    $$('.review-tabs button').forEach(button => button.classList.toggle('active', button === reviewTab));
    $$('.review-tab').forEach(tab => tab.classList.toggle('active', tab.id === `review-tab-${reviewTab.dataset.reviewTab}`));
    return;
  }
  const commentTime = event.target.closest('[data-comment-time]');
  if (commentTime && commentTime.dataset.commentTime !== '') {
    const media = reviewMedia();
    if (media) {
      media.currentTime = Number(commentTime.dataset.commentTime);
      media.play().catch(() => {});
    }
    return;
  }
  const resolve = event.target.closest('[data-resolve-comment]');
  if (resolve) {
    try {
      await api(`/api/comments/${resolve.dataset.resolveComment}/resolve`, {
        method: 'PUT',
        body: JSON.stringify({ resolved: resolve.dataset.resolved !== '1' }),
      });
      state.currentAsset = await api(`/api/assets/${state.currentAsset.id}`);
      renderReviewComments();
      loadCurrentProjectReviewTasks();
    } catch (error) { toast(error.message, true); }
    return;
  }
  if (event.target.closest('#playPause')) {
    const media = reviewMedia();
    if (media) media.paused ? media.play().catch(() => {}) : media.pause();
    return;
  }
  if (event.target.closest('#stepBack')) {
    const media = reviewMedia();
    if (media) media.currentTime = Math.max(0, media.currentTime - 1 / 25);
    return;
  }
  if (event.target.closest('#stepForward')) {
    const media = reviewMedia();
    if (media) media.currentTime = Math.min(media.duration || Infinity, media.currentTime + 1 / 25);
    return;
  }
  if (event.target.closest('#toggleDraw')) {
    state.drawingActive = !state.drawingActive;
    $('#toggleDraw').classList.toggle('active', state.drawingActive);
    $('#annotationLayer').classList.toggle('drawing', state.drawingActive);
    return;
  }
  if (event.target.closest('#clearDrawing')) {
    state.annotationStrokes = [];
    renderAnnotations();
    return;
  }
  if (event.target.closest('#generateReviewBrief') && state.currentAsset) {
    const button = $('#generateReviewBrief');
    button.disabled = true;
    $('#aiReviewBrief').textContent = '正在结合版本描述和审阅意见生成摘要…';
    try {
      const data = await api(`/api/assets/${state.currentAsset.id}/review-brief`);
      $('#aiReviewBrief').textContent = data.brief;
    } catch (error) {
      $('#aiReviewBrief').textContent = error.message;
    } finally { button.disabled = false; }
    return;
  }
  if (event.target.closest('#projectShareButton')) return openShareCreator();
  if (event.target.closest('#exportReviewCsv')) {
    downloadAuthenticated(`/api/projects/${state.currentProjectId}/review-export?format=csv`, `${state.currentProject?.name || 'project'}-review.csv`);
    return;
  }
  if (event.target.closest('#exportReviewXml')) {
    downloadAuthenticated(`/api/projects/${state.currentProjectId}/review-export?format=fcpxml`, `${state.currentProject?.name || 'project'}-review.fcpxml`);
    return;
  }
  if (event.target.closest('#refreshReviewTasks')) return loadCurrentProjectReviewTasks();
  if (event.target.closest('#refreshAllReviews')) return loadAllReviewTasks();
  const delivery = event.target.closest('[data-open-project-delivery]');
  if (delivery) {
    await openProject(delivery.dataset.openProjectDelivery);
    return openShareCreator();
  }
  if (event.target.closest('#notificationButton')) {
    await loadNotifications();
    return $('#notificationModal').classList.add('open');
  }
  if (event.target.closest('#readAllNotifications')) {
    try {
      await api('/api/notifications/read', { method: 'POST' });
      await loadNotifications();
    } catch (error) { toast(error.message, true); }
    return;
  }
  const notification = event.target.closest('[data-notification]');
  if (notification) {
    try {
      await api(`/api/notifications/read?notification_id=${notification.dataset.notification}`, { method: 'POST' });
      if (notification.dataset.notificationTarget === 'asset') {
        $('#notificationModal').classList.remove('open');
        openAssetReview(notification.dataset.notificationTargetId);
      }
    } catch (error) { toast(error.message, true); }
    return;
  }
  if (event.target.closest('#copyShareLink')) {
    const input = $('#shareCreated input');
    try {
      await navigator.clipboard.writeText(input.value);
      toast('分享链接已复制');
    } catch { input.select(); }
    return;
  }
  const publicVersion = event.target.closest('[data-public-version]');
  if (publicVersion && state.publicShareData) {
    const assetData = state.publicShareData.assets.find(item => Number(item.id) === Number(publicVersion.dataset.publicAssetId));
    const version = assetData?.versions.find(item => Number(item.id) === Number(publicVersion.dataset.publicVersion));
    const card = publicVersion.closest('.public-asset');
    if (version && card) {
      state.publicVersionIds[assetData.id] = Number(version.id);
      $('.public-asset-stage', card).innerHTML = `${publicStage(version, assetData.id)}${state.publicShareData.share.watermark_text ? `<span class="review-watermark">${esc(state.publicShareData.share.watermark_text)}</span>` : ''}`;
      const download = $('.public-version-row a', card);
      if (download) {
        download.href = version.download_url || '#';
        download.hidden = !version.download_url;
      }
    }
  }
});

document.addEventListener('pointerdown', event => {
  if (event.target.id !== 'annotationLayer' || !state.drawingActive) return;
  const rect = event.target.getBoundingClientRect();
  state.activeStroke = {
    color: '#ffcc57',
    points: [{
      x: (event.clientX - rect.left) / rect.width,
      y: (event.clientY - rect.top) / rect.height,
    }],
  };
  event.target.setPointerCapture(event.pointerId);
});

document.addEventListener('pointermove', event => {
  if (!state.activeStroke || event.target.id !== 'annotationLayer') return;
  const rect = event.target.getBoundingClientRect();
  state.activeStroke.points.push({
    x: Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)),
    y: Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height)),
  });
  renderAnnotations();
});

document.addEventListener('pointerup', event => {
  if (!state.activeStroke) return;
  if (state.activeStroke.points.length > 1) state.annotationStrokes.push(state.activeStroke);
  state.activeStroke = null;
  renderAnnotations();
});

document.addEventListener('change', event => {
  if (event.target.matches('[data-select-person]')) updatePeopleSelection();
  if (event.target.matches('[data-select-event]')) updateEventSelection();
  if (event.target.matches('[data-duplicate-file]')) updateDuplicateSelection();
});

$('#timelineMore').addEventListener('click', () => loadTimeline(false));
$('#searchMore').addEventListener('click', () => runSearch(state.searchQuery, true));
$('#libraryMore').addEventListener('click', () => loadLibraryFiles(false));
$('#peopleMore').addEventListener('click', () => loadPeople(false));
$('#eventsMore').addEventListener('click', () => loadEvents(false));
['#timelineYear', '#timelineMonth', '#timelineKind'].forEach(selector => $(selector).addEventListener('change', () => loadTimeline(true)));
['#libraryKind', '#libraryStatus', '#librarySource', '#libraryTag', '#librarySort', '#libraryFavorite'].forEach(selector => {
  $(selector).addEventListener('change', () => loadLibraryFiles(true));
});
['#searchLibrary', '#searchDateFrom', '#searchDateTo', '#searchPerson', '#searchPlace', '#searchEvent', '#searchTag', '#searchFavorite'].forEach(selector => {
  $(selector).addEventListener('change', () => {
    if (state.searchQuery) runSearch(state.searchQuery);
  });
});
$('#conversationSelect').addEventListener('change', event => openConversation(event.currentTarget.value));
$('#precisionSearch').addEventListener('change', event => {
  state.preciseSearch = event.currentTarget.checked;
  const query = $('#mainSearchInput').value.trim();
  if (query) runSearch(query);
});

$$('[data-search-form]').forEach(form => form.addEventListener('submit', event => {
  event.preventDefault();
  runSearch(new FormData(form).get('q'));
}));

$('#askForm').addEventListener('submit', event => {
  event.preventDefault();
  const input = event.currentTarget.elements.question;
  const question = input.value.trim();
  if (question) {
    input.value = '';
    ask(question);
  }
});

const hourOptions = Array.from({ length: 24 }, (_, hour) => `<option value="${hour}">${String(hour).padStart(2, '0')}:00</option>`).join('');
$('#indexPolicyForm').elements.start_hour.innerHTML = hourOptions;
$('#indexPolicyForm').elements.end_hour.innerHTML = hourOptions;

$('#indexBatchForm').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const values = Object.fromEntries(new FormData(form));
  const payload = {
    limit: Number(values.limit),
    library_id: values.library_id ? Number(values.library_id) : null,
    kind: values.kind,
    order: values.order,
  };
  const button = $('button', form);
  button.disabled = true;
  try {
    const result = await api('/api/index', { method: 'POST', body: JSON.stringify(payload) });
    toast(result.existing ? '已有索引任务在运行' : `已加入 ${fmtCount(payload.limit)} 个文件的索引批次`);
    await Promise.all([loadTasks(), loadIndexStatus()]);
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
});

$('#indexPolicyForm').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const saved = state.indexStatus?.policy || {};
  const payload = {
    ...saved,
    enabled: form.elements.enabled.checked,
    start_hour: Number(form.elements.start_hour.value),
    end_hour: Number(form.elements.end_hour.value),
    batch_size: Number(form.elements.batch_size.value),
  };
  const button = $('button', form);
  button.disabled = true;
  try {
    await api('/api/index/policy', { method: 'PUT', body: JSON.stringify(payload) });
    toast(payload.enabled ? '夜间自动索引已启用' : '已切换为手动索引');
    await loadIndexStatus(true);
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
});

$('#libraryForm').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form));
  try {
    const library = await api('/api/libraries', { method: 'POST', body: JSON.stringify(data) });
    await api(`/api/libraries/${library.id}/discover`, { method: 'POST' });
    $('#libraryModal').classList.remove('open');
    form.reset();
    toast('媒体库已添加，开始快速扫描');
    showView('tasks');
  } catch (error) {
    toast(error.message, true);
  }
});

$('#tokenForm').addEventListener('submit', event => {
  event.preventDefault();
  state.token = new FormData(event.currentTarget).get('token').trim();
  state.authReady = false;
  localStorage.setItem('nasAiToken', state.token);
  $('#tokenModal').classList.remove('open');
  toast('令牌已保存');
  boot();
});

$('#bootstrapForm').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form));
  if (data.password !== data.password_confirm) {
    toast('两次输入的密码不一致', true);
    form.elements.password_confirm.focus();
    return;
  }
  delete data.password_confirm;
  try {
    const response = await api('/api/auth/bootstrap', { method: 'POST', body: JSON.stringify(data) });
    state.bootstrapRequired = false;
    state.token = response.token;
    state.user = response.user;
    state.authReady = true;
    localStorage.setItem('nasAiToken', state.token);
    form.reset();
    $('#tokenModal').classList.remove('open');
    toast(`设置完成，欢迎 ${response.user.display_name}`);
    boot();
  } catch (error) { toast(error.message, true); }
});

$('#loginForm').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form));
  try {
    const response = await api('/api/auth/login', { method: 'POST', body: JSON.stringify(data) });
    state.token = response.token;
    state.user = response.user;
    state.authReady = true;
    localStorage.setItem('nasAiToken', state.token);
    form.reset();
    $('#tokenModal').classList.remove('open');
    toast(`欢迎，${response.user.display_name}`);
    boot();
  } catch (error) { toast(error.message, true); }
});

$('#logoutButton').addEventListener('click', async () => {
  try { await api('/api/auth/logout', { method: 'POST' }); } catch {}
  state.token = '';
  state.user = null;
  state.authReady = false;
  localStorage.removeItem('nasAiToken');
  $('#tokenForm [name=token]').value = '';
  applyRole();
  $('#tokenModal').classList.add('open');
});

$('#uploadForm [name=files]').addEventListener('change', event => {
  const files = [...event.target.files];
  $('#uploadSelection').textContent = files.length ? `${files.length} 个文件 · ${fmtBytes(files.reduce((sum, file) => sum + file.size, 0))}` : '可一次选择多个文件';
});

$('#uploadForm').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const files = [...form.elements.files.files];
  if (!files.length) return;
  const progressBox = $('#uploadProgress');
  const progressBar = $('#uploadProgress .progress span');
  const progressText = $('#uploadProgress b');
  const button = $('button', form);
  progressBox.hidden = false;
  button.disabled = true;
  try {
    for (let index = 0; index < files.length; index += 1) {
      await uploadOne(files[index], value => {
        const percent = Math.round((index + value) / files.length * 100);
        progressBar.style.width = `${percent}%`;
        progressText.textContent = `${percent}%`;
      });
    }
    progressBar.style.width = '100%';
    progressText.textContent = '100%';
    toast(`已上传 ${files.length} 个文件，正在本地索引`);
    $('#uploadModal').classList.remove('open');
    form.reset();
    $('#uploadSelection').textContent = '可一次选择多个文件';
    showView('tasks');
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
    setTimeout(() => { progressBox.hidden = true; progressBar.style.width = '0'; }, 500);
  }
});

$('#userForm').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const formData = new FormData(form);
  const id = formData.get('id');
  const editingUser = state.users.find(user => Number(user.id) === Number(id));
  const data = {
    username: formData.get('username'),
    display_name: formData.get('display_name'),
    password: formData.get('password'),
    role: form.elements.role.value,
    enabled: form.elements.enabled.checked,
    library_ids: editingUser?.role === 'owner'
      ? editingUser.library_ids
      : $$('[name=library_ids]:checked', form).map(element => Number(element.value)),
  };
  if (id) delete data.username;
  try {
    await api(id ? `/api/users/${id}` : '/api/users', { method: id ? 'PUT' : 'POST', body: JSON.stringify(data) });
    $('#userModal').classList.remove('open');
    toast(id ? '用户权限已更新' : '用户已创建');
    loadUsers();
  } catch (error) { toast(error.message, true); }
});

$('#projectForm').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const values = Object.fromEntries(new FormData(form));
  try {
    const project = await api('/api/projects', { method: 'POST', body: JSON.stringify(values) });
    $('#projectModal').classList.remove('open');
    form.reset();
    form.elements.color.value = '#7c8cff';
    toast('项目已创建');
    await loadProjects();
    openProject(project.id);
  } catch (error) { toast(error.message, true); }
});

$('#folderForm').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const values = Object.fromEntries(new FormData(form));
  try {
    await api(`/api/projects/${state.currentProjectId}/folders`, {
      method: 'POST',
      body: JSON.stringify({ name: values.name, parent_id: values.parent_id ? Number(values.parent_id) : null }),
    });
    $('#folderModal').classList.remove('open');
    form.reset();
    toast('项目文件夹已创建');
    await loadProjectWorkspace(state.currentProjectId);
  } catch (error) { toast(error.message, true); }
});

$('#projectMemberForm').addEventListener('submit', async event => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(event.currentTarget));
  try {
    await api(`/api/projects/${state.currentProjectId}/members`, {
      method: 'PUT',
      body: JSON.stringify({ user_id: Number(values.user_id), role: values.role }),
    });
    toast('项目角色已更新');
    await loadProjectWorkspace(state.currentProjectId);
    $('#projectMemberModal').classList.remove('open');
  } catch (error) { toast(error.message, true); }
});

$('#assetPickerForm').addEventListener('submit', event => {
  event.preventDefault();
  loadAssetPickerResults(new FormData(event.currentTarget).get('q').trim());
});

$('#assetTargetProject').addEventListener('change', refreshAssetTargetFolders);

$('#projectAssetSearch').addEventListener('submit', event => {
  event.preventDefault();
  loadProjectAssets();
});

$('#projectStatusFilter').addEventListener('change', loadProjectAssets);
$('#projectSort').addEventListener('change', renderProjectGrid);

$('#reviewVersionSelect').addEventListener('change', event => loadReviewVersion(Number(event.currentTarget.value)));

$('#reviewCommentForm').addEventListener('submit', async event => {
  event.preventDefault();
  if (!state.currentAsset || !state.currentVersionId) return;
  const form = event.currentTarget;
  const values = Object.fromEntries(new FormData(form));
  const media = reviewMedia();
  const automaticTime = media ? Number(media.currentTime.toFixed(3)) : null;
  const timeStart = values.time_start !== '' ? Number(values.time_start) : automaticTime;
  const timeEnd = values.time_end !== '' ? Number(values.time_end) : null;
  const drawing = [...state.annotationStrokes];
  try {
    await api(`/api/assets/${state.currentAsset.id}/comments`, {
      method: 'POST',
      body: JSON.stringify({
        version_id: state.currentVersionId,
        body: values.body,
        comment_type: drawing.length ? 'drawing' : timeEnd != null ? 'range' : timeStart != null ? 'point' : 'text',
        time_start: timeStart,
        time_end: timeEnd,
        drawing,
        visibility: form.elements.external.checked ? 'external' : 'team',
      }),
    });
    form.reset();
    state.annotationStrokes = [];
    state.currentAsset = await api(`/api/assets/${state.currentAsset.id}`);
    renderReviewComments();
    await loadCurrentProjectReviewTasks();
    toast('审阅意见已发布');
  } catch (error) { toast(error.message, true); }
});

$('#assetDetailForm').addEventListener('submit', async event => {
  event.preventDefault();
  if (!state.currentAsset) return;
  const form = event.currentTarget;
  const values = Object.fromEntries(new FormData(form));
  try {
    state.currentAsset = await api(`/api/assets/${state.currentAsset.id}`, {
      method: 'PUT',
      body: JSON.stringify({
        title: values.title,
        description: values.description,
        status: values.status,
        rating: Number(values.rating),
        folder_id: values.folder_id ? Number(values.folder_id) : null,
        assignee_id: values.assignee_id ? Number(values.assignee_id) : null,
      }),
    });
    $('#reviewAssetTitle').textContent = state.currentAsset.title;
    $('#reviewAssetStatus').textContent = assetStatusName(state.currentAsset.status);
    toast('素材信息已保存');
    if (state.currentProjectId) await loadProjectWorkspace(state.currentProjectId, true);
  } catch (error) { toast(error.message, true); }
});

$('#versionForm').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const values = Object.fromEntries(new FormData(form));
  if (!values.file_id) return toast('请先选择版本文件', true);
  try {
    const version = await api(`/api/assets/${form.dataset.assetId}/versions`, {
      method: 'POST',
      body: JSON.stringify({ file_id: Number(values.file_id), label: values.label, notes: values.notes }),
    });
    $('#versionModal').classList.remove('open');
    form.reset();
    toast(`V${version.version_number} 已添加`);
    await openAssetReview(form.dataset.assetId);
  } catch (error) { toast(error.message, true); }
});

$('#searchVersionFiles').addEventListener('click', () => {
  loadVersionPickerResults($('#versionForm').elements.file_query.value.trim());
});

$('#searchLookFiles').addEventListener('click', () => {
  loadLookPickerResults($('#lookForm').elements.file_query.value.trim());
});

$('#lookForm').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  if (!form.elements.lut_file_id.value || !state.currentVersionId) return toast('请先选择 LUT 文件', true);
  try {
    const result = await api(`/api/asset-versions/${state.currentVersionId}/look-preview`, {
      method: 'POST',
      body: JSON.stringify({ lut_file_id: Number(form.elements.lut_file_id.value) }),
    });
    $('#lookModal').classList.remove('open');
    toast(result.existing ? '该版本的 LUT 任务正在运行' : 'LUT 预览已加入硬件加速队列');
    const version = state.currentAsset.versions.find(item => Number(item.id) === Number(state.currentVersionId));
    if (version) {
      version.look_status = 'processing';
      updateReviewActions(version);
    }
    await loadTasks(true);
  } catch (error) { toast(error.message, true); }
});

$('#shareForm').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = new FormData(form);
  const expires = data.get('expires_at');
  try {
    const share = await api(`/api/projects/${state.currentProjectId}/shares`, {
      method: 'POST',
      body: JSON.stringify({
        name: data.get('name'),
        asset_id: data.get('asset_id') ? Number(data.get('asset_id')) : null,
        access_code: data.get('access_code'),
        expires_at: expires ? new Date(expires).toISOString() : null,
        brand_name: data.get('brand_name'),
        watermark_text: data.get('watermark_text'),
        can_comment: form.elements.can_comment.checked,
        can_view_versions: form.elements.can_view_versions.checked,
        can_download: form.elements.can_download.checked,
      }),
    });
    const absolute = new URL(share.url, location.origin).href;
    $('#shareCreated input').value = absolute;
    $('#shareCreated').hidden = false;
    toast('安全分享已创建');
    await loadProjectWorkspace(state.currentProjectId, true);
  } catch (error) { toast(error.message, true); }
});

$('#publicAccessForm').addEventListener('submit', event => {
  event.preventDefault();
  loadPublicShare(new FormData(event.currentTarget).get('access_code'));
});

document.addEventListener('submit', async event => {
  const form = event.target.closest('[data-public-comment-form]');
  if (!form) return;
  event.preventDefault();
  const token = decodeURIComponent(location.pathname.split('/').filter(Boolean)[1] || '');
  const values = Object.fromEntries(new FormData(form));
  try {
    const response = await fetch(`/api/public/shares/${encodeURIComponent(token)}/comments`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        access_code: state.publicAccessCode,
        asset_id: Number(form.dataset.publicCommentForm),
        version_id: state.publicVersionIds[form.dataset.publicCommentForm] || null,
        guest_name: values.guest_name,
        body: values.body,
        time_start: values.time_start === '' ? null : Number(values.time_start),
      }),
    });
    if (!response.ok) {
      const payload = await response.json();
      throw new Error(payload.detail || '评论发布失败');
    }
    form.reset();
    toast('审阅意见已发布');
    await loadPublicShare(state.publicAccessCode);
  } catch (error) { toast(error.message, true); }
});

$('#globalScan').addEventListener('click', async event => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const libraries = await api('/api/libraries');
    await Promise.all(libraries.map(item => api(`/api/libraries/${item.id}/discover`, { method: 'POST' })));
    toast(libraries.length ? `已加入 ${libraries.length} 个快速扫描任务` : '暂无可扫描的媒体库');
    if (libraries.length) showView('tasks');
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
});

document.addEventListener('keydown', event => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
    event.preventDefault();
    showView('search');
  }
  if (event.key === 'Escape') {
    if ($('#fileModal').classList.contains('open')) closeFileViewer();
    $$('.modal.open').forEach(modal => {
      if (modal.id !== 'tokenModal' || !state.bootstrapRequired) modal.classList.remove('open');
    });
  }
});

setInterval(() => {
  if (state.authReady && !state.publicMode && !document.hidden && $('#view-tasks').classList.contains('active')) {
    loadTasks(true);
    if (isAdmin()) loadIndexStatus(false, true);
  }
}, 3000);

setInterval(() => {
  if (state.authReady && !state.publicMode && !document.hidden && $('#view-hardware').classList.contains('active')) loadRuntimeMetrics();
}, 5000);

setInterval(() => {
  if (state.authReady && !state.publicMode && !document.hidden && $('#view-home').classList.contains('active')) {
    loadDashboard(true);
    loadTasks(true);
  }
}, 10000);

setInterval(() => {
  if (state.authReady && !state.publicMode && !document.hidden && $('#view-operations').classList.contains('active')) loadOperations();
}, 60000);

document.addEventListener('visibilitychange', () => {
  if (document.hidden || !state.authReady || state.publicMode) return;
  if ($('#view-home').classList.contains('active')) {
    loadDashboard(true);
    loadTasks(true);
  }
  if ($('#view-tasks').classList.contains('active')) {
    loadTasks(true);
    if (isAdmin()) loadIndexStatus(false, true);
  }
});

function setGreeting() {
  const hour = new Date().getHours();
  $('#dayGreeting').textContent = hour < 6 ? '夜深了' : hour < 12 ? '早上好' : hour < 18 ? '下午好' : '晚上好';
}

async function boot() {
  state.authReady = false;
  try {
    const health = await api('/api/health');
    $('.service-card').classList.add('ok');
    $('#serviceStatus').textContent = '本地服务在线';
    state.bootstrapRequired = Boolean(health.bootstrap_required);
    if (state.bootstrapRequired) {
      state.user = null;
      applyRole();
      $('#bootstrapForm').hidden = false;
      $('#loginForm').hidden = true;
      $('#tokenForm').hidden = true;
      $('#tokenDivider').hidden = true;
      $('#logoutButton').hidden = true;
      $('#authCloseButton').hidden = true;
      $('#authEyebrow').textContent = '首次启动';
      $('#authTitle').textContent = '设置你的管理员账号';
      $('#authDescription').textContent = '用户名和密码由你自行设置，只保存在这台 NAS。完成后会直接进入空间。';
      $('#tokenModal').classList.add('open');
      return;
    }
    $('#bootstrapForm').hidden = true;
    $('#loginForm').hidden = false;
    $('#tokenForm').hidden = false;
    $('#tokenDivider').hidden = false;
    $('#authCloseButton').hidden = false;
    $('#authEyebrow').textContent = '安全登录';
    if (health.auth_enabled && !state.token) {
      state.user = null;
      applyRole();
      $('#tokenModal').classList.add('open');
      return;
    }
    state.user = await api('/api/auth/me');
    state.authReady = true;
    applyRole();
    await Promise.all([
      loadDashboard(),
      loadLibraries(),
      loadSystem(),
      loadTasks(),
      loadSearchFacets(),
      loadProjects(true),
      state.user?.id ? loadNotifications() : Promise.resolve(),
      state.user?.id ? loadConversations() : Promise.resolve(),
      isAdmin() ? loadIndexStatus(true) : Promise.resolve(),
    ]);
  } catch (error) {
    if (error.status === 401) return;
    $('.service-card').classList.remove('ok');
    $('#serviceStatus').textContent = '连接失败';
    toast(error.message, true);
  }
}

const cleanLocation = new URL(location.href);
if (cleanLocation.searchParams.has('token')) {
  cleanLocation.searchParams.delete('token');
  history.replaceState(null, '', `${cleanLocation.pathname}${cleanLocation.search}${cleanLocation.hash}`);
}

if (state.publicMode) {
  bootPublicShare();
} else {
  setGreeting();
  boot();
}
