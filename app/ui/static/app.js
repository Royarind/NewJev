'use strict';

// === State ===
const state = {
  ws: null,
  sessionId: null,
  running: false,
  stepCount: 0,
};

// === DOM ===
const $ = (id) => document.getElementById(id);
const messages = $('messages');
const inspector = $('inspector');
const goalInput = $('goal-input');
const urlInput = $('url-input');
const modeSelect = $('mode-select');
const sendBtn = $('send-btn');
const connDot = $('conn-dot');
const connLabel = $('conn-label');
const configBtn = $('config-btn');
const configModal = $('config-modal');
const modalClose = $('modal-close');
const configBody = $('config-body');
const clearInspector = $('clear-inspector');

// === WebSocket ===
function connect() {
  state.sessionId = 'sess_' + Math.random().toString(36).slice(2, 10);
  const wsUrl = `ws://${location.host}/ws/${state.sessionId}`;
  state.ws = new WebSocket(wsUrl);

  state.ws.onopen = () => {
    setConnStatus('connected', 'Connected');
  };

  state.ws.onclose = () => {
    setConnStatus('disconnected', 'Disconnected');
    setTimeout(connect, 3000);
  };

  state.ws.onerror = () => {
    setConnStatus('disconnected', 'Error');
  };

  state.ws.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      handleEvent(msg.event, msg.data);
    } catch (_) {}
  };
}

function send(command, data = {}) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify({ command, ...data }));
  }
}

// === Event Handler ===
function handleEvent(event, data) {
  switch (event) {
    case 'config':
      state.config = data;
      break;

    case 'plan':
      addMessage('assistant', [
        `<strong>Plan${data.from_recipe ? ' (reused from an earlier verified run — no planner call)' : ''}:</strong> ${escHtml(data.goal)}`,
        data.steps && data.steps.length > 1
          ? '<ol>' + data.steps.map(s => `<li>${escHtml(s)}</li>`).join('') + '</ol>'
          : '',
        data.url ? `<span style="color:#8b949e;font-size:11px">URL: ${escHtml(data.url)}</span>` : '',
        `<span style="color:#8b949e;font-size:11px">Mode: ${escHtml(data.capture_mode)}</span>`,
      ].filter(Boolean).join('<br/>'));
      break;

    case 'status':
      updateStatus(data.status, data.message);
      addStatusMessage(data.message || data.status);
      break;

    case 'phase':
      addStatusMessage(data.total ? `Phase ${data.index}/${data.total} [${data.mode}]: ${data.task}` : `${data.task}…`);
      break;

    case 'confirm_request':
      addRequestCard(data, 'confirm');
      break;

    case 'handoff':
      addRequestCard(data, 'handoff');
      break;

    case 'rescue_request':
      addHintCard(data);
      break;

    case 'rescue':
      if (data.by === 'llm') {
        const what = data.kind === 'actions' ? (data.actions || []).map(a => `${a.operation} '${a.label}'`).join(', ')
          : data.kind === 'replan' ? 'a new procedure' : data.kind === 'needs_person' ? 'needs you: ' + (data.question || '') : 'no idea';
        addStatusMessage(`\ud83d\udee0\ufe0f Rescue model: ${what}${data.why ? ' — ' + data.why : ''}`);
      }
      break;

    case 'job': {
      state.jobCard = addMessage('assistant',
        `<strong>\ud83d\udd01 Long job</strong> — ${escHtml(data.request)}<br/>` +
        `<span class="usage-line">Each item: ${escHtml(data.per_item)}${data.after ? '<br/>Then: ' + escHtml(data.after) : ''}</span>` +
        `<div class="job-bar"><div class="job-fill" style="width:${data.total ? Math.round(100 * data.done / data.total) : 0}%"></div></div>` +
        `<div class="job-line">${data.done}/${data.total} done${data.failed ? ', ' + data.failed + ' failed' : ''} · ${data.total} items</div>` +
        `<span class="usage-line">Progress is saved after every item — if anything stops, resume it from Tasks.</span>`);
      break;
    }

    case 'job_progress': {
      if (state.jobCard) {
        const pct = data.total ? Math.round(100 * data.done / data.total) : 0;
        state.jobCard.querySelector('.job-fill').style.width = pct + '%';
        state.jobCard.querySelector('.job-line').textContent =
          `${data.done}/${data.total} done${data.failed ? ', ' + data.failed + ' failed' : ''}` +
          (data.item ? ` · ${data.state}: ${data.item}${data.index ? ' (' + data.index + '/' + data.total + ')' : ''}` : ` · ${data.state}`);
      }
      break;
    }

    case 'paused':
      addStatusMessage('\u23f8\ufe0f Paused — you took over (' + (data.what || 'mouse/keyboard') + ')');
      break;

    case 'takeover_request':
      addRequestCard(data, 'takeover');
      break;

    case 'quick_goal':
      if (state.running) { send('steer', { text: data.goal }); addMessage('user steer', '\ud83e\udded ' + escHtml(data.goal)); }
      else startRun(data.goal);
      break;

    case 'connections_changed':
      if (data.connected) addStatusMessage('\ud83d\udd0c Connected: ' + (data.what || data.provider));
      if (panel.style.display !== 'none' && panelTitle.textContent === 'Connections') showConnections();
      break;

    case 'info':
      addStatusMessage('\u2139\ufe0f ' + (data.message || ''));
      break;

    case 'steer_ack':
      addStatusMessage('\ud83e\udded Applying your instruction: ' + (data.message || ''));
      break;

    case 'run_started':
      state.runId = data.run_id;
      break;

    case 'task_queued':
      addStatusMessage(`\ud83d\udccb Task queued: ${data.task} — ${data.goal}`);
      break;

    case 'task_started':
      addStatusMessage(`\u23f0 Scheduled task started (${data.trigger}): ${data.task} — ${data.goal}`);
      break;

    case 'task_event':
      if (data.event === 'status' && data.data && ['blocked', 'needs_user', 'error'].includes(data.data.status))
        addStatusMessage(`\u23f0 ${data.task}: ${data.data.message || data.data.status}`);
      break;

    case 'task_result': {
      const ok = data.status === 'done';
      addMessage('result', `${ok ? '\u2705' : '\u26a0\ufe0f'} <strong>Scheduled task ${escHtml(data.task)}</strong> ` +
        `(${escHtml(data.trigger)}): ${escHtml((data.status || '').toUpperCase())}` +
        (data.error ? ` — ${escHtml(data.error)}` : '') + `<br/>${escHtml(data.goal || '')}` +
        (data.trace ? `<br/><a href="#" class="open-run" data-name="${escHtml(data.trace)}">View run</a>` : ''));
      break;
    }

    case 'request_closed':
      closeRequestCard(data.request_id);
      break;

    case 'validation':
      break;

    case 'procedure': {
      const src = { recipe: ' (learned from an earlier run)', rescue: ' (from the rescue model)', user: ' (updated for your instruction)' }[data.source] || (data.revision ? ' (revised)' : '');
      const html = `<strong>\ud83d\udccb Procedure${src}</strong><ol class="proc-list">` +
        data.steps.map((s, i) => `<li class="${data.done[i] ? 'proc-done' : ''}">${escHtml(s)}</li>`).join('') + '</ol>';
      if (state.procCard && state.procRevision === data.revision) {
        state.procCard.querySelector('.msg-bubble').innerHTML = html;
      } else {
        state.procCard = addMessage('assistant', html);
        state.procRevision = data.revision;
      }
      break;
    }

    case 'step':
      state.stepCount++;
      addInspectorStep(data);
      break;

    case 'report':
      addMessage('assistant', `<strong>\ud83d\udcca Extracted Report / Answer:</strong><br/><div style="margin-top:6px;white-space:pre-wrap;background:rgba(255,255,255,0.05);padding:8px 12px;border-radius:6px;border-left:3px solid #58a6ff">${escHtml(data.text || '')}</div>`);
      break;

    case 'result': {
      const icons = { done: '\u2705', unverified: '\u26a0\ufe0f', blocked: '\u26d4', needs_user: '\ud83d\ude4b',
        stopped: '\u23f9\ufe0f', error: '\u274c', max_steps: '\u23f0' };
      const icon = icons[data.status] || '\u2139\ufe0f';
      const phases = (data.phases || []).length > 1
        ? '<ol class="phase-list">' + data.phases.map(p =>
            `<li><span class="phase-status ps-${escHtml(p.status)}">${escHtml(p.status)}</span> [${escHtml(p.mode)}] ${escHtml(p.task)}</li>`).join('') + '</ol>'
        : '';
      const u = data.usage || {};
      const models = (u.models || []).map(m =>
        `${escHtml(m.role)} ${escHtml(m.model)}: ${m.calls}× ${(m.time_ms / 1000).toFixed(1)}s` +
        (m.errors ? ` (${m.errors} failed)` : '') + (m.cost_known && m.cost_usd ? ` $${m.cost_usd.toFixed(4)}` : '')).join(' · ');
      const cost = u.cost_usd ? ` &nbsp; Cost: $${u.cost_usd.toFixed(4)}${u.cost_complete ? '' : '+'}` : '';
      const rs = data.rescue_stats || {};
      const rescues = (rs.llm || rs.user_hints) ? ` &nbsp; Rescues: ${rs.llm_helpful || 0}/${rs.llm || 0} model, ${rs.user_hints || 0} hints` : '';
      const recipe = (data.recipes_used || []).length ? ' &nbsp; \ud83e\udde0 used a learned recipe' : '';
      const reportBlock = data.final_report ? `<div style="margin:8px 0;white-space:pre-wrap;background:rgba(56,139,253,0.1);padding:10px 14px;border-radius:6px;border-left:3px solid #388bfd"><strong>\ud83d\udcca Result Report:</strong><br/>${escHtml(data.final_report)}</div>` : '';
      const div = addMessage('result',
        `${icon} <strong>${escHtml(data.status.toUpperCase())}</strong> — ${escHtml(data.goal)}<br/>` +
        `Steps: ${data.steps} &nbsp; Time: ${(data.elapsed_ms / 1000).toFixed(1)}s` + cost + rescues + recipe + reportBlock + phases +
        (models ? `<div class="usage-line">${models}${u.fallbacks ? ` · ${u.fallbacks} fallback(s)` : ''}</div>` : '') +
        `<div class="panel-actions">` +
        (data.trace ? `<button class="secondary open-run" data-name="${escHtml(data.trace)}">View details</button>` : '') +
        `<button class="secondary run-again">Run again</button><button class="secondary save-task">Save as task</button></div>`
      );
      const req = data.user_request || data.goal;
      div.querySelector('.run-again').addEventListener('click', () => startRun(req));
      div.querySelector('.save-task').addEventListener('click', () => openTaskForm({ template: req, name: req.slice(0, 40) }));
      setRunning(false);
      break;
    }

    case 'error':
      addMessage('error', '\u274C ' + escHtml(data.message || 'Unknown error'));
      setRunning(false);
      break;

    case 'warning':
      addStatusMessage('\u26a0\ufe0f ' + (data.message || ''));
      break;

    case 'pong':
      break;
  }
}

// === UI Helpers ===
function setConnStatus(type, label) {
  connDot.className = 'status-dot ' + (type === 'disconnected' ? 'disconnected' : '');
  connLabel.textContent = label;
}

function updateStatus(status, message) {
  const statusMap = {
    planning: { cls: 'planning', label: 'Planning' },
    running: { cls: 'running', label: 'Running' },
    starting_browser: { cls: 'running', label: 'Starting' },
    done: { cls: '', label: 'Done' },
    unverified: { cls: 'planning', label: 'Unverified' },
    needs_user: { cls: 'planning', label: 'Your turn' },
    stopped: { cls: '', label: 'Stopped' },
    max_steps: { cls: 'disconnected', label: 'Step limit' },
    closed: { cls: '', label: 'Closed' },
    blocked: { cls: 'disconnected', label: 'Blocked' },
    error: { cls: 'disconnected', label: 'Error' },
    stopping: { cls: 'running', label: 'Stopping' },
    connected: { cls: '', label: 'Connected' },
  };
  const s = statusMap[status] || { cls: '', label: status };
  connDot.className = 'status-dot ' + s.cls;
  connLabel.textContent = s.label;
}

function setRunning(running) {
  state.running = running;
  sendBtn.disabled = false;
  if (running) {
    sendBtn.textContent = '\u25A0 Stop';
    sendBtn.className = 'btn-send running';
    goalInput.placeholder = 'Steer the run: type an instruction (e.g. "use the second sheet") and press Enter';
  } else {
    sendBtn.innerHTML = '&#9654; Run';
    sendBtn.className = 'btn-send';
    goalInput.placeholder = 'Type your goal here... (Enter to send, Shift+Enter for newline)';
    goalInput.disabled = false;
    goalInput.focus();
  }
}

function addMessage(type, html) {
  const div = document.createElement('div');
  const isStatus = type === 'status';
  div.className = `message ${type}`;
  if (!isStatus) {
    div.innerHTML = `
      <div class="msg-icon">${type.startsWith('user') ? '\uD83D\uDC64' : '\u26A1'}</div>
      <div class="msg-bubble">${html}</div>
    `;
  } else {
    div.innerHTML = `<div class="msg-bubble">${html}</div>`;
  }
  messages.appendChild(div);
  messages.scrollTop = messages.scrollHeight;
  return div;
}

function addStatusMessage(text) {
  if (!text) return;
  addMessage('status', `<span style="color:#8b949e;font-size:12px">&#9679; ${escHtml(text)}</span>`);
}

function addInspectorStep(data) {
  const empty = inspector.querySelector('.inspector-empty');
  if (empty) empty.remove();

  const op = (data.operation || 'unknown').toLowerCase();
  const conf = data.effective_confidence ?? data.confidence ?? 0;
  const confClass = conf >= 0.85 ? 'conf-high' : conf >= 0.65 ? 'conf-med' : 'conf-low';
  const opClass = `op-${op}`;

  const opProbs = data.operation_probabilities || {};
  const topOps = Object.entries(opProbs)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 4);

  const probsHtml = topOps.map(([k, v]) => {
    const pct = Math.round(v * 100);
    const isSelected = k.toUpperCase() === (data.operation || '').toUpperCase();
    return `
      <div class="prob-row">
        <span class="prob-label">${escHtml(k)}</span>
        <div class="prob-bar-bg"><div class="prob-bar${isSelected ? ' selected' : ''}" style="width:${pct}%"></div></div>
        <span class="prob-val">${pct}%</span>
      </div>`;
  }).join('');

  const card = document.createElement('div');
  card.className = 'step-card';
  card.innerHTML = `
    <div class="step-header">
      <span class="step-num">Step ${data.step}</span>
      <span class="step-op ${opClass}">${escHtml(data.operation || '')}</span>
      <span class="step-confidence">
        <span class="${confClass}">${Math.round(conf * 100)}%</span>
      </span>
    </div>
    ${data.label || data.choice ? `<div class="step-target">${escHtml(data.label || data.choice)}</div>` : ''}
    <div class="step-meta">
      <span>&#9889; ${data.latency_ms || 0}ms</span>
      <span>&#8986; ${(data.elapsed_ms / 1000).toFixed(1)}s</span>
      ${data.title ? `<span title="${escHtml(data.url || '')}">${escHtml(data.title.slice(0, 24))}</span>` : ''}
    </div>
    ${probsHtml ? `<div class="step-probs">${probsHtml}</div>` : ''}
    ${(data.flags || []).includes('low_confidence') ? `<div class="step-flag">Low confidence${data.retries ? ` (after ${data.retries} retry)` : ''}</div>` : ''}
    ${data.text ? `<div class="step-text">\u270e ${escHtml(String(data.text).slice(0, 120))}</div>` : ''}
  `;

  inspector.insertBefore(card, inspector.firstChild);
}

function escHtml(str) {
  return String(str || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// === Confirmations & hand-offs ===
const openRequests = new Map();

function addRequestCard(data, type) {
  const isHandoff = type === 'handoff' || type === 'takeover';
  const title = type === 'takeover' ? '\u23f8\ufe0f Paused \u2014 you took over' : isHandoff ? '\ud83d\ude4b Your turn' :
    data.kind === 'delete' ? '\ud83d\uddd1\ufe0f Confirm deletion' :
    data.kind === 'send_email' ? '\u2709\ufe0f Confirm email' :
    data.kind === 'send_message' ? '\ud83d\udcac Confirm message' :
    data.kind === 'send_invite' ? '\ud83d\udcc5 Confirm meeting invite' :
    data.kind === 'upload' ? '\u2601\ufe0f Confirm upload' :
    data.kind === 'risky_action' ? '\u26a0\ufe0f Confirm action' : '\u2753 Low confidence';
  const yes = type === 'takeover' ? 'Resume' : isHandoff ? "I've done it \u2014 continue" : data.kind === 'delete' ? 'Delete' : ['send_email', 'send_message', 'send_invite'].includes(data.kind) ? 'Send' : data.kind === 'upload' ? 'Upload' : 'Allow';
  const no = type === 'takeover' ? 'Stop run' : isHandoff ? 'Abort run' : 'Reject';
  const div = addMessage('request',
    `<strong>${title}</strong><br/><div class="msg-pre">${escHtml(data.message)}</div>` +
    `<div class="request-actions"><button class="btn-approve">${yes}</button>` +
    (data.kind === 'low_confidence' ? '<button class="btn-approve-all">Allow all unsure steps this run</button>' : '') +
    `<button class="btn-reject">${no}</button></div>`);
  const answer = (approved, remember = false) => {
    send('respond', { request_id: data.request_id, approved, remember });
    closeRequestCard(data.request_id, remember ? 'answered: allow all this run' : approved ? 'answered: ' + yes : 'answered: ' + no);
  };
  const all = div.querySelector('.btn-approve-all');
  if (all) all.addEventListener('click', () => answer(true, true));
  div.querySelector('.btn-approve').addEventListener('click', () => answer(true));
  div.querySelector('.btn-reject').addEventListener('click', () => answer(false));
  openRequests.set(data.request_id, div);
}

function closeRequestCard(requestId, note) {
  const div = openRequests.get(requestId);
  if (!div) return;
  const actions = div.querySelector('.request-actions');
  if (actions) actions.outerHTML = `<div class="request-done">${escHtml(note || 'closed')}</div>`;
  openRequests.delete(requestId);
}

// === Actions ===
function steerRun() {
  const text = goalInput.value.trim();
  if (!text) return;
  addMessage('user steer', '\ud83e\udded ' + escHtml(text));
  send('steer', { text });
  goalInput.value = '';
}

function startRun(goal, url, mode) {
  if (state.running) { addStatusMessage('A run is in progress — stop it first.'); return; }
  goalInput.value = goal;
  if (url !== undefined) urlInput.value = url || '';
  if (mode) modeSelect.value = mode;
  closePanel();
  runAgent();
}

function runAgent() {
  if (state.running) {
    send('stop');
    return;
  }
  const goal = goalInput.value.trim();
  if (!goal) { goalInput.focus(); return; }

  addMessage('user', escHtml(goal));
  state.procCard = null;
  state.stepCount = 0;

  inspector.innerHTML = '<div class="inspector-empty">Starting...</div>';

  send('run', {
    goal,
    url: urlInput.value.trim() || null,
    capture_mode: modeSelect.value,
  });

  setRunning(true);
  goalInput.value = '';
}

// === Config Modal ===
async function loadConfig() {
  try {
    const resp = await fetch('/config');
    const cfg = await resp.json();
    configBody.innerHTML = Object.entries(cfg).map(([k, v]) =>
      `<div class="config-row"><span class="config-key">${escHtml(k)}</span><span class="config-val">${escHtml(String(v))}</span></div>`
    ).join('');
  } catch (e) {
    configBody.textContent = 'Failed to load config.';
  }
}

// === Event Listeners ===
sendBtn.addEventListener('click', runAgent);

goalInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    if (state.running) steerRun(); else runAgent();
  }
});

configBtn.addEventListener('click', () => {
  configModal.style.display = 'flex';
  loadConfig();
});

modalClose.addEventListener('click', () => {
  configModal.style.display = 'none';
});

configModal.addEventListener('click', (e) => {
  if (e.target === configModal) configModal.style.display = 'none';
});

clearInspector.addEventListener('click', () => {
  inspector.innerHTML = '<div class="inspector-empty">Cleared.</div>';
});

const closeAppBtn = $('close-app-btn');
if (closeAppBtn) {
  closeAppBtn.addEventListener('click', () => {
    send('close');
    addStatusMessage('Closing active window / session...');
  });
}

// === Stuck: ask the user for a hint ===
function addHintCard(data) {
  const div = addMessage('request',
    `<strong>\ud83e\udded I'm stuck — what should I do?</strong><br/><div class="msg-pre">${escHtml(data.message)}</div>` +
    `<textarea class="hint-input" rows="2" placeholder="e.g. 'Rename is under Show more options'"></textarea>` +
    `<div class="request-actions"><button class="btn-approve">Send hint</button>` +
    `<button class="btn-approve-all">I did it myself — continue</button><button class="btn-reject">Give up</button></div>`);
  const input = div.querySelector('.hint-input');
  const answer = (approved, text, note) => {
    send('respond', { request_id: data.request_id, approved, text });
    closeRequestCard(data.request_id, note);
  };
  div.querySelector('.btn-approve').addEventListener('click', () => {
    const t = input.value.trim();
    if (!t) { input.focus(); return; }
    answer(true, t, 'hint sent: ' + t);
  });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); div.querySelector('.btn-approve').click(); }
  });
  div.querySelector('.btn-approve-all').addEventListener('click', () => answer(true, 'done', 'you did the step'));
  div.querySelector('.btn-reject').addEventListener('click', () => answer(false, null, 'gave up'));
  openRequests.set(data.request_id, div);
  input.focus();
}

// === History / Tasks panel ===
const panel = $('panel-modal');
const panelBody = $('panel-body');
const panelTitle = $('panel-title');
const panelBack = $('panel-back');
let panelBackFn = null;

function openPanel(title, html, back) {
  panelTitle.textContent = title;
  panelBody.innerHTML = html;
  panelBackFn = back || null;
  panelBack.style.display = back ? 'inline-block' : 'none';
  panel.style.display = 'flex';
}
function closePanel() { panel.style.display = 'none'; }
$('panel-close').addEventListener('click', closePanel);
panel.addEventListener('click', (e) => { if (e.target === panel) closePanel(); });
panelBack.addEventListener('click', () => panelBackFn && panelBackFn());

async function api(path, opts = {}) {
  const resp = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || resp.statusText);
  return data;
}

const fmtTime = (ms) => ms == null ? '' : (ms / 1000).toFixed(1) + 's';
const fmtCost = (c) => c ? '$' + Number(c).toFixed(4) : '';

async function showHistory() {
  openPanel('History', 'Loading...');
  try {
    const { runs } = await api('/api/runs');
    if (!runs.length) { panelBody.innerHTML = '<div class="inspector-empty">No runs yet.</div>'; return; }
    panelBody.innerHTML = runs.map(r => `
      <div class="run-row" data-name="${escHtml(r.name)}">
        <span><span class="badge ${escHtml(r.status)}">${escHtml(r.status)}</span></span>
        <span>${escHtml(r.user_request || r.goal)}</span>
        <span class="run-meta">${escHtml((r.started || '').replace('T', ' '))} · ${r.steps ?? '?'} steps · ${fmtTime(r.elapsed_ms)}
          ${r.cost_usd ? ' · ' + fmtCost(r.cost_usd) : ''}${r.rescues ? ' · ' + r.rescues + ' rescue' : ''}</span>
      </div>`).join('');
    panelBody.querySelectorAll('.run-row').forEach(el => el.addEventListener('click', () => showRun(el.dataset.name)));
  } catch (e) { panelBody.textContent = 'Could not load history: ' + e.message; }
}

async function showRun(name) {
  openPanel('Run details', 'Loading...', showHistory);
  let r;
  try { r = await api('/api/runs/' + encodeURIComponent(name)); }
  catch (e) { panelBody.textContent = 'Could not load run: ' + e.message; return; }
  const req = r.user_request || r.goal;
  const decisions = {};
  (r.decisions || []).forEach(d => { decisions[d.step] = d; });
  const rows = (r.history || []).map(h => h.operation === 'NOTE'
    ? `<tr class="note"><td></td><td colspan="3">${escHtml(h.note)}</td></tr>`
    : `<tr class="${h.rescue ? 'rescue' : ''}"><td>${escHtml(h.operation)}</td><td>${escHtml(h.label || '')}</td>` +
      `<td>${escHtml(String(h.text ?? '').slice(0, 120))}${h.result ? '<br/>\u2192 ' + escHtml(String(h.result).slice(0, 200)) : ''}</td>` +
      `<td>${h.page_changed === false ? 'no change' : ''} ${h.confidence != null ? Math.round(h.confidence * 100) + '%' : ''}</td></tr>`).join('');
  const u = r.usage || {};
  const usage = (u.models || []).map(m => `<div>${escHtml(m.role)} · ${escHtml(m.model)}: ${m.calls} calls, ${fmtTime(m.time_ms)}` +
    `${m.errors ? `, ${m.errors} failed` : ''}${m.cost_usd ? ', ' + fmtCost(m.cost_usd) : ''}</div>`).join('') || '<div>(not recorded)</div>';
  const phases = (r.phases || []).map(p => `<li><span class="phase-status ps-${escHtml(p.status)}">${escHtml(p.status)}</span> [${escHtml(p.mode)}] ${escHtml(p.task)}</li>`).join('');
  const rescues = (r.rescues || []).map(x => `<li>${escHtml(x.by)}: ${escHtml(x.reason || '')} → ${escHtml(x.kind || (x.hint ? 'hint: ' + x.hint : 'none'))}${x.why ? ' (' + escHtml(x.why) + ')' : ''}</li>`).join('');
  const shots = (r.shots || []).map(sh => `<figure><a href="/api/shots/${encodeURIComponent(r.run_id)}/${encodeURIComponent(sh.file)}" target="_blank">` +
    `<img src="/api/shots/${encodeURIComponent(r.run_id)}/${encodeURIComponent(sh.file)}" loading="lazy"/></a>` +
    `<figcaption>${escHtml(sh.tag)} · step ${sh.step} · ${fmtTime(sh.elapsed_ms)}</figcaption></figure>`).join('');
  panelBody.innerHTML = `
    <div><span class="badge ${escHtml(r.status)}">${escHtml(r.status)}</span> <strong>${escHtml(req)}</strong></div>
    <div class="usage-line">${escHtml((r.started || '').replace('T', ' '))} · ${r.steps} steps · ${fmtTime(r.elapsed_ms)}
      ${u.cost_usd ? ' · ' + fmtCost(u.cost_usd) + (u.cost_complete ? '' : '+') : ''}</div>
    <div class="panel-actions"><button id="rerun">Run again</button><button class="secondary" id="save-as-task">Save as task</button></div>
    <div class="detail-grid">
      <div class="detail-box"><h4>Phases</h4><ol class="phase-list">${phases || '<li>—</li>'}</ol>
        ${(r.steering || []).length ? '<h4>Your mid-run instructions</h4><ul>' + r.steering.map(m => `<li>${escHtml(m)}</li>`).join('') + '</ul>' : ''}
        ${rescues ? '<h4>Rescues</h4><ul>' + rescues + '</ul>' : ''}
        ${(r.recipes_used || []).length ? '<h4>Learned recipe used</h4><div>' + escHtml(r.recipes_used[0].task || '') + '</div>' : ''}</div>
      <div class="detail-box"><h4>Models: time and cost</h4>${usage}</div>
    </div>
    ${shots ? `<div class="detail-box" style="margin-top:12px"><h4>Screenshots</h4><div class="shots">${shots}</div></div>` : ''}
    <div class="detail-box" style="margin-top:12px"><h4>Steps</h4><table class="steps-table">${rows}</table></div>`;
  $('rerun').addEventListener('click', () => startRun(req));
  $('save-as-task').addEventListener('click', () => openTaskForm({ template: req, name: req.slice(0, 40) }));
}

async function showTasks() {
  openPanel('Saved tasks', 'Loading...');
  let data;
  try { data = await api('/api/tasks'); } catch (e) { panelBody.textContent = 'Could not load tasks: ' + e.message; return; }
  const running = data.running ? `<div class="detail-box">\u23f0 Running now: <strong>${escHtml(data.running.task)}</strong> — ${escHtml(data.running.goal)}
      <button class="secondary" id="stop-bg">Stop</button></div>` : '';
  const rows = data.tasks.map(t => `
    <div class="task-row" data-id="${escHtml(t.id)}">
      <span><span class="badge ${escHtml(t.last_status || '')}">${escHtml(t.last_status || (t.enabled ? 'ready' : 'off'))}</span></span>
      <span><strong>${escHtml(t.name)}</strong><br/><span class="run-meta">${escHtml(t.template)}</span></span>
      <span class="run-meta">${t.schedule ? '\u23f0 ' + escHtml(t.schedule) + (t.next_run ? ' (next ' + escHtml(t.next_run.replace('T', ' ')) + ')' : '') : ''}
        ${t.watch_folder ? '<br/>\ud83d\udcc2 ' + escHtml(t.watch_folder) + ' ' + escHtml(t.watch_pattern || '*') : ''}</span>
    </div>`).join('') || '<div class="inspector-empty">No saved tasks yet. Save one from a run result, or create one here.</div>';
  let jobsHtml = '';
  try {
    const { jobs } = await api('/api/jobs');
    const open = jobs.filter(j => !['done', 'cancelled'].includes(j.status));
    if (open.length) jobsHtml = '<div class="detail-box" style="margin-bottom:10px"><h4>Unfinished long jobs</h4>' + open.map(j => `
      <div class="task-row" style="cursor:default"><span><span class="badge ${escHtml(j.status)}">${escHtml(j.status)}</span></span>
        <span>${escHtml(j.request)}<br/><span class="run-meta">${j.done}/${j.total} done, ${j.failed} failed · ${escHtml((j.updated || '').replace('T', ' '))}</span></span>
        <span><button class="secondary job-resume" data-id="${escHtml(j.id)}">Resume</button>
              <button class="secondary job-cancel" data-id="${escHtml(j.id)}">Cancel</button></span></div>`).join('') + '</div>';
  } catch (_) {}
  panelBody.innerHTML = running + jobsHtml + `<div class="panel-actions"><button id="new-task">+ New task</button></div>` + rows +
    (data.scheduler ? '' : '<div class="usage-line">Scheduler is off (SCHEDULER_ENABLED=false): schedules and folder triggers will not run.</div>');
  $('new-task').addEventListener('click', () => openTaskForm({}));
  panelBody.querySelectorAll('.job-resume').forEach(b => b.addEventListener('click', () => {
    if (state.running) { alertBox('A run is in progress — stop it first.'); return; }
    send('resume_job', { job_id: b.dataset.id }); setRunning(true); closePanel();
  }));
  panelBody.querySelectorAll('.job-cancel').forEach(b => b.addEventListener('click', async () => {
    await api(`/api/jobs/${b.dataset.id}/cancel`, { method: 'POST' }); showTasks();
  }));
  const stop = $('stop-bg');
  if (stop) stop.addEventListener('click', async () => { await api('/api/tasks/stop', { method: 'POST' }); showTasks(); });
  panelBody.querySelectorAll('.task-row').forEach(el => el.addEventListener('click', () =>
    showTask(data.tasks.find(t => t.id === el.dataset.id))));
}

function paramInputs(t) {
  return (t.placeholders || []).map(p => `<label>{${escHtml(p)}}</label>` +
    `<input data-param="${escHtml(p)}" value="${escHtml((t.params || {})[p] || '')}" placeholder="value for ${escHtml(p)}"/>`).join('');
}

function readParams() {
  const out = {};
  panelBody.querySelectorAll('[data-param]').forEach(i => { out[i.dataset.param] = i.value; });
  return out;
}

function showTask(t) {
  const runs = (t.runs || []).slice().reverse().map(r => `<tr><td>${escHtml((r.ts || '').replace('T', ' '))}</td><td>${escHtml(r.trigger)}</td>` +
    `<td><span class="badge ${escHtml(r.status)}">${escHtml(r.status)}</span></td><td>${escHtml(r.goal || '')}${r.error ? ' — ' + escHtml(r.error) : ''}</td>` +
    `<td>${r.trace ? `<a href="#" class="open-run" data-name="${escHtml(r.trace)}">details</a>` : ''}</td></tr>`).join('');
  openPanel('Task: ' + t.name, `
    <div class="usage-line">${escHtml(t.template)}</div>
    ${(t.placeholders || []).length ? `<div class="task-form" style="margin-top:10px">${paramInputs(t)}</div>` : ''}
    <div class="panel-actions">
      <button id="t-run">Run now (watch it)</button>
      <button class="secondary" id="t-bg">Run in background</button>
      <button class="secondary" id="t-edit">Edit</button>
      <button class="secondary" id="t-del">Delete task</button>
    </div>
    <div class="detail-box"><h4>Recent runs</h4><table class="steps-table">${runs || '<tr><td>No runs yet.</td></tr>'}</table></div>`, showTasks);
  $('t-run').addEventListener('click', async () => {
    try {
      const r = await api(`/api/tasks/${t.id}/render`, { method: 'POST', body: JSON.stringify({ params: readParams() }) });
      startRun(r.goal, r.url, r.mode);
    } catch (e) { alertBox(e.message); }
  });
  $('t-bg').addEventListener('click', async () => {
    try { await api(`/api/tasks/${t.id}/run_background`, { method: 'POST', body: JSON.stringify({ params: readParams() }) }); showTasks(); }
    catch (e) { alertBox(e.message); }
  });
  $('t-edit').addEventListener('click', () => openTaskForm(t));
  $('t-del').addEventListener('click', async () => { await api(`/api/tasks/${t.id}`, { method: 'DELETE' }); showTasks(); });
}

function alertBox(msg) {
  const d = document.createElement('div');
  d.className = 'step-flag';
  d.textContent = msg;
  panelBody.prepend(d);
}

function openTaskForm(t) {
  openPanel(t.id ? 'Edit task' : 'New task', `
    <div class="task-form">
      <label>Name</label><input id="f-name" value="${escHtml(t.name || '')}"/>
      <label>Goal template</label><textarea id="f-template" rows="3">${escHtml(t.template || '')}</textarea>
      <div class="help">Use {placeholders} for values that change, e.g. "Rename {file} to {new_name}". A folder trigger fills {file}, {filename} and {folder}.</div>
      <label>Default values</label><input id="f-params" value="${escHtml(Object.entries(t.params || {}).map(([k, v]) => k + '=' + v).join('; '))}" placeholder="new_name=Report.docx; who=bob@x.com"/>
      <label>Mode</label><select id="f-mode">${['auto', 'desktop', 'browser'].map(m => `<option ${t.mode === m ? 'selected' : ''}>${m}</option>`).join('')}</select>
      <label>Start URL</label><input id="f-url" value="${escHtml(t.url || '')}" placeholder="optional"/>
      <label>Schedule</label><input id="f-schedule" value="${escHtml(t.schedule || '')}" placeholder="every 30m · daily 09:00 · weekdays 08:30 · mon,fri 10:00 · once 2026-10-05 09:00"/>
      <label>Watch folder</label><input id="f-folder" value="${escHtml(t.watch_folder || '')}" placeholder="optional, e.g. Downloads or C:\\Inbox"/>
      <label>File pattern</label><input id="f-pattern" value="${escHtml(t.watch_pattern || '*')}"/>
      <label>Unattended</label><label><input type="checkbox" id="f-risky" ${t.allow_risky ? 'checked' : ''} style="width:auto"/> allow Send / Submit / Pay-type clicks and sending email when nobody is watching (deleting is never allowed)</label>
      <label>Enabled</label><label><input type="checkbox" id="f-enabled" ${t.enabled === false ? '' : 'checked'} style="width:auto"/> run schedules and folder triggers</label>
    </div>
    <div class="panel-actions"><button id="f-save">Save</button><button class="secondary" id="f-cancel">Cancel</button></div>`,
    t.id ? () => showTask(t) : showTasks);
  $('f-cancel').addEventListener('click', () => (t.id ? showTask(t) : showTasks()));
  $('f-save').addEventListener('click', async () => {
    const params = {};
    $('f-params').value.split(';').map(x => x.trim()).filter(Boolean).forEach(kv => {
      const i = kv.indexOf('=');
      if (i > 0) params[kv.slice(0, i).trim()] = kv.slice(i + 1).trim();
    });
    try {
      await api('/api/tasks', { method: 'POST', body: JSON.stringify({
        id: t.id, name: $('f-name').value, template: $('f-template').value, params, mode: $('f-mode').value,
        url: $('f-url').value, schedule: $('f-schedule').value, watch_folder: $('f-folder').value,
        watch_pattern: $('f-pattern').value, allow_risky: $('f-risky').checked, enabled: $('f-enabled').checked,
      }) });
      showTasks();
    } catch (e) { alertBox(e.message); }
  });
}

// === Connections (one-click sign-in) ===
async function showConnections() {
  openPanel('Connections', 'Loading...');
  let data;
  try { data = await api('/api/connections'); } catch (e) { panelBody.textContent = 'Could not load: ' + e.message; return; }
  panelBody.innerHTML = '<div class="usage-line" style="margin-bottom:10px">Sign in once and NewJev can send and read ' +
    'messages, mail and calendar for you. Anything that sends to other people is always shown to you first.</div>' +
    data.connections.map(c => `
    <div class="task-row" style="cursor:default">
      <span><span class="badge ${c.connected ? 'done' : ''}">${c.connected ? 'connected' : 'not connected'}</span></span>
      <span><strong>${escHtml(c.name)}</strong> <span class="run-meta">— ${escHtml(c.what)}</span>
        ${c.account ? `<br/><span class="run-meta">${escHtml(c.account)}</span>` : ''}
        ${c.note ? `<br/><span class="run-meta">${escHtml(c.note)}</span>` : ''}</span>
      <span>${c.automatic ? '<span class="run-meta">automatic</span>' :
        !c.available ? '' : c.connected
          ? `<button class="secondary conn-off" data-id="${escHtml(c.id)}">Disconnect</button>`
          : `<button class="conn-on" data-id="${escHtml(c.id)}">Connect</button>`}</span>
    </div>`).join('');
  panelBody.querySelectorAll('.conn-on').forEach(b => b.addEventListener('click', () => {
    window.open('/oauth/' + b.dataset.id + '/start', '_blank', 'width=560,height=720');
    b.textContent = 'Waiting for sign-in…'; b.disabled = true;
  }));
  panelBody.querySelectorAll('.conn-off').forEach(b => b.addEventListener('click', async () => {
    await api(`/api/connections/${b.dataset.id}/disconnect`, { method: 'POST' }); showConnections();
  }));
}
$('connections-btn').addEventListener('click', showConnections);

$('history-btn').addEventListener('click', showHistory);
$('tasks-btn').addEventListener('click', showTasks);
document.addEventListener('click', (e) => {
  const link = e.target.closest('.open-run');
  if (link) { e.preventDefault(); showRun(link.dataset.name); }
});

// === Voice input (Web Speech API: Edge / Chrome) ===
const micBtn = $('mic-btn');
const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
let recog = null;
function startVoice(autoSend) {
  if (!SpeechRec) { addStatusMessage('Voice input needs Edge or Chrome.'); return; }
  if (recog) { recog.stop(); return; }
  recog = new SpeechRec();
  recog.lang = navigator.language || 'en-US';
  recog.interimResults = true;
  const before = goalInput.value;
  recog.onresult = (ev) => {
    goalInput.value = (before ? before + ' ' : '') + Array.from(ev.results).map(r => r[0].transcript).join('');
  };
  recog.onerror = (ev) => addStatusMessage('Voice: ' + ev.error);
  recog.onend = () => {
    micBtn.classList.remove('listening');
    recog = null;
    if (autoSend && goalInput.value.trim()) { if (state.running) steerRun(); else runAgent(); }
  };
  micBtn.classList.add('listening');
  recog.start();
}
if (micBtn) {
  if (!SpeechRec) micBtn.title = 'Voice input needs Edge or Chrome';
  micBtn.addEventListener('click', () => startVoice(true));
}

// === Start from a link (?goal=... from the quick box, ?voice=1) ===
function startFromUrl() {
  const params = new URLSearchParams(location.search);
  const g = params.get('goal');
  if (params.get('voice') === '1') startVoice(true);
  if (g) {
    history.replaceState(null, '', location.pathname);
    const tryRun = () => (state.ws && state.ws.readyState === WebSocket.OPEN) ? startRun(g) : setTimeout(tryRun, 300);
    tryRun();
  }
}

// === Init ===
connect();
goalInput.focus();
startFromUrl();
