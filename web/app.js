const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const state = {
  view: 'overview',
  overview: null,
  jobs: [],
  pipeline: {},
  sources: [],
  page: 1,
  pages: 1,
  track: '',
  currentJob: null,
};

const titles = {overview: 'Today', discover: 'Discover', pipeline: 'Pipeline', sources: 'Sources'};
const statusMeta = {
  saved: {label: 'Saved', color: '#5574c6'},
  applied: {label: 'Applied', color: '#c9972b'},
  interview: {label: 'Interview', color: '#f06f52'},
  offer: {label: 'Offer', color: '#3a8a61'},
  rejected: {label: 'Closed', color: '#a1a8a3'},
  archived: {label: 'Archived', color: '#767d78'},
};

function esc(value = '') {
  return String(value).replace(/[&<>'"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
}

function safeUrl(value = '') {
  try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) ? url.href : '#'; }
  catch { return '#'; }
}

async function api(path, options = {}) {
  const response = await fetch(path, {headers: {'Content-Type': 'application/json'}, ...options});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function toast(message) {
  const node = $('#toast');
  node.textContent = message;
  node.classList.add('show');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => node.classList.remove('show'), 2300);
}

function initials(company) {
  const words = String(company || '?').trim().split(/\s+/).filter(Boolean);
  return (words.length > 1 ? words[0][0] + words[1][0] : words[0]?.slice(0, 2) || '?').toUpperCase();
}

function age(value) {
  if (!value) return 'date unknown';
  let timestamp = typeof value === 'number' ? value * 1000 : new Date(value).getTime();
  if (typeof value === 'string' && /^\d+(\.\d+)?$/.test(value)) timestamp = Number(value) * 1000;
  if (!Number.isFinite(timestamp)) return 'date unknown';
  const hours = Math.max(0, (Date.now() - timestamp) / 36e5);
  if (hours < 2) return 'just posted';
  if (hours < 24) return `${Math.floor(hours)}h ago`;
  const days = Math.floor(hours / 24);
  return days < 45 ? `${days}d ago` : new Date(timestamp).toLocaleDateString(undefined, {month:'short', day:'numeric'});
}

function locationLabel(bucket) {
  return {us:'United States', canada:'Canada', remote:'Remote', other:'Other', unknown:'Unknown'}[bucket] || bucket;
}

function switchView(view) {
  state.view = view;
  $$('.view').forEach(node => node.classList.toggle('active', node.id === `view-${view}`));
  $$('.nav-item').forEach(node => node.classList.toggle('active', node.dataset.view === view));
  $('#page-title').textContent = titles[view];
  $('.sidebar').classList.remove('open');
  history.replaceState(null, '', `#${view}`);
  window.scrollTo({top: 0, behavior: 'smooth'});
  if (view === 'discover') loadJobs();
  if (view === 'pipeline') loadPipeline();
  if (view === 'sources') loadSources();
}

function fitBadge(score) {
  const kind = score >= 80 ? '' : score >= 60 ? 'mid' : 'low';
  return `<span class="fit-badge ${kind}">${score}% fit</span>`;
}

async function loadOverview() {
  try {
    const data = await api('/api/overview');
    state.overview = data;
    const s = data.stats;
    $('#profile-headline').textContent = data.profile.headline;
    $('#hero-score').textContent = s.high_fit;
    $('#nav-job-count').textContent = s.searchable_jobs > 999 ? `${Math.round(s.searchable_jobs / 100) / 10}k` : s.searchable_jobs;
    $('#nav-pipeline-count').textContent = s.active_applications;
    const values = [
      [s.fresh_24h, 'posted in the last 24 hours'],
      [s.high_fit, 'scored 80 or higher'],
      [s.active_applications, 'saved through offer'],
      [`${s.healthy_sources}/${s.checked_sources || s.enabled_sources}`, 'checked without errors'],
    ];
    $$('.stat-card').forEach((card, index) => {
      $('strong', card).textContent = values[index][0];
      $('p', card).textContent = values[index][1];
    });
    renderRecommendations(data.recommendations);
    renderPipelineSummary(data.pipeline);
    if (data.last_run) {
      $('#watcher-status').textContent = `${data.last_run.ok_count} checked · ${age(data.last_run.finished_at)}`;
      $('.pulse i').style.background = data.last_run.fail_count ? '#f3c85f' : '#68d69c';
    } else if (s.checked_sources) {
      $('#watcher-status').textContent = `${s.healthy_sources} sources healthy`;
    }
  } catch (error) {
    toast(error.message);
    $('#recommendations').innerHTML = emptyMarkup('No local data yet', 'Run python watcher.py --once to seed the dashboard.');
  }
}

function renderRecommendations(jobs) {
  const root = $('#recommendations');
  if (!jobs.length) {
    root.innerHTML = emptyMarkup('Your queue is ready for data', 'Run one polling cycle, then the strongest matches will appear here.');
    return;
  }
  root.innerHTML = jobs.map(job => `
    <article class="recommendation">
      <div class="company-avatar">${esc(initials(job.company))}</div>
      <div><h3>${esc(job.title)}</h3><p>${esc(job.company)} · ${esc(job.location || 'Location not listed')}</p></div>
      ${fitBadge(job.score)}
      <a class="open-link" href="${esc(safeUrl(job.url))}" target="_blank" rel="noopener" title="Open role">↗</a>
    </article>`).join('');
}

function renderPipelineSummary(pipeline) {
  const order = ['saved', 'applied', 'interview', 'offer'];
  const active = order.reduce((sum, key) => sum + (pipeline[key] || 0), 0);
  const all = Object.values(pipeline).reduce((a, b) => a + b, 0);
  $('#pipeline-total').textContent = all;
  $('#pipeline-donut').style.setProperty('--p', `${all ? Math.min(100, (active / Math.max(all, 1)) * 100) : 0}%`);
  $('#pipeline-legend').innerHTML = order.map(key => `<div class="legend-row"><i style="background:${statusMeta[key].color}"></i><span>${statusMeta[key].label}</span><b>${pipeline[key] || 0}</b></div>`).join('');
}

function queryParams() {
  const params = new URLSearchParams({page: state.page, per_page: 30, sort: $('#sort-filter').value});
  const values = {
    q: $('#search-input').value.trim(), track: state.track,
    location: $('#location-filter').value, seniority: $('#seniority-filter').value,
    company_type: $('#startup-filter').checked ? 'startup' : '',
    fresh: $('#fresh-filter').checked ? '1' : '',
    min_score: $('#high-fit-filter').checked ? '80' : '',
  };
  Object.entries(values).forEach(([key, value]) => value && params.set(key, value));
  return params;
}

async function loadJobs() {
  const root = $('#job-list');
  root.innerHTML = `<div class="empty-state"><div class="spinner"></div><h3>Ranking the market…</h3></div>`;
  const exportParams = queryParams();
  exportParams.delete('page'); exportParams.delete('per_page');
  $('#export-button').href = `/api/export.csv?${exportParams}`;
  try {
    const data = await api(`/api/jobs?${queryParams()}`);
    state.jobs = data.jobs;
    state.pages = data.pages;
    $('#result-count').textContent = data.total.toLocaleString();
    renderJobs(data.jobs);
    renderPagination(data.page, data.pages);
  } catch (error) {
    root.innerHTML = emptyMarkup('Could not load jobs', error.message);
  }
}

function renderJobs(jobs) {
  const root = $('#job-list');
  if (!jobs.length) {
    root.innerHTML = emptyMarkup('No roles match those filters', 'Try broadening the location, track, or search terms.');
    return;
  }
  root.innerHTML = jobs.map((job, index) => {
    const reasons = job.reasons?.slice(0, 2).map(reason => `<span class="reason">${esc(reason)}</span>`).join('') || '<span class="reason">broad market match</span>';
    const tracked = job.status !== 'untracked';
    return `<article class="job-card" data-index="${index}">
      <div class="company-avatar">${esc(initials(job.company))}</div>
      <div class="job-main">
        <div class="job-company">${esc(job.company)}</div>
        <h3>${esc(job.title)}</h3>
        <div class="job-meta"><span>${esc(job.location || 'Location not listed')}</span><span>${esc(job.track_label)}</span><span>${esc(age(job.posted || job.first_seen))}</span>${job.salary ? `<span>${esc(job.salary)}</span>` : ''}</div>
        ${tracked ? `<span class="status-pill">${esc(statusMeta[job.status]?.label || job.status)}</span>` : ''}
      </div>
      <div class="job-reasons">${reasons}</div>
      <div class="score-block"><div class="score-ring" style="--score:${job.score * 3.6}deg"><b>${job.score}</b></div><small>FIT SCORE</small></div>
      <div class="job-actions"><button class="save-button ${tracked ? 'saved' : ''}" title="${tracked ? 'Edit application' : 'Save role'}">${tracked ? '✓' : '+'}</button><a href="${esc(safeUrl(job.url))}" target="_blank" rel="noopener" title="Open application">↗</a></div>
    </article>`;
  }).join('');
}

function renderPagination(page, pages) {
  const root = $('#pagination');
  if (pages <= 1) { root.innerHTML = ''; return; }
  const start = Math.max(1, Math.min(page - 2, pages - 4));
  const end = Math.min(pages, start + 4);
  let html = `<button data-page="${page - 1}" ${page === 1 ? 'disabled' : ''}>←</button>`;
  for (let i = start; i <= end; i++) html += `<button data-page="${i}" class="${i === page ? 'active' : ''}">${i}</button>`;
  html += `<button data-page="${page + 1}" ${page === pages ? 'disabled' : ''}>→</button>`;
  root.innerHTML = html;
}

function emptyMarkup(title, detail) {
  return `<div class="empty-state"><span style="font-size:28px">⌕</span><h3>${esc(title)}</h3><p>${esc(detail)}</p></div>`;
}

async function quickSave(job) {
  try {
    await api('/api/applications', {method: 'POST', body: JSON.stringify({job, status: 'saved'})});
    job.status = 'saved';
    toast('Saved to your pipeline');
    renderJobs(state.jobs);
    loadOverview();
  } catch (error) { toast(error.message); }
}

function openApplication(job) {
  state.currentJob = job;
  $('#dialog-key').value = job.key;
  $('#dialog-title').textContent = job.title;
  $('#dialog-company').textContent = `${job.company} · ${job.location || 'Location not listed'}`;
  $('#dialog-status').value = job.status === 'untracked' ? 'saved' : job.status;
  $('#dialog-next-step').value = job.next_step || '';
  $('#dialog-due-date').value = job.due_date || '';
  $('#dialog-notes').value = job.notes || '';
  $('#delete-application').style.visibility = job.status === 'untracked' ? 'hidden' : 'visible';
  $('#application-dialog').showModal();
}

async function saveDialog() {
  const job = state.currentJob;
  if (!job) return;
  const payload = {
    job,
    status: $('#dialog-status').value,
    next_step: $('#dialog-next-step').value.trim(),
    due_date: $('#dialog-due-date').value,
    notes: $('#dialog-notes').value.trim(),
  };
  try {
    await api('/api/applications', {method:'POST', body:JSON.stringify(payload)});
    Object.assign(job, {
      status: payload.status,
      next_step: payload.next_step,
      due_date: payload.due_date,
      notes: payload.notes,
    });
    $('#application-dialog').close();
    toast('Application updated');
    loadOverview();
    if (state.view === 'pipeline') loadPipeline(); else renderJobs(state.jobs);
  } catch (error) { toast(error.message); }
}

async function deleteApplication() {
  const job = state.currentJob;
  if (!job || job.status === 'untracked') return;
  try {
    await api(`/api/applications?key=${encodeURIComponent(job.key)}`, {method:'DELETE'});
    job.status = 'untracked'; job.notes = ''; job.next_step = ''; job.due_date = '';
    $('#application-dialog').close();
    toast('Removed from pipeline');
    loadOverview(); loadPipeline();
  } catch (error) { toast(error.message); }
}

async function loadPipeline() {
  const root = $('#kanban');
  root.innerHTML = `<div class="empty-state"><div class="spinner"></div></div>`;
  try {
    const data = await api('/api/pipeline');
    state.pipeline = data.pipeline;
    $('#nav-pipeline-count').textContent = data.total;
    renderKanban(data.pipeline);
  } catch (error) { root.innerHTML = emptyMarkup('Could not load your pipeline', error.message); }
}

function renderKanban(pipeline) {
  const order = ['saved', 'applied', 'interview', 'offer', 'rejected', 'archived'];
  $('#kanban').innerHTML = order.map(status => {
    const jobs = pipeline[status] || [];
    return `<section class="kanban-column" data-status="${status}">
      <header class="kanban-head"><i style="background:${statusMeta[status].color}"></i>${statusMeta[status].label}<b>${jobs.length}</b></header>
      <div>${jobs.length ? jobs.map((job, index) => `<article class="pipeline-card" data-status="${status}" data-index="${index}">
        <small>${esc(job.company)}</small><h3>${esc(job.title)}</h3>
        ${job.next_step ? `<div class="next-step">→ ${esc(job.next_step)}</div>` : ''}
        <div class="card-foot"><span>${esc(job.location || 'No location')}</span><span>${job.due_date ? `Due ${esc(job.due_date)}` : `${job.score}% fit`}</span></div>
      </article>`).join('') : `<div class="empty-state" style="min-height:120px"><p>No roles here</p></div>`}</div>
    </section>`;
  }).join('');
}

async function loadSources() {
  try {
    const data = await api('/api/sources');
    state.sources = data.sources;
    renderSources();
  } catch (error) { toast(error.message); }
}

function renderSources() {
  const query = $('#source-search').value.trim().toLowerCase();
  const sources = state.sources.filter(s => `${s.label} ${s.adapter} ${s.tags.join(' ')}`.toLowerCase().includes(query));
  const healthy = state.sources.filter(s => s.seeded && !s.failures && !s.disabled).length;
  const attention = state.sources.filter(s => s.failures && !s.disabled).length;
  $('#source-summary').innerHTML = `<span>${healthy} healthy</span><span>${attention} need attention</span><span>${state.sources.length} configured</span>`;
  $('#source-rows').innerHTML = sources.map(source => {
    const kind = source.disabled ? 'idle' : source.failures ? 'bad' : source.seeded ? 'ok' : 'idle';
    const text = source.disabled ? 'Disabled' : source.failures ? `${source.failures} failures` : source.seeded ? 'Healthy' : 'Not checked';
    return `<tr><td>${esc(source.label)}</td><td>${esc(source.adapter || '—')}</td><td>${source.tags.map(tag => `<span class="source-tag">${esc(tag)}</span>`).join('') || '—'}</td><td>${source.last_ok ? esc(age(source.last_ok)) : '—'}</td><td><span class="source-status ${kind}"><i class="${kind}"></i>${esc(text)}</span></td></tr>`;
  }).join('');
}

function bindEvents() {
  $$('.nav-item').forEach(button => button.addEventListener('click', () => switchView(button.dataset.view)));
  $$('[data-view-target]').forEach(button => button.addEventListener('click', () => switchView(button.dataset.viewTarget)));
  $('#browse-button').addEventListener('click', () => switchView('discover'));
  $('#hero-review').addEventListener('click', () => { $('#high-fit-filter').checked = true; state.page = 1; switchView('discover'); });
  $('.mobile-menu').addEventListener('click', () => $('.sidebar').classList.toggle('open'));
  $('#refresh-button').addEventListener('click', () => { loadOverview(); if (state.view === 'discover') loadJobs(); toast('Refreshing local data'); });

  let searchTimer;
  $('#search-input').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { state.page = 1; loadJobs(); }, 250); });
  ['location-filter','seniority-filter','sort-filter','startup-filter','fresh-filter','high-fit-filter'].forEach(id => $(`#${id}`).addEventListener('change', () => { state.page = 1; loadJobs(); }));
  $$('#track-tabs button').forEach(button => button.addEventListener('click', () => { $$('#track-tabs button').forEach(b => b.classList.remove('active')); button.classList.add('active'); state.track = button.dataset.track; state.page = 1; loadJobs(); }));
  $('#pagination').addEventListener('click', event => { const page = event.target.dataset.page; if (page) { state.page = Number(page); loadJobs(); window.scrollTo({top: 0, behavior:'smooth'}); } });
  $('#job-list').addEventListener('click', event => {
    const card = event.target.closest('.job-card'); if (!card) return;
    const job = state.jobs[Number(card.dataset.index)];
    if (event.target.closest('.save-button')) job.status === 'untracked' ? quickSave(job) : openApplication(job);
  });
  $('#kanban').addEventListener('click', event => { const card = event.target.closest('.pipeline-card'); if (card) openApplication(state.pipeline[card.dataset.status][Number(card.dataset.index)]); });
  $('#application-form').addEventListener('submit', event => { event.preventDefault(); if (event.submitter?.value === 'save') saveDialog(); else $('#application-dialog').close(); });
  $('#delete-application').addEventListener('click', deleteApplication);
  $('#source-search').addEventListener('input', renderSources);
}

function init() {
  const now = new Date();
  $('#eyebrow-date').textContent = now.toLocaleDateString(undefined, {weekday:'long', month:'short', day:'numeric'});
  const hour = now.getHours();
  $('.hero h1').innerHTML = `${hour < 12 ? 'Good morning' : hour < 18 ? 'Good afternoon' : 'Good evening'}, Shaun.<br><em>Let’s find the right move.</em>`;
  bindEvents();
  loadOverview();
  const initial = location.hash.slice(1);
  if (titles[initial]) switchView(initial);
}

init();
