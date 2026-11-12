import { fetchSnapshot, snapshotMode } from './shell.js';

const byId = (id) => document.getElementById(id);
const refresh = byId('refresh-sectors');
const status = byId('sector-status');
const search = byId('sector-search');
const sort = byId('sector-sort');
const previous = byId('sector-previous');
const next = byId('sector-next');
const rowTemplate = byId('sector-row');
const detailPanel = byId('sector-detail');
const narrow = matchMedia('(max-width: 760px)');
const kindButtons = Array.from(document.querySelectorAll('#sector-kinds button'));
const formatNumber = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 });
const categoryNames = { industry: '行业', concept: '概念' };
const pageSize = 12;
const memberCache = new Map();
let overview = null;
let category = 'industry';
let selected = null;
let page = 0;
let detailVersion = 0;

async function request(path) {
  const response = await fetch(path, { cache: 'no-store', signal: AbortSignal.timeout(20000) });
  if (!response.ok) throw new Error('板块行情暂不可用，请稍后重试。');
  return response.json();
}

function currentGroup() {
  return overview?.groups[category];
}

function signed(value) {
  if (value == null) return '—';
  return (value > 0 ? '+' : '') + value.toFixed(2) + '%';
}

function showChange(element, value) {
  element.textContent = signed(value);
  element.classList.toggle('rise', value > 0);
  element.classList.toggle('fall', value < 0);
}

function showStatus(message, error = false) {
  status.textContent = message;
  status.toggleAttribute('data-error', error);
}

function closeDetail(returnFocus = false) {
  const previousId = selected;
  selected = null;
  detailVersion += 1;
  if (detailPanel.open) detailPanel.close();
  byId('sector-layout').classList.remove('has-detail');
  const active = byId('sector-rows').querySelector('.is-selected');
  active?.classList.remove('is-selected');
  active?.querySelector('button').setAttribute('aria-expanded', 'false');
  if (returnFocus && previousId) {
    (active?.querySelector('button') ?? search).focus();
  }
}

function openDetail() {
  if (!detailPanel.open) {
    if (narrow.matches) detailPanel.showModal();
    else detailPanel.show();
  }
  byId('detail-title').focus();
}

function rankingValue(row, group) {
  if (sort.value === 'amount') return row.amount_rank;
  if (sort.value === 'losers') return group.rows.length - row.rank + 1;
  return row.rank;
}

function renderRanking() {
  const group = currentGroup();
  if (!group) return;
  const query = search.value.trim().toLowerCase();
  const ordered = group.rows.filter((row) => row.name.toLowerCase().includes(query));
  ordered.sort((a, b) => (rankingValue(a, group) ?? Infinity) - (rankingValue(b, group) ?? Infinity) || a.rank - b.rank);
  page = Math.min(page, Math.max(0, Math.ceil(ordered.length / pageSize) - 1));
  const start = page * pageSize;
  const visible = ordered.slice(start, start + pageSize);
  byId('sector-rows').replaceChildren(...visible.map((row) => {
    const entry = rowTemplate.content.cloneNode(true).querySelector('tr');
    entry.classList.toggle('is-selected', row.id === selected);
    entry.querySelector('.sector-rank').textContent = rankingValue(row, group) ?? '—';
    const name = entry.querySelector('.sector-name');
    name.textContent = row.name;
    name.setAttribute('aria-label', '查看' + row.name + '板块');
    name.setAttribute('aria-controls', 'sector-detail');
    name.setAttribute('aria-expanded', String(row.id === selected));
    name.addEventListener('click', () => void selectSector(row, entry));
    showChange(entry.querySelector('.sector-change'), row.change_pct);
    entry.querySelector('.sector-amount').textContent = row.amount_yi == null ? '—' : formatNumber.format(row.amount_yi);
    return entry;
  }));
  byId('sector-range').textContent = ordered.length
    ? `${start + 1}–${start + visible.length} / ${ordered.length} 个板块`
    : '没有找到匹配的板块。';
  previous.disabled = page === 0;
  next.disabled = start + pageSize >= ordered.length;
}

function renderGroup() {
  const group = currentGroup();
  for (const button of kindButtons) button.setAttribute('aria-pressed', String(button.dataset.category === category));
  byId('sector-report').hidden = !group;
  byId('sector-meta').textContent = [overview?.trade_date && overview.trade_date + ' 收盘', group?.source].filter(Boolean).join(' · ');
  if (!group) {
    showStatus('暂无最近已收盘交易日的' + categoryNames[category] + '板块数据。');
    return;
  }
  showStatus(group.refresh_failed ? '更新暂不可用，显示同一交易日的已保存数据。' : '');
  const { up, down, flat, median_pct: median } = group.summary;
  for (const [index, [field, count]] of Object.entries({ up, down, flat }).entries()) {
    byId('sectors-' + field).textContent = count;
    byId('sector-bar').children[index].style.width = 100 * count / group.rows.length + '%';
  }
  byId('sector-summary').textContent = '同类板块涨跌中位数 ' + signed(median);
  const amountOption = sort.querySelector('option[value=amount]');
  amountOption.disabled = group.amount_coverage === 0;
  if (amountOption.disabled && sort.value === 'amount') sort.value = 'gainers';
  renderRanking();
}

function memberList(element, rows, emptyMessage) {
  if (!rows.length) {
    const item = document.createElement('li');
    item.className = 'market-muted';
    item.textContent = emptyMessage;
    element.replaceChildren(item);
    return;
  }
  element.replaceChildren(...rows.map((stock) => {
    const item = document.createElement('li');
    const name = document.createElement('span');
    const change = document.createElement('span');
    name.textContent = stock.name + ' ' + stock.code;
    showChange(change, stock.change_pct);
    item.append(name, change);
    return item;
  }));
}

async function selectSector(row, entry) {
  const group = currentGroup();
  const version = ++detailVersion;
  selected = row.id;
  const active = byId('sector-rows').querySelector('.is-selected');
  active?.classList.remove('is-selected');
  active?.querySelector('button').setAttribute('aria-expanded', 'false');
  entry.classList.add('is-selected');
  entry.querySelector('button').setAttribute('aria-expanded', 'true');
  byId('sector-layout').classList.add('has-detail');
  byId('detail-title').textContent = row.name;
  openDetail();

  const gap = row.change_pct - group.summary.median_pct;
  const comparison = Math.abs(gap) < .005
    ? '与同类中位数持平'
    : '比同类中位数' + (gap > 0 ? '高 ' : '低 ') + Math.abs(gap).toFixed(2) + ' 个百分点';
  byId('detail-relative').textContent = '涨幅第 ' + row.rank + ' / ' + group.rows.length + ' · ' + comparison;
  showChange(byId('detail-change'), row.change_pct);
  byId('detail-amount').textContent = row.amount_yi == null
    ? '暂无'
    : formatNumber.format(row.amount_yi) + ' 亿元 · 第 ' + row.amount_rank;
  byId('detail-count').textContent = row.member_count ?? '暂无';
  byId('detail-leader').textContent = row.leader ? row.leader + ' ' + signed(row.leader_change_pct) : '暂无';
  const memberStatus = byId('member-status');
  const memberPanel = byId('member-report');
  memberPanel.hidden = true;
  memberStatus.textContent = '正在读取成分股…';
  const key = [overview.trade_date, category, group.source, row.id].join('/');
  try {
    let detail = memberCache.get(key);
    if (!detail) {
      detail = snapshotMode ? overview.members?.[category]?.[row.id]
        : await request('/api/sectors/' + category + '/' + encodeURIComponent(row.id));
    }
    if (version !== detailVersion) return;
    if (!detail?.members || detail.trade_date !== overview.trade_date || detail.source !== group.source) {
      memberStatus.textContent = snapshotMode ? '本期快照未收录此板块的成分股明细。' : '暂无同日成分股明细。';
      return;
    }
    memberCache.set(key, detail);
    const { up, down, flat, leaders, laggards } = detail.members;
    byId('member-breadth').textContent = `有成交的样本中，上涨 ${up} 只、下跌 ${down} 只、平盘 ${flat} 只。`;
    memberList(byId('member-leaders'), leaders, '无上涨样本');
    memberList(byId('member-laggards'), laggards, '无下跌样本');
    memberStatus.textContent = '';
    memberPanel.hidden = false;
  } catch {
    if (version === detailVersion) memberStatus.textContent = '成分股明细暂不可用。';
  }
}

async function loadOverview(force = false) {
  refresh.disabled = true;
  showStatus(force ? '正在更新板块收盘数据…' : '正在读取板块收盘数据…');
  try {
    overview = snapshotMode ? await fetchSnapshot('sectors') : await request('/api/sectors' + (force ? '?refresh=true' : ''));
    closeDetail();
    memberCache.clear();
    page = 0;
    renderGroup();
  } catch (error) {
    showStatus(error.name === 'TimeoutError' ? '行情请求超时，请稍后更新。' : error.message, true);
  } finally {
    refresh.disabled = false;
  }
}

for (const button of kindButtons) {
  button.addEventListener('click', () => {
    category = button.dataset.category;
    search.value = '';
    page = 0;
    closeDetail();
    if (overview) renderGroup();
  });
}
search.addEventListener('input', () => { page = 0; renderRanking(); });
sort.addEventListener('change', () => { page = 0; renderRanking(); });
previous.addEventListener('click', () => { page -= 1; renderRanking(); });
next.addEventListener('click', () => { page += 1; renderRanking(); });
byId('close-detail').addEventListener('click', () => closeDetail(true));
detailPanel.addEventListener('cancel', (event) => {
  event.preventDefault();
  closeDetail(true);
});
narrow.addEventListener('change', () => {
  if (!detailPanel.open) return;
  detailPanel.close();
  openDetail();
});
refresh.addEventListener('click', () => void loadOverview(true));
if (snapshotMode) refresh.hidden = true;
void loadOverview();
