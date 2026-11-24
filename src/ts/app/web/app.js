// Trading System B3 — terminal (U1). JavaScript sem build; só lê a API local.
import { setupServices } from './services.js';

const TOKEN = document.querySelector('meta[name="ts-token"]').content;
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const nf = (d = 2) => new Intl.NumberFormat('pt-BR', { minimumFractionDigits: d, maximumFractionDigits: d });
const fmt = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v) ? '—' : nf(d).format(v));
const pct = (v, d = 2) => {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  const x = Math.abs(v * 100) < 0.5 * 10 ** -d ? 0 : v * 100;  // sem "-0,0%"
  return `${x > 0 ? '+' : ''}${nf(d).format(x)}%`;
};
const brl = (v) => (v === null || v === undefined ? '—' : `R$ ${nf(0).format(v)}`);
const dmy = (iso) => (iso ? iso.slice(0, 10).split('-').reverse().join('/') : '—');
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// Ícones em SVG inline (traço, herdam a cor do texto) — sem emoji, nítidos em qualquer DPI.
const ICONS = {
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-2.6-6.4L21 8"/><path d="M21 3v5h-5"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
  keyboard: '<rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M7 14h10"/>',
  briefcase: '<rect x="3" y="7" width="18" height="13" rx="2"/><path d="M9 7V5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2"/>',
  stop: '<path d="M7.9 2h8.2L22 7.9v8.2L16.1 22H7.9L2 16.1V7.9z"/><path d="M12 8v4M12 16h.01"/>',
  play: '<path d="m6 4 14 8-14 8z"/>', download: '<path d="M12 3v12m0 0-4-4m4 4 4-4M4 21h16"/>',
  chart: '<path d="M3 3v18h18"/><path d="m7 15 4-4 3 3 5-6"/>', tab: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  check: '<path d="m5 12 5 5L20 7"/>', server: '<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/>', info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5h.01"/>', chevL: '<path d="m15 18-6-6 6-6"/>', chevR: '<path d="m9 18 6-6-6-6"/>', chevD: '<path d="m6 9 6 6 6-6"/>', clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
};
const ico = (n) => `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true">${ICONS[n] || ''}</svg>`;
function paintIcons(root = document) { for (const el of $$('[data-ico]', root)) el.outerHTML = ico(el.dataset.ico); }

const store = {
  get(k, d) { try { const v = localStorage.getItem('ts.' + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem('ts.' + k, JSON.stringify(v)); } catch { /* sem storage */ } },
};

const state = {
  status: null, watch: [], selected: store.get('selected', null), period: store.get('period', '1A'),
  sort: store.get('sort', { key: 'ticker', dir: 1 }), filters: new Set(store.get('filters', [])),
  favs: new Set(store.get('favs', [])), tab: store.get('tab', 'today'), refreshedAt: 0,
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
  const theme = store.get('theme', 'system');
  root.dataset.theme = theme === 'system' ? (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark') : theme;
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
    $('#set-theme').value = store.get('theme', 'system'); $('#set-colors').value = store.get('colors', 'classic');
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
      b.innerHTML = ico(which === 'watch' ? 'chevR' : 'chevL'); b.title = which === 'watch' ? 'Mostrar Market Watch' : 'Mostrar Raio-X';
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
  const br = s.breadth;
  $('#st-breadth').innerHTML = br && br.pct_above_sma50 != null
    ? `Amplitude: <b>${nf(0).format(br.pct_above_sma50 * 100)}% acima da MM50</b> · Roteador: <b>${br.router_enabled ? 'ligado' : 'desligado'}</b>` : '';
  $('#st-breadth').title = 'Parcela das ações do universo fechando acima da média de 50 dias (força interna do mercado).';
  renderVersionBadge(s.version || {});
  document.body.classList.toggle('no-brain', !s.cerebro?.enabled);  // hides the "Fund." column
  renderBanner(s);
}

function renderBanner(s) {
  const cb = s.circuit_breaker || {}; const q = s.quarantine || {}; const cand = s.candidate; const ov = s.override || {};
  const banner = $('#cb-banner');
  if (ov.status === 'error') {
    banner.className = 'banner cfgerr';
    banner.textContent = `Configuração inválida — o EOD não roda até corrigir: ${ov.error}`;
    return;
  }
  if (q.active) {
    banner.className = 'banner q';
    let tail = '';
    if (cand && cand.status === 'RUNNING') tail = ' Reotimização em andamento…';
    else if (cand && cand.status === 'PENDING') tail = ' <button type="button" id="btn-candidate" class="primary">Revisar candidata</button>';
    else tail = ' A volta acontece quando a curva sombra se recuperar.';
    banner.innerHTML = `<span>${ico('stop')} <b>Quarentena</b>: ${esc(q.reason || 'drawdown')}. Nenhuma entrada nova; stops, alvos e saídas das posições abertas seguem normalmente.</span>${tail}`;
    $('#btn-candidate')?.addEventListener('click', openCandidate);
    return;
  }
  if (cand && cand.status === 'PENDING') {
    banner.className = 'banner l1';
    banner.innerHTML = 'Há uma configuração candidata aguardando decisão. <button type="button" id="btn-candidate">Revisar</button>';
    $('#btn-candidate').addEventListener('click', openCandidate);
    return;
  }
  if (ov.status === 'stale') {
    banner.className = 'banner l1';
    banner.textContent = 'strategy.yaml mudou depois da última aprovação: a configuração aprovada foi descartada e vale a base do git.';
    return;
  }
  banner.className = 'banner ' + (cb.level === 1 ? 'l1' : 'hidden');
  banner.textContent = cb.level === 1 ? `Circuit breaker nível 1: drawdown ${pct(cb.drawdown, 1)} — risco por operação reduzido.` : '';
}

async function openCandidate() {
  const list = await api('/api/candidates'); const c = list.find((x) => x.status === 'PENDING');
  if (!c) { toast('Nenhuma candidata pendente.'); return; }
  const sm = c.summary || {};
  $('#dlg-candidate-body').innerHTML = `
    <p><b>Motivo da quarentena:</b> ${esc(c.reason)}<br><b>Regime:</b> ${esc(c.regime || '—')}</p>
    <p>Testada em todo o histórico dentro das suas travas: esperança <b>${fmt(sm.expectancy_r, 3)}R</b>
      (IC 95% inferior ${fmt(sm.ci_lower, 3)}R), ${sm.trades ?? '—'} operações, DSR ${fmt(sm.dsr)}, PBO da grade ${fmt(sm.pbo)}.</p>
    <table class="grid diff"><thead><tr><th>Parâmetro</th><th class="num">Novo valor</th></tr></thead><tbody>
      ${Object.entries(c.params).map(([k, v]) => `<tr><td><code>${esc(k)}</code></td><td class="num">${esc(v)}</td></tr>`).join('')}
    </tbody></table>
    <p class="note">Ao aprovar, a quarentena termina e a contagem do paper reinicia com a nova configuração (PRD 1.4.4).
      Ao rejeitar, a configuração atual continua e a volta acontece pela curva sombra.</p>`;
  $('#dlg-candidate-report').textContent = c.report || '';
  const dlg = $('#dlg-candidate'); dlg.showModal();
  $('#dlg-candidate-close').onclick = () => dlg.close();
  $('#dlg-candidate-approve').onclick = async () => { dlg.close(); await runAction(`approve?id=${encodeURIComponent(c.id)}`, 'Aplicando a configuração aprovada…'); };
  $('#dlg-candidate-reject').onclick = async () => { dlg.close(); await runAction(`reject?id=${encodeURIComponent(c.id)}`, 'Rejeitando a candidata…'); };
}

// ======================================================================== versão e atualizações
function renderVersion(v) {
  const body = $('#dlg-update-body'); const go = $('#dlg-update-go');
  if (!v || v.checking) { body.innerHTML = '<div class="skeleton"></div><div class="skeleton" style="width:60%"></div>'; go.disabled = true; return; }
  const short = (h) => (h ? h.slice(0, 7) : '—');
  const checked = v.checked_at ? new Date(v.checked_at).toLocaleString('pt-BR') : '—';
  const mod = v.modified || [];
  let status;
  if (v.error) status = `<p class="vstate bad">${ico('stop')} Não foi possível consultar o GitHub: ${esc(v.error)}</p><p class="note">Confira a internet e tente "Verificar agora".</p>`;
  else if (v.update_available) status = `<p class="vstate warn">${ico('refresh')} <b>${(v.pending || []).length} mudança(s) disponível(is)</b></p>
      <ul class="changes">${(v.pending || []).slice(0, 20).map((l) => `<li><code>${esc(l.slice(0, 7))}</code> ${esc(l.slice(8))}</li>`).join('')}</ul>`;
  else status = `<p class="vstate ok">${ico('check')} Você está na versão mais recente.</p>`;
  const blocked = mod.length ? `<p class="vstate bad">${ico('stop')} A atualização será recusada: há arquivos alterados nesta máquina</p>
      <ul class="changes">${mod.slice(0, 10).map((f) => `<li><code>${esc(f)}</code></li>`).join('')}</ul>
      <p class="note">Desfaça essas alterações (ou envie-as pelo repositório de desenvolvimento) antes de atualizar.</p>` : '';
  const data = v.data_files || [];
  const dataNote = data.length ? `<p class="note">${ico('info')} Arquivos de dados gerados nesta máquina serão substituídos pela versão do GitHub
      (a cópia local vai para <code>data\\backups\\local_data</code>): ${data.map((f) => `<code>${esc(f)}</code>`).join(', ')}</p>` : '';
  body.innerHTML = `<table class="mini"><tbody>
      <tr><td>Versão instalada</td><td class="num"><code>${short(v.installed)}</code> · ${esc(v.installed_date || '—')}</td></tr>
      <tr><td>Versão no GitHub</td><td class="num"><code>${short(v.available)}</code></td></tr>
      <tr><td>Última verificação</td><td class="num">${checked}</td></tr></tbody></table>${status}${blocked}${dataNote}`;
  go.disabled = !v.update_available || mod.length > 0;
  go.title = go.disabled ? (mod.length ? 'Há arquivos alterados nesta máquina' : 'Nada para atualizar') : '';
}

async function openVersion(refresh = false) {
  const dlg = $('#dlg-update');
  if (!dlg.open) dlg.showModal();
  renderVersion(refresh ? { checking: true } : (state.status?.version || { checking: true }));
  try {
    const v = await api(`/api/version${refresh ? '?refresh=true' : ''}`);
    if (state.status) state.status.version = v;
    renderVersion(v); renderVersionBadge(v);
  } catch (e) { renderVersion({ error: e.message }); }
}

function renderVersionBadge(v) {
  $('#st-ver').textContent = v?.installed ? v.installed.slice(0, 7) : 'versão';
  $('#st-update').classList.toggle('hidden', !v?.update_available);
}

function setupUpdate() {
  const dlg = $('#dlg-update');
  $('#st-update').onclick = () => openVersion(false);
  $('#btn-version').onclick = () => openVersion(false);
  $('#dlg-update-check').onclick = () => openVersion(true);
  $('#dlg-update-cancel').onclick = () => dlg.close();
  $('#dlg-update-go').onclick = async () => { dlg.close(); await runAction('update', 'Atualizando e rodando os testes (alguns minutos)…'); };
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
const PHASE_HINT = {
  1: 'Recuperação: preço acima da MM50, abaixo da MM200 — possível fundo', 2: 'Acumulação: acima das duas médias, MM50 ainda abaixo da MM200',
  3: 'Altista: tendência de alta plena — compras e pullbacks', 4: 'Aviso: perdeu a MM50 com MM50 > MM200 — perda de momentum',
  5: 'Distribuição: abaixo das duas médias, MM50 ainda acima da MM200', 6: 'Baixista: tendência de baixa plena — ficar fora',
};
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
  const val = (r) => (key === 'fav' ? (state.favs.has(r.ticker) ? 0 : 1) : key === 'signal' ? ({ accepted: 0, rejected: 1 }[r.signal] ?? 2)
    : key === 'cerebro' ? (r.cerebro ? (r.cerebro.libera_compra ? 0 : 1) : 2) : r[key]);
  const rows = state.watch.filter((r) => r.is_index || passFilters(r)).sort((a, b) => {
    if (a.is_index !== b.is_index) return a.is_index ? -1 : 1;
    const x = val(a), y = val(b);
    if (x === y) return a.ticker.localeCompare(b.ticker);
    if (x === null || x === undefined) return 1; if (y === null || y === undefined) return -1;
    return (x > y ? 1 : -1) * dir;
  });
  $('#watch-table tbody').innerHTML = rows.map((r) => {
    if (!r.has_data) return `<tr data-t="${r.ticker}" class="stale"><td></td><td>${r.ticker}</td><td colspan="7" class="muted">sem cotações</td></tr>`;
    const chg = r.change_pct === null ? '—' : `<span class="${r.change_pct >= 0 ? 'up' : 'down'}">${pct(r.change_pct)}</span>`;
    const trend = r.trend ? `<span class="trend ${r.trend}" title="${r.trend === 'up' ? 'Fechamento acima da média de 200 dias' : 'Abaixo da média de 200 dias'}">${r.trend === 'up' ? 'Alta' : 'Baixa'}</span>` : '—';
    const sig = r.signal ? `<span class="sig ${r.signal}" title="${esc(r.signals.map((s) => `${s.setup_name}: ${s.result}${s.reason ? ' — ' + s.reason : ''}`).join('\n'))}">${r.signal === 'accepted' ? 'aceito' : 'recusado'}</span>` : '';
    return `<tr data-t="${r.ticker}" class="${r.is_index ? 'index' : ''} ${r.stale ? 'stale' : ''} ${state.selected === r.ticker ? 'sel' : ''}" title="${r.stale ? 'Sem a barra do último pregão' : ''}">
      <td>${r.is_index ? '' : `<span class="star ${state.favs.has(r.ticker) ? 'on' : ''}" data-fav="${r.ticker}" role="button" aria-label="Favorito">★</span>`}</td>
      <td>${r.ticker}${r.held ? `<span class="held-ico" title="Em carteira">${ico('briefcase')}</span>` : ''}</td>
      <td class="num">${fmt(r.last, r.is_index ? 0 : 2)}</td><td class="num">${chg}</td>
      <td>${trend}</td><td title="${esc(PHASE_HINT[r.phase] || '')}">${r.phase ? `${r.phase} ${esc(r.phase_name)}` : '—'}</td>
      <td class="num">${r.atr_pct === null ? '—' : nf(1).format(r.atr_pct * 100)}</td><td>${sig}</td>
      <td class="fund-col">${r.is_index ? '' : brainMark(r.cerebro)}</td></tr>`;
  }).join('');
}

// Cérebro (TradingAgents): decisão fundamentalista por ativo
function brainMark(c) {
  if (!state.status?.cerebro?.enabled) return '';
  if (!c) return '<span class="fund none" title="Sem análise do Cérebro">—</span>';
  const mk = c.libera_compra ? '✓' : c.status === 'pausa_analise' ? '⏸' : '✗';
  const cls = c.libera_compra ? 'ok' : c.status === 'pausa_analise' ? 'warn' : 'no';
  return `<span class="fund ${cls}" title="${esc(c.motivo)}">${mk}</span>`;
}

async function renderBrain(t) {
  const el = $('#brain'); if (!el) return;
  if (!state.status?.cerebro?.enabled) { el.innerHTML = ''; return; }
  let d;
  try { d = await api(`/api/cerebro/${encodeURIComponent(t)}`); } catch (e) { el.innerHTML = ''; return; }
  if (state.selected !== t) return;
  const a = d.atual;
  const ind = a ? Object.entries(a.indicadores || {}).filter(([k]) => ['PL', 'PVP', 'DY_pct', 'ROE_pct', 'ROIC_pct', 'EV_EBITDA', 'Div_Liq_Patrimonio', 'Margem_Liquida_pct'].includes(k)) : [];
  const lab = { PL: 'P/L', PVP: 'P/VP', DY_pct: 'DY %', ROE_pct: 'ROE %', ROIC_pct: 'ROIC %', EV_EBITDA: 'EV/EBITDA', Div_Liq_Patrimonio: 'Dív.líq./PL', Margem_Liquida_pct: 'Margem líq. %' };
  const head = !a ? '<p class="note">Ainda sem análise do Cérebro para este ativo.</p>'
    : `<div class="check ${a.libera_compra ? 'ok' : 'no'}"><span><b>${esc(a.motivo)}</b></span></div>
       <p class="note">${esc(a.tese || '')}</p>
       ${ind.length ? `<dl class="kv">${ind.map(([k, v]) => `<dt>${lab[k]}</dt><dd>${typeof v === 'number' ? fmt(v) : esc(v)}</dd>`).join('')}</dl>` : ''}
       <p class="note">Decidido por ${esc(a.origem || '—')} em ${esc((a.autorizado || '').slice(0, 16).replace('T', ' '))} UTC${a.fontes ? ` · fontes: ${esc(a.fontes)}` : ''}</p>`;
  el.innerHTML = `<div class="card"><h4>Cérebro (fundamentos)${d.mode === 'advisory' ? ' · consultivo' : ''}</h4>${head}
    <div><button type="button" id="brain-ask">${ico('refresh')} Pedir análise agora</button></div></div>`;
  $('#brain-ask').onclick = async () => {
    try {
      const r = await api(`/api/cerebro/demand?ticker=${encodeURIComponent(t)}`, { method: 'POST' });
      toast(r.message, r.ok ? 'ok' : 'err');
      setTimeout(() => { if (state.selected === t) renderBrain(t); }, 20000);
    } catch (e) { toast(e.message, 'err'); }
  };
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
let chart = null; let series = {}; let markers = null; let chartData = null; let chartReq = 0; let priceLines = [];

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
    const trs = (chartData.trades || []).filter((x) => x.time === p.time);
    tip.innerHTML = `<b>${dmy(String(p.time))}</b> &nbsp; A ${fmt(c.open)} · M ${fmt(c.high)} · m ${fmt(c.low)} · F ${fmt(c.close)}`
      + sigs.map((s) => `<div>${s.accepted ? '★' : '✕'} ${esc(s.text)}</div>`).join('')
      + trs.map((x) => `<div>${x.kind === 'entry' ? '▲' : '▼'} ${esc(x.text)}</div>`).join('');
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
  const req = ++chartReq;  // ignora respostas antigas quando o usuário troca de ativo rápido (setas)
  let data;
  try { data = await api(`/api/chart/${encodeURIComponent(t)}?period=${state.period}`); }
  catch (e) {
    if (req !== chartReq) return;
    empty.innerHTML = `<h3>Sem gráfico para ${esc(t)}</h3><p>${esc(e.message)}</p>`; empty.classList.remove('hidden'); return;
  }
  if (req !== chartReq) return;
  chartData = data;
  empty.classList.add('hidden');
  const up = css('--up'), down = css('--down');
  series.candles.setData(chartData.candles);
  series.volume.setData(chartData.volume.map((v) => ({ time: v.time, value: v.value, color: (v.up ? up : down) + '66' })));
  for (const k of ['sma200', 'sma50', 'ema20']) series[k].setData(chartData.lines[k]);
  for (const k of ['sma200', 'sma50', 'ema20', 'volume']) series[k].applyOptions({ visible: !!state.layers[k] });
  const sigMarks = state.layers.signals ? chartData.signals.map((s) => ({
    time: s.time, position: s.accepted ? 'belowBar' : 'aboveBar', shape: s.accepted ? 'arrowUp' : 'circle',
    color: s.accepted ? '#f5c542' : '#8b939e', text: s.setup,
  })) : [];
  const tradeMarks = (chartData.trades || []).map((x) => x.kind === 'entry'
    ? { time: x.time, position: 'belowBar', shape: 'arrowUp', color: css('--accent'), text: 'C' }
    : { time: x.time, position: 'aboveBar', shape: 'arrowDown', color: x.win ? up : down, text: 'V' });
  markers.setMarkers([...sigMarks, ...tradeMarks].sort((a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : 0)));
  for (const pl of priceLines) series.candles.removePriceLine(pl);
  priceLines = [];
  const p = chartData.position;
  if (p) {
    const add = (price, color, title, style = 0) => price != null && priceLines.push(series.candles.createPriceLine({
      price, color, title, lineWidth: 1, lineStyle: style, axisLabelVisible: true }));
    add(p.entry, css('--accent'), 'Entrada', 2); add(p.stop, down, 'Stop'); add(p.target, up, 'Alvo');
  }
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

// ======================================================================== Raio-X
let checkReq = 0;
function condItem(x) {
  const cls = x.ok === true ? 'ok' : x.ok === false ? 'no' : 'na';
  const mk = x.ok === true ? '✓' : x.ok === false ? '✗' : '–';
  return `<li class="${cls}"><span class="mk">${mk}</span><span>${esc(x.label)}<br>
    <span class="val"><b>${esc(x.value)}</b>${x.limit && x.limit !== '—' ? ` · precisa ${esc(x.limit)}` : ''}${x.hint ? ` · ${esc(x.hint)}` : ''}</span></span></li>`;
}

async function renderChecklist(t) {
  const el = $('#checklist'); if (!el) return;
  const req = ++checkReq;
  el.innerHTML = '<div class="card"><div class="skeleton"></div><div class="skeleton"></div><div class="skeleton" style="width:60%"></div></div>';
  let d;
  try { d = await api(`/api/checklist/${encodeURIComponent(t)}`); } catch (e) {
    if (req === checkReq) el.innerHTML = `<p class="note">Checklist indisponível: ${esc(e.message)}</p>`; return;
  }
  if (req !== checkReq || state.selected !== t) return;
  const common = `<div class="card"><h4>Filtros gerais · ${dmy(d.date)}</h4><ul class="conds">${d.common.map(condItem).join('')}</ul>
    ${d.common_ok ? '' : '<p class="note">Com um filtro geral reprovado, nenhum setup compra este ativo hoje.</p>'}</div>`;
  const setups = d.setups.map((s) => {
    if (!s.enabled) return `<div class="card"><div class="setup-head"><b>${esc(s.id)} · ${esc(s.name)}</b><span class="tag">desligado</span></div></div>`;
    if (!s.total) return `<div class="card"><div class="setup-head"><b>${esc(s.id)} · ${esc(s.name)}</b>${s.fired ? '<span class="tag fire">SINAL</span>' : ''}</div><p class="note">Sem checklist detalhado para este setup.</p></div>`;
    const pctOk = Math.round((s.passed / s.total) * 100);
    return `<div class="card setup-card ${s.fired ? 'fired' : ''}"><div class="setup-head"><b>${esc(s.id)} · ${esc(s.name)}</b>
      ${s.fired ? '<span class="tag fire">SINAL</span>' : ''}<span class="score">${s.passed}/${s.total} condições</span></div>
      <div class="bar"><i style="width:${pctOk}%"></i></div><ul class="conds">${s.conditions.map(condItem).join('')}</ul></div>`;
  }).join('');
  el.innerHTML = common + setups;
}

function renderInspector() {
  const body = $('#inspector-body'); const w = state.watch.find((r) => r.ticker === state.selected);
  if (!w) { body.innerHTML = '<p class="note">Selecione um ativo no Market Watch.</p>'; return; }
  const key = `${w.ticker}|${w.last_date}`;
  const keep = body.dataset.key === key ? $('#checklist')?.innerHTML : null;  // refresh periódico sem piscar
  body.innerHTML = ''; body.dataset.key = key;
  const keepBrain = body.dataset.key === key ? $('#brain')?.innerHTML : null;
  renderInspectorSummary(body, w);
  if (w.is_index || !w.has_data) return;
  body.insertAdjacentHTML('beforeend', '<div id="brain"></div><div id="checklist"></div>');
  renderBrain(w.ticker).catch(() => {});  // cheap; refreshes the decision when a webhook arrives
  if (keepBrain) $('#brain').innerHTML = keepBrain;
  if (keep) $('#checklist').innerHTML = keep; else renderChecklist(w.ticker);
}

function renderInspectorSummary(body, w) {
  const sigs = (w.signals || []).map((s) => `<div class="check ${s.disposition === 'ACCEPTED' ? 'ok' : 'no'}"><span><b>${esc(s.setup_name)}</b> — ${esc(s.result)}${s.reason ? `<br><span class="note">${esc(s.reason)}</span>` : ''}</span></div>`).join('');
  body.insertAdjacentHTML('beforeend', `
    <div class="card"><h4>${esc(w.ticker)}</h4><dl class="kv">
      <dt>Último</dt><dd>${fmt(w.last, w.is_index ? 0 : 2)}</dd>
      <dt>Variação</dt><dd class="${(w.change_pct ?? 0) >= 0 ? 'up' : 'down'}">${pct(w.change_pct)}</dd>
      <dt>Tendência primária</dt><dd>${w.trend === 'up' ? 'Alta (acima da SMA 200)' : w.trend === 'down' ? 'Baixa (abaixo da SMA 200)' : '—'}</dd>
      <dt>ATR 14</dt><dd>${w.atr_pct == null ? '—' : nf(2).format(w.atr_pct * 100) + '% do preço'}</dd>
      <dt>Fase (Diamante)</dt><dd title="${esc(PHASE_HINT[w.phase] || '')}">${w.phase ? `${w.phase} · ${esc(w.phase_name)}` : '—'}</dd>
      <dt>Eficiência (Kaufman)</dt><dd>${w.er == null ? '—' : `${nf(2).format(w.er)} · ${w.er >= 0.3 ? 'tendência' : 'lateral'}`}</dd>
      <dt>Última barra</dt><dd>${dmy(w.last_date)}</dd></dl></div>
    <div class="card"><h4>Decisões no último pregão processado</h4>
      ${sigs || `<p class="note">${state.status?.paper_started ? 'Nenhum setup disparou para este ativo.' : 'O paper ainda não rodou nenhum EOD.'}</p>`}</div>`);
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
    const label = { eod: 'Rodando o EOD (sync, sinais, simulação)…', g0: 'Rodando o G0 (leva alguns minutos)…' }[b.dataset.act] || 'Baixando cotações…';
    b.onclick = () => runAction(b.dataset.act, label);
  }
}

function table(cols, rows) {
  return `<table class="grid"><thead><tr>${cols.map((c) => `<th class="${c.num ? 'num' : ''}">${c.h}</th>`).join('')}</tr></thead><tbody>${
    rows.map((r) => `<tr data-t="${esc(r.ticker || '')}">${cols.map((c) => `<td class="${c.num ? 'num' : ''}">${c.f(r)}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
}

// ---------------------------------------------------------------- Hoje: o que aconteceu, o que falta, o que vem
function nextStep(s) {
  if (!s) return { cls: '', title: 'Carregando…', text: '' };
  const q = s.quarantine || {}; const cand = s.candidate; const ov = s.override || {};
  if (ov.status === 'error') return { cls: 'warn', title: 'Corrija a configuração', text: `O EOD não roda até corrigir: ${esc(ov.error)}` };
  if (!s.data.last_bar) return { cls: 'warn', title: 'Baixe as cotações', text: 'Sem histórico ainda (1–3 min).', act: 'sync', label: 'Baixar cotações' };
  if (cand && cand.status === 'PENDING') return { cls: 'warn', title: 'Revise a configuração candidata', text: 'A reotimização terminou e espera sua decisão.', cand: true };
  if (s.eod_pending) return { cls: 'warn', title: `Processe o pregão ${dmy(s.data.last_closed_session)}`, text: 'O EOD de hoje ainda não rodou (ele roda sozinho às 19:30).', act: 'eod', label: 'Rodar o EOD agora' };
  if (s.g0?.due) return { cls: 'warn', title: 'Hora de rodar o G0', text: `${esc(s.g0.reason)} Leva alguns minutos; o relatório vai para a pasta reports.`, act: 'g0', label: 'Rodar o G0 agora' };
  if (s.version?.update_available) return { cls: '', title: 'Há uma atualização', text: 'Instale quando quiser; os testes rodam antes e há volta automática.', upd: true };
  if (q.active) return { cls: 'warn', title: 'Sistema em quarentena', text: `${esc(q.reason || '')} — sem entradas novas; saídas seguem normais.` };
  const n = s.next_event;
  return { cls: 'ok', title: 'Tudo em dia', text: `Nada a fazer agora. Próxima rotina: ${n.at ? `${esc(n.label)} ${dmy(n.at)} às ${n.at.slice(11, 16)}` : '—'}.` };
}

async function renderToday(body) {
  const s = state.status;
  const [pos, ords, eq, sig] = await Promise.all([
    api('/api/positions').catch(() => []), api('/api/orders').catch(() => []),
    api('/api/equity').catch(() => ({ summary: null })), api('/api/signals').catch(() => ({ items: [] })),
  ]);
  $('#c-positions').textContent = pos.length;
  const active = ords.filter((o) => o.active); $('#c-orders').textContent = active.length;
  $('#c-signals').textContent = sig.items.length;
  const ns = nextStep(s); const sm = eq.summary;
  const dot = (ok) => `<span class="dot ${ok === true ? 'ok' : ok === false ? 'bad' : 'warn'}"></span>`;
  const accepted = sig.items.filter((x) => x.disposition === 'ACCEPTED');
  body.innerHTML = `<div class="today">
    <div class="card next ${ns.cls}"><h4>Próximo passo</h4><div class="big">${ns.title}</div><div class="note">${ns.text}</div>
      ${ns.act ? `<div><button class="primary" data-act="${ns.act}">${ico(ns.act === 'sync' ? 'download' : 'play')} ${ns.label}</button></div>` : ''}
      ${ns.cand ? '<div><button class="primary" id="today-cand">Revisar candidata</button></div>' : ''}
      ${ns.upd ? '<div><button class="primary" id="today-upd">Ver atualização</button></div>' : ''}</div>
    <div class="card"><h4 class="card-h">Saúde do sistema <button type="button" class="link-btn" id="today-svc">${ico('server')} Gerenciar serviços</button></h4><table class="mini"><tbody>
      <tr><td>${dot(s && !s.data.stale)}Dados</td><td class="num">${s?.data.last_bar ? dmy(s.data.last_bar) : 'sem cotações'}</td></tr>
      <tr><td>${dot(s?.last_eod ? s.last_eod.status === 'OK' : null)}Último EOD</td><td class="num">${s?.last_eod ? `${dmy(s.last_eod.session_date)} · ${esc(s.last_eod.status)}` : 'nenhum'}</td></tr>
      <tr><td>${dot(true)}Fonte</td><td class="num">${esc(s?.connection.label || '—')}</td></tr>
      ${s?.cerebro?.enabled ? `<tr title="Webhook: ${esc(s.cerebro.webhook_url)} · Cérebro: ${esc(s.cerebro.base_url)}${s.cerebro.last_error ? ` · último erro: ${esc(s.cerebro.last_error.error)}` : ''}">
        <td>${dot(s.cerebro.listener_alive && !(s.cerebro.outbox || {}).failed ? true : s.cerebro.listener_alive ? null : false)}Cérebro (fundamentos)</td>
        <td class="num">${s.cerebro.listener_alive ? 'conectado' : 'webhook parado'} · ${s.cerebro.authorized_now ?? 0} autorizado(s)${(s.cerebro.outbox || {}).pending ? ` · ${s.cerebro.outbox.pending} na fila` : ''}${(s.cerebro.outbox || {}).failed ? ` · ${s.cerebro.outbox.failed} falha(s)` : ''}${s.cerebro.mode === 'advisory' ? ' · consultivo' : ''}</td></tr>` : ''}
      <tr><td>${dot(!(s?.quarantine || {}).active)}Proteções</td><td class="num">${(s?.quarantine || {}).active ? 'quarentena' : (s?.circuit_breaker || {}).level ? `circuit breaker ${s.circuit_breaker.level}` : 'normais'}</td></tr>
      <tr title="O G0 decide só se o sistema pode ir para DINHEIRO REAL. O paper (simulação) opera normalmente em qualquer resultado.${s?.g0?.last ? ` Relatório: ${esc(s.g0.last.file)}` : ''}">
        <td>${dot(s?.g0?.last?.verdict === 'APROVADO' && !s.g0.due ? true : null)}Dinheiro real (G0)</td><td class="num">${!s?.g0?.last
          ? 'validação pendente'
          : s.g0.last.verdict === 'APROVADO' ? `liberado · G0 aprovado em ${dmy(s.g0.last.date)}`
          : `ainda não · paper segue normal · novo G0 em ${dmy(s.g0.next_due)}`}</td></tr>
      <tr><td>${dot(null)}Mercado</td><td class="num">${s?.breadth?.pct_above_sma50 != null ? `${nf(0).format(s.breadth.pct_above_sma50 * 100)}% acima da MM50` : '—'}</td></tr>
    </tbody></table></div>
    <div class="card"><h4>Carteira (paper)</h4>${sm ? `<table class="mini"><tbody>
      <tr><td>Patrimônio</td><td class="num">${brl(sm.equity)}</td></tr>
      <tr><td>Retorno</td><td class="num ${sm.return_pct >= 0 ? 'up' : 'down'}">${pct(sm.return_pct)}</td></tr>
      <tr><td>Drawdown atual</td><td class="num">${pct(-sm.drawdown_pct, 1)}</td></tr>
      <tr><td>Risco aberto</td><td class="num">${pct(sm.heat_pct)}</td></tr>
      <tr><td>Operações · esperança</td><td class="num">${sm.trades} · ${sm.expectancy_r == null ? '—' : `${fmt(sm.expectancy_r)} R`}</td></tr>
      <tr><td>Pregões no paper</td><td class="num">${sm.sessions} desde ${dmy(sm.start)}</td></tr></tbody></table>`
      : '<p class="note">O paper começa no primeiro EOD.</p>'}</div>
    <div class="card"><h4>Posições abertas · ${pos.length}</h4>${pos.length ? `<table class="mini"><tbody>${pos.map((p) => {
      const toStop = p.last && p.stop ? (p.stop / p.last - 1) : null;
      return `<tr data-t="${esc(p.ticker)}"><td><b>${esc(p.ticker)}</b> <span class="muted">${esc(p.setup_name)}</span></td>
        <td class="num ${p.pnl_r >= 0 ? 'up' : 'down'}">${fmt(p.pnl_r)} R</td>
        <td class="num muted" title="Distância até o stop">stop ${toStop == null ? '—' : pct(toStop, 1)}</td></tr>`;
    }).join('')}</tbody></table>` : '<p class="note">Nenhuma posição aberta.</p>'}</div>
    <div class="card"><h4>Ordens para o próximo pregão · ${active.length}</h4>${active.length ? `<table class="mini"><tbody>${active.map((o) => `
      <tr data-t="${esc(o.ticker)}"><td><b>${esc(o.ticker)}</b> ${o.side === 'BUY' ? 'compra' : 'venda'} ${nf(0).format(o.qty)}</td>
        <td class="muted">${esc(o.kind_pt)}</td><td class="num">${o.price ? fmt(o.price) : 'a mercado'}</td>
        <td>${o.side === 'BUY' ? brainMark((state.watch.find((w) => w.ticker === o.ticker) || {}).cerebro) : ''}</td></tr>`).join('')}</tbody></table>
        ${state.status?.cerebro?.enabled && state.status.cerebro.mode === 'required' && active.some((o) => o.side === 'BUY')
          ? '<p class="note">Compras só executam com ✓ do Cérebro na abertura (pedido automático no fim do EOD).</p>' : ''}`
      : '<p class="note">Nenhuma ordem programada.</p>'}</div>
    <div class="card"><h4>Sinais do último pregão${sig.date ? ` · ${dmy(sig.date)}` : ''}</h4>${sig.items.length
      ? `<p class="note">${accepted.length} aceito(s) de ${sig.items.length}.</p><table class="mini"><tbody>${sig.items.slice(0, 8).map((x) => `
        <tr data-t="${esc(x.ticker)}"><td><b>${esc(x.ticker)}</b> <span class="muted">${esc(x.setup_name)}</span></td>
          <td class="num"><span class="sig ${x.disposition === 'ACCEPTED' ? 'accepted' : 'rejected'}">${esc(x.result)}</span></td></tr>`).join('')}</tbody></table>`
      : '<p class="note">Nenhum setup disparou — é normal, as regras exigem tendência, volume e estrutura ao mesmo tempo.</p>'}</div>
  </div>`;
  $('#today-cand')?.addEventListener('click', openCandidate);
  $('#today-upd')?.addEventListener('click', () => $('#st-update').click());
  $('#today-svc')?.addEventListener('click', () => svc.open());
}

async function renderTab() {
  for (const b of $$('.tab')) b.classList.toggle('on', b.dataset.tab === state.tab);
  const body = $('#tab-body'); const s = state.status;
  try {
    if (state.tab === 'today') {
      await renderToday(body);
    } else if (state.tab === 'positions') {
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

async function refreshAll() {
  await loadStatus(); await loadWatch(); await renderChart(); await renderTab();
  state.refreshedAt = Date.now(); renderFresh();
}

// ======================================================================== paleta de comandos (Ctrl+K)
const TAB_NAMES = { today: 'Hoje', positions: 'Posições', orders: 'Ordens p/ amanhã', signals: 'Sinais', equity: 'Patrimônio', runs: 'Diário' };
function commands() {
  const s = state.status || {};
  const cmds = [
    { grp: 'Ação', label: 'Rodar o EOD agora', sub: s.eod_pending ? `pregão ${dmy(s.data?.last_closed_session)}` : 'já processado', icon: 'play',
      run: () => runAction('eod', 'Rodando o EOD (sync, sinais, simulação)…'), keys: 'eod fim do dia processar rodar' },
    { grp: 'Ação', label: 'Baixar cotações', sub: 'sync', icon: 'download', run: () => runAction('sync', 'Baixando cotações…'), keys: 'sync cotacoes baixar dados' },
    { grp: 'Ação', label: 'Recarregar a tela', sub: 'R', icon: 'refresh', run: refreshAll, keys: 'atualizar recarregar refresh' },
    { grp: 'Ação', label: 'Rodar o G0 (validação)', sub: s.g0?.last ? `último ${dmy(s.g0.last.date)}` : 'pendente', icon: 'play',
      run: () => runAction('g0', 'Rodando o G0 (leva alguns minutos)…'), keys: 'g0 go validacao backtest portao' },
  ];
  cmds.push({ grp: 'Ação', label: 'Versão e atualizações', sub: s.version?.update_available ? 'há versão nova' : 'verificar agora',
    icon: 'refresh', run: () => openVersion(true), keys: 'atualizar atualizacao update versao github instalar' });
  for (const [k, v] of Object.entries(TAB_NAMES)) {
    cmds.push({ grp: 'Aba', label: v, icon: 'tab', run: () => { state.tab = k; store.set('tab', k); renderTab(); }, keys: `aba ${k}` });
  }
  for (const p of ['3M', '6M', '1A', '2A', '5A', 'TUDO']) {
    cmds.push({ grp: 'Gráfico', label: `Período ${p}`, icon: 'chart', run: () => $(`#period button[data-p="${p}"]`)?.click(), keys: 'periodo grafico' });
  }
  for (const [k, v] of [['system', 'Igual ao Windows'], ['dark', 'Escuro'], ['light', 'Claro']]) {
    cmds.push({ grp: 'Tema', label: `Tema: ${v}`, icon: 'sun', run: () => { store.set('theme', k); applyLook(); renderChart(); }, keys: 'tema aparencia' });
  }
  cmds.push({ grp: 'Ação', label: 'Central de Serviços', sub: 'tarefas, ligar/parar', icon: 'server', run: () => svc.open('servicos'), keys: 'servicos tarefas agendadas ligar parar iniciar eod' });
  cmds.push({ grp: 'Ação', label: 'Configurar o Cérebro', sub: 'TradingAgents', icon: 'settings', run: () => svc.open('cerebro'), keys: 'cerebro tradingagents webhook configurar modo consultivo advisory' });
  cmds.push({ grp: 'Ação', label: 'Testar conexões', sub: 'internet, Cérebro, webhook, Telegram', icon: 'play', run: () => svc.open('testes'), keys: 'testar teste conexao ping internet' });
  cmds.push({ grp: 'Ajuda', label: 'Atalhos de teclado', sub: '?', icon: 'keyboard', run: () => $('#dlg-help').showModal(), keys: 'ajuda teclado' });
  return cmds;
}

function fuzzy(q, text) {
  // subsequência com bônus para começo de palavra e letras seguidas; 0 = não casa
  q = q.toLowerCase(); text = text.toLowerCase();
  let score = 0, ti = 0, run = 0;
  for (const ch of q) {
    const i = text.indexOf(ch, ti);
    if (i < 0) return 0;
    run = i === ti ? run + 1 : 0;
    score += 1 + run * 2 + (i === 0 || text[i - 1] === ' ' ? 3 : 0);
    ti = i + 1;
  }
  return score - text.length * 0.01;
}

const norm = (x) => x.normalize('NFD').replace(/[̀-ͯ]/g, '');
let palItems = []; let palSel = 0;
function renderPalette() {
  const q = norm($('#palette-q').value.trim());
  const tickers = state.watch.filter((r) => r.has_data).map((r) => ({
    grp: 'Ativo', label: r.ticker, sub: `${fmt(r.last, r.is_index ? 0 : 2)} ${r.change_pct == null ? '' : pct(r.change_pct)}${r.held ? ' · em carteira' : ''}`,
    icon: 'chart', run: () => { select(r.ticker); $(`#watch-table tr[data-t="${r.ticker}"]`)?.scrollIntoView({ block: 'nearest' }); }, keys: '',
  }));
  const all = [...tickers, ...commands()];
  palItems = q
    ? all.map((it) => ({ it, sc: Math.max(fuzzy(q, norm(it.label)), fuzzy(q, norm(it.keys || '')) * 0.8) + (it.grp === 'Ativo' && it.label.toLowerCase().startsWith(q.toLowerCase()) ? 50 : 0) }))
      .filter((x) => x.sc > 0).sort((a, b) => b.sc - a.sc).slice(0, 12).map((x) => x.it)
    : [...commands().slice(0, 3), ...tickers.filter((t) => state.favs.has(t.label)).slice(0, 6), ...commands().slice(3, 9)];
  palSel = Math.min(palSel, Math.max(0, palItems.length - 1));
  $('#palette-list').innerHTML = palItems.map((it, i) => `<li role="option" data-i="${i}" class="${i === palSel ? 'on' : ''}" aria-selected="${i === palSel}">
    <span class="grp">${esc(it.grp)}</span>${ico(it.icon)}<span>${esc(it.label)}</span><span class="sub">${esc(it.sub || '')}</span></li>`).join('')
    || '<li class="muted">Nada encontrado</li>';
  $('#palette-list li.on')?.scrollIntoView({ block: 'nearest' });
}

function openPalette() {
  const dlg = $('#palette'); if (dlg.open) return;
  $('#palette-q').value = ''; palSel = 0; renderPalette(); dlg.showModal(); $('#palette-q').focus();
}

function setupPalette() {
  const dlg = $('#palette');
  $('#btn-palette').onclick = openPalette;
  $('#palette-q').oninput = () => { palSel = 0; renderPalette(); };
  $('#palette-q').onkeydown = (ev) => {
    if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
      ev.preventDefault(); palSel = (palSel + (ev.key === 'ArrowDown' ? 1 : -1) + palItems.length) % Math.max(1, palItems.length); renderPalette();
    } else if (ev.key === 'Enter' && palItems[palSel]) { ev.preventDefault(); dlg.close(); palItems[palSel].run(); }
  };
  $('#palette-list').onclick = (ev) => { const li = ev.target.closest('li[data-i]'); if (li) { dlg.close(); palItems[+li.dataset.i].run(); } };
  dlg.addEventListener('click', (ev) => { if (ev.target === dlg) dlg.close(); });  // clique fora fecha
}

// ======================================================================== atalhos
function setupShortcuts() {
  $('#btn-help').onclick = () => $('#dlg-help').showModal();
  $('#dlg-help-close').onclick = () => $('#dlg-help').close();
  document.addEventListener('keydown', (ev) => {
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === 'k') { ev.preventDefault(); openPalette(); return; }
    if (ev.ctrlKey || ev.metaKey || ev.altKey || ev.target.matches('input, select, textarea') || document.querySelector('dialog[open]')) return;
    const tabs = Object.keys(TAB_NAMES);
    if (/^[1-6]$/.test(ev.key)) { state.tab = tabs[+ev.key - 1]; store.set('tab', state.tab); renderTab(); }
    else if (ev.key === '/') { ev.preventDefault(); $('#watch-filter').focus(); }
    else if (ev.key === '?') { $('#dlg-help').showModal(); }
    else if (ev.key.toLowerCase() === 'u') { openVersion(true); }
    else if (ev.key.toLowerCase() === 's') { svc.open(); }
    else if (ev.key.toLowerCase() === 'r') { refreshAll(); toast('Dados recarregados.'); }
    else if (ev.key.toLowerCase() === 'f' && state.selected) {
      state.favs.has(state.selected) ? state.favs.delete(state.selected) : state.favs.add(state.selected);
      store.set('favs', [...state.favs]); renderWatch();
    }
  });
}

// ======================================================================== atualização automática (pausa com a janela oculta)
function renderFresh() {
  if (!state.refreshedAt) return;
  const s = Math.round((Date.now() - state.refreshedAt) / 1000);
  $('#st-fresh').innerHTML = `${ico('clock')} ${s < 60 ? 'atualizado agora' : `atualizado há ${Math.round(s / 60)} min`}`;
}

function setupPolling() {
  const every = (ms, fn) => setInterval(() => { if (!document.hidden) fn(); }, ms);
  every(30_000, async () => { await loadStatus(); state.refreshedAt = Date.now(); renderFresh(); });
  every(60_000, loadWatch);
  every(60_000, renderTab);
  every(15_000, renderFresh);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && Date.now() - state.refreshedAt > 60_000) refreshAll();  // voltou depois de um tempo: atualiza tudo
  });
  matchMedia('(prefers-color-scheme: light)').addEventListener('change', () => {
    if (store.get('theme', 'system') === 'system') { applyLook(); renderChart(); }
  });
}

// ======================================================================== boot
paintIcons();
applyLook(); setupSettings(); setupSplitters(); setupUpdate(); setupWatch(); setupChartControls(); setupTabs();
setupPalette(); setupShortcuts(); setupPolling();
const svc = setupServices({ api, toast, esc, ico, dmy, onChange: () => refreshAll() });
svc.checkAlert();
setInterval(() => { if (!document.hidden) svc.checkAlert(); }, 120_000);
// link direto: http://127.0.0.1:8765/#servicos (ou #cerebro, #testes) abre a Central na aba
{ const h = location.hash.slice(1); if (['servicos', 'cerebro', 'testes'].includes(h)) svc.open(h); }
refreshAll();
