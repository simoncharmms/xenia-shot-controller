/**
 * Xenia Shot Controller — frontend app
 * WebSocket client + Chart.js dashboard + AI coaching chat
 */

// ── WebSocket connection ──────────────────────────────────────────────────────

const WS_URL = `ws://${location.hostname}:8765`;
let ws = null;
let wsReconnectTimer = null;
let latestState = {};
let shotLog = [];
let currentConfig = {};

function connect() {
  console.log('Connecting to', WS_URL);
  ws = new WebSocket(WS_URL);

  ws.onopen = () => {
    console.log('WebSocket connected');
    clearTimeout(wsReconnectTimer);
  };

  ws.onmessage = (evt) => {
    try {
      const msg = JSON.parse(evt.data);
      handleMessage(msg);
    } catch (e) {
      console.error('Bad message:', e);
    }
  };

  ws.onerror = (e) => console.warn('WebSocket error:', e);

  ws.onclose = () => {
    console.log('WebSocket disconnected, reconnecting in 2s...');
    wsReconnectTimer = setTimeout(connect, 2000);
  };
}

function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(obj));
  }
}

function handleMessage(msg) {
  switch (msg.type) {
    case 'sensor_update':
      latestState = msg;
      updateUI(msg);
      break;
    case 'shot_log':
      shotLog = msg.shots || [];
      renderShotLog();
      break;
    case 'chat_message':
      appendChatMessage(msg);
      break;
    case 'chat_clear':
      clearChat();
      break;
    case 'chat_history':
      clearChat();
      (msg.messages || []).forEach(appendChatMessage);
      break;
    case 'scripts_list':
      renderScriptsList(msg.scripts || []);
      break;
    case 'config': {
      const incoming = msg.config || {};
      // Server always masks api_key as ●●●●. Preserve the real key the user
      // typed (stored in currentConfig) so re-opening settings doesn't
      // replace it with the placeholder, causing subsequent saves to be no-ops.
      const prevKey = (currentConfig.llm || {}).api_key || '';
      currentConfig = incoming;
      if (incoming.llm && incoming.llm.api_key === '●●●●' && prevKey && prevKey !== '●●●●') {
        currentConfig.llm.api_key = prevKey;
      }
      populateSettingsForm(currentConfig);
      break;
    }
    case 'error':
      showToast(msg.msg, 'error');
      document.getElementById('alert-bar').classList.remove('hidden');
      document.getElementById('alert-text').textContent = msg.msg;
      break;
    case 'warn':
      showToast(msg.msg, 'warn');
      break;
    case 'info':
      showToast(msg.msg, 'info');
      break;
  }
}

// ── Chart setup ──────────────────────────────────────────────────────────────

const MAX_POINTS = 600;  // 60s × 10 Hz = 600 pts at 100ms poll
const chartLabels = [];
const pressureData = [];
const targetPData = [];

const ctx = document.getElementById('shot-chart').getContext('2d');
const chart = new Chart(ctx, {
  type: 'line',
  data: {
    labels: chartLabels,
    datasets: [
      {
        label: 'Pressure (bar)',
        data: pressureData,
        borderColor: '#c4a882',
        backgroundColor: 'rgba(196,168,130,0.10)',
        borderWidth: 2.5,
        tension: 0.25,
        fill: true,
        pointRadius: 0,
        yAxisID: 'y',
      },
      {
        label: 'Target',
        data: targetPData,
        borderColor: 'rgba(52,196,124,0.55)',
        borderWidth: 1.5,
        borderDash: [5, 4],
        tension: 0,
        fill: false,
        pointRadius: 0,
        yAxisID: 'y',
      },
    ],
  },
  options: {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    interaction: { mode: 'index', intersect: false },
    scales: {
      x: {
        type: 'category',
        ticks: {
          color: '#8892a4',
          maxTicksLimit: 10,
          font: { family: "'SF Mono', 'Fira Code', monospace", size: 10 },
          callback: (val, idx) => {
            const label = chartLabels[idx];
            if (!label) return '';
            const n = parseFloat(label);
            if (isNaN(n)) return label;
            return n % 10 === 0 ? `${n}s` : '';
          }
        },
        grid: { color: 'rgba(255,255,255,0.04)' },
      },
      y: {
        type: 'linear',
        position: 'left',
        min: 0,
        max: 13,
        ticks: {
          color: '#c4a882',
          font: { family: "'SF Mono', 'Fira Code', monospace", size: 10 },
          callback: v => `${v}b`,
        },
        grid: { color: 'rgba(255,255,255,0.04)' },
      },
    },
    plugins: {
      legend: {
        labels: {
          color: '#8892a4',
          font: { family: "'SF Mono', 'Fira Code', monospace", size: 11 },
          usePointStyle: true,
          pointStyleWidth: 10,
        },
      },
      tooltip: {
        backgroundColor: '#111827',
        borderColor: 'rgba(255,255,255,0.08)',
        borderWidth: 1,
        titleColor: '#e0e0e0',
        bodyColor: '#8892a4',
        callbacks: {
          label: ctx => `${ctx.dataset.label}: ${ctx.parsed.y.toFixed(2)}`,
        },
      },
    },
  },
});

function pushChartData(elapsed, pressure, targetP) {
  const label = elapsed.toFixed(1);
  chartLabels.push(label);
  pressureData.push(pressure);
  targetPData.push(targetP);

  if (chartLabels.length > MAX_POINTS) {
    chartLabels.shift();
    pressureData.shift();
    targetPData.shift();
  }

  chart.update('none');
}

function clearChart() {
  chartLabels.length = 0;
  pressureData.length = 0;
  targetPData.length = 0;
  chart.update('none');
}

// ── Chat ─────────────────────────────────────────────────────────────────────

function clearChat() {
  const container = document.getElementById('chat-messages');
  container.innerHTML = '';
}

function appendChatMessage(msg) {
  const container = document.getElementById('chat-messages');

  // Remove welcome message if still present
  const welcome = container.querySelector('.chat-welcome');
  if (welcome) welcome.remove();

  const el = document.createElement('div');
  el.className = `chat-msg chat-msg-${msg.role}`;

  const time = msg.time_display || '';
  const content = escapeHtml(msg.content || '');

  el.innerHTML = `
    <div class="chat-msg-meta">
      <span class="chat-msg-role">${roleLabel(msg.role)}</span>
      <span class="chat-msg-time">${time}</span>
    </div>
    <div class="chat-msg-body">${content}</div>
  `;

  container.appendChild(el);
  container.scrollTop = container.scrollHeight;
}

function roleLabel(role) {
  switch (role) {
    case 'assistant': return '🤖 Coach';
    case 'user':      return '👤 You';
    case 'system':    return '⚙️ System';
    default:          return role;
  }
}

function escapeHtml(str) {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/\n/g, '<br>');
}

function sendChat() {
  const input = document.getElementById('chat-input');
  const content = input.value.trim();
  if (!content) return;
  input.value = '';
  send({ cmd: 'chat_message', content });
}

// ── UI update ─────────────────────────────────────────────────────────────────

let prevShotActive = false;

function updateUI(s) {
  // Machine / demo status
  const statusBadge = document.getElementById('machine-status');
  if (s.demo) {
    statusBadge.textContent = 'DEMO';
    statusBadge.className = 'status-badge demo';
  } else if (s.machine_online) {
    statusBadge.textContent = 'ONLINE';
    statusBadge.className = 'status-badge online';
  } else {
    statusBadge.textContent = 'OFFLINE';
    statusBadge.className = 'status-badge offline';
  }

  // Phase badge in chat header
  const phaseBadge = document.getElementById('shot-phase-badge');
  if (phaseBadge) {
    phaseBadge.textContent = s.phase || 'IDLE';
    phaseBadge.className = `phase-badge phase-badge-${(s.phase || 'IDLE').toLowerCase()}`;
  }

  // Metric cards
  setMetric('m-pressure', (s.pressure || 0).toFixed(1));
  if (s.shot_active) {
    const remaining = Math.max(0, (s.target_time || 30) - (s.elapsed || 0));
    setMetric('m-time-remaining', fmtTime(remaining));
  } else {
    setMetric('m-time-remaining', fmtTime(s.target_time || 30));
  }
  setMetric('m-bg-temp', (s.bg_temp || 0).toFixed(1));
  setMetric('m-bb-temp', (s.bb_temp || 0).toFixed(1));

  colorCard('card-pressure', s.pressure, s.current_target_pressure, s.shot_active);

  updatePhase(s.phase);

  // Stats
  document.getElementById('stat-time').textContent = fmtTime(s.elapsed);
  document.getElementById('stat-time-target').textContent = fmtTime(s.target_time || 30);
  document.getElementById('stat-target-p').textContent = `${(s.target_pressure || 0).toFixed(1)} bar`;

  // Alert
  if (s.alert) {
    document.getElementById('alert-bar').classList.remove('hidden');
    document.getElementById('alert-text').textContent = s.alert;
  }

  // Buttons
  document.getElementById('btn-start').disabled = !!s.shot_active;
  document.getElementById('btn-stop').disabled  = !s.shot_active;
  const hint = document.getElementById('shot-hint');
  if (hint) {
    hint.textContent = s.shot_active
      ? '⏱ Tracking active — stop manually or wait for timer'
      : 'Press to arm tracking, then brew on the machine';
  }

  // Chart
  if (s.shot_active) {
    if (!prevShotActive) clearChart();
    pushChartData(s.elapsed, s.pressure, s.current_target_pressure || s.target_pressure);
  }

  // Mode
  const isManual = s.mode === 'MANUAL';
  document.getElementById('btn-mode-auto').classList.toggle('active', !isManual);
  document.getElementById('btn-mode-manual').classList.toggle('active', isManual);
  document.getElementById('manual-controls').classList.toggle('hidden', !isManual);
  document.getElementById('manual-param-controls').classList.toggle('hidden', !isManual);

  // Sync sliders (manual only — in auto, values come from script)
  if (isManual) {
    syncSlider('sl-target-pressure', 'lbl-target-pressure', (s.target_pressure || 9).toFixed(1));
    syncSlider('sl-target-time',     'lbl-target-time',     Math.round(s.target_time || 30));
    syncSlider('sl-target-temp',     'lbl-target-temp',     (s.target_temp || 93).toFixed(1));
  }

  prevShotActive = !!s.shot_active;
}

function setMetric(id, val) {
  const el = document.getElementById(id);
  if (el && el.textContent !== val) el.textContent = val;
}

function colorCard(cardId, pressure, targetP, shotActive) {
  const card = document.getElementById(cardId);
  if (!card) return;
  card.classList.remove('warn', 'alert');
  if (cardId === 'card-pressure' && shotActive && pressure != null && targetP != null) {
    const diff = Math.abs(pressure - targetP);
    if (diff > 1.5) card.classList.add('alert');
    else if (diff > 0.7) card.classList.add('warn');
  }
}

const PHASE_CONFIG = {
  IDLE:         { label: 'IDLE',         cls: 'idle',         pct: 0 },
  PRE_INFUSION: { label: 'PRE-INFUSION', cls: 'pre_infusion', pct: 20 },
  RAMP:         { label: 'RAMP',         cls: 'ramp',         pct: 40 },
  EXTRACTION:   { label: 'EXTRACTION',   cls: 'extraction',   pct: 70 },
  DECLINING:    { label: 'DECLINING',    cls: 'declining',    pct: 90 },
  DONE:         { label: 'DONE',         cls: 'done',         pct: 100 },
};

function updatePhase(phase) {
  const cfg = PHASE_CONFIG[phase] || PHASE_CONFIG.IDLE;
  const bar = document.getElementById('phase-bar');
  const name = document.getElementById('phase-name');
  if (bar) { bar.style.width = `${cfg.pct}%`; bar.className = `phase-bar ${cfg.cls}`; }
  if (name) name.textContent = cfg.label;
}

function fmtTime(sec) {
  const s = Math.floor(sec || 0);
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
}

let _sliderLocks = {};
function syncSlider(sliderId, labelId, val) {
  if (_sliderLocks[sliderId]) return;
  const sl = document.getElementById(sliderId);
  const lb = document.getElementById(labelId);
  if (sl && sl.value !== String(val)) sl.value = parseFloat(val);
  if (lb && lb.textContent !== String(val)) lb.textContent = val;
}

// ── Scripts ───────────────────────────────────────────────────────────────────

let _scripts = [];       // cached script list
let _selectedScriptId = null;
let _selectedScriptTitle = null;

function refreshScripts() {
  send({ cmd: 'get_scripts' });
}

function selectScript(id, title) {
  _selectedScriptId = id;
  _selectedScriptTitle = title;
  // Update all script items' visual state
  document.querySelectorAll('.script-item').forEach(el => {
    el.classList.toggle('script-selected', parseInt(el.dataset.scriptId) === id);
  });
  // Update stat display
  const statEl = document.getElementById('stat-active-script');
  if (statEl) statEl.textContent = title;
}

function executeScript(id, title) {
  selectScript(id, title);
  const isManual = document.getElementById('btn-mode-manual').classList.contains('active');
  if (isManual) {
    if (!confirm(`Run "${title}" on the machine?`)) return;
  }
  send({ cmd: 'execute_script', id });
}

function renderScriptsList(scripts) {
  _scripts = scripts || [];
  const container = document.getElementById('scripts-list');
  if (!_scripts.length) {
    container.innerHTML = '<span class="scripts-empty">No scripts found</span>';
    return;
  }
  container.innerHTML = _scripts.map(s => `
    <div class="script-item ${_selectedScriptId === s.id ? 'script-selected' : ''}"
         data-script-id="${s.id}"
         onclick="selectScript(${s.id}, '${escapeHtml(s.title).replace(/'/g, "\\'")}')">
      <span class="script-title">${escapeHtml(s.title)}</span>
      <span class="script-id">#${s.id}</span>
      <button class="script-run-btn" onclick="event.stopPropagation(); executeScript(${s.id}, '${escapeHtml(s.title).replace(/'/g, "\\'")}')">▶ Run</button>
    </div>
  `).join('');
}

// ── Shot log ──────────────────────────────────────────────────────────────────

function renderShotLog() {
  const tbody = document.getElementById('shot-log-body');
  if (!shotLog || shotLog.length === 0) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty-log">No shots yet</td></tr>';
    return;
  }

  // Store in localStorage
  try { localStorage.setItem('xenia_shots', JSON.stringify(shotLog)); } catch(e) {}

  const rows = [...shotLog].reverse().slice(0, 20).map((shot, idx) => {
    const target = shot.target_time || 30;
    const dur = shot.duration_s || 0;
    let quality = 'ok', icon = '~ ok';
    if (dur >= target * 0.9 && dur <= target * 1.1) {
      quality = 'good'; icon = '✓ good';
    } else if (dur < target * 0.8) {
      quality = 'poor'; icon = '✗ short';
    } else if (dur > target * 1.2) {
      quality = 'poor'; icon = '✗ long';
    }
    const hasCurve = shot.curve && shot.curve.length > 2;
    const rowStyle = hasCurve ? 'cursor:pointer' : '';
    const title = hasCurve ? 'title="Click to view pressure curve"' : '';
    // reverse index back to original shotLog index
    const originalIdx = shotLog.length - 1 - idx;
    return `<tr style="${rowStyle}" ${title} onclick="openShotHistory(${originalIdx})">
      <td>${shot.time_display || '—'}</td>
      <td>${dur.toFixed(0)}s</td>
      <td>${target.toFixed(0)}s</td>
      <td>${(shot.peak_pressure || 0).toFixed(1)} bar</td>
      <td class="result-${quality}">${icon}${hasCurve ? ' 📈' : ''}</td>
    </tr>`;
  }).join('');

  tbody.innerHTML = rows;
}

// ── Shot history chart ────────────────────────────────────────────────────────

let _historyChart = null;

function openShotHistory(idx) {
  const shot = shotLog[idx];
  if (!shot) return;

  const overlay = document.getElementById('shot-history-overlay');
  const noData  = document.getElementById('shot-history-no-data');
  const canvas  = document.getElementById('shot-history-chart');

  document.getElementById('shot-history-title').textContent =
    `Shot — ${shot.ts ? shot.ts.replace('T', ' ').slice(0, 16) : shot.time_display || '?'}`;

  const curve = shot.curve || [];

  if (curve.length < 2) {
    noData.style.display = 'block';
    canvas.style.display = 'none';
    document.getElementById('shot-history-meta').textContent = '';
    overlay.classList.remove('hidden');
    return;
  }

  noData.style.display = 'none';
  canvas.style.display = 'block';

  // Stats line
  const peakP   = Math.max(...curve.map(p => p.p)).toFixed(2);
  const dur     = shot.duration_s ? shot.duration_s.toFixed(1) + 's' : '—';
  const target  = shot.target_pressure ? shot.target_pressure.toFixed(1) + ' bar target' : '';
  document.getElementById('shot-history-meta').textContent =
    `${dur} · Peak ${peakP} bar · ${curve.length} data points` + (target ? ` · ${target}` : '');

  if (_historyChart) { _historyChart.destroy(); _historyChart = null; }

  const ctx = canvas.getContext('2d');
  _historyChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: curve.map(p => p.t.toFixed(1)),
      datasets: [
        {
          label: 'Pressure (bar)',
          data: curve.map(p => p.p),
          borderColor: '#c4a882',
          backgroundColor: 'rgba(196,168,130,0.12)',
          fill: true,
          tension: 0.3,
          pointRadius: curve.length < 30 ? 3 : 0,
          borderWidth: 2.5,
          yAxisID: 'y',
        },
        ...(shot.target_pressure ? [{
          label: 'Target',
          data: curve.map(() => shot.target_pressure),
          borderColor: 'rgba(52,196,124,0.45)',
          borderWidth: 1.5,
          borderDash: [5, 4],
          tension: 0,
          fill: false,
          pointRadius: 0,
          yAxisID: 'y',
        }] : []),
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      scales: {
        x: {
          ticks: {
            color: '#8892a4',
            maxTicksLimit: 10,
            font: { size: 10 },
            callback: (val, idx) => {
              const n = parseFloat(curve[idx]?.t);
              return n % 10 === 0 ? `${n}s` : '';
            },
          },
          grid: { color: 'rgba(255,255,255,0.04)' },
          title: { display: true, text: 'seconds', color: '#8892a4', font: { size: 10 } },
        },
        y: {
          min: 0,
          max: 13,
          ticks: {
            color: '#c4a882',
            font: { size: 10 },
            callback: v => `${v}b`,
          },
          grid: { color: 'rgba(255,255,255,0.04)' },
          title: { display: true, text: 'bar', color: '#c4a882', font: { size: 10 } },
        },
      },
      plugins: {
        legend: {
          labels: { color: '#8892a4', font: { size: 11 }, usePointStyle: true, pointStyleWidth: 10 },
        },
        tooltip: {
          backgroundColor: '#111827',
          borderColor: 'rgba(255,255,255,0.08)',
          borderWidth: 1,
          titleColor: '#e0e0e0',
          bodyColor: '#8892a4',
          callbacks: {
            title: items => `t = ${items[0].label}s`,
            label: ctx => `${ctx.dataset.label}: ${ctx.parsed.y.toFixed(2)}`,
          },
        },
      },
    },
  });

  overlay.classList.remove('hidden');
}

function closeShotHistory() {
  document.getElementById('shot-history-overlay').classList.add('hidden');
}

// ── Settings overlay ──────────────────────────────────────────────────────────

function toggleSettings() {
  const overlay = document.getElementById('settings-overlay');
  overlay.classList.toggle('hidden');
  if (!overlay.classList.contains('hidden')) {
    populateSettingsForm(currentConfig);
  }
}

function populateSettingsForm(cfg) {
  const machine = cfg.machine || {};
  const llm = cfg.llm || {};
  setInputVal('cfg-machine-host',  machine.host || '');
  setInputVal('cfg-llm-base-url',  llm.base_url || '');
  setInputVal('cfg-llm-model',     llm.model || '');

  // If we have a real key (not the server mask), show it in the field so the
  // user can edit it. If the server returned ●●●● and we have no local copy,
  // leave the field empty and rely on the placeholder text to signal a key exists.
  const apiKeyEl = document.getElementById('cfg-llm-api-key');
  if (apiKeyEl) {
    const key = llm.api_key || '';
    apiKeyEl.value = (key === '●●●●') ? '' : key;
    apiKeyEl.placeholder = (key === '●●●●') ? '●●●● (key saved — leave blank to keep)' : 'sk-...';
  }
}

function setInputVal(id, val) {
  const el = document.getElementById(id);
  if (el) el.value = val;
}

function saveSettings() {
  const apiKey = document.getElementById('cfg-llm-api-key').value.trim();
  const llmCfg = {
    base_url: document.getElementById('cfg-llm-base-url').value.trim(),
    model:    document.getElementById('cfg-llm-model').value.trim(),
  };
  // Only include api_key if the user actually typed something.
  // Empty field = "keep existing key on server" (placeholder already signals this).
  if (apiKey) llmCfg.api_key = apiKey;

  const config = {
    machine: { host: document.getElementById('cfg-machine-host').value.trim() },
    llm: llmCfg,
  };

  // Optimistically update local currentConfig with the real key before
  // the server echoes back the masked version.
  if (apiKey) {
    currentConfig.llm = currentConfig.llm || {};
    currentConfig.llm.api_key = apiKey;
  }

  send({ cmd: 'set_config', config });

  const status = document.getElementById('settings-status');
  status.textContent = '✅ Saved!';
  setTimeout(() => { status.textContent = ''; toggleSettings(); }, 1200);
}

// ── Commands ──────────────────────────────────────────────────────────────────

function startShot() {
  const isManual = document.getElementById('btn-mode-manual').classList.contains('active');
  if (isManual) {
    const targetP  = parseFloat(document.getElementById('sl-target-pressure').value);
    const targetTm = parseFloat(document.getElementById('sl-target-time').value);
    const targetT  = parseFloat(document.getElementById('sl-target-temp').value);
    send({ cmd: 'start_shot', target_pressure: targetP, target_time: targetTm, target_temp: targetT });
  } else {
    // Auto mode — tracking only, targets derived from machine/script
    send({ cmd: 'start_shot' });
  }
}

function stopShot() {
  send({ cmd: 'stop_shot' });
}

function setMode(mode) {
  send({ cmd: 'set_mode', mode });
}

function dismissAlert() {
  document.getElementById('alert-bar').classList.add('hidden');
}

// ── Slider handlers ────────────────────────────────────────────────────────────

function onTargetPressureChange(val) {
  _sliderLocks['sl-target-pressure'] = true;
  document.getElementById('lbl-target-pressure').textContent = parseFloat(val).toFixed(1);
  clearTimeout(_sliderLocks['sl-target-pressure-t']);
  _sliderLocks['sl-target-pressure-t'] = setTimeout(() => { delete _sliderLocks['sl-target-pressure']; }, 500);
}

function onTargetTimeChange(val) {
  _sliderLocks['sl-target-time'] = true;
  document.getElementById('lbl-target-time').textContent = parseInt(val);
  clearTimeout(_sliderLocks['sl-target-time-t']);
  _sliderLocks['sl-target-time-t'] = setTimeout(() => { delete _sliderLocks['sl-target-time']; }, 500);
}

function onTargetTempChange(val) {
  _sliderLocks['sl-target-temp'] = true;
  document.getElementById('lbl-target-temp').textContent = parseFloat(val).toFixed(1);
  clearTimeout(_sliderLocks['sl-target-temp-t']);
  _sliderLocks['sl-target-temp-t'] = setTimeout(() => { delete _sliderLocks['sl-target-temp']; }, 500);
}

function onLivePressureChange(val) {
  const v = parseFloat(val).toFixed(1);
  document.getElementById('lbl-live-pressure').textContent = v;
  send({ cmd: 'set_pressure', value: parseFloat(v) });
}

function onLiveTempChange(val) {
  const v = parseFloat(val).toFixed(1);
  document.getElementById('lbl-live-temp').textContent = v;
  send({ cmd: 'set_temp', value: parseFloat(v) });
}

function showToast(msg, type) {
  console.log(`[${type}] ${msg}`);
  const toast = document.createElement('div');
  toast.className = `toast toast-${type || 'info'}`;
  toast.textContent = msg;
  document.body.appendChild(toast);
  requestAnimationFrame(() => toast.classList.add('show'));
  setTimeout(() => {
    toast.classList.remove('show');
    setTimeout(() => toast.remove(), 300);
  }, 4000);
}

// ── Init ──────────────────────────────────────────────────────────────────────

// Restore shot log from localStorage while waiting for WS
try {
  const saved = localStorage.getItem('xenia_shots');
  if (saved) {
    shotLog = JSON.parse(saved);
    renderShotLog();
  }
} catch(e) {}

connect();
