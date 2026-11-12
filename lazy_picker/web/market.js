import { fetchSnapshot, snapshotMode } from './shell.js';

const byId = (id) => document.getElementById(id);
const status = byId('market-status');
const refresh = byId('refresh-market');
const reportPanel = byId('market-report');
const options = byId('index-options');
const chart = byId('trend-chart');
const indexTemplate = byId('index-template');
const pointsFormat = new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const numberFormat = new Intl.NumberFormat('zh-CN');
let report = null;
let selected = '';

function signed(value) {
  return value == null ? '暂无' : (value > 0 ? '+' : '') + value.toFixed(2) + '%';
}

function changeText(element, value) {
  element.textContent = signed(value);
  element.classList.toggle('rise', value > 0);
  element.classList.toggle('fall', value < 0);
}

function setStatus(message, error = false) {
  status.textContent = message;
  status.toggleAttribute('data-error', error);
}

function showSectors(element, rows, placeholder) {
  const items = rows.map(({ name, change_pct }) => {
    const row = document.createElement('li');
    const label = document.createElement('span');
    const value = document.createElement('span');
    label.textContent = name;
    changeText(value, change_pct);
    row.append(label, value);
    return row;
  });
  if (!items.length) {
    const row = document.createElement('li');
    row.className = 'market-muted';
    row.textContent = placeholder;
    items.push(row);
  }
  element.replaceChildren(...items);
}

function currentIndex() {
  return report?.indices.find((index) => index.code === selected);
}

function paintChart() {
  const index = currentIndex();
  const width = chart.clientWidth;
  const height = chart.clientHeight;
  if (!index || !width || !height) return;

  const ratio = window.devicePixelRatio || 1;
  chart.width = Math.round(width * ratio);
  chart.height = Math.round(height * ratio);
  const context = chart.getContext('2d');
  context.scale(ratio, ratio);
  const style = getComputedStyle(chart);
  const color = (name) => style.getPropertyValue(name).trim();
  const series = index.series;
  const values = series.flatMap(({ close, ma20 }) => ma20 == null ? [close] : [close, ma20]);
  const minimum = Math.min(...values);
  const maximum = Math.max(...values);
  const padding = Math.max((maximum - minimum) * .07, maximum * .001);
  const low = minimum - padding;
  const high = maximum + padding;
  const left = 49;
  const right = width - 10;
  const top = 12;
  const bottom = height - 23;
  const x = (position) => left + position * (right - left) / Math.max(1, series.length - 1);
  const y = (value) => top + (high - value) * (bottom - top) / (high - low);

  context.font = '11px system-ui, sans-serif';
  context.fillStyle = color('--muted');
  context.strokeStyle = color('--line');
  context.lineWidth = 1;
  context.textAlign = 'right';
  for (let row = 0; row < 4; row += 1) {
    const value = low + (high - low) * row / 3;
    const offset = y(value);
    context.beginPath();
    context.moveTo(left, offset);
    context.lineTo(right, offset);
    context.stroke();
    context.fillText(Math.round(value).toString(), left - 7, offset + 4);
  }
  context.textAlign = 'left';
  context.fillText(series[0].date.slice(5), left, height - 5);
  context.textAlign = 'right';
  context.fillText(series.at(-1).date.slice(5), right, height - 5);

  for (const [field, token, lineWidth] of [['ma20', '--average', 1.5], ['close', '--green', 2.2]]) {
    context.beginPath();
    context.strokeStyle = color(token);
    context.lineWidth = lineWidth;
    let connected = false;
    series.forEach((point, position) => {
      if (point[field] == null) {
        connected = false;
      } else {
        if (connected) context.lineTo(x(position), y(point[field]));
        else context.moveTo(x(position), y(point[field]));
        connected = true;
      }
    });
    context.stroke();
  }
}

function showIndex(code) {
  selected = code;
  const index = currentIndex();
  if (!index) return;
  for (const button of options.querySelectorAll('button')) {
    button.setAttribute('aria-pressed', String(button.dataset.index === code));
  }
  byId('chart-title').textContent = index.name + ' · 近 ' + index.series.length + ' 个交易日';
  byId('trend-label').textContent = index.trend;
  byId('trend-detail').textContent = index.trend_detail;
  changeText(byId('change-5d'), index.change_5d);
  byId('ma20').textContent = index.ma20 == null ? '暂无' : pointsFormat.format(index.ma20);
  byId('ma60').textContent = index.ma60 == null ? '暂无' : pointsFormat.format(index.ma60);
  byId('index-source').textContent = index.source;
  chart.setAttribute('aria-label', index.name + '近 ' + index.series.length + ' 个交易日收盘价和 20 日均线，' + index.trend);
  paintChart();
}

function render(data) {
  report = data;
  byId('trade-date').textContent = data.trade_date + ' 收盘';
  byId('temperature-score').textContent = data.temperature.score;
  byId('temperature-label').textContent = data.temperature.label;
  const details = ['主要指数平均 ' + signed(data.temperature.index_change)];
  if (data.breadth && data.breadth.up + data.breadth.down > 0) {
    const { up, down } = data.breadth;
    details.push('上涨占有涨跌股票的 ' + Math.round(100 * up / (up + down)) + '%');
  }
  byId('temperature-reason').textContent = details.join(' · ');
  const breadth = data.breadth;
  byId('breadth-counts').hidden = !breadth;
  if (breadth) {
    for (const field of ['up', 'down', 'flat']) {
      byId(field + '-count').textContent = numberFormat.format(breadth[field]);
    }
  }
  byId('breadth-detail').textContent = breadth
    ? '样本成交额 ' + numberFormat.format(breadth.amount_yi) + ' 亿元 · ' + breadth.source
    : '暂无同日市场涨跌统计。';
  byId('limit-detail').textContent = data.limits
    ? '涨停 ' + data.limits.up + ' 只 · 跌停 ' + data.limits.down + ' 只'
    : '暂无同日涨跌停统计。';
  const missing = byId('missing-indices');
  missing.hidden = !data.missing_indices.length;
  missing.textContent = '暂无 ' + data.missing_indices.join('、');
  options.replaceChildren(...data.indices.map((index) => {
    const button = indexTemplate.content.cloneNode(true).querySelector('button');
    button.dataset.index = index.code;
    button.querySelector('.direction-title').textContent = index.name;
    button.querySelector('.index-close').textContent = pointsFormat.format(index.close);
    changeText(button.querySelector('.index-change'), index.change_pct);
    button.addEventListener('click', () => showIndex(index.code));
    return button;
  }));
  const sectors = data.sectors;
  byId('sector-source').textContent = sectors?.source ?? '';
  showSectors(byId('leaders'), sectors?.leaders ?? [], sectors ? '本日无上涨行业' : '暂无同日行业数据');
  showSectors(byId('laggards'), sectors?.laggards ?? [], sectors ? '本日无下跌行业' : '暂无同日行业数据');
  reportPanel.hidden = false;
  showIndex(currentIndex()?.code ?? data.indices[0].code);
  const messages = [];
  if (data.refresh_failed) messages.push('更新失败，显示上次取得的同日数据。');
  if (!breadth || !data.limits || data.missing_indices.length) messages.push('部分盘面数据暂缺，评分按可用行情计算。');
  setStatus(messages.join(' '));
}

async function loadMarket(force = false) {
  refresh.disabled = true;
  setStatus(force ? '正在更新收盘行情…' : '正在读取收盘行情…');
  try {
    if (snapshotMode) {
      render(await fetchSnapshot('market'));
      return;
    }
    const response = await fetch('/api/market' + (force ? '?refresh=true' : ''), {
      cache: 'no-store', signal: AbortSignal.timeout(35000),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '收盘行情暂不可用。');
    render(data);
  } catch (error) {
    setStatus(error.name === 'TimeoutError' ? '行情请求超时，请稍后更新。' : error.message, true);
  } finally {
    refresh.disabled = false;
  }
}

refresh.addEventListener('click', () => void loadMarket(true));
if (snapshotMode) refresh.hidden = true;
new ResizeObserver(paintChart).observe(chart);
document.addEventListener('picker-theme-change', paintChart);
void loadMarket();
