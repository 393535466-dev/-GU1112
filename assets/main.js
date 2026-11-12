import { fetchSnapshot, snapshotMode } from './shell.js';
import { initAssistant } from './assistant.js';

const options = document.getElementById('options');
const start = document.getElementById('start');
const status = document.getElementById('status');
const meta = document.getElementById('result-meta');
const candidates = document.getElementById('candidates');
const note = document.getElementById('holding-note');
const directionTemplate = document.getElementById('direction-template');
const candidateTemplate = document.getElementById('candidate-template');
const timeFormat = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit',
});

let direction = 'swing';
let loading = false;
let requestVersion = 0;
let validDirections = [];
let publishedRuns = {};
let currentRun = null;
const openAssistant = initAssistant(() => ({ direction, run: currentRun }));

async function api(path, init = {}) {
  const response = await fetch(path, { cache: 'no-store', signal: AbortSignal.timeout(12000), ...init });
  if (!response.ok) {
    const error = new Error('连接选股服务失败，请稍后重试。');
    error.status = response.status;
    throw error;
  }
  return response.json();
}

function showStatus(message, error = false) {
  status.textContent = message;
  status.toggleAttribute('data-error', error);
}

function clearResults() {
  currentRun = null;
  meta.textContent = '';
  candidates.replaceChildren();
  note.hidden = true;
}

function updateControls() {
  for (const button of options.querySelectorAll('button')) {
    button.setAttribute('aria-pressed', String(button.dataset.direction === direction));
    button.disabled = loading;
  }
  start.disabled = loading || validDirections.length === 0;
  start.textContent = loading ? '筛选中…' : '筛选候选';
}

function setDirection(next) {
  direction = next;
  try {
    localStorage.setItem('lazy-picker.direction', next);
  } catch {
    // The direction still works when browser storage is disabled.
  }
  updateControls();
}

function renderResult(run) {
  clearResults();
  currentRun = run;
  const result = run.result;
  const stamp = new Date(result.snapshot_time);
  const timestamp = Number.isNaN(stamp.valueOf()) ? '' : timeFormat.format(stamp) + ' 北京时间';
  const source = result.snapshot_source === 'last_good_cache' ? '本地行情缓存' : result.snapshot_source;
  meta.textContent = [result.direction_label, source, timestamp].filter(Boolean).join(' · ');
  const messages = [];
  if (result.stale) {
    messages.push('行情源暂不可用，本轮使用了上次成功的行情。');
  } else if (result.cached) {
    messages.push('本轮复用了近期行情快照。');
  }
  if (result.daily_unavailable) {
    messages.push('本轮缺少可用日线，评分基于行情快照。');
  }
  const picks = result.candidates;
  if (!picks.length) {
    messages.push('本轮没有满足条件的候选，可以换个方向再筛。');
  } else if (picks.length < 3) {
    messages.push(`本轮只有 ${picks.length} 只满足条件。`);
  }
  showStatus(messages.join(' '));

  for (const pick of picks) {
    const row = candidateTemplate.content.cloneNode(true);
    for (const [name, value] of Object.entries({
      'stock-name': pick.name,
      'stock-code': pick.code,
      'score-value': pick.score.toFixed(1),
      reason: pick.reason,
      risk: pick.risk,
      holding: pick.holding,
      review: pick.review,
    })) {
      row.querySelector('.' + name).textContent = value;
    }
    const ask = row.querySelector('.ask-stock');
    ask.setAttribute('aria-label', '问这只：' + pick.name);
    ask.addEventListener('click', () => openAssistant(`${pick.name}（${pick.code}）为什么入选？`));
    candidates.append(row);
  }
  note.hidden = !picks.length;
}

async function loadLatest() {
  const version = ++requestVersion;
  clearResults();
  showStatus('正在查看上次筛选结果…');
  try {
    const run = snapshotMode ? publishedRuns[direction] : await api('/api/runs/latest?direction=' + encodeURIComponent(direction));
    if (version !== requestVersion) return;
    if (run?.result) {
      renderResult(run);
    } else {
      showStatus(snapshotMode ? '本期收盘快照暂无该方向的结果。' : '点击「筛选候选」开始。');
    }
  } catch {
    if (version === requestVersion) {
      showStatus('暂时无法读取上次结果。你可以重新筛选。', true);
    }
  }
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function followRun(run) {
  setDirection(run.direction);
  while (run.status === 'queued' || run.status === 'running') {
    showStatus(run.message + '…');
    let failedAttempts = 0;
    while (true) {
      await wait(failedAttempts ? 4500 : 1800);
      try {
        run = await api('/api/runs/' + encodeURIComponent(run.id));
        break;
      } catch (error) {
        if (error.status === 404 || ++failedAttempts >= 5) {
          throw new Error('暂时无法读取筛选任务，刷新页面后可继续查看。');
        }
        showStatus('进度暂时不可用，正在重新连接…');
      }
    }
  }
  if (run.status === 'completed' && run.result) {
    renderResult(run);
  } else {
    showStatus(run.message || '筛选未完成，请重试。', true);
  }
}

start.addEventListener('click', async () => {
  if (loading) return;
  loading = true;
  requestVersion += 1;
  updateControls();
  clearResults();
  showStatus('正在提交筛选…');
  document.getElementById('result-title').focus({ preventScroll: true });
  document.getElementById('results').scrollIntoView({ block: 'start' });
  try {
    const run = await api('/api/runs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ direction }),
    });
    await followRun(run);
  } catch (error) {
    showStatus(error.message || '暂时无法提交筛选，请重试。', true);
  } finally {
    loading = false;
    updateControls();
  }
});

async function initialize() {
  loading = true;
  try {
    let styles, active;
    if (snapshotMode) {
      start.hidden = true;
      const data = await fetchSnapshot('picker');
      styles = data.directions;
      publishedRuns = data.runs;
    } else {
      [styles, active] = await Promise.all([api('/api/directions'), api('/api/runs/active')]);
    }
    validDirections = styles.map((style) => style.id);
    let saved;
    try {
      saved = localStorage.getItem('lazy-picker.direction');
    } catch {
      saved = null;
    }
    if (validDirections.includes(saved)) direction = saved;
    for (const style of styles) {
      const button = directionTemplate.content.cloneNode(true).querySelector('button');
      button.dataset.direction = style.id;
      button.querySelector('.direction-title').textContent = style.title;
      button.querySelector('.direction-holding').textContent = style.holding;
      button.querySelector('.direction-description').textContent = style.description;
      button.addEventListener('click', () => {
        if (loading || direction === style.id) return;
        setDirection(style.id);
        void loadLatest();
      });
      options.append(button);
    }
    updateControls();
    if (active) {
      await followRun(active);
    } else {
      await loadLatest();
    }
  } catch (error) {
    showStatus(error.message || '无法加载选股页面，请刷新重试。', true);
  } finally {
    loading = false;
    updateControls();
  }
}

void initialize();
