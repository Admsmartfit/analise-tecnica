# PRD v2 — Terminal Visual (estilo Superalgos)

Nov 17, 2026 · complementa o *PRD — Sistema de Swing Trade Automatizado B3* (motor, risco, paper e Go/No-Go não mudam).

## Sumário executivo

O motor está pronto: sinais com barra fechada, paper com paridade de backtest, risco, auto-update. A interface é o elo fraco. O painel atual é uma página Streamlit com abas e tabelas, que mostra ferramentas de desenvolvedor (o botão "Deploy"), fica vazia até o primeiro EOD sem explicar o porquê, e não responde rápido às duas perguntas que o usuário faz todo dia:

1. **"O que aconteceu hoje e o que vai acontecer amanhã?"** (posições, ordens planejadas, saúde)
2. **"Por que o sistema comprou (ou não comprou) este ativo?"**

Esta versão troca o painel por um **terminal de uma tela só**, inspirado no Charting Space do Superalgos e no layout do MetaTrader 5:

- gráfico grande no centro, em **camadas** que se ligam e desligam;
- lista de ativos à esquerda;
- **terminal de posições e ordens** embaixo;
- **raio-X da decisão** à direita, que explica cada sinal aceito ou recusado.

Nada aqui altera regras de trading. A interface só **lê** o banco e os Parquet; toda ação (rodar EOD, backtest, atualizar) passa pela CLI existente e pelo mesmo lock das rotinas.

| Fase | Entrega | Duração estimada |
| --- | --- | --- |
| U1 | API de leitura + casca do terminal (layout, tema, barra de status) | 1 semana |
| U2 | Charting Space com camadas e Market Watch | 1 a 2 semanas |
| U3 | Raio-X da decisão (explicação de sinais e recusas) | 1 semana |
| U4 | Terminal inferior + Mapa da Estratégia (funil do dia) | 1 semana |
| U5 | Time Machine (replay), Laboratório de backtest, Configurações | 1 a 2 semanas |
| U6 | Polimento: atalhos, acessibilidade, onboarding, empacotamento desktop | 1 semana |

## 1. O que aproveitar do Superalgos (e o que não)

O Superalgos é uma plataforma em JavaScript/Node.js com interface em canvas. O código dele não se encaixa num sistema Python, e copiá-lo traria uma arquitetura inteira que não usamos. **Aproveitamos os padrões de interface**, não o código.

| Padrão do Superalgos | Como ele funciona | Adaptação neste sistema |
| --- | --- | --- |
| **Charting Space** | Um "quadro de desenho" enorme; o usuário vê uma parte dele por um *viewport* ([docs](https://superalgos.org/Docs/Foundations/Topic/charting-space-hierarchy.shtml)) | Gráfico central ocupa a maior área da tela; zoom e pan livres; vários ativos em abas ou lado a lado |
| **Layers Manager** | Painel que liga e desliga camadas de dados dentro de uma Time Machine ([tutorial](https://superalgos.org/Docs/Foundations/Tutorial/tutorial-step-back-in-time.shtml)) | Painel de camadas com toggles: preço, médias, pivôs, regime, sinais, recusas, execuções, stops |
| **Time Machine** | Sincroniza camadas no tempo: candles, indicadores e o momento exato de cada ordem ([tutorial](https://superalgos.org/Docs/Foundations/Tutorial/tutorial-step-the-chart-of-many-talents.shtml)) | Cursor de data que mostra "como o sistema via o mercado naquele dia" (pivôs e sinais *as of*) e todos os painéis sincronizados |
| **Tela dividida** | Metade superior para gráficos, metade inferior para o Designer ([README](https://github.com/AAMasters/DesktopApp)) | Gráfico em cima, terminal de posições/ordens embaixo, com divisória arrastável |
| **Temas do espaço** | Tema escuro padrão ("Space Grey") e tema claro configurável ([tema claro](https://superalgos.org/Docs/Foundations/Topic/light-charting-space-theme-example.shtml), [Space Grey](https://superalgos.org/Docs/Foundations/Topic/space-grey.shtml)) | Tema escuro padrão, tema claro opcional, paleta segura para daltonismo |
| **Design Space (nós)** | Estratégia montada como árvore de nós visuais | **Não copiar.** Editor de nós é poderoso, mas complexo para o usuário final e arriscaria mudanças de parâmetro durante o paper. Em vez disso: **Mapa da Estratégia** só de leitura (funil do dia) |

Também ficam fora: multi-exchange, cripto, governança/token, mineração de dados distribuída.

## 2. Princípios de UX (com referências)

1. **Leitura em 5 segundos.** Ao abrir, o usuário entende o estado do mercado e da carteira sem rolar a tela: resumo primeiro, detalhe sob demanda ([ChartsWatcher](https://chartswatcher.com/pages/blog/top-dashboard-design-best-practices-for-traders-in-2025)). NN/g resume: dashboard é para *bater o olho*, não para estudar ([Playfair, heurísticas de Nielsen em analytics](https://playfairdata.com/how-nielsens-usability-heuristics-apply-to-visual-analytics/)).
2. **Status do sistema sempre visível.** O usuário deve saber o estado e as ações possíveis só de olhar; mostrar sempre a hora da última atualização dos dados ([avaliação heurística de dashboards, PMC](https://pmc.ncbi.nlm.nih.gov/articles/PMC6041119)).
3. **Revelação progressiva.** Mostrar só o necessário em cada passo e abrir detalhe ao interagir ([UX Collective, densidade de informação](https://uxdesign.cc/designing-for-information-density-69775165a18e)). Esconder profundidade, não removê-la ([Skins Factory](https://www.theskinsfactory.com/uiux-design-questions-answers/how-do-you-simplify-a-complex-fintech-dashboard)).
4. **Hierarquia antes de volume.** Se tudo tem o mesmo peso visual, o olho se perde; ênfase deliberada no que exige ação ([DEV, UX de exchanges](https://dev.to/vinu_digital/crypto-exchange-ui-design-interface-design-for-trust-speed-and-risk-management-49f7); [Phenomenon Studio](https://phenomenonstudio.com/article/why-your-dashboard-looks-busy-and-your-users-feel-lost/)).
5. **Do insight à ação segura.** Um bom painel financeiro responde "o que está acontecendo, para onde vai o dinheiro e o que fazer agora", e leva a uma ação segura ([Ramotion](https://www.ramotion.com/blog/fintech-dashboard-design-examples/)). Toda ação com efeito (rodar EOD, atualizar, alterar config) pede confirmação e mostra o resultado.
6. **Reconhecer em vez de lembrar.** Nada de nomes internos (`REJECTED_CORR`, `sma_b`); rótulos em português com explicação no tooltip.
7. **Cor nunca é o único sinal.** Vermelho/verde é o par mais problemático para daltonismo ([NKI](https://nki.nl/about-us/responsible-research/guidelines-color-blind-friendly-figures)); azul com laranja é a combinação mais segura ([Datawrapper](https://www.datawrapper.de/blog/colorblindness-part2)); usar também forma, ícone e rótulo ([Visualising Data](https://visualisingdata.com/2019/08/five-ways-to-design-for-red-green-colour-blindness/)). Candles com opção "azul/laranja" e setas com formas diferentes para compra, venda e recusa.
8. **Estados vazios que ensinam.** Nenhuma tela fica em branco: ela diz por que está vazia, quando vai encher e oferece a ação (ex.: "O paper começa no primeiro EOD, hoje às 19:30 — [Rodar agora com o último pregão fechado]").
9. **Densidade para quem usa todo dia.** Usuários frequentes toleram mais densidade; usuários ocasionais precisam de espaço e rótulos claros ([guia de densidade de UI](https://claudskills.com/skills/ui-density/SKILL.md)). Oferecer modo **Compacto** e modo **Confortável**.

## 3. Layout

```text
┌────────────────────────────────────────────────────────────────────────────────────────┐
│ ● PAPER  · Dados: Yahoo (atraso 15 min) · Último EOD: hoje 19:31 ✓ · Próximo: Pré-abertura 08:45 │ ⟳ 1 atualização │
├──────────────┬──────────────────────────────────────────────────────┬──────────────────┤
│ MARKET WATCH │  PETR4 · D1     [Camadas ▾] [Time Machine ⏱] [⛶]    │ RAIO-X           │
│ 🔍 filtrar    │                                                      │ 17/11 · PETR4    │
│ ★ PETR4  38,9 │        candles + médias + regime (fundo)             │ Camada 1         │
│   VALE3  61,2 │        ▲ entrada  ✕ recusa  ━ stop  ┄ alvo          │  ✓ IBOV em alta  │
│   ITUB4  34,0 │                                                      │  ✓ C > SMA200    │
│   ...         │──────────────────────────────────────────────────────│ Setup A          │
│ Tendência ▲▼  │        volume                                        │  ✓ P1<P3         │
│ Sinal hoje ●  │        IFR 14                                        │  ✗ RVol 1,2<1,5  │
├──────────────┴──────────────────────────────────────────────────────┴──────────────────┤
│ [Posições 3] [Ordens p/ amanhã 2] [Sinais de hoje 7] [Histórico] [Alertas 1] [Diário]  │
│ PETR4  A  400 @ 37,52  stop 35,80  +1,4R  heat 0,7%  ...                               │
└────────────────────────────────────────────────────────────────────────────────────────┘
  Navegação lateral (ícones): Terminal · Mapa da Estratégia · Laboratório · Saúde · Configurações
```

Regras de layout:

- Divisórias arrastáveis entre painéis; o layout é salvo por usuário e há um botão "Restaurar padrão".
- Painéis recolhíveis (Market Watch, Raio-X, terminal) para foco total no gráfico (`F` = foco).
- Largura mínima suportada: 1366 px. Abaixo disso, o Raio-X vira gaveta sobreposta.

## 4. Requisitos por tela

### T1 — Barra de status (sempre visível)

| Item | Regra |
| --- | --- |
| Modo | Pílula `PAPER` (azul) ou `REAL` (laranja, com borda). Nunca a mesma cor dos dois. |
| Dados | Provedor + atraso + hora da última barra gravada. Vermelho se a última barra for mais velha que o último pregão fechado. |
| Último EOD | Data, hora e resultado (✓ / ✗ com link para o erro). |
| Próximo evento | Próxima rotina agendada (pré-abertura, EOD) com contagem regressiva. |
| Conexão | MT5: ping do servidor e estado (`CONECTADO`, `AGUARDANDO`, `SEM LOGIN`). Yahoo: último sucesso. |
| Atualização | Badge "1 atualização" quando há versão nova no GitHub; abre o diálogo com a lista de mudanças e o botão **Atualizar agora** (já existe no backend: `ts.app.version`). |
| Circuit breaker | Quando ativo, faixa amarela/vermelha em toda a largura: "Nível 1: risco reduzido a 50%" / "Nível 2: novas entradas suspensas; sinais em carteira sombra". |

Aceite: com a barra de status, o usuário identifica modo, frescor dos dados e saúde sem abrir outra tela.

### T2 — Market Watch

- Lista o universo vigente (`config/universe/ibx100_*.csv`) mais o IBOV fixo no topo.
- Colunas: ativo, último preço, variação do dia, **tendência** (ícone ▲/▼ + cor: C > SMA200), ATR % do preço, **sinal hoje** (● aceito, ○ recusado, vazio), posição aberta (💼).
- Filtro por texto, favoritos (★), ordenação por coluna, filtros rápidos: "Só com sinal hoje", "Só em carteira", "Só tendência de alta".
- Clicar seleciona o ativo no gráfico e no Raio-X (todos os painéis sincronizados).
- Atualização: no modo MT5, cotação a cada 5 s durante o pregão; no Yahoo, a cada 60 s com rótulo "atrasado".

### T3 — Charting Space (gráfico em camadas)

Biblioteca: **TradingView Lightweight Charts** (Apache 2.0, ~35 KB, marcadores, linhas de preço e plugins — [TradingView](https://www.tradingview.com/free-charting-libraries/), [v5](https://www.tradingview.com/blog/en/tradingview-lightweight-charts-version-5-50837), [release notes](https://tradingview.github.io/lightweight-charts/docs/release-notes)). A licença exige o aviso de atribuição e um link para tradingview.com visível na interface.

Camadas (cada uma com toggle no **Painel de Camadas**, agrupadas):

| Grupo | Camada | Fonte | Visual |
| --- | --- | --- | --- |
| Preço | Candles | Parquet (série ajustada) | candles; opção "valores nominais" mostra a série bruta |
| Preço | Volume | Parquet | histograma no painel inferior |
| Indicadores | SMA 200, SMA 50, EMA 20, Bollinger, Donchian 20/10 | `strategy.features.compute_features` | linhas; Bollinger com área |
| Indicadores | IFR 14, MACD, ADX | idem | painéis inferiores empilháveis |
| Estrutura | Pivôs confirmados | `indicators.pivots` (estado *as of* a data) | ▲ fundo / ▼ topo; tooltip com data de confirmação |
| Regime | Filtro de mercado e tendência do ativo | `strategy.regime` | **faixa de fundo** pastel: liberado / bloqueado (com padrão listrado, não só cor) |
| Regime | Percentil de volatilidade do IBOV | `regime.realized_vol_percentile_series` | faixa no rodapé com o tier (100% / 75% / 50% / 0% de risco) |
| Decisões | Sinais aceitos | tabela `signals` | ★ dourada com letra do setup |
| Decisões | **Sinais recusados** | tabela `signals` (`disposition`, `reason`) | ✕ cinza; tooltip com o motivo em português (ex.: "Recusado: risco correlacionado 3,4% > 3,0% com VALE3 em carteira") |
| Execução | Entradas e saídas | `order_events` / `positions` | setas ▲ compra / ▼ venda, rótulo com motivo de saída (Stop, Alvo, Trailing, Tempo) |
| Execução | Stop e alvo | `positions` + snapshots | linha em degraus do stop ao longo do tempo (trailing visível) e linha tracejada do alvo |

- **Presets** de camadas: "Limpo" (candles + volume), "Análise" (indicadores + pivôs), "Auditoria" (decisões + execução + regime). O preset ativo fica salvo por ativo.
- Tooltip no candle: OHLC, variação, volume relativo e as decisões daquele dia.
- Períodos: 3M, 6M, 1A, 2A, 5A, Tudo; zoom com roda do mouse; duplo clique volta ao enquadramento padrão.
- Desempenho: 5 anos de D1 com todas as camadas abrem em < 1 s.

### T4 — Raio-X da decisão

O diferencial do terminal: explicar **por que** cada decisão aconteceu.

- Ao clicar num candle (ou num sinal/recusa), o painel mostra a decisão daquele dia para aquele ativo:
  - **Camada 1:** IBOV (C > SMA200 e SMA50 > SMA200), tendência do ativo, histórico mínimo, barra do dia presente.
  - **Cada setup (A–E):** checklist das condições com **valor medido × limite** (ex.: `RVol 1,21 < 1,50 ✗`, `ADX 27,4 ≥ 20 ✓`).
  - **Risco:** stop técnico, distância em ATR, quantidade calculada, tetos (capital, gap), risco efetivo vs alvo (com aviso se < 70%).
  - **Carteira:** heat, cluster, risco correlacionado, beta, cooldown, setup suspenso, regime e circuit breaker — com o item que barrou destacado.
- Texto final em linguagem simples: "PETR4 não entrou porque o volume (1,21×) ficou abaixo do mínimo de 1,5× do Setup A."

**Requisito técnico (paridade):** a explicação não pode ser uma reimplementação paralela das regras, que um dia divergiria. Os avaliadores de setup passam a montar uma lista de `Check(nome, valor, limite, ok)` e retornam o sinal apenas se todos passarem; o Raio-X lê essa mesma lista. Um teste garante que "sinal emitido" ⇔ "todos os checks ok" para todo o histórico de fixtures.

Aceite: para qualquer ativo e dia, o usuário descobre o motivo de entrada/não entrada em até 3 cliques.

### T5 — Terminal inferior

Abas com contador; colunas ordenáveis; clique numa linha sincroniza o gráfico.

| Aba | Conteúdo |
| --- | --- |
| Posições | ativo, setup, qtd, preço médio, último, stop atual, alvo, P&L R$ e em R, pregões, % do PL, risco aberto |
| Ordens p/ amanhã | ordens planejadas (leilão, buy-stop, saída na abertura) com estado (PLANEJADA → ENVIADA → ACEITA → EXECUTADA/EXPIRADA) |
| Sinais de hoje | todos os sinais com resultado (aceito/recusado + motivo) |
| Histórico | operações encerradas com R, MAE/MFE e motivo de saída; exportar CSV |
| Alertas | mesmas mensagens do Telegram, com filtro por severidade |
| Diário | linha do tempo das rotinas (sync, EOD, pré-abertura, watchdog, backup, update) |

### T6 — Mapa da Estratégia (funil do dia)

Substitui o "Design Space" do Superalgos por uma visão **só de leitura** de como a estratégia filtrou o universo no dia escolhido:

```text
Universo 100 → com barra do dia 99 → filtro IBOV ✓ → tendência (C>SMA200) 41 → setups disparados 6
→ sizing ok 5 → carteira ok 2 → ordens planejadas 2
```

- Cada etapa é clicável e lista os ativos que pararam ali, com o motivo.
- Acima do funil: os parâmetros vigentes (`strategy.yaml`) em cartões legíveis e o **hash congelado** do paper (PRD 1.4.4), com aviso se o arquivo mudou desde o início do paper.

### T7 — Laboratório (backtest e G0)

- Rodar backtest/G0 com barra de progresso e estimativa de tempo (hoje o botão bloqueia a tela).
- Histórico de execuções com data, hash da config, esperança em R, DSR, PBO e veredito G0.
- Comparar duas execuções lado a lado (curvas e métricas).
- Heatmap do platô ±20% por parâmetro (já calculado no `walkforward`; falta visualizar).

### T8 — Saúde do sistema

- Tarefas do Agendador (`TS_*`): próxima execução, última execução e resultado (via `Get-ScheduledTask`).
- Cobertura de dados por ativo e problemas de qualidade (WARN/BLOCK) com explicação.
- Versão instalada, histórico de atualizações e botão **Atualizar agora**.

### T9 — Configurações

- Formulário para `settings.local.yaml`: provedor de dados, login MT5, Telegram (com botão "Enviar mensagem de teste"), capital de referência, tema, densidade.
- `strategy.yaml` em **somente leitura** durante o paper. Mudança de parâmetro gera um arquivo candidato e um aviso: "Alterar parâmetros reinicia a contagem do paper (PRD 1.4.4)".
- Segredos (senha MT5, token do bot) nunca aparecem na tela; só "configurado ✓" e "trocar".

### T10 — Primeira execução (onboarding)

Checklist no primeiro acesso, que some quando tudo estiver ✓:

1. Cotações carregadas (com botão "Baixar agora").
2. Universo do trimestre atualizado.
3. Telegram configurado (opcional).
4. Primeiro EOD executado (explica o horário e permite rodar com o último pregão fechado).
5. Tour de 4 passos pelos painéis (Market Watch → Gráfico/Camadas → Raio-X → Terminal).

## 5. Time Machine (replay)

- Controle de data na barra do gráfico (calendário + ◀ ▶ por pregão + teclas `,` e `.`).
- Ao voltar para uma data D, **todos** os painéis mostram o estado de D: pivôs *as of* D (o motor já guarda o log de eventos e reconstrói o estado), sinais e recusas de D, posições e ordens do snapshot de D (`book_snapshots`), funil de D.
- Modo "reproduzir": avança um pregão por segundo, útil para revisar uma semana de operações.
- Nada no futuro de D aparece durante o replay (mesma regra de lookahead do motor).

## 6. Arquitetura técnica

### 6.1 Opções avaliadas

| Opção | Prós | Contras |
| --- | --- | --- |
| **A. Continuar no Streamlit** + componente Lightweight Charts | Menor esforço; já instalado | Modelo de "rerun" do script inteiro a cada clique ([Bitdoze](https://bitdoze.com/streamlit-vs-nicegui)); difícil fazer painéis sincronizados, divisórias arrastáveis e atualização em tempo real; ferramentas de dev visíveis |
| **B. NiceGUI** (Python, janela nativa) | Só Python; janela desktop com `ui.run(native=True)` ([Talk Python](https://talkpython.fm/525)); bom para telas de controle | Layout de "terminal" com painéis encaixáveis exige componentes JS próprios; ecossistema menor |
| **C. API Python (FastAPI) + frontend web** com Lightweight Charts e layout de painéis, aberto numa janela desktop (pywebview) | Mesmo modelo do Superalgos (web app); interface fluida, painéis encaixáveis, WebSocket para status ao vivo; Lightweight Charts é nativo de JS | Duas linguagens (Python + TypeScript); exige etapa de build |

### 6.2 Recomendação: opção C

- **Backend:** FastAPI dentro do pacote `ts` (`ts.app.api`), **somente leitura** sobre SQLite (`mode=ro`) e Parquet, reaproveitando `compute_features`, `pivots`, `regime`, `version`. Ações (EOD, sync, backtest, atualizar) chamam a CLI existente em processo separado e respeitam o lock.
- **Frontend:** TypeScript + Vite, Lightweight Charts, um gerenciador de painéis encaixáveis (ex.: Dockview ou Golden Layout), tema escuro/claro.
- **Entrega:** o build do frontend (arquivos estáticos) é feito no CI do GitHub e versionado como artefato; a máquina do usuário **não precisa de Node**. O atalho "Trading System B3" abre uma janela desktop (pywebview) apontando para a API local, em vez do navegador.
- **Instalador e auto-update** continuam iguais (o pacote só ganha dependências `fastapi`, `uvicorn`, `pywebview`).
- O Streamlit atual fica como fallback até a U4 e é removido quando o terminal cobrir todas as telas.

Se o time preferir ficar só em Python, a opção B é aceitável, com o Charting Space implementado como componente JS embutido.

### 6.3 Endpoints mínimos

| Endpoint | Retorno |
| --- | --- |
| `GET /status` | modo, provedor, último EOD, próxima rotina, conexão, versão, circuit breaker |
| `GET /watchlist` | linhas do Market Watch |
| `GET /chart/{ticker}?asof=&layers=` | candles, indicadores, pivôs *as of*, regime, sinais, recusas, execuções, stops |
| `GET /explain/{ticker}/{date}` | checklist do Raio-X |
| `GET /funnel/{date}` | etapas do funil com ativos e motivos |
| `GET /positions`, `/orders`, `/signals`, `/trades`, `/alerts`, `/runs` | dados do terminal |
| `POST /actions/{eod,sync,backtest,update}` | dispara ação, devolve id; progresso via WebSocket `/ws/jobs` |

### 6.4 Requisitos não funcionais

- Abrir o terminal (até o gráfico desenhado): < 2 s com 100 ativos e 7 anos de histórico.
- Trocar de ativo: < 300 ms (cache de features por ativo, invalidado pelo `mtime` do Parquet).
- A interface nunca escreve no banco; derrubar a interface não afeta as rotinas.
- Funciona offline com Yahoo indisponível (mostra o último dado gravado com aviso).
- Textos em português; formatação brasileira de números e datas (vírgula decimal, `dd/mm/aaaa`).
- Acessibilidade: contraste de texto conforme WCAG 2.1 AA, navegação por teclado nos painéis, cores nunca como único sinal.

## 7. Sprints e critérios de aceite

| Sprint | Entregas | Aceite |
| --- | --- | --- |
| U1 — Fundação | `ts.app.api` (FastAPI) com `/status`, `/watchlist`, `/positions`; casca do frontend com layout de painéis, tema escuro/claro, barra de status; janela pywebview no atalho | Atalho abre a janela em < 3 s; barra de status reflete modo, dados, último EOD e versão; nenhum elemento de ferramenta de dev visível |
| U2 — Charting Space | `/chart`, Lightweight Charts com todas as camadas da tabela T3, Painel de Camadas e presets, Market Watch sincronizado | 5 anos de D1 com todas as camadas em < 1 s; toggles persistem; clique no Market Watch troca o gráfico em < 300 ms |
| U3 — Raio-X | Refatoração dos setups para `Check(...)`; `/explain`; painel Raio-X | Teste de paridade (sinal ⇔ todos os checks ok) passa em todas as fixtures; motivo de qualquer recusa em ≤ 3 cliques |
| U4 — Terminal e Funil | Abas do terminal inferior; `/funnel`; Mapa da Estratégia | Contadores corretos contra o banco; cada etapa do funil lista ativos e motivos |
| U5 — Time Machine, Laboratório, Configurações | Replay *as of* usando `book_snapshots` e log de pivôs; jobs com progresso; formulários de config | Em replay nada após a data aparece (teste automatizado); backtest não bloqueia a interface |
| U6 — Polimento | Atalhos de teclado, onboarding, modos Compacto/Confortável, paleta para daltonismo, remoção do Streamlit | Teste com 3 usuários: SUS ≥ 80; tarefas-chave cumpridas sem ajuda (seção 8) |

## 8. Métricas de sucesso

| Métrica | Meta |
| --- | --- |
| Tempo para responder "como está minha carteira hoje?" | < 10 s após abrir |
| Cliques para descobrir por que um ativo não entrou | ≤ 3 |
| Usuário identifica modo (paper/real) e frescor dos dados | 100% em teste de 5 segundos |
| Pontuação SUS (System Usability Scale) | ≥ 80 |
| Telas vazias sem explicação e sem ação | 0 |
| Divergência entre Raio-X e decisão real do motor | 0 (teste automatizado) |

## 9. Fora de escopo

- Editor visual de estratégia (nós estilo Design Space) e alteração de parâmetros pela interface durante o paper.
- Envio manual de ordens pela interface (o sistema é automático; ordens manuais quebrariam a reconciliação).
- Gráficos intradiários (M5), multi-corretora, aplicativo móvel.
- Recursos sociais, compartilhamento público de estratégias.

## 10. Riscos

| Risco | Mitigação |
| --- | --- |
| Duas linguagens aumentam a manutenção | Frontend pequeno e sem lógica de trading; toda regra continua em Python; build automático no CI |
| Raio-X divergir do motor | Checks são a própria implementação dos setups (seção T4) + teste de paridade |
| Interface lenta com 100 ativos | Cache por `mtime`, payload só do período visível, carregamento sob demanda das camadas |
| Licença do Lightweight Charts | Aviso de atribuição e link para tradingview.com no rodapé do gráfico |
| Usuário confundir paper com real | Pílula de modo com cor e texto distintos; modo real com borda laranja permanente e confirmação dupla nas ações |

## Referências

**Superalgos**
- [Charting Space Hierarchy](https://superalgos.org/Docs/Foundations/Topic/charting-space-hierarchy.shtml)
- [Tutorial — The Chart of Many Talents](https://superalgos.org/Docs/Foundations/Tutorial/tutorial-step-the-chart-of-many-talents.shtml)
- [Tutorial — Back in Time](https://superalgos.org/Docs/Foundations/Tutorial/tutorial-step-back-in-time.shtml)
- [Tutorial — Dashboards in the Future](https://superalgos.org/Docs/Foundations/Tutorial/tutorial-step-dashboards-in-the-future.shtml)
- [Light Charting Space Theme Example](https://superalgos.org/Docs/Foundations/Topic/light-charting-space-theme-example.shtml) · [Space Grey](https://superalgos.org/Docs/Foundations/Topic/space-grey.shtml)
- [Superalgos Desktop App (README)](https://github.com/AAMasters/DesktopApp)

**Gráficos**
- [TradingView — bibliotecas gratuitas](https://www.tradingview.com/free-charting-libraries/) · [Lightweight Charts v5](https://www.tradingview.com/blog/en/tradingview-lightweight-charts-version-5-50837) · [Release notes](https://tradingview.github.io/lightweight-charts/docs/release-notes)

**UX de painéis financeiros**
- [ChartsWatcher — boas práticas de dashboards para traders](https://chartswatcher.com/pages/blog/top-dashboard-design-best-practices-for-traders-in-2025)
- [UX Collective — Designing for information density](https://uxdesign.cc/designing-for-information-density-69775165a18e)
- [Skins Factory — simplificar dashboards fintech](https://www.theskinsfactory.com/uiux-design-questions-answers/how-do-you-simplify-a-complex-fintech-dashboard)
- [Ramotion — fintech dashboard design](https://www.ramotion.com/blog/fintech-dashboard-design-examples/)
- [Phenomenon Studio — dashboards poluídos](https://phenomenonstudio.com/article/why-your-dashboard-looks-busy-and-your-users-feel-lost/)
- [DEV — UX de exchanges: confiança, velocidade e risco](https://dev.to/vinu_digital/crypto-exchange-ui-design-interface-design-for-trust-speed-and-risk-management-49f7)
- [Lollypop — guia de UX de dashboards de investimento](https://lollypop.design/blog/2026/may/investment-dashboard-ux-design-guide/)
- [Playfair Data — heurísticas de Nielsen em analytics](https://playfairdata.com/how-nielsens-usability-heuristics-apply-to-visual-analytics/)
- [Avaliação heurística de dashboards (PMC)](https://pmc.ncbi.nlm.nih.gov/articles/PMC6041119)
- [Guia de densidade de UI](https://claudskills.com/skills/ui-density/SKILL.md)

**Acessibilidade de cores**
- [NKI — figuras amigáveis a daltonismo](https://nki.nl/about-us/responsible-research/guidelines-color-blind-friendly-figures)
- [Datawrapper — daltonismo, parte 2](https://www.datawrapper.de/blog/colorblindness-part2)
- [Visualising Data — cinco formas de desenhar para daltonismo vermelho-verde](https://visualisingdata.com/2019/08/five-ways-to-design-for-red-green-colour-blindness/)

**Frameworks de interface em Python**
- [Bitdoze — Streamlit vs NiceGUI](https://bitdoze.com/streamlit-vs-nicegui) · [Talk Python #525 (NiceGUI)](https://talkpython.fm/525) · [Kanaries — Streamlit vs Dash](https://docs.kanaries.net/en/topics/Streamlit/streamlit-vs-dash)
