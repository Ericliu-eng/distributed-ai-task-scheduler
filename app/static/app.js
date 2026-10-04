const $ = (selector) => document.querySelector(selector);
const esc = (value) => (value ?? '').toString().replace(/[&<>'"]/g, (c) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
}[c]));
const number = (value, digits = 0) => Number(value).toLocaleString(undefined, {maximumFractionDigits: digits});
const duration = (ms) => ms == null ? '—' : ms < 1000 ? number(ms) + ' ms' : number(ms / 1000, 2) + ' s';
const titleCase = (value) => value ? value[0].toUpperCase() + value.slice(1) : 'Unknown';
const stateClasses = new Set(['queued', 'running', 'succeeded', 'failed', 'idle', 'busy', 'offline']);
const stateClass = (value) => stateClasses.has(value) ? value : '';
// SQLite returns UTC timestamps without an offset; PostgreSQL includes one.
const eventDate = (iso) => new Date(/(?:Z|[+-]\d{2}:\d{2})$/i.test(iso) ? iso : iso + 'Z');
let currentFilter = 'all';
let expandedTaskId = null;
let cachedTasks = [];
let cachedEvents = [];
let refreshPending = null;
let toastTimer;
let submitting = false;
const rendered = new WeakMap();

// Preserve DOM nodes, selection and keyboard focus when a poll brings no changes.
function setHTML(selector, html) {
  const element = $(selector);
  if (rendered.get(element) === html) return;
  const focusedTask = element.contains(document.activeElement) ? document.activeElement.dataset.taskId : null;
  element.innerHTML = html;
  rendered.set(element, html);
  if (focusedTask) {
    element.querySelector('[data-task-id="' + CSS.escape(focusedTask) + '"]')?.focus({preventScroll: true});
  }
}

function toast(message) {
  clearTimeout(toastTimer);
  $('#toast').textContent = message;
  $('#toast').classList.add('show');
  toastTimer = setTimeout(() => $('#toast').classList.remove('show'), 3500);
}

async function api(path, options = {}) {
  const response = await fetch(path, {...options, signal: AbortSignal.timeout(8000)});
  if (!response.ok) throw new Error('Request failed: ' + response.status);
  return response.json();
}

const views = {
  overview: ['Inference overview', 'Monitor performance, then drill into individual tasks.'],
  activity: ['Activity', 'Trace routing decisions, retries and recovery events.'],
  benchmarks: ['Benchmarks', 'Review saved measurements and their test conditions.'],
};

function showView(name) {
  const view = Object.hasOwn(views, name) ? name : 'overview';
  document.querySelectorAll('.view').forEach((section) => { section.hidden = section.id !== view; });
  document.querySelectorAll('[data-view]').forEach((link) => {
    if (link.dataset.view === view) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
  $('#pageTitle').textContent = views[view][0];
  $('#pageDescription').textContent = views[view][1];
  document.title = 'Orbit · ' + views[view][0];
}

function navigate(view) {
  if (location.hash !== '#' + view) history.pushState(null, '', '#' + view);
  showView(view);
}

document.querySelectorAll('[data-view], .brand').forEach((link) => link.addEventListener('click', (event) => {
  event.preventDefault();
  navigate(link.dataset.view || 'overview');
}));
window.addEventListener('hashchange', () => {
  if (location.hash !== '#main') showView(location.hash.slice(1));
});
window.addEventListener('popstate', () => showView(location.hash.slice(1)));

function renderMetrics(m) {
  $('#mActive').textContent = number(m.queued + m.running);
  $('#mQueueDetail').textContent = number(m.queued) + ' queued · ' + number(m.running) + ' running';
  $('#mCompleted').textContent = number(m.succeeded);
  $('#mSuccess').textContent = m.succeeded + m.failed ? number(m.success_rate, 1) + '% of finished tasks succeeded' : 'No finished tasks yet';
  $('#mFailed').textContent = number(m.failed);
  $('#mFailed').classList.toggle('has-failures', m.failed > 0);
  $('#mRecovery').textContent = number(m.retries) + (m.retries === 1 ? ' retry' : ' retries') + ' · ' + number(m.recoveries) + (m.recoveries === 1 ? ' recovery' : ' recoveries');
  $('#mSaved').textContent = m.total ? number(m.cost_saved_pct, 1) + '%' : '—';
  $('#mLatency').textContent = m.succeeded + m.failed ? duration(m.avg_latency_ms) : '—';
  $('#smallCount').textContent = number(m.small);
  $('#largeCount').textContent = number(m.large);
  const total = m.small + m.large;
  $('#smallBar').style.width = (total ? m.small / total * 100 : 0) + '%';
  $('#largeBar').style.width = (total ? m.large / total * 100 : 0) + '%';
  $('#routingBar').setAttribute('aria-label', total ? m.small + ' small-tier tasks and ' + m.large + ' large-tier tasks' : 'No routed tasks');
}

function setSystemState(state, label) {
  $('#systemCard').className = 'connection ' + state;
  $('#systemLabel').textContent = label;
}

function renderWorkers(workers) {
  const online = workers.filter((w) => w.status !== 'offline');
  $('#workerCount').textContent = online.length + ' / ' + workers.length;
  $('#workerDetail').textContent = online.length ? online.filter((w) => w.status === 'busy').length + ' busy · ' + online.filter((w) => w.status === 'idle').length + ' idle' : 'No active workers';
  const hasBothTiers = ['small', 'large'].every((tier) => online.some((w) => w.tier === tier));
  setSystemState(hasBothTiers ? 'online' : 'degraded', hasBothTiers ? 'System online' : online.length ? 'Partial worker coverage' : 'No workers online');
  setHTML('#workers', workers.length ? workers.map((w) => '<div class="worker"><div class="worker-icon" aria-hidden="true">' + (w.tier === 'small' ? 'S' : 'L') + '</div><p><b>' + esc(w.id) + '</b><small>' + esc(titleCase(w.tier)) + ' tier · ' + (w.status === 'offline' ? 'Heartbeat unavailable' : w.task_id ? 'Task ' + esc(w.task_id.slice(0, 8)) : 'Ready for tasks') + '</small></p><span class="state ' + stateClass(w.status) + '">' + esc(titleCase(w.status)) + '</span></div>').join('') : '<p class="empty">No workers registered. Start a worker to process queued tasks.</p>');
}

function details(task) {
  const item = (label, value, className = '') => '<div class="' + className + '"><dt>' + label + '</dt><dd>' + esc(value) + '</dd></div>';
  return '<dl class="task-details">' +
    item('Prompt', task.prompt, 'full-width') +
    item('Routing decision', task.route_reason, 'full-width') +
    item('Model tier', titleCase(task.route_tier)) +
    item('Service level / priority', titleCase(task.sla) + ' / ' + task.priority) +
    item('Worker', task.worker_id || 'Not assigned') +
    item('Attempts / recoveries', task.attempt + ' of ' + task.max_attempts + ' / ' + task.recovery_count) +
    item('Time to latest start / execution', duration(task.queue_ms) + ' / ' + duration(task.run_ms)) +
    (task.error ? item('Last error', task.error, 'full-width') : '') +
    item('Result', task.result || 'No result yet.', 'full-width detail-result') +
    '</dl>';
}

function renderTasks() {
  const query = $('#taskSearch').value.trim().toLowerCase();
  const shown = cachedTasks.filter((task) =>
    (currentFilter === 'all' || task.status === currentFilter) &&
    (!query || task.prompt.toLowerCase().includes(query) || task.id.toLowerCase().includes(query)));
  $('#taskCount').textContent = shown.length + (shown.length === 1 ? ' task' : ' tasks');
  $('#queueNote').textContent = 'Showing ' + shown.length + ' of ' + cachedTasks.length + ' recent tasks · Latest 100';
  if (!shown.length) {
    setHTML('#taskRows', '<tr><td colspan="6" class="empty"><strong>' + (cachedTasks.length ? 'No matching tasks' : 'Your queue is clear') + '</strong>' + (cachedTasks.length ? 'Try another search or status filter.' : 'Create a task or run the demo to get started.') + '</td></tr>');
    return;
  }
  setHTML('#taskRows', shown.map((task) => {
    const open = expandedTaskId === task.id;
    const id = esc(task.id);
    const total = task.queue_ms != null && task.run_ms != null ? task.queue_ms + task.run_ms : null;
    return '<tr class="task-row' + (open ? ' expanded' : '') + '"><td><div class="task-name" title="' + esc(task.prompt) + '">' + esc(task.prompt) + '</div><div class="task-id">' + esc(task.id.slice(0, 8)) + '</div></td>' +
      '<td class="tier">' + esc(titleCase(task.route_tier)) + '</td><td><span class="state ' + stateClass(task.status) + '">' + esc(titleCase(task.status)) + '</span></td>' +
      '<td class="mono">' + task.attempt + ' / ' + task.max_attempts + '</td><td class="mono">' + duration(total) + '</td>' +
      '<td><button class="detail-toggle" data-task-id="' + id + '" aria-expanded="' + open + '" aria-controls="detail-' + id + '" aria-label="' + (open ? 'Hide' : 'View') + ' details for task ' + esc(task.id.slice(0, 8)) + '">' + (open ? '−' : '+') + '</button></td></tr>' +
      '<tr id="detail-' + id + '"' + (open ? '' : ' hidden') + '><td colspan="6" class="detail-cell">' + (open ? details(task) : '') + '</td></tr>';
  }).join(''));
}

function renderEvents() {
  const filter = $('#eventFilter').value;
  const events = cachedEvents.filter((event) => filter === 'all' || (filter === 'issues' ? ['retry', 'failed'].includes(event.kind) : event.kind === 'recovered'));
  const time = (iso) => eventDate(iso).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'});
  setHTML('#events', events.length ? events.map((event) => '<div class="event ' + (['retry', 'failed', 'recovered'].includes(event.kind) ? event.kind : '') + '"><time title="' + esc(eventDate(event.created_at).toLocaleString()) + '">' + esc(time(event.created_at)) + '</time><b>' + esc(titleCase(event.kind)) + '</b><span>' + esc(event.message) + '</span></div>').join('') : '<p class="empty">' + (filter === 'all' ? 'No activity yet. Events will appear as tasks are processed.' : 'No matching events in the latest 30 records.') + '</p>');
}

function refresh() {
  if (refreshPending) return refreshPending;
  refreshPending = (async () => {
    try {
      const [metrics, workers, tasks, events] = await Promise.all([
        api('/metrics/summary'), api('/workers'), api('/tasks'), api('/events'),
      ]);
      cachedTasks = tasks;
      cachedEvents = events;
      renderMetrics(metrics);
      renderWorkers(workers);
      renderTasks();
      renderEvents();
      $('#connectionNotice').hidden = true;
      $('#lastUpdated').textContent = 'Updated ' + new Date().toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
    } catch (error) {
      $('#connectionNotice').hidden = false;
      setSystemState('offline', 'Connection lost');
    } finally {
      refreshPending = null;
    }
  })();
  return refreshPending;
}

function renderPerformance(report) {
  $('#perfDatabase').textContent = report.database;
  $('#perfThroughput').innerHTML = number(report.throughput_tasks_per_second, 1) + '<span>tasks/s</span>';
  $('#perfCompleted').textContent = number(report.tasks_succeeded) + ' / ' + number(report.tasks_submitted);
  $('#perfP50').textContent = duration(report.queue_latency_ms.p50);
  $('#perfP99').textContent = duration(report.queue_latency_ms.p99);
  $('#perfMeta').textContent = report.workers + ' workers · ' + report.workload + ' · ' + report.execution_mode + '. Queue latency includes time waiting behind the burst. Measured ' + new Date(report.measured_at).toLocaleDateString() + ' on ' + report.platform + '.';
}

let trendRequest = 0;
function renderTrends(report) {
  $('#rateValue').textContent = number(report.summary.throughput_per_minute, 2);
  $('#queueValue').textContent = number(report.summary.peak_queued);
  $('#latencyValue').textContent = duration(report.summary.latency_p95_ms);
  $('#rateNote').textContent = number(report.summary.completed) + ' completed';
  $('#intervalNote').textContent = report.interval_seconds + '-second intervals · Queue peaks · Successful-task latency';
  const count = (value) => number(value, 1);
  const draw = (id, lines, format, readout) => OrbitCharts.render($(id), report, lines, format, $(readout));
  draw('#rateChart', [{key: 'throughput_per_minute', label: 'Succeeded / min', color: '#527acc'}], count, '#rateReadout');
  draw('#queueChart', [
    {key: 'queued', label: 'Queued', color: '#b58334'},
    {key: 'running', label: 'Running', color: '#527acc'},
  ], count, '#queueReadout');
  draw('#latencyChart', [
    {key: 'latency_p50_ms', label: 'p50', color: '#aaa0ce', dots: true},
    {key: 'latency_p95_ms', label: 'p95', color: '#7962ac', dots: true},
  ], duration, '#latencyReadout');
  setHTML('#trendRows', report.points.map((point) => '<tr><td>' +
    esc(new Date(point.at).toLocaleTimeString()) + '</td><td>' + point.completed + '</td><td>' +
    number(point.throughput_per_minute, 2) + '</td><td>' + point.queued + '</td><td>' + point.running +
    '</td><td>' + duration(point.latency_p50_ms) + '</td><td>' + duration(point.latency_p95_ms) + '</td></tr>').join(''));
  $('#trendUpdated').textContent = 'Updated ' + new Date(report.measured_at).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'});
}

async function loadTrends() {
  const request = ++trendRequest;
  const minutes = $('#trendRange').value;
  $('#trendGrid').setAttribute('aria-busy', 'true');
  try {
    const report = await api('/metrics/timeseries?minutes=' + minutes);
    if (request !== trendRequest) return;
    renderTrends(report);
    $('#trendNotice').hidden = true;
  } catch (error) {
    if (request !== trendRequest) return;
    $('#trendNotice').hidden = false;
    $('#trendNotice').textContent = 'Trend data is unavailable for the selected range. Existing charts may be out of date. Reconnecting automatically…';
  } finally {
    if (request === trendRequest) $('#trendGrid').setAttribute('aria-busy', 'false');
  }
}

$('#trendRange').addEventListener('change', () => {
  $('#trendUpdated').textContent = 'Loading selected range…';
  loadTrends();
});

async function pollTrends() {
  if (!document.hidden) await loadTrends();
  setTimeout(pollTrends, 5000);
}
pollTrends();

function renderRouting(report) {
  $('#routingProvider').textContent = report.provider;
  setHTML('#routingRows', report.threshold_runs.map((run) => '<tr><td>' + Number(run.difficulty_threshold).toFixed(2) + '</td><td>' + number(run.quality_retention_pct, 2) + '%</td><td>' + number(run.cost_saving_pct, 2) + '%</td><td>' + number(run.routing.small) + ' / ' + number(run.routing.large) + '</td></tr>').join(''));
  $('#routingMeta').textContent = report.dataset_size + ' graded cases against an all-large baseline (' + report.baseline.correct + ' correct). ' + report.limitations[0];
}

async function loadReport(kind, render, unavailable) {
  try { render(await api('/benchmarks/' + kind)); }
  catch (error) { unavailable(); }
}

function setComposer(open) {
  $('#composer').hidden = !open;
  $('#newTaskBtn').setAttribute('aria-expanded', String(open));
  (open ? $('#prompt') : $('#newTaskBtn')).focus({preventScroll: !open});
}
$('#newTaskBtn').addEventListener('click', () => setComposer($('#composer').hidden));
$('#closeComposer').addEventListener('click', () => setComposer(false));
$('#priority').addEventListener('input', (event) => { $('#priorityValue').textContent = event.target.value; });

$('#taskForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (submitting) return;
  submitting = true;
  $('#submitTask').disabled = true;
  $('#submitTask').textContent = 'Dispatching…';
  $('#formFeedback').hidden = true;
  try {
    const result = await api('/tasks', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        prompt: $('#prompt').value, sla: $('#sla').value, priority: +$('#priority').value,
        fail_once: $('#failOnce').checked, idempotency_key: 'ui-' + crypto.randomUUID(),
      }),
    });
    currentFilter = 'all';
    $('#taskSearch').value = '';
    updateFilters();
    navigate('overview');
    setComposer(false);
    await refresh();
    toast('Task dispatched · ' + titleCase(result.route_tier) + ' tier · ' + result.task_id.slice(0, 8));
  } catch (error) {
    $('#formFeedback').textContent = 'Could not confirm dispatch. Check the queue before trying again.';
    $('#formFeedback').className = 'form-feedback error';
    $('#formFeedback').hidden = false;
  } finally {
    submitting = false;
    $('#submitTask').disabled = false;
    $('#submitTask').textContent = 'Dispatch task';
  }
});

$('#demoBtn').addEventListener('click', () => {
  $('#demoDialog').returnValue = '';
  $('#demoDialog').showModal();
});
$('#demoDialog').addEventListener('close', async () => {
  if ($('#demoDialog').returnValue !== 'confirm') return;
  const button = $('#demoBtn');
  button.disabled = true;
  button.textContent = 'Starting…';
  try {
    const result = await api('/demo/reset', {method: 'POST'});
    currentFilter = 'all';
    expandedTaskId = null;
    $('#taskSearch').value = '';
    updateFilters();
    navigate('overview');
    await refresh();
    toast(result.seeded + ' demo tasks dispatched');
  } catch (error) {
    toast('Could not start the demo. Check the API connection.');
  } finally {
    button.disabled = false;
    button.textContent = 'Run demo';
  }
});

function updateFilters() {
  document.querySelectorAll('.filter').forEach((button) => {
    const selected = button.dataset.status === currentFilter;
    button.classList.toggle('active', selected);
    button.setAttribute('aria-pressed', String(selected));
  });
}
document.querySelectorAll('.filter').forEach((button) => button.addEventListener('click', () => {
  currentFilter = button.dataset.status;
  updateFilters();
  renderTasks();
}));
$('#taskSearch').addEventListener('input', renderTasks);
$('#eventFilter').addEventListener('change', renderEvents);
$('#taskRows').addEventListener('click', (event) => {
  const button = event.target.closest('[data-task-id]');
  if (!button) return;
  expandedTaskId = expandedTaskId === button.dataset.taskId ? null : button.dataset.taskId;
  renderTasks();
});
document.addEventListener('keydown', (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key === 'Enter' && !$('#composer').hidden && !$('#demoDialog').open) {
    event.preventDefault();
    $('#taskForm').requestSubmit($('#submitTask'));
  }
  if (event.key === 'Escape' && !$('#composer').hidden && !$('#demoDialog').open) setComposer(false);
});

showView(location.hash.slice(1));
async function poll() {
  if (!document.hidden) await refresh();
  setTimeout(poll, 2000);
}
poll();
// Saved reports load independently, once, even when the live queue is unavailable.
loadReport('performance', renderPerformance, () => {
  $('#perfDatabase').textContent = 'Unavailable';
  $('#perfMeta').textContent = 'Saved performance report unavailable. Reload to try again.';
});
loadReport('routing', renderRouting, () => {
  $('#routingProvider').textContent = 'Unavailable';
  setHTML('#routingRows', '<tr><td colspan="4" class="empty">Saved routing report unavailable. Reload to try again.</td></tr>');
});
