// Trading System B3 — terminal (U1). JavaScript sem build; só lê a API local.
const TOKEN = document.querySelector('meta[name="ts-token"]').content;
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const nf = (d = 2) => new Intl.NumberFormat('pt-BR', { minimumFractionDigits: d, maximumFractionDigits: d });
const fmt = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v) ? '—' : nf(d).format(v));
const pct = (v, d = 2) => (v === null || v === undefined ? '—' : `${v > 0 ? '+' : ''}${nf(d).format(v * 100)}%`);
const brl = (v) => (v === null || v === undefined ? '—' : `R$ ${nf(0).format(v)}`);
const dmy = (iso) => (iso ? iso.slice(0, 10).split('-').reverse().join('/') : '—');
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const store = {
  get(k, d) { try { const v = localStorage.getItem('ts.' + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem('ts.' + k, JSON.stringify(v)); } catch { /* sem storage */ } },
};

const state = {
  status: null, watch: [], selected: store.get('selected', null), period: store.get('period', '1A'),
  sort: store.get('sort', { key: 'ticker', dir: 1 }), filters: new Set(store.get('filters', [])),
  favs: new Set(store.get('favs', [])), tab: store.get('tab', 'positions'),
  layers: store.get('layers', { sma200: true, sma50: true, ema20: true, volume: true, signals: true }),
};

async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, headers: { 'X-TS-Token': TOKEN, ...(opts.headers || {}) } });
  if (!r.ok) { let m = r.statusText; try { m = (await r.json()).detail || m; } catch { /* */ } throw new Error(m); }
  return r.json();
}

function toast(msg, kind = '') {
  const el = document.createElement('div');
  el.className = `toast ${kind}`; el.textContent = msg;
  $('#toasts').appendChild(el); setTimeout(() => el.remove(), 7000);
}

// ======================================================================== aparência
function applyLook() {
  const root = document.documentElement;
  root.dataset.theme = store.get('theme', 'dark');
  root.dataset.colors = store.get('colors', 'classic');
  root.dataset.density = store.get('density', 'comfortable');
  const sizes = store.get('sizes', {});
  for (const [k, v] of Object.entries(sizes)) root.style.setProperty(`--${k}`, v);
  const collapsed = store.get('collapsed', {});
  $('#layout').classList.toggle('no-watch', !!collapsed.watch);
  $('#layout').classList.toggle('no-inspector', !!collapsed.inspector);
  $('#center').classList.toggle('no-terminal', !!collapsed.terminal);
  if (chart) styleChart();
}

function setupSettings() {
  const dlg = $('#dlg-settings');
  $('#btn-settings').onclick = () => {
    $('#set-theme').value = store.get('theme', 'dark'); $('#set-colors').value = store.get('colors', 'classic');
    $('#set-density').value = store.get('density', 'comfortable'); dlg.showModal();
  };
  for (const [id, key] of [['#set-theme', 'theme'], ['#set-colors', 'colors'], ['#set-density', 'density']]) {
    $(id).onchange = (e) => { store.set(key, e.target.value); applyLook(); renderChart(); };
  }
  $('#set-reset-layout').onclick = () => {
    store.set('sizes', {}); store.set('collapsed', {});
    for (const k of ['watch-w', 'inspector-w', 'terminal-h']) document.documentElement.style.removeProperty(`--${k}`);
    applyLook(); toast('Layout restaurado.');
  };
  $('#dlg-settings-close').onclick = () => dlg.close();
}

// ======================================================================== splitters / recolher
function setupSplitters() {
  const varFor = { watch: 'watch-w', inspector: 'inspector-w', terminal: 'terminal-h' };
  for (const sp of $$('.splitter')) {
    const which = sp.dataset.resize;
    sp.addEventListener('pointerdown', (ev) => {
      sp.setPointerCapture(ev.pointerId);
      const start = { x: ev.clientX, y: ev.clientY };
      const el = $('#' + which);
      const startSize = which === 'terminal' ? el.getBoundingClientRect().height : el.getBoundingClientRect().width;
      const move = (e) => {
        let size;
        if (which === 'watch') size = startSize + (e.clientX - start.x);
        else if (which === 'inspector') size = startSize - (e.clientX - start.x);
        else size = startSize - (e.clientY - start.y);
        size = Math.max(which === 'terminal' ? 80 : 180, Math.min(size, which === 'terminal' ? window.innerHeight * 0.7 : 640));
        document.documentElement.style.setProperty(`--${varFor[which]}`, `${size}px`);
      };
      const up = () => {
        sp.removeEventListener('pointermove', move); sp.removeEventListener('pointerup', up);
        const sizes = store.get('sizes', {});
        sizes[varFor[which]] = getComputedStyle(document.documentElement).getPropertyValue(`--${varFor[which]}`).trim();
        store.set('sizes', sizes);
      };
      sp.addEventListener('pointermove', move); sp.addEventListener('pointerup', up);
    });
  }
  for (const b of $$('[data-collapse]')) {
    b.onclick = () => {
      const c = store.get('collapsed', {}); c[b.dataset.collapse] = !c[b.dataset.collapse];
      store.set('collapsed', c); applyLook(); renderReopen();
    };
  }
  renderReopen();
}

function renderReopen() {
  const c = store.get('collapsed', {});
  for (const which of ['watch', 'inspector']) {
    let b = $('#reopen-' + which);
    if (c[which] && !b) {
      b = document.createElement('button'); b.id = 'reopen-' + which; b.className = 'reopen';
      b.textContent = which === 'watch' ? '⟩' : '⟨'; b.title = which === 'watch' ? 'Mostrar Market Watch' : 'Mostrar Raio-X';
      b.onclick = () => { const cc = store.get('collapsed', {}); cc[which] = false; store.set('collapsed', cc); applyLook(); renderReopen(); };
      document.body.appendChild(b);
    } else if (!c[which] && b) b.remove();
  }
}

// ======================================================================== status bar
async function loadStatus() {
  try { state.status = await api('/api/status'); } catch (e) { $('#st-data').innerHTML = `<b>API indisponível</b>`; return; }
  const s = state.status;
  const mode = $('#st-mode');
  mode.textContent = s.mode === 'live' ? 'REAL' : 'PAPER';
  mode.className = `pill ${s.mode === 'live' ? 'live' : 'paper'}`;
  mode.title = s.mode === 'live' ? 'Ordens reais na corretora' : 'Simulação com dados reais (sem ordens na corretora)';
  const d = s.data;
  $('#st-data').className = `sb-item ${d.stale ? 'bad' : ''}`;
  $('#st-data').innerHTML = `Dados: <b>${d.last_bar ? dmy(d.last_bar) : 'sem cotações'}</b>${d.stale ? ' · desatualizado' : ''}`;
  $('#st-data').title = d.stale ? `Falta a barra do pregão ${dmy(d.last_closed_session)}` : 'Barra do último pregão fechado presente';
  const e = s.last_eod;
  const eodEl = $('#st-eod');
  if (!e) { eodEl.className = 'sb-item'; eodEl.innerHTML = 'Último EOD: <b>nenhum</b>'; }
  else {
    const ok = e.status === 'OK';
    eodEl.className = `sb-item ${ok ? 'okc' : 'bad'}`;
    eodEl.innerHTML = `Último EOD: <b>${dmy(e.session_date)} ${ok ? '' : '· ' + e.status}</b>`;
    eodEl.title = e.error || 'Rotina de fim de dia';
  }
  const n = s.next_event;
  $('#st-next').innerHTML = n.at ? `Próximo: <b>${n.label} ${dmy(n.at)} ${n.at.slice(11, 16)}</b>` : '';
  $('#st-conn').innerHTML = `Fonte: <b>${esc(s.connection.label)}</b>`; $('#st-conn').title = s.connection.detail || '';
  const v = s.version || {};
  $('#st-update').classList.toggle('hidden', !v.update_available);
  const cb = s.circuit_breaker || {}; const banner = $('#cb-banner');
  banner.className = 'banner ' + (cb.level === 2 ? 'l2' : cb.level === 1 ? 'l1' : 'hidden');
  banner.textContent = cb.level === 2 ? `Circuit breaker nível 2: drawdown ${pct(cb.drawdown, 1)} — novas entradas suspensas; sinais seguem na carteira sombra.`
    : cb.level === 1 ? `Circuit breaker nível 1: drawdown ${pct(cb.drawdown, 1)} — risco por operação reduzido.` : '';
}

function setupUpdate() {
  const dlg = $('#dlg-update');
  $('#st-update').onclick = () => {
    const v = state.status?.version || {};
    $('#dlg-update-list').innerHTML = (v.pending || []).slice(0, 15).map((l) => `<li>${esc(l)}</li>`).join('') || '<li>Correções e melhorias</li>';
    dlg.showModal();
  };
  $('#dlg-update-cancel').onclick = () => dlg.close();
  $('#dlg-update-go').onclick = async () => { dlg.close(); await runAction('update', 'Atualizando e rodando os testes…'); };
}

// ======================================================================== ações (jobs)
async function runAction(kind, label) {
  try {
    const { job } = await api(`/api/actions/${kind}`, { method: 'POST' });
    toast(label);
    for (;;) {
      await new Promise((r) => setTimeout(r, 2000));
      const j = await api(`/api/jobs/${job}`);
      if (j.status === 'done') {
        toast(j.message || `Concluído (código ${j.code}).`, j.code === 0 ? 'ok' : 'err');
        if (kind === 'update' && j.code === 0) setTimeout(() => location.reload(), 1500);
        await refreshAll(); return j;
      }
    }
  } catch (e) { toast(e.message, 'err'); return null; }
}

// ======================================================================== market watch
function passFilters(r) {
  const q = $('#watch-filter').value.trim().toUpperCase();
  if (q && !r.ticker.includes(q)) return false;
  if (state.filters.has('signal') && !r.signal) return false;
  if (state.filters.has('held') && !r.held) return false;
  if (state.filters.has('up') && r.trend !== 'up') return false;
  if (state.filters.has('fav') && !state.favs.has(r.ticker)) return false;
  return true;
}

function renderWatch() {
  const { key, dir } = state.sort;
  const val = (r) => (key === 'fav' ? (state.favs.has(r.ticker) ? 0 : 1) : key === 'signal' ? ({ accepted: 0, rejected: 1 }[r.signal] ?? 2) : r[key]);
  const rows = state.watch.filter((r) => r.is_index || passFilters(r)).sort((a, b) => {
    if (a.is_index !== b.is_index) return a.is_index ? -1 : 1;
    const x = val(a), y = val(b);
    if (x === y) return a.ticker.localeCompare(b.ticker);
    if (x === null || x === undefined) return 1; if (y === null || y === undefined) return -1;
    return (x > y ? 1 : -1) * dir;
  });
  $('#watch-table tbody').innerHTML = rows.map((r) => {
    if (!r.has_data) return `<tr data-t="${r.ticker}" class="stale"><td></td><td>${r.ticker}</td><td colspan="5" class="muted">sem cotações</td></tr>`;
    const chg = r.change_pct === null ? '—' : `<span class="${r.change_pct >= 0 ? 'up' : 'down'}">${pct(r.change_pct)}</span>`;
    const trend = r.trend ? `<span class="trend ${r.trend}" title="${r.trend === 'up' ? 'Fechamento acima da média de 200 dias' : 'Abaixo da média de 200 dias'}">${r.trend === 'up' ? 'Alta' : 'Baixa'}</span>` : '—';
    const sig = r.signal ? `<span class="sig ${r.signal}" title="${esc(r.signals.map((s) => `${s.setup_name}: ${s.result}${s.reason ? ' — ' + s.reason : ''}`).join('\n'))}">${r.signal === 'accepted' ? 'aceito' : 'recusado'}</span>` : '';
    return `<tr data-t="${r.ticker}" class="${r.is_index ? 'index' : ''} ${r.stale ? 'stale' : ''} ${state.selected === r.ticker ? 'sel' : ''}" title="${r.stale ? 'Sem a barra do último pregão' : ''}">
      <td>${r.is_index ? '' : `<span class="star ${state.favs.has(r.ticker) ? 'on' : ''}" data-fav="${r.ticker}" role="button" aria-label="Favorito">★</span>`}</td>
      <td>${r.ticker}${r.held ? '<span class="held-ico" title="Em carteira">💼</span>' : ''}</td>
      <td class="num">${fmt(r.last, r.is_index ? 0 : 2)}</td><td class="num">${chg}</td>
      <td>${trend}</td><td class="num">${r.atr_pct === null ? '—' : nf(1).format(r.atr_pct * 100)}</td><td>${sig}</td></tr>`;
  }).join('');
}

function setupWatch() {
  $('#watch-filter').oninput = renderWatch;
  for (const c of $$('.chip')) {
    c.classList.toggle('on', state.filters.has(c.dataset.filter));
    c.onclick = () => {
      const f = c.dataset.filter; state.filters.has(f) ? state.filters.delete(f) : state.filters.add(f);
      c.classList.toggle('on'); store.set('filters', [...state.filters]); renderWatch();
    };
  }
  for (const th of $$('#watch-table th')) {
    th.onclick = () => {
      const k = th.dataset.sort; state.sort = { key: k, dir: state.sort.key === k ? -state.sort.dir : 1 };
      store.set('sort', state.sort); renderWatch();
    };
  }
  $('#watch-table tbody').onclick = (ev) => {
    const fav = ev.target.closest('[data-fav]');
    if (fav) {
      const t = fav.dataset.fav; state.favs.has(t) ? state.favs.delete(t) : state.favs.add(t);
      store.set('favs', [...state.favs]); renderWatch(); return;
    }
    const tr = ev.target.closest('tr[data-t]'); if (tr) select(tr.dataset.t);
  };
  document.addEventListener('keydown', (ev) => {
    if (!['ArrowDown', 'ArrowUp'].includes(ev.key) || ev.target.matches('input, select')) return;
    const rows = $$('#watch-table tbody tr[data-t]'); const i = rows.findIndex((r) => r.dataset.t === state.selected);
    const next = rows[Math.max(0, Math.min(rows.length - 1, i + (ev.key === 'ArrowDown' ? 1 : -1)))];
    if (next) { ev.preventDefault(); select(next.dataset.t); next.scrollIntoView({ block: 'nearest' }); }
  });
}

async function loadWatch() {
  try { state.watch = await api('/api/watchlist'); } catch (e) { toast(`Market Watch: ${e.message}`, 'err'); return; }
  if (!state.selected || !state.watch.some((r) => r.ticker === state.selected)) {
    state.selected = (state.watch.find((r) => r.ticker === 'PETR4' && r.has_data) || state.watch.find((r) => r.has_data && !r.is_index) || {}).ticker || null;
  }
  renderWatch(); renderInspector();
}

function select(t) {
  state.selected = t; store.set('selected', t);
  for (const tr of $$('#watch-table tbody tr')) tr.classList.toggle('sel', tr.dataset.t === t);
  renderChart(); renderInspector();
}

// ======================================================================== gráfico
let chart = null; let series = {}; let markers = null; let chartData = null;

function css(v) { return getComputedStyle(document.documentElement).getPropertyValue(v).trim(); }

function styleChart() {
  chart.applyOptions({
    layout: { background: { color: css('--panel') }, textColor: css('--muted'), fontFamily: css('--font') },
    grid: { vertLines: { color: css('--line') + '55' }, horzLines: { color: css('--line') + '55' } },
    rightPriceScale: { borderColor: css('--line') }, timeScale: { borderColor: css('--line') },
  });
  const up = css('--up'), down = css('--down');
  series.candles?.applyOptions({ upColor: up, downColor: down, wickUpColor: up, wickDownColor: down });
}

function ensureChart() {
  if (chart) return;
  const LW = window.LightweightCharts;
  chart = LW.createChart($('#chart'), {
    autoSize: true,
    localization: { locale: 'pt-BR', dateFormat: 'dd/MM/yyyy', priceFormatter: (p) => nf(2).format(p) },
    crosshair: { mode: LW.CrosshairMode.Normal },
    timeScale: { rightOffset: 4, barSpacing: 7 },
  });
  series.candles = chart.addSeries(LW.CandlestickSeries, { borderVisible: false, priceLineVisible: true });
  series.volume = chart.addSeries(LW.HistogramSeries, { priceFormat: { type: 'volume' }, priceScaleId: 'vol', lastValueVisible: false, priceLineVisible: false });
  chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
  const lineOpts = { lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false };
  series.sma200 = chart.addSeries(LW.LineSeries, { ...lineOpts, color: '#9aa4b2', lineWidth: 2 });
  series.sma50 = chart.addSeries(LW.LineSeries, { ...lineOpts, color: '#4aa3ff' });
  series.ema20 = chart.addSeries(LW.LineSeries, { ...lineOpts, color: '#f5a623' });
  markers = LW.createSeriesMarkers(series.candles, []);
  const tip = document.createElement('div'); tip.id = 'chart-tip'; tip.className = 'hidden'; $('#chart-panel').appendChild(tip);
  chart.subscribeCrosshairMove((p) => {
    if (!p.time || !p.point || !chartData) { tip.classList.add('hidden'); return; }
    const c = p.seriesData.get(series.candles); if (!c) { tip.classList.add('hidden'); return; }
    const sigs = chartData.signals.filter((s) => s.time === p.time);
    tip.innerHTML = `<b>${dmy(String(p.time))}</b> &nbsp; A ${fmt(c.open)} · M ${fmt(c.high)} · m ${fmt(c.low)} · F ${fmt(c.close)}`
      + sigs.map((s) => `<div>${s.accepted ? '★' : '✕'} ${esc(s.text)}</div>`).join('');
    tip.classList.remove('hidden');
    const x = Math.min(p.point.x + 16, $('#chart').clientWidth - 350);
    tip.style.left = `${x}px`; tip.style.top = `${p.point.y + 52}px`;
  });
  styleChart();
}

async function renderChart() {
  const t = state.selected; const empty = $('#chart-empty');
  if (!t) { $('#chart-title').textContent = '—'; empty.innerHTML = emptyNoData(); empty.classList.remove('hidden'); bindEmptyActions(); return; }
  ensureChart();
  try { chartData = await api(`/api/chart/${encodeURIComponent(t)}?period=${state.period}`); }
  catch (e) { empty.innerHTML = `<h3>Sem gráfico para ${esc(t)}</h3><p>${esc(e.message)}</p>`; empty.classList.remove('hidden'); return; }
  empty.classList.add('hidden');
  const up = css('--up'), down = css('--down');
  series.candles.setData(chartData.candles);
  series.volume.setData(chartData.volume.map((v) => ({ time: v.time, value: v.value, color: (v.up ? up : down) + '66' })));
  for (const k of ['sma200', 'sma50', 'ema20']) series[k].setData(chartData.lines[k]);
  for (const k of ['sma200', 'sma50', 'ema20', 'volume']) series[k].applyOptions({ visible: !!state.layers[k] });
  markers.setMarkers(state.layers.signals ? chartData.signals.map((s) => ({
    time: s.time, position: s.accepted ? 'belowBar' : 'aboveBar', shape: s.accepted ? 'arrowUp' : 'circle',
    color: s.accepted ? '#f5c542' : '#8b939e', text: s.setup,
  })) : []);
  chart.timeScale().fitContent();
  const w = state.watch.find((r) => r.ticker === t) || {};
  $('#chart-title').textContent = t;
  $('#chart-sub').innerHTML = w.last !== undefined ? `${fmt(w.last, w.is_index ? 0 : 2)} <span class="${(w.change_pct ?? 0) >= 0 ? 'up' : 'down'}">${pct(w.change_pct)}</span> · D1 · preços ajustados por proventos` : '';
}

function setupChartControls() {
  for (const b of $$('#period button')) {
    b.classList.toggle('on', b.dataset.p === state.period);
    b.onclick = () => { state.period = b.dataset.p; store.set('period', state.period); for (const x of $$('#period button')) x.classList.toggle('on', x === b); renderChart(); };
  }
  for (const cb of $$('#layers input')) {
    cb.checked = !!state.layers[cb.dataset.layer];
    cb.onchange = () => { state.layers[cb.dataset.layer] = cb.checked; store.set('layers', state.layers); renderChart(); };
  }
}

// ======================================================================== Raio-X (U1: resumo; checklist completo na U3)
function renderInspector() {
  const body = $('#inspector-body'); const w = state.watch.find((r) => r.ticker === state.selected);
  if (!w) { body.innerHTML = '<p class="note">Selecione um ativo no Market Watch.</p>'; return; }
  const sigs = (w.signals || []).map((s) => `<div class="check ${s.disposition === 'ACCEPTED' ? 'ok' : 'no'}"><span><b>${esc(s.setup_name)}</b> — ${esc(s.result)}${s.reason ? `<br><span class="note">${esc(s.reason)}</span>` : ''}</span></div>`).join('');
  body.innerHTML = `
    <div class="card"><h4>${esc(w.ticker)}</h4><dl class="kv">
      <dt>Último</dt><dd>${fmt(w.last, w.is_index ? 0 : 2)}</dd>
      <dt>Variação</dt><dd class="${(w.change_pct ?? 0) >= 0 ? 'up' : 'down'}">${pct(w.change_pct)}</dd>
      <dt>Tendência primária</dt><dd>${w.trend === 'up' ? 'Alta (acima da SMA 200)' : w.trend === 'down' ? 'Baixa (abaixo da SMA 200)' : '—'}</dd>
      <dt>ATR 14</dt><dd>${w.atr_pct == null ? '—' : nf(2).format(w.atr_pct * 100) + '% do preço'}</dd>
      <dt>Última barra</dt><dd>${dmy(w.last_date)}</dd></dl></div>
    <div class="card"><h4>Decisões no último pregão processado</h4>
      ${sigs || `<p class="note">${state.status?.paper_started ? 'Nenhum setup disparou para este ativo.' : 'O paper ainda não rodou nenhum EOD.'}</p>`}</div>
    <p class="note">Em breve: checklist de cada condição (valor medido × limite) para explicar por que o ativo entrou ou não.</p>`;
}

// ======================================================================== terminal inferior
function emptyNoData() {
  const s = state.status;
  if (!s) return '<h3>Carregando…</h3>';
  if (!s.data.last_bar) return `<h3>Sem cotações ainda</h3><p>Baixe o histórico desde 2019 para começar (1–3 min).</p><div class="actions"><button class="primary" data-act="sync">Baixar cotações</button></div>`;
  return '<h3>Selecione um ativo no Market Watch</h3>';
}

function eodAction() {
  const s = state.status; if (!s) return '';
  if (!s.eod_pending) return `<p class="note">O pregão ${dmy(s.data.last_closed_session)} já foi processado. Próximo EOD: ${s.next_event.at ? dmy(s.next_event.at) + ' ' + s.next_event.at.slice(11, 16) : '—'}.</p>`;
  return `<div class="actions"><button class="primary" data-act="eod">Rodar o EOD agora (pregão ${dmy(s.data.last_closed_session)})</button></div>`;
}

function emptyPaper() {
  return `<div class="empty"><h3>O paper trading ainda não começou</h3>
    <p>A rotina de fim de dia roda sozinha às 19:30 em dias úteis: baixa as cotações, gera os sinais, simula as ordens e preenche estas abas.</p>
    ${eodAction()}</div>`;
}

let eqChart = null;
function renderEquity(body, data) {
  const sm = data.summary;
  if (!sm) { body.innerHTML = emptyPaper(); return; }
  const k = (l, v, cls = '') => `<div class="kpi"><div class="l">${l}</div><div class="v ${cls}">${v}</div></div>`;
  body.innerHTML = `<div class="equity-wrap"><div class="kpis">
      ${k('Patrimônio', brl(sm.equity))}${k('Retorno', pct(sm.return_pct), sm.return_pct >= 0 ? 'up' : 'down')}
      ${k('Drawdown atual', pct(-sm.drawdown_pct, 1), sm.drawdown_pct > 0 ? 'down' : '')}${k('Drawdown máx.', pct(-sm.max_drawdown_pct, 1))}
      ${k('Risco aberto (heat)', pct(sm.heat_pct))}${k('Caixa', brl(sm.cash))}
      ${k('Operações encerradas', sm.trades)}${k('Esperança', sm.expectancy_r == null ? '—' : `${fmt(sm.expectancy_r)} R`)}
    </div><div id="equity-chart"></div></div>`;
  const el = $('#equity-chart');
  if (eqChart) { eqChart.remove(); eqChart = null; }
  if (data.paper.length < 2) {
    el.innerHTML = `<div class="empty"><h3>Paper iniciado em ${dmy(sm.start)}</h3>
      <p>A curva de patrimônio aparece a partir do 2º pregão processado (${sm.sessions} até agora).</p>${eodAction()}</div>`;
    bindEmptyActions(); return;
  }
  const LW = window.LightweightCharts;
  eqChart = LW.createChart(el, {
    autoSize: true, layout: { background: { color: css('--panel') }, textColor: css('--muted') },
    grid: { vertLines: { visible: false }, horzLines: { color: css('--line') + '55' } },
    localization: { locale: 'pt-BR', dateFormat: 'dd/MM/yyyy', priceFormatter: (p) => nf(0).format(p) },
  });
  const a = eqChart.addSeries(LW.AreaSeries, { lineColor: css('--accent'), topColor: css('--accent') + '55', bottomColor: css('--accent') + '05', lineWidth: 2 });
  a.setData(data.paper.map((p) => ({ time: p.time, value: p.value })));
  if (data.shadow.length) {
    const s = eqChart.addSeries(LW.LineSeries, { color: css('--muted'), lineWidth: 1, lineStyle: 2, priceLineVisible: false, lastValueVisible: false });
    s.setData(data.shadow);
  }
  eqChart.timeScale().fitContent();
}

function bindEmptyActions() {
  for (const b of $$('[data-act]')) {
    b.onclick = () => runAction(b.dataset.act, b.dataset.act === 'eod' ? 'Rodando o EOD (sync, sinais, simulação)…' : 'Baixando cotações…');
  }
}

function table(cols, rows) {
  return `<table class="grid"><thead><tr>${cols.map((c) => `<th class="${c.num ? 'num' : ''}">${c.h}</th>`).join('')}</tr></thead><tbody>${
    rows.map((r) => `<tr data-t="${esc(r.ticker || '')}">${cols.map((c) => `<td class="${c.num ? 'num' : ''}">${c.f(r)}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
}

async function renderTab() {
  for (const b of $$('.tab')) b.classList.toggle('on', b.dataset.tab === state.tab);
  const body = $('#tab-body'); const s = state.status;
  try {
    if (state.tab === 'positions') {
      const rows = await api('/api/positions'); $('#c-positions').textContent = rows.length;
      body.innerHTML = rows.length ? table([
        { h: 'Ativo', f: (r) => r.ticker }, { h: 'Setup', f: (r) => esc(r.setup_name) }, { h: 'Qtd', num: 1, f: (r) => nf(0).format(r.qty) },
        { h: 'Preço médio', num: 1, f: (r) => fmt(r.entry_price) }, { h: 'Último', num: 1, f: (r) => fmt(r.last) },
        { h: 'Stop', num: 1, f: (r) => fmt(r.stop) }, { h: 'Alvo', num: 1, f: (r) => fmt(r.target) },
        { h: 'P&L', num: 1, f: (r) => `<span class="${r.pnl_brl >= 0 ? 'up' : 'down'}">${brl(r.pnl_brl)}</span>` },
        { h: 'R', num: 1, f: (r) => fmt(r.pnl_r) }, { h: '% PL', num: 1, f: (r) => pct(r.exposure_pct, 1) },
        { h: 'Risco aberto', num: 1, f: (r) => pct(r.open_risk_pct) }, { h: 'Pregões', num: 1, f: (r) => r.bars_held ?? '—' },
      ], rows) : (s && !s.paper_started ? emptyPaper() : '<div class="empty"><h3>Sem posições abertas</h3><p>Quando um setup for aceito, a compra acontece no leilão de abertura do dia seguinte.</p></div>');
    } else if (state.tab === 'orders') {
      const all = await api('/api/orders'); const rows = all.filter((r) => r.active);
      $('#c-orders').textContent = rows.length;
      const show = rows.length ? rows : all.slice(0, 30);
      body.innerHTML = show.length ? (rows.length ? '' : '<p class="note" style="padding:6px 10px">Nenhuma ordem para o próximo pregão. Últimas ordens:</p>') + table([
        { h: 'Ativo', f: (r) => r.ticker }, { h: 'Lado', f: (r) => (r.side === 'BUY' ? 'Compra' : 'Venda') },
        { h: 'Tipo', f: (r) => esc(r.kind_pt) }, { h: 'Qtd', num: 1, f: (r) => nf(0).format(r.qty) },
        { h: 'Preço', num: 1, f: (r) => fmt(r.price) }, { h: 'Stop', num: 1, f: (r) => fmt(r.stop_loss) },
        { h: 'Válida até', f: (r) => (r.valid_until?.length === 10 ? dmy(r.valid_until) : esc(r.valid_until)) },
        { h: 'Estado', f: (r) => `<span class="state ${r.state}">${esc(r.state_pt)}</span>` }, { h: 'Motivo', f: (r) => esc(r.reason) },
      ], show) : (s && !s.paper_started ? emptyPaper() : '<div class="empty"><h3>Nenhuma ordem ainda</h3></div>');
    } else if (state.tab === 'signals') {
      const res = await api('/api/signals'); const rows = res.items; $('#c-signals').textContent = rows.length;
      body.innerHTML = rows.length ? `<p class="note" style="padding:6px 10px">Pregão ${dmy(res.date)}</p>` + table([
        { h: 'Ativo', f: (r) => r.ticker }, { h: 'Setup', f: (r) => esc(r.setup_name) }, { h: 'Entrada', f: (r) => esc(r.entry_kind) },
        { h: 'Preço ref.', num: 1, f: (r) => fmt(r.entry_ref) }, { h: 'Stop', num: 1, f: (r) => fmt(r.stop_price) },
        { h: 'Resultado', f: (r) => `<span class="sig ${r.disposition === 'ACCEPTED' ? 'accepted' : 'rejected'}">${esc(r.result)}</span>` },
        { h: 'Motivo', f: (r) => esc(r.reason || '') },
      ], rows) : (s && !s.paper_started ? emptyPaper() : '<div class="empty"><h3>Nenhum sinal no último pregão processado</h3><p>Isso é normal: os setups exigem tendência, volume e estrutura de preço ao mesmo tempo.</p></div>');
    } else if (state.tab === 'equity') {
      renderEquity(body, await api('/api/equity'));
    } else {
      const rows = await api('/api/runs');
      body.innerHTML = rows.length ? table([
        { h: 'Rotina', f: (r) => esc(r.routine_pt) }, { h: 'Pregão', f: (r) => dmy(r.session_date) },
        { h: 'Início', f: (r) => (r.started_at ? new Date(r.started_at).toLocaleString('pt-BR') : '—') },
        { h: 'Resultado', f: (r) => `<span class="status-${r.status}">${esc(r.status)}</span>` }, { h: 'Detalhe', f: (r) => esc(r.error || '') },
      ], rows) : '<div class="empty"><h3>Nenhuma rotina executada ainda</h3></div>';
    }
  } catch (e) { body.innerHTML = `<div class="empty"><h3>Erro ao carregar</h3><p>${esc(e.message)}</p></div>`; }
  bindEmptyActions();
  $('#tab-body').onclick = (ev) => { const tr = ev.target.closest('tr[data-t]'); if (tr && tr.dataset.t) select(tr.dataset.t); };
}

function setupTabs() {
  for (const b of $$('.tab')) b.onclick = () => { state.tab = b.dataset.tab; store.set('tab', state.tab); renderTab(); };
}

async function refreshAll() { await loadStatus(); await loadWatch(); await renderChart(); await renderTab(); }

// ======================================================================== boot
applyLook(); setupSettings(); setupSplitters(); setupUpdate(); setupWatch(); setupChartControls(); setupTabs();
refreshAll();
setInterval(loadStatus, 30_000);
setInterval(async () => { await loadWatch(); }, 60_000);
setInterval(renderTab, 60_000);
