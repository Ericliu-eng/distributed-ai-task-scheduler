const $ = (selector) => document.querySelector(selector);
let currentFilter = 'all';

const esc = (value) => (value ?? '').toString().replace(/[&<>'"]/g, (c) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
}[c]));

function toast(message) {
  const el = $('#toast');
  el.textContent = message;
  el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 2600);
}

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

function renderMetrics(m) {
  $('#mTotal').textContent = m.total;
  $('#mActive').textContent = `${m.queued + m.running} active now`;
  $('#mSuccess').innerHTML = `${m.success_rate}<span>%</span>`;
  $('#mSaved').innerHTML = `${m.cost_saved_pct}<span>%</span>`;
  $('#mLatency').textContent = m.avg_latency_ms ? `${m.avg_latency_ms}ms` : '—';
  $('#smallCount').textContent = m.small;
  $('#largeCount').textContent = m.large;
  $('#routeRatio').textContent = `${m.small} / ${m.large}`;
  const total = m.small + m.large;
  $('#smallBar').style.width = (total ? m.small / total * 100 : 0) + '%';
  $('#largeBar').style.width = (total ? m.large / total * 100 : 0) + '%';
}

function renderWorkers(workers) {
  $('#workers').innerHTML = workers.map((w) => `
    <div class="worker">
      <div class="worker-icon">${w.tier === 'small' ? 'S' : 'L'}</div>
      <p><b>${esc(w.id)}</b><small>${w.tier.toUpperCase()} MODEL · ${w.task_id ? 'task ' + w.task_id.slice(0, 8) : 'awaiting task'}</small></p>
      <span class="status ${w.status}">${w.status.toUpperCase()}</span>
    </div>`).join('');
}

function renderTasks(tasks) {
  const shown = currentFilter === 'all' ? tasks : tasks.filter((t) => t.status === currentFilter);
  if (!shown.length) {
    $('#taskRows').innerHTML = '<tr><td colspan="6" class="empty">No matching tasks.</td></tr>';
    return;
  }
  $('#taskRows').innerHTML = shown.map((t) => `
    <tr title="${esc(t.route_reason)}">
      <td>
        <div class="task-name">${esc(t.prompt)}</div>
        <div class="task-id">${t.id.slice(0, 8)} · ${esc(t.route_reason)}</div>
      </td>
      <td><span class="badge ${t.route_tier}">${t.route_tier}</span></td>
      <td>${t.sla}</td>
      <td><span class="badge ${t.status}">${t.status}</span></td>
      <td>${t.attempt} / ${t.max_attempts}</td>
      <td>${t.run_ms !== null ? t.run_ms + 'ms' : '—'}</td>
    </tr>`).join('');
}

function renderEvents(events) {
  if (!events.length) {
    $('#events').innerHTML = '<p class="empty">Waiting for activity…</p>';
    return;
  }
  const time = (iso) => new Date(iso).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'});
  $('#events').innerHTML = events.map((e) => `
    <div class="event ${e.kind}">
      <time>${time(e.created_at)}</time>
      <b>${e.kind}</b>
      <span>${esc(e.message)}</span>
    </div>`).join('');
}

async function refresh() {
  try {
    const [metrics, workers, tasks, events] = await Promise.all([
      api('/metrics/summary'), api('/workers'), api('/tasks'), api('/events'),
    ]);
    renderMetrics(metrics);
    renderWorkers(workers);
    renderTasks(tasks);
    renderEvents(events);
  } catch (err) {
    console.error(err);
  }
}

$('#priority').addEventListener('input', (e) => {
  $('#priorityValue').textContent = e.target.value;
});

$('#taskForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const button = e.submitter;
  button.disabled = true;
  try {
    const result = await api('/tasks', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        prompt: $('#prompt').value,
        sla: $('#sla').value,
        priority: +$('#priority').value,
        fail_once: $('#failOnce').checked,
        idempotency_key: 'ui-' + Date.now(),
      }),
    });
    const box = $('#decision');
    box.classList.remove('hidden');
    box.innerHTML = `<b>${result.route_tier.toUpperCase()} TIER SELECTED</b><br>${esc(result.route_reason)}`;
    toast('Task dispatched · ' + result.task_id.slice(0, 8));
    refresh();
  } catch (err) {
    toast('Dispatch failed');
  } finally {
    button.disabled = false;
  }
});

$('#demoBtn').addEventListener('click', async (e) => {
  const demoButton = e.currentTarget;
  demoButton.disabled = true;
  demoButton.textContent = 'Running demo…';
  try {
    const result = await api('/demo/reset', {method: 'POST'});
    toast(`${result.seeded} demo tasks dispatched`);
    await refresh();
  } finally {
    setTimeout(() => {
      demoButton.disabled = false;
      demoButton.textContent = '▶ Run 90-sec demo';
    }, 1700);
  }
});

document.querySelectorAll('.filter').forEach((button) => button.addEventListener('click', () => {
  document.querySelectorAll('.filter').forEach((other) => other.classList.remove('active'));
  button.classList.add('active');
  currentFilter = button.dataset.status;
  refresh();
}));

document.addEventListener('keydown', (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') $('#taskForm').requestSubmit();
});

refresh();
setInterval(refresh, 850);
