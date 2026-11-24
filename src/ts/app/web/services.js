// Central de Serviços: tarefas agendadas, configuração do Cérebro e testes — sem PowerShell.
// Recebe do app.js os utilitários (api, toast, esc, ico, dmy) para não duplicar código.
export function setupServices({ api, toast, esc, ico, dmy, onChange, onRestart }) {
  const $ = (s, el = document) => el.querySelector(s);
  const dlg = $('#dlg-services');
  let tab = 'servicos';
  let data = null;
  const results = {};  // resultados dos testes (persistem enquanto a janela está aberta)

  const when = (iso) => (iso ? `${dmy(iso)} ${iso.slice(11, 16)}` : '—');
  const dot = (ok) => `<span class="dot ${ok === true ? 'ok' : ok === false ? 'bad' : 'warn'}"></span>`;

  // explica erros técnicos em português simples
  function hint(err) {
    const e = String(err || '');
    if (/getaddrinfo|Errno 11001|Name or service/i.test(e)) return 'Sem internet (o computador não encontrou o servidor). Confira a conexão e rode de novo.';
    if (/ConnectError|Connection refused|10061/i.test(e)) return 'O serviço de destino não está aberto (conexão recusada).';
    if (/timeout|Timed out/i.test(e)) return 'O servidor demorou demais para responder.';
    if (/EOD not completed/i.test(e)) return 'O fim do dia não terminou nesse dia.';
    return '';
  }

  // resultados que não são problema por si: o Windows encerra o Watchdog no fim do horário (267014); o Dead-man
  // "falha" justamente para avisar do EOD (já listado); o EOD aparece uma vez só, pelo erro detalhado do banco.
  function harmless(t, eodFailed) {
    if (t.name === 'TS_Cerebro') return true;
    if (t.result === 267014) return true;
    if (t.name === 'TS_DeadMan' || t.name === 'TS_EOD') return eodFailed;
    return false;
  }

  function problems() {
    if (!data) return [];
    const out = [];
    const eod = (data.last_runs || []).find((r) => r.routine === 'eod');
    const eodFailed = eod && eod.status === 'FAILED';
    for (const t of data.tasks) {
      if (t.state === 'Missing') out.push(`${t.label}: tarefa não instalada (use "Reinstalar tarefas").`);
      else if (t.state === 'Disabled') out.push(`${t.label}: está desativada.`);
      else if (t.ok === false && !harmless(t, eodFailed)) out.push(`${t.label}: última execução terminou com "${t.result_pt}".`);
    }
    if (eodFailed) out.push(`Último EOD (${dmy(eod.session_date)}) falhou. ${hint(eod.error) || ''} Use "Executar agora" no Fim do dia quando a causa estiver resolvida.`);
    const c = data.cerebro || {};
    if (c.enabled && !c.listener_alive) out.push('Webhook do Cérebro parado: o sistema não recebe as decisões.');
    if (c.enabled && results.cerebro && !results.cerebro.ok) out.push('O Cérebro (TradingAgents) não está respondendo.');
    return out;
  }

  // ------------------------------------------------------------------ aba Serviços
  function renderServicos() {
    const probs = problems();
    const runs = Object.fromEntries((data.last_runs || []).map((r) => [r.routine, r]));
    const routineOf = { TS_EOD: 'eod', TS_PreOpen: 'preopen', TS_Watchdog: 'watchdog', TS_DeadMan: 'deadman', TS_Backup: 'backup' };
    const rows = data.tasks.map((t) => {
      const r = runs[routineOf[t.name]];
      const err = r && r.status === 'FAILED' ? `<div class="svc-err">${esc(hint(r.error) || '')} <span class="muted">${esc(String(r.error || '').slice(0, 160))}</span></div>` : '';
      const running = t.state === 'Running';
      const disabled = t.state === 'Disabled';
      const btn = (act, label, cls = '') => `<button type="button" class="${cls}" data-task="${t.name}" data-act="${act}">${label}</button>`;
      const acts = t.state === 'Missing' ? '' : [
        running ? (t.name === 'TS_Cerebro' ? btn('restart', 'Reiniciar') : '') : btn('run', t.name === 'TS_Cerebro' ? 'Iniciar' : 'Executar agora', 'primary'),
        running ? btn('stop', 'Parar') : '',
        disabled ? btn('enable', 'Ativar') : btn('disable', 'Desativar'),
      ].join('');
      return `<tr>
        <td><b>${esc(t.label)}</b><div class="note">${esc(t.what || '')} · ${esc(t.when || '')}</div>${err}</td>
        <td>${dot(t.state === 'Missing' || disabled ? false : running ? true : t.ok === false ? null : true)}${esc(t.state_pt)}</td>
        <td>${when(t.last)}<div class="note ${t.ok === false ? 'bad' : ''}">${esc(t.result_pt || '')}</div></td>
        <td>${when(t.next)}</td>
        <td class="svc-acts">${acts}</td></tr>`;
    }).join('');
    return `${probs.length ? `<div class="svc-banner bad">${ico('stop')} <div><b>Atenção</b><ul>${probs.map((p) => `<li>${esc(p)}</li>`).join('')}</ul></div></div>`
      : `<div class="svc-banner ok">${ico('check')} <b>Tudo certo:</b> todas as tarefas instaladas e sem erros.</div>`}
      <table class="grid svc-table"><thead><tr><th>Serviço</th><th>Situação</th><th>Última execução</th><th>Próxima</th><th></th></tr></thead>
      <tbody>${rows}</tbody></table>
      <div class="svc-foot">
        <button type="button" id="svc-register">${ico('refresh')} Reinstalar tarefas agendadas</button>
        <span class="note">Use se alguma tarefa aparecer como "não instalada". Instalação: <code>${esc(data.root)}</code> · modo ${esc(data.mode)} · cotações ${esc(data.data_provider)}</span>
      </div>`;
  }

  // ------------------------------------------------------------------ aba Cérebro
  function renderCerebro() {
    const c = data.cerebro || {}; const cfg = c.config || {};
    const reach = results.cerebro;
    const ob = c.outbox || {};
    const status = `<table class="mini svc-status"><tbody>
      <tr><td>${dot(cfg.enabled ? true : null)}Integração</td><td class="num">${cfg.enabled ? 'ligada' : 'desligada'}</td></tr>
      <tr><td>${dot(cfg.enabled ? !!c.listener_alive : null)}Webhook deste sistema</td><td class="num">${c.listener_alive ? 'no ar' : 'parado'} · <code>${esc(c.webhook_url || '')}</code></td></tr>
      <tr><td>${dot(reach ? reach.ok : null)}Cérebro (TradingAgents)</td><td class="num">${reach ? esc(reach.message) : 'clique em "Testar Cérebro"'}
        ${reach?.suggested_url ? `<br><button type="button" class="primary" id="svc-use-url" data-url="${esc(reach.suggested_url)}">Usar ${esc(reach.suggested_url)}</button>` : ''}</td></tr>
      <tr><td>${dot(null)}Decisões recebidas</td><td class="num">${c.decisions ?? 0}${c.last_decision_at ? ` · última ${when(c.last_decision_at)} UTC` : ''} · ${c.authorized_now ?? 0} autorizada(s) agora</td></tr>
      <tr><td>${dot(ob.failed ? false : null)}Envios ao Cérebro</td><td class="num">${ob.sent ?? 0} enviados · ${ob.pending ?? 0} na fila · ${ob.failed ?? 0} com falha</td></tr>
      ${c.last_error ? `<tr><td></td><td class="num bad">último erro: ${esc(hint(c.last_error.error) || c.last_error.error)}</td></tr>` : ''}
    </tbody></table>`;
    return `${status}
      <div class="svc-actions">
        <button type="button" data-test="cerebro">${ico('play')} Testar Cérebro</button>
        <button type="button" data-test="webhook">${ico('play')} Testar webhook (ping)</button>
        ${ob.failed ? `<button type="button" id="svc-retry">${ico('refresh')} Reenviar ${ob.failed} falha(s)</button>` : ''}
      </div>
      <form id="svc-cfg" class="svc-form">
        <h4>Configuração</h4>
        <label class="chk"><input type="checkbox" name="enabled" ${cfg.enabled ? 'checked' : ''}> <span><b>Ligar a integração com o Cérebro</b><br><span class="note">Pede análise das compras planejadas e recebe as decisões.</span></span></label>
        <fieldset><legend>Quando o Cérebro não autorizar uma compra</legend>
          <label class="chk"><input type="radio" name="mode" value="required" ${cfg.mode !== 'advisory' ? 'checked' : ''}> <span><b>Bloquear a compra</b> (obrigatório)<br><span class="note">Só compra com "autorizado" válido às 10:00. Cérebro fora do ar = sem compras.</span></span></label>
          <label class="chk"><input type="radio" name="mode" value="advisory" ${cfg.mode === 'advisory' ? 'checked' : ''}> <span><b>Só avisar</b> (consultivo)<br><span class="note">Compra normalmente e manda um aviso 🧠 dizendo que teria sido bloqueada.</span></span></label>
        </fieldset>
        <div class="svc-grid">
          <label>Endereço do Cérebro<input name="base_url" value="${esc(cfg.base_url || 'http://127.0.0.1:8000')}" placeholder="http://127.0.0.1:8000"></label>
          <label>Porta do webhook<input name="webhook_port" type="number" min="1024" max="65535" value="${cfg.webhook_port ?? 5000}"></label>
          <label>Convicção mínima (0 a 1)<input name="min_conviccao" type="number" min="0" max="1" step="0.05" value="${cfg.min_conviccao ?? 0}"></label>
          <label>Prioridade dos pedidos<select name="demand_priority">${['normal', 'alta', 'urgente'].map((p) => `<option ${cfg.demand_priority === p ? 'selected' : ''}>${p}</option>`).join('')}</select></label>
        </div>
        <label class="chk"><input type="checkbox" name="allow_contingency" ${cfg.allow_contingency !== false ? 'checked' : ''}> <span>Aceitar decisões da "regra de contingência" (quando a IA do Cérebro está indisponível)</span></label>
        <label class="chk"><input type="checkbox" name="send_feedback" ${cfg.send_feedback !== false ? 'checked' : ''}> <span>Enviar o resultado de cada trade encerrado para o Cérebro aprender</span></label>
        <p class="note">No Cockpit do Cérebro (Watchdog &amp; Ajustes → "URL de Callback do Robô Técnico"), use
          <code>${esc(c.webhook_url || 'http://127.0.0.1:5000/webhook/decisao')}</code>. Use 127.0.0.1, não "localhost":
          no Windows o localhost deixa cada chamada uns 2 s mais lenta.</p>
        <div class="dlg-actions"><button type="submit" class="primary">${ico('check')} Salvar e aplicar</button></div>
      </form>`;
  }

  // ------------------------------------------------------------------ aba Testes
  function renderTestes() {
    const T = [
      ['dados', 'Fonte de cotações', 'Confere se o Yahoo/MT5 responde (precisa de internet).'],
      ['cerebro', 'Cérebro (TradingAgents)', 'Confere se o Cérebro está aberto no endereço configurado.'],
      ['webhook', 'Webhook deste sistema', 'Manda um "ping" para o receptor das decisões (como o Cérebro faz).'],
      ['telegram', 'Notificação no Telegram', 'Envia uma mensagem de teste para o celular.'],
    ];
    return `<p class="note">Cada teste leva poucos segundos e não altera nada no sistema.</p>
      <table class="grid svc-table"><tbody>${T.map(([k, label, what]) => {
        const r = results[k];
        return `<tr><td><b>${label}</b><div class="note">${what}</div></td>
          <td class="svc-res">${r ? `${dot(r.ok)}${esc(r.message)}` : '<span class="muted">não testado</span>'}</td>
          <td class="svc-acts"><button type="button" data-test="${k}">${ico('play')} Testar</button></td></tr>`;
      }).join('')}</tbody></table>
      <div class="svc-foot"><button type="button" class="primary" id="svc-all">${ico('play')} Rodar todos os testes</button></div>`;
  }

  function render() {
    for (const b of dlg.querySelectorAll('[data-svc]')) b.classList.toggle('on', b.dataset.svc === tab);
    const body = $('#svc-body');
    if (!data) { body.innerHTML = '<div class="skeleton"></div><div class="skeleton"></div><div class="skeleton" style="width:60%"></div>'; return; }
    body.innerHTML = tab === 'servicos' ? renderServicos() : tab === 'cerebro' ? renderCerebro() : renderTestes();
    const n = problems().length;
    $('#svc-sub').textContent = n ? `${n} ponto(s) de atenção` : 'tudo certo';
    $('#svc-alert').classList.toggle('hidden', !n);
  }

  async function load() {
    const slow = setTimeout(() => {
      if (!data) $('#svc-body').insertAdjacentHTML('beforeend', '<p class="note">Lendo as tarefas do Windows… isso costuma levar menos de 1 segundo.</p>');
    }, 4000);
    try { data = await api('/api/services'); } catch (e) {
      const old = /not found/i.test(e.message);
      $('#svc-body').innerHTML = `<div class="svc-banner bad">${ico('stop')} <div><b>Não foi possível carregar a Central.</b><br>
        ${old ? 'O terminal aberto ainda está rodando a versão anterior do sistema (a atualização chegou com ele aberto).'
              : esc(e.message)}<br><br><button type="button" class="primary" id="svc-restart">Reiniciar o terminal</button></div></div>`;
      return;
    } finally { clearTimeout(slow); }
    render();
  }

  async function runTest(kind, quiet = false) {
    results[kind] = { ok: null, message: 'testando…' }; render();
    try { results[kind] = await api(`/api/services/test?kind=${kind}`, { method: 'POST' }); } catch (e) { results[kind] = { ok: false, message: e.message }; }
    render();
    if (!quiet) toast(results[kind].message, results[kind].ok ? 'ok' : 'err');
  }

  async function act(url, label) {
    try {
      const r = await api(url, { method: 'POST' });
      toast(r.message || label, r.ok ? 'ok' : 'err');
    } catch (e) { toast(e.message, 'err'); }
    await load(); onChange?.();
  }

  dlg.addEventListener('click', async (ev) => {
    const b = ev.target.closest('button'); if (!b) return;
    if (b.dataset.svc) { tab = b.dataset.svc; render(); return; }
    if (b.dataset.task) {
      if (b.dataset.act === 'disable' && !confirm('Desativar esta tarefa? Ela deixa de rodar sozinha até ser ativada de novo.')) return;
      b.disabled = true; await act(`/api/services/task?name=${b.dataset.task}&action=${b.dataset.act}`); return;
    }
    if (b.dataset.test) { b.disabled = true; await runTest(b.dataset.test); return; }
    if (b.id === 'svc-all') { for (const k of ['dados', 'cerebro', 'webhook', 'telegram']) await runTest(k, true); toast('Testes concluídos.'); return; }
    if (b.id === 'svc-register') { b.disabled = true; await act('/api/services/register', 'Tarefas registradas'); return; }
    if (b.id === 'svc-retry') { b.disabled = true; await act('/api/services/cerebro-retry', 'Reenvio feito'); return; }
    if (b.id === 'svc-refresh') { data = null; render(); await load(); return; }
    if (b.id === 'svc-close') dlg.close();
    if (b.id === 'svc-restart') { b.disabled = true; onRestart?.(); }
    if (b.id === 'svc-use-url') {
      // the Cérebro was found at another address: save it and test again
      b.disabled = true;
      try {
        const r = await api('/api/settings/cerebro', { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ base_url: b.dataset.url }) });
        toast(`Endereço do Cérebro atualizado para ${b.dataset.url}. ${r.message}`, 'ok');
      } catch (e) { toast(e.message, 'err'); }
      delete results.cerebro;
      setTimeout(async () => { await load(); await runTest('cerebro', true); onChange?.(); }, 2500);
    }
  });

  dlg.addEventListener('submit', async (ev) => {
    if (ev.target.id !== 'svc-cfg') return;
    ev.preventDefault();
    const f = new FormData(ev.target);
    const body = {
      enabled: f.get('enabled') === 'on', mode: f.get('mode'), base_url: String(f.get('base_url') || '').trim(),
      webhook_port: Number(f.get('webhook_port')), min_conviccao: Number(f.get('min_conviccao')),
      demand_priority: f.get('demand_priority'), allow_contingency: f.get('allow_contingency') === 'on',
      send_feedback: f.get('send_feedback') === 'on',
    };
    const btn = ev.target.querySelector('button[type=submit]'); btn.disabled = true; btn.textContent = 'Salvando…';
    try {
      const r = await api('/api/settings/cerebro', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      toast(r.message, 'ok');
      setTimeout(async () => { await load(); if (body.enabled) runTest('webhook', true); onChange?.(); }, 2500);
    } catch (e) { toast(e.message, 'err'); btn.disabled = false; btn.textContent = 'Salvar e aplicar'; }
  });

  async function open(which) {
    if (which) tab = which;
    if (!dlg.open) dlg.showModal();
    render();
    await load();
    if (data?.cerebro?.enabled && !results.cerebro) runTest('cerebro', true);  // status do Cérebro sem clicar
  }

  // sinal de alerta no botão da barra mesmo com a janela fechada
  async function checkAlert() {
    try { data = await api('/api/services'); } catch { return; }
    const n = problems().length;
    $('#svc-alert').classList.toggle('hidden', !n);
    $('#btn-services').title = n ? `Central de Serviços: ${n} ponto(s) de atenção` : 'Central de Serviços: tarefas, Cérebro e testes (S)';
  }

  $('#btn-services').onclick = () => open();
  return { open, checkAlert };
}
