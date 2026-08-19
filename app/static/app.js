const state = {
  meta: null,
  dashboard: null,
  selectedStores: new Set(),
  sessionId: null,
  loading: false,
};

const $ = (selector) => document.querySelector(selector);
const currency = new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY', minimumFractionDigits: 2 });
const integer = new Intl.NumberFormat('zh-CN');
const svgNs = 'http://www.w3.org/2000/svg';
let toastTimer;

function iconRefresh() {
  if (window.lucide) window.lucide.createIcons();
}

function showToast(message) {
  const toast = $('#toast');
  toast.textContent = message;
  toast.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove('show'), 2200);
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const type = response.headers.get('content-type') || '';
  const payload = type.includes('application/json') ? await response.json() : null;
  if (!response.ok) throw new Error(payload?.error || `请求失败 (${response.status})`);
  return payload;
}

function queryString() {
  const params = new URLSearchParams({
    start_date: $('#start-date').value,
    end_date: $('#end-date').value,
  });
  if (state.selectedStores.size) params.set('store_ids', [...state.selectedStores].join(','));
  return params.toString();
}

function setLoading(loading) {
  state.loading = loading;
  $('#apply-filters').disabled = loading;
  $('#apply-filters').querySelector('span').textContent = loading ? '查询中' : '更新看板';
  $('#products-body').innerHTML = loading ? '<tr class="loading-row"><td colspan="6">正在读取统一指标服务…</td></tr>' : $('#products-body').innerHTML;
}

async function initialize() {
  try {
    state.meta = await api('/api/meta');
    const { min_date: min, max_date: max } = state.meta;
    for (const input of [$('#start-date'), $('#end-date')]) {
      input.min = min;
      input.max = max;
    }
    $('#start-date').value = min;
    $('#end-date').value = max;
    $('#dataset-status').textContent = `${min} — ${max} · ${state.meta.stores.length} 家门店`;
    $('#provider-chip').textContent = `AI · ${state.meta.ai_provider === 'deepseek' ? 'DeepSeek' : 'Mock'}`;
    renderStoreMenu();
    await loadDashboard();
  } catch (error) {
    $('#dataset-status').textContent = '数据连接失败';
    showToast(error.message);
  }
}

function renderStoreMenu() {
  $('#store-menu').innerHTML = state.meta.stores.map((store) => `
    <label class="store-option">
      <input type="checkbox" value="${store.store_id}">
      <span><strong>${store.store_name}</strong><small>${store.category} · ${store.district}</small></span>
    </label>`).join('');
  $('#store-menu').querySelectorAll('input').forEach((input) => input.addEventListener('change', () => {
    input.checked ? state.selectedStores.add(input.value) : state.selectedStores.delete(input.value);
    updateStoreLabel();
  }));
}

function updateStoreLabel() {
  const label = state.selectedStores.size === 0
    ? '全部门店'
    : state.selectedStores.size === 1
      ? state.meta.stores.find((store) => state.selectedStores.has(store.store_id)).store_name
      : `${state.selectedStores.size} 家门店`;
  $('#store-label').textContent = label;
}

async function loadDashboard() {
  if (!$('#start-date').value || !$('#end-date').value) return;
  if ($('#start-date').value > $('#end-date').value) {
    showToast('开始日期不能晚于结束日期');
    return;
  }
  setLoading(true);
  try {
    state.dashboard = await api(`/api/dashboard?${queryString()}`);
    renderOverview(state.dashboard.overview);
    renderTrend(state.dashboard.daily);
    renderProducts(state.dashboard.top_products);
    renderStores(state.dashboard.stores);
  } catch (error) {
    showToast(error.message);
  } finally {
    setLoading(false);
  }
}

function changeLabel(value) {
  if (value === null || value === undefined) return { text: '无上一周期数据', className: '' };
  const prefix = value > 0 ? '↑' : value < 0 ? '↓' : '—';
  return {
    text: `${prefix} ${Math.abs(value).toFixed(2)}% 较上一等长周期`,
    className: value > 0 ? 'up' : value < 0 ? 'down' : '',
  };
}

function renderOverview(overview) {
  $('#period-label').textContent = `${overview.period.start} — ${overview.period.end}`;
  $('#revenue-value').textContent = currency.format(Number(overview.revenue));
  $('#orders-value').textContent = integer.format(overview.order_count);
  $('#aov-value').textContent = currency.format(Number(overview.aov));
  [['#revenue-change', overview.changes.revenue_pct], ['#orders-change', overview.changes.orders_pct], ['#aov-change', overview.changes.aov_pct]]
    .forEach(([selector, value]) => {
      const label = changeLabel(value);
      const node = $(selector);
      node.textContent = label.text;
      node.className = label.className;
    });
}

function svgElement(name, attributes = {}) {
  const node = document.createElementNS(svgNs, name);
  Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, value));
  return node;
}

function renderTrend(rows) {
  const svg = $('#trend-chart');
  svg.replaceChildren();
  const width = 920;
  const height = 300;
  const bounds = { left: 58, right: 14, top: 15, bottom: 38 };
  const plotWidth = width - bounds.left - bounds.right;
  const plotHeight = height - bounds.top - bounds.bottom;
  const maxValue = Math.max(...rows.map((row) => row.revenue_cents), 1);
  const ceiling = Math.ceil(maxValue / 100000) * 100000;

  for (let line = 0; line <= 4; line += 1) {
    const y = bounds.top + (plotHeight * line / 4);
    svg.append(svgElement('line', { x1: bounds.left, y1: y, x2: width - bounds.right, y2: y, class: 'chart-grid' }));
    const label = svgElement('text', { x: bounds.left - 10, y: y + 3, 'text-anchor': 'end', class: 'chart-axis-label' });
    label.textContent = `¥${integer.format(Math.round((ceiling * (4 - line) / 4) / 100))}`;
    svg.append(label);
  }

  const points = rows.map((row, index) => {
    const x = bounds.left + (rows.length === 1 ? plotWidth / 2 : plotWidth * index / (rows.length - 1));
    const y = bounds.top + plotHeight - (row.revenue_cents / ceiling * plotHeight);
    return { x, y, row };
  });
  const path = points.map((point, index) => `${index ? 'L' : 'M'} ${point.x.toFixed(2)} ${point.y.toFixed(2)}`).join(' ');
  if (points.length) {
    const area = `${path} L ${points.at(-1).x} ${bounds.top + plotHeight} L ${points[0].x} ${bounds.top + plotHeight} Z`;
    svg.append(svgElement('path', { d: area, class: 'chart-area' }));
    svg.append(svgElement('path', { d: path, class: 'chart-line' }));
  }

  const labelEvery = Math.max(1, Math.ceil(rows.length / 7));
  points.forEach((point, index) => {
    if (index % labelEvery === 0 || index === points.length - 1) {
      const label = svgElement('text', { x: point.x, y: height - 12, 'text-anchor': 'middle', class: 'chart-axis-label' });
      label.textContent = point.row.date.slice(5);
      svg.append(label);
    }
    const circle = svgElement('circle', { cx: point.x, cy: point.y, r: 3.5, class: 'chart-point', tabindex: 0, 'aria-label': `${point.row.date}，营业额 ${point.row.revenue} 元` });
    const reveal = () => showChartTooltip(point, circle);
    circle.addEventListener('mouseenter', reveal);
    circle.addEventListener('focus', reveal);
    circle.addEventListener('mouseleave', hideChartTooltip);
    circle.addEventListener('blur', hideChartTooltip);
    svg.append(circle);
  });
}

function showChartTooltip(point) {
  const tooltip = $('#chart-tooltip');
  const shell = $('#chart-shell');
  const scaleX = shell.clientWidth / 920;
  const scaleY = shell.clientHeight / 300;
  tooltip.innerHTML = `<strong>${point.row.date}</strong><br>营业额 ${currency.format(Number(point.row.revenue))}<br>${integer.format(point.row.order_count)} 单 · 客单价 ${currency.format(Number(point.row.aov))}`;
  tooltip.style.left = `${point.x * scaleX}px`;
  tooltip.style.top = `${point.y * scaleY}px`;
  tooltip.hidden = false;
}

function hideChartTooltip() { $('#chart-tooltip').hidden = true; }

function renderProducts(rows) {
  $('#products-body').innerHTML = rows.length ? rows.map((row) => `
    <tr>
      <td class="rank">${String(row.rank).padStart(2, '0')}</td>
      <td class="product-name">${row.product_name}</td>
      <td>${row.product_category}</td>
      <td>${integer.format(row.qty)}</td>
      <td>${integer.format(row.order_count)}</td>
      <td class="revenue-cell">${currency.format(Number(row.revenue))}</td>
    </tr>`).join('') : '<tr class="loading-row"><td colspan="6">所选范围没有商品数据</td></tr>';
}

function renderStores(rows) {
  const max = Math.max(...rows.map((row) => row.revenue_cents), 1);
  $('#store-compare').innerHTML = rows.map((row) => `
    <div class="store-row" data-store-id="${row.store_id}">
      <div class="store-meta"><strong>${row.store_name}</strong><small>${row.category} · ${row.district}</small></div>
      <div class="store-bar-track"><div class="store-bar" style="width:${(row.revenue_cents / max * 100).toFixed(2)}%"></div></div>
      <div class="store-revenue">${currency.format(Number(row.revenue))}</div>
      <div class="store-aov">客单 ${currency.format(Number(row.aov))}</div>
    </div>`).join('');
}

function switchTab(target) {
  const products = target === 'products';
  $('#products-tab').classList.toggle('active', products);
  $('#stores-tab').classList.toggle('active', !products);
  $('#products-tab').setAttribute('aria-selected', String(products));
  $('#stores-tab').setAttribute('aria-selected', String(!products));
  $('#products-panel').hidden = !products;
  $('#stores-panel').hidden = products;
}

function appendUserMessage(text) {
  const wrapper = document.createElement('div');
  wrapper.className = 'message user-message';
  wrapper.innerHTML = `<div class="message-content"><p></p></div>`;
  wrapper.querySelector('p').textContent = text;
  $('#chat-log').append(wrapper);
  scrollChat();
}

function appendAssistantPlaceholder() {
  const wrapper = document.createElement('div');
  wrapper.className = 'message assistant-message';
  wrapper.innerHTML = '<div class="message-avatar"><i data-lucide="sparkles"></i></div><div class="message-content"><p class="thinking">正在查询</p><div class="evidence-line"></div></div>';
  $('#chat-log').append(wrapper);
  iconRefresh();
  scrollChat();
  return wrapper;
}

function scrollChat() {
  const log = $('#chat-log');
  log.scrollTop = log.scrollHeight;
}

async function askAssistant(message) {
  if (state.loading || !message.trim()) return;
  appendUserMessage(message.trim());
  const responseMessage = appendAssistantPlaceholder();
  const answer = responseMessage.querySelector('p');
  const evidenceLine = responseMessage.querySelector('.evidence-line');
  $('#suggestions').hidden = true;
  const sendButton = $('.send-command');
  sendButton.disabled = true;
  let finalResult = null;
  try {
    const response = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: message.trim(), session_id: state.sessionId }),
    });
    if (!response.ok || !response.body) throw new Error('AI 查询失败');
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let rendered = '';
    answer.textContent = '';
    answer.classList.remove('thinking');
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const events = buffer.split('\n\n');
      buffer = events.pop() || '';
      for (const block of events) {
        const lines = block.split('\n');
        const event = lines.find((line) => line.startsWith('event:'))?.slice(6).trim();
        const dataLine = lines.find((line) => line.startsWith('data:'));
        if (!event || !dataLine) continue;
        const data = JSON.parse(dataLine.slice(5).trim());
        if (event === 'delta') {
          rendered += data.content;
          answer.textContent = rendered;
          scrollChat();
        }
        if (event === 'done') finalResult = data;
      }
      if (done) break;
    }
    if (!finalResult) throw new Error('AI 流式响应不完整');
    state.sessionId = finalResult.session_id;
    renderEvidence(evidenceLine, finalResult);
  } catch (error) {
    answer.classList.remove('thinking');
    answer.textContent = error.message;
  } finally {
    sendButton.disabled = false;
    scrollChat();
  }
}

function renderEvidence(container, result) {
  if (!result.evidence) return;
  const chip = document.createElement('span');
  chip.className = 'evidence-chip';
  chip.innerHTML = '<i data-lucide="database"></i><span></span>';
  chip.querySelector('span').textContent = `${result.tool_call.name} · 已核验`;
  container.append(chip);
  if (result.chart_action) {
    const action = document.createElement('button');
    action.className = 'chart-action';
    action.type = 'button';
    action.textContent = '同步到看板';
    action.addEventListener('click', () => applyChartAction(result.chart_action));
    container.append(action);
  }
  iconRefresh();
}

async function applyChartAction(action) {
  $('#start-date').value = action.start_date;
  $('#end-date').value = action.end_date;
  state.selectedStores = new Set(action.store_ids || []);
  $('#store-menu').querySelectorAll('input').forEach((input) => { input.checked = state.selectedStores.has(input.value); });
  updateStoreLabel();
  await loadDashboard();
  if (action.view === 'stores') switchTab('stores');
  if (action.view === 'products') switchTab('products');
  $('#trend-view').scrollIntoView({ behavior: 'smooth', block: 'start' });
  showToast('看板已同步到 AI 查询范围');
}

async function openQuality() {
  const dialog = $('#quality-dialog');
  $('#quality-content').innerHTML = '<p class="quality-note">正在读取导入审计记录…</p>';
  dialog.showModal();
  try {
    const quality = await api('/api/data-quality');
    const labels = {
      duplicate: '规范化后重复',
      missing_or_invalid_amount: '金额缺失或非法',
      negative_amount: '负金额',
      nonpositive_qty: '非正数量',
      unknown_product: '不存在的商品外键',
      unknown_store: '不存在的门店外键',
      invalid_date: '无法识别的日期',
    };
    $('#quality-content').innerHTML = `
      <div class="quality-summary">
        <div><span>原始行</span><strong>${integer.format(quality.raw_rows)}</strong></div>
        <div><span>有效行</span><strong>${integer.format(quality.accepted_rows)}</strong></div>
        <div><span>隔离行</span><strong>${integer.format(quality.rejected_rows)}</strong></div>
      </div>
      <div class="quality-list">
        ${Object.entries(quality.reason_counts).map(([key, value]) => `<div class="quality-row"><span>${labels[key] || key}</span><strong>${integer.format(value)}</strong></div>`).join('')}
      </div>
      <p class="quality-note">有效率 ${quality.acceptance_rate.toFixed(2)}% · 单价校验差异 ${quality.price_mismatch_rows} 行<br>日期、货币符号和 ID 大小写格式已规范化；重复、业务无效值与脏外键保留在拒绝表中，不参与任何看板或 AI 指标。</p>`;
  } catch (error) {
    $('#quality-content').innerHTML = `<p class="quality-note">${error.message}</p>`;
  }
}

function bindEvents() {
  $('#apply-filters').addEventListener('click', loadDashboard);
  $('#store-toggle').addEventListener('click', () => {
    const menu = $('#store-menu');
    menu.hidden = !menu.hidden;
    $('#store-toggle').setAttribute('aria-expanded', String(!menu.hidden));
  });
  document.addEventListener('click', (event) => {
    if (!event.target.closest('.store-picker')) {
      $('#store-menu').hidden = true;
      $('#store-toggle').setAttribute('aria-expanded', 'false');
    }
  });
  $('#products-tab').addEventListener('click', () => switchTab('products'));
  $('#stores-tab').addEventListener('click', () => switchTab('stores'));
  $('#quality-open').addEventListener('click', openQuality);
  $('#quality-close').addEventListener('click', () => $('#quality-dialog').close());
  $('#quality-dialog').addEventListener('click', (event) => {
    if (event.target === $('#quality-dialog')) $('#quality-dialog').close();
  });
  $('#chat-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const input = $('#chat-input');
    const text = input.value;
    input.value = '';
    input.style.height = 'auto';
    askAssistant(text);
  });
  $('#chat-input').addEventListener('input', (event) => {
    event.target.style.height = 'auto';
    event.target.style.height = `${Math.min(event.target.scrollHeight, 100)}px`;
  });
  $('#chat-input').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      $('#chat-form').requestSubmit();
    }
  });
  $('#suggestions').querySelectorAll('button').forEach((button) => button.addEventListener('click', () => askAssistant(button.textContent)));
}

document.addEventListener('DOMContentLoaded', () => {
  iconRefresh();
  bindEvents();
  initialize();
});

