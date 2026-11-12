import { fetchSnapshot, snapshotMode } from './shell.js';

const timeFormat = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit',
});
const directionTerms = { short: '短线', swing: '波段', defensive: '低波', long: '长线' };

function dataSource(result) {
  if (!result) return '产品规则';
  const timestamp = new Date(result.snapshot_time);
  const date = Number.isNaN(timestamp.valueOf()) ? '' : `${timeFormat.format(timestamp)} 北京时间`;
  return ['候选快照', date, result.snapshot_source, result.stale ? '备用缓存' : ''].filter(Boolean).join(' · ');
}

function scoreExplanation(rules, strategy) {
  const weights = strategy.weights.map(({ name, pct }) => `${name} ${pct}%`).join('、');
  return `${rules.ranking}\n最多对初筛前 ${rules.shortlist_limit} 只做日线复核，展示最多 ${rules.candidate_limit} 只。\n${strategy.label}的权重为 ${weights}。`;
}

function stockAnswer(question, pick) {
  if (/怎么算|怎么来|扣分|分项|因子|评分|得分|分数|多少分/.test(question)) {
    const detail = pick.score_detail;
    if (!detail) return `${pick.name}本轮为 ${pick.score.toFixed(1)} 分。历史结果没有保留因子分和扣分，新一轮筛选会记录这些明细。`;
    const factors = detail.factors.map(({ name, score, weight_pct }) => `${name} ${score}（权重 ${weight_pct}%）`).join('、');
    return `${pick.name}最终 ${pick.score.toFixed(1)} 分，基础分 ${detail.base}，个股风险扣 ${detail.risk_penalty} 分，行业集中度扣 ${detail.portfolio_penalty} 分。\n${factors}。`;
  }
  if (/风险|风险点|稳不稳/.test(question)) return `${pick.name}本轮的风险提示：${pick.risk}`;
  if (/多久|重新评估|复查|持仓|观察/.test(question)) {
    return `${pick.name}对应策略的预计持仓区间为 ${pick.holding}。${pick.review}`;
  }
  if (/入选|为什么|上榜|理由/.test(question)) {
    return `${pick.name}（${pick.code}）本轮为 ${pick.score.toFixed(1)} 分。${pick.reason}\n风险：${pick.risk}\n预计持仓：${pick.holding}。${pick.review}`;
  }
  return null;
}

function ruleAnswer(question, rules, context) {
  const result = context.run?.result;
  const strategy = rules.strategies[context.direction];
  const picks = result?.candidates ?? [];
  const codes = question.match(/\b\d{6}\b/g) ?? [];
  const found = picks.filter((pick) => codes.includes(pick.code) || question.includes(pick.name));
  if (codes.some((code) => !picks.some((pick) => pick.code === code))) {
    return '所问股票代码不在当前候选名单里。可以先切换对应的持仓方向，或用「问这只」查看候选。';
  }
  if (found.length > 1) return '请每次指定一只候选，或使用名单里的「问这只」。';
  if (found.length === 1) return stockAnswer(question, found[0]);

  if (/大盘|盘面|均线/.test(question) && /怎么|依据|规则|权重|判断/.test(question)) return rules.market;
  if (/板块|行业|概念/.test(question) && /排名|排行|排序/.test(question)) return rules.sectors;
  const styles = Object.entries(rules.strategies)
    .filter(([key, item]) => question.includes(item.label) || question.includes(directionTerms[key]))
    .map(([, item]) => item);
  if (styles.length > 1) return styles.map((item) => `${item.label}：${item.focus} 预计持仓 ${item.holding}。`).join('\n');
  if (/多少天|多久|预计持仓|重新评估|复查/.test(question)) {
    const style = styles[0] ?? strategy;
    return `${style.label}的预计持仓区间为 ${style.holding}。${style.review}`;
  }
  if (/评分怎么算|分数怎么|怎么算分|因子|评分规则|权重|筛选逻辑|选股逻辑/.test(question)) {
    return scoreExplanation(rules, styles[0] ?? strategy);
  }
  const threshold = question.match(/(\d{1,3}(?:\.\d+)?)\s*分\s*(以上|及以上|起)/);
  if (threshold && Number(threshold[1]) <= 100 && !styles.some((item) => item !== strategy)) {
    if (!picks.length) return '当前没有候选分数可供核对。';
    const matched = picks.filter((pick) => pick.score >= Number(threshold[1]));
    return `本轮${strategy.label}有 ${matched.length} 只候选达到 ${threshold[1]} 分，最高 ${picks[0].score.toFixed(1)} 分。${matched.length ? '\n' + matched.map((pick) => `${pick.name} ${pick.score.toFixed(1)} 分`).join('、') + '。' : ''}`;
  }
  if (/分数.*(偏低|不高|低)|低分|高分|最高分|第一名/.test(question) && !styles.some((item) => item !== strategy)) {
    const best = picks.length ? `本轮${strategy.label}的候选分数为 ${picks.at(-1).score.toFixed(1)}–${picks[0].score.toFixed(1)}。` : '当前没有候选分数。';
    return `${best}评分用于比较同方向的策略匹配度，由多个因子加权，再考虑个股和集中度风险。当前按最终分排序展示最多 ${rules.candidate_limit} 只，没有最低展示分。`;
  }
  if (/数据.*(时间|日期|哪天|更新|来源)|哪天.*行情|快照.*(时间|日期)/.test(question)) {
    return result ? dataSource(result) + '。' : '当前方向还没有可核对的筛选快照。';
  }
  return null;
}

export function initAssistant(getContext) {
  const byId = (id) => document.getElementById(id);
  const dialog = byId('assistant');
  const form = byId('assistant-form');
  const input = byId('assistant-question');
  const send = byId('assistant-send');
  const mode = byId('assistant-mode');
  const body = dialog.querySelector('.assistant-body');
  const suggestions = Array.from(dialog.querySelectorAll('.assistant-suggestions button'));
  let settings;
  let context;
  let active;

  function show(question, answer, source = '') {
    byId('assistant-asked').textContent = question;
    byId('assistant-answer').textContent = answer;
    byId('assistant-source').textContent = source;
  }

  function setPending(pending) {
    send.disabled = pending;
    for (const button of suggestions) button.disabled = pending;
  }

  function getSettings() {
    if (!settings) {
      const load = snapshotMode ? fetchSnapshot('assistant')
        : fetch('/api/assistant/rules', { signal: AbortSignal.timeout(8000) }).then((response) => {
          if (!response.ok) throw new Error('Rules unavailable');
          return response.json();
        });
      settings = load.catch((error) => { settings = null; throw error; });
    }
    return settings;
  }

  async function ask(question, preferRules = false) {
    if (!question || active) return;
    const controller = new AbortController();
    active = controller;
    const selection = context;
    setPending(true);
    show(question, '正在查阅产品规则和候选结果…');
    try {
      const config = await getSettings();
      if (controller.signal.aborted) return;
      if (config.ai_available && !preferRules) {
        try {
          const response = await fetch('/api/assistant', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ direction: selection.direction, run_id: selection.run?.id ?? null, question }),
            signal: AbortSignal.any([controller.signal, AbortSignal.timeout(27000)]),
          });
          const reply = response.ok ? await response.json() : null;
          if (controller.signal.aborted) return;
          if (reply?.mode === 'ai' && reply.answer) {
            show(question, reply.answer, 'DeepSeek · 产品规则与已保存的快照');
            return;
          }
        } catch {
          if (controller.signal.aborted) return;
        }
        mode.textContent = 'DeepSeek 暂不可用，已切换规则问答。';
      }
      const answer = ruleAnswer(question, config.rules, selection);
      show(question, answer ?? '规则问答暂未覆盖这个问题。可以试试“评分怎么算”“有没有 80 分以上的”，或从候选名单点「问这只」。',
        answer ? `规则问答 · ${dataSource(selection.run?.result)}` : '');
    } catch {
      if (!controller.signal.aborted) show(question, '暂时无法读取问答规则，请重试。');
    } finally {
      if (active === controller) {
        active = null;
        setPending(false);
        if (body.scrollHeight > body.clientHeight) body.scrollTop = byId('assistant-reply').offsetTop - body.offsetTop;
      }
    }
  }

  function syncViewport() {
    const viewport = window.visualViewport;
    if (!dialog.open || !viewport) return;
    dialog.style.setProperty('--assistant-viewport', `${viewport.height}px`);
    dialog.style.setProperty('--assistant-keyboard', `${Math.max(0, innerHeight - viewport.height - viewport.offsetTop)}px`);
  }

  function open(question = '') {
    active?.abort();
    active = null;
    setPending(false);
    context = getContext();
    input.value = '';
    byId('assistant-context').textContent = context.run?.result?.direction_label ?? '当前持仓方向';
    show('', '可以问候选为什么入选、分数怎么来的，或什么时候重新评估。');
    mode.textContent = '正在读取问答规则…';
    dialog.showModal();
    byId('assistant-title').focus();
    syncViewport();
    const openedContext = context;
    void getSettings().then((config) => {
      if (!dialog.open || context !== openedContext) return;
      byId('assistant-context').textContent = config.rules.strategies[openedContext.direction].label;
      mode.textContent = config.ai_available ? '自由提问会发送给 DeepSeek。'
        : snapshotMode ? '收盘快照 · 规则问答' : '规则问答 · 配置 DeepSeek 后可自由提问';
    }).catch(() => {
      if (dialog.open && context === openedContext) mode.textContent = '暂时无法加载规则。';
    });
    if (question) void ask(question, true);
  }

  byId('open-assistant').addEventListener('click', () => open());
  byId('close-assistant').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => {
    if (dialog.open) return;
    active?.abort();
    active = null;
    setPending(false);
  });
  window.visualViewport?.addEventListener('resize', syncViewport);
  window.visualViewport?.addEventListener('scroll', syncViewport);
  for (const button of suggestions) button.addEventListener('click', () => void ask(button.textContent, true));
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const question = input.value.trim();
    if (!question) return;
    input.blur();
    void ask(question);
  });
  return open;
}
