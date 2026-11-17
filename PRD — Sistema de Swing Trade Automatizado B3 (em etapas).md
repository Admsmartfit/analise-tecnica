# PRD — Sistema de Swing Trade Automatizado B3 (em etapas)

Nov 17, 2026 · @ricardo

## Sumário executivo

A arquitetura atual é boa como ponto de partida, mas não está pronta para ser codificada: há **duas versões conflitantes das regras** (o PDF "PRD Definitivo" e o `prd-sistema-swing-trade-b3.md`), **três fontes de lookahead bias**, uma **fórmula de stop que faz o contrário do que pretende** e uma **política de retry que pode duplicar ordens reais**. Este PRD unifica as duas versões, corrige esses pontos e divide a entrega em 7 sprints, cada uma com critério de aceite testável sem o MT5 aberto.

Princípios que governam todo o documento:

1. **Paridade backtest = paper = real.** O mesmo código de sinal roda nos três modos; só o adaptador de execução muda (`PaperBroker` ou `MT5Broker`).
2. **Decisão só com barra fechada.** Nenhum sinal usa dado que não existia no instante da decisão. Isso é verificado por teste automatizado, não por revisão.
3. **Um parâmetro, uma fonte.** Todo número (ADX 20 ou 25, ATR 1,5× ou 2×) vive em `config/strategy.yaml`, versionado, e cada ordem registra o hash da configuração que a gerou.
4. **Nada de ordem real antes do Go formal.** A flag `execution.mode` começa em `paper` e só muda com o relatório de Go/No-Go assinado (seção 1.4).
5. **Falha segura.** Na dúvida (dado faltando, terminal desconectado, horário errado), o sistema não abre posição e avisa no Telegram.

| Fase | Sprints | Duração estimada | Saída |
| --- | --- | --- | --- |
| Fundação | S1 Conexão MT5 e dados · S2 Indicadores e pivôs | 2 a 3 semanas | Base Parquet íntegra e indicadores testados |
| Estratégia | S3 Sinais · S4 Risco e carteira | 2 a 3 semanas | Sinais e lotes reproduzíveis |
| Validação | S5 Backtest walk-forward | 1 a 2 semanas | Relatório de robustez com custos B3 |
| Operação | S6 Paper broker e orquestração · S7 Observabilidade | 2 semanas | Sistema rodando sozinho no Windows |
| Forward test | Sem código novo, só correções | 60 a 90 pregões | Relatório Go/No-Go |
| Piloto real | Mudança de flag | 30 pregões a 0,5% de risco | Expansão gradual |

## Etapa 1.1 — Sinais (Camadas 1 e 2): lookahead e ambiguidades

Há lookahead real em três lugares (barra em formação às 16:50, colunas de fractal com `shift(-i)` e Squeeze avaliado na mesma barra do rompimento) e ambiguidade em todos os cinco setups. A tabela resume; os detalhes vêm em seguida.

| # | Problema | Onde | Gravidade | Correção proposta |
| --- | --- | --- | --- | --- |
| 1 | Decisão tomada com a barra D1 incompleta (16:50) e backtest feito com a barra final | Cronograma, todos os setups | Alta | Decidir com a barra fechada e executar no leilão de abertura seguinte (ver 1.1.1) |
| 2 | `is_fractal_top/bottom` usa `shift(-i)`, ou seja, dados futuros | `calculate_fractals` | Alta | Indexar o pivô pela data de confirmação, nunca pela data do extremo |
| 3 | Squeeze e rompimento na mesma barra: o rompimento já alarga a banda | Setup D | Alta | Squeeze medido em t−1; rompimento em t |
| 4 | Setup A do `.md` (máxima de 20 dias) é idêntico ao Setup C | Setups A e C | Média | Setup A vira o Pivô de Alta de 3 pontos (Lemos) |
| 5 | Divergência com defasagem fixa `k` entre 5 e 20 testa 16 hipóteses por dia | Setup E | Média | Divergência entre dois fundos pivô confirmados |
| 6 | Entrada "rompimento da máxima no pregão seguinte" exige ordem stop intradiária | Setups B e E | Média | Ordem stop de compra com validade de 1 pregão e regra de gap explícita |
| 7 | ADX ≥ 25 global bloqueia o Squeeze (que é, por definição, ADX baixo) e os pullbacks | Camada 1 | Média | Filtro de ADX por setup, não global |
| 8 | Dados não ajustados por proventos e grupamentos distorcem SMA200, pivôs e stops | Dados | Alta | Série ajustada para sinais, série bruta para ordens |
| 9 | Universo IBX-100 de hoje aplicado a 5 anos de histórico | Backtest | Alta | Composição histórica das carteiras teóricas |
| 10 | Valores divergentes entre os documentos (ADX 20 ou 25, IBOV SMA50 ou SMA200, ATR 1,5× ou 2×) | PDF e `.md` | Média | Um único `strategy.yaml` com os valores do quadro 1.1.6 |

### 1.1.1 Barra em formação e horário de decisão

Às 16:50 faltam o fim do pregão contínuo e o leilão de fechamento, que concentra parcela relevante do volume do dia. O sinal calculado ao vivo usa um fechamento e um volume que o backtest nunca viu; o filtro `RVol ≥ 1,5` fica sistematicamente mais difícil ao vivo do que no teste. Além disso, o horário de fechamento da B3 muda quando os EUA entram ou saem do horário de verão, então um gatilho fixo em 16:50 no Agendador do Windows fica errado em metade do ano.

**Recomendação (fase de paper):** decidir com a barra D1 fechada (rotina às 19:00) e executar no leilão de abertura do dia seguinte. A paridade backtest/paper fica exata, ao custo de pagar o gap de abertura, que passa a ser medido como custo. Murphy recomenda tomar decisões "longe do mercado, de preferência quando os mercados estiverem fechados" (cap. 16, Resumo das Diretrizes, item 13).

**Alternativa para depois do Go:** manter a execução perto do fechamento, mas só se o backtest for refeito com a barra sintética das 16:50 (reconstruída de barras M5 gravadas) e se a ordem participar do leilão de fechamento. Isso vira um experimento A/B no paper, não a regra inicial.

Em qualquer caso, os horários saem de `config/b3_calendar.yaml` (feriados, pregões reduzidos, horário de leilão por período), nunca de constantes no código.

### 1.1.2 Pivôs 100% determinísticos

O código do PDF calcula `is_fractal_bottom` em t olhando t+1 e t+2. A coluna `last_confirmed_bottom` está certa (usa `shift(window)`), mas nada impede o Setup A de ler `is_fractal_top` diretamente, e aí o backtest enxerga dois dias no futuro. Além disso, a desigualdade estrita faz com que fundos duplos com mínimas iguais nunca virem pivô, e fractais sem filtro de magnitude marcam qualquer ruído de 5 barras como topo.

Regra proposta, combinando o fractal de Bill Williams com o critério de magnitude de Pring (um pivô legítimo exige retração de cerca de um terço do movimento anterior; *Análise Técnica Explicada*, cap. 1, "O que constitui um pico e um vale legítimos"):

1. **Candidato a fundo em i:** `L[i] < min(L[i-n..i-1])` (estrito à esquerda) e `L[i] <= min(L[i+1..i+n])` (não estrito à direita). Empate dentro da janela: vence a barra mais antiga. Padrão `n = 2`.
2. **Confirmação:** o pivô só existe a partir da barra `c = i + n`. Ele é gravado como evento `(pivot_bar=i, confirmed_bar=c, price=L[i], kind='LOW')`. Todo consumidor filtra por `confirmed_bar <= t`.
3. **Magnitude:** o candidato só é aceito se a distância até o último pivô de tipo oposto for `>= k_atr × ATR14[c]` (padrão `k_atr = 1,0`). O ATR usado é o da data de confirmação, não o da data do extremo.
4. **Alternância:** topos e fundos se alternam. Dois fundos seguidos: fica o mais baixo; empate, o mais antigo.
5. **Implementação incremental:** uma classe `PivotTracker.update(bar)` processa barra a barra. A versão vetorizada existe só para pesquisa e precisa produzir exatamente os mesmos eventos.

O teste que garante o determinismo é o **teste de prefixo**: para todo t, rodar o detector em `df[:t]` e comparar com a saída do detector rodado em `df` inteiro filtrada por `confirmed_bar <= t`. Qualquer diferença é lookahead. O mesmo teste vale para todos os indicadores e sinais.

### 1.1.3 Setups revisados

**Setup A — Pivô de Alta (trend following).** Usa a definição de Flavio Lemos (*Análise Técnica Clássica*, cap. 12, "Estratégias de Pivô"): fundo P1, topo P2, fundo P3 mais alto que P1, todos confirmados antes de t. Gatilho: `C[t] > H[P2]` com `RVol[t] >= 1,5` e histograma MACD positivo e crescente. Stop inicial abaixo de P3. Assim o setup deixa de ser cópia do Donchian e passa a exigir estrutura de topos e fundos ascendentes, o critério central da Teoria de Dow em Murphy (cap. 2) e Pring (cap. 3).

**Setup B — Pullback.** Unificar para: contexto `C > SMA50` e `EMA20 > SMA50`; correção de 2 a 5 barras com máximas descendentes; mínima da barra de toque `T` dentro da faixa `[SMA50, EMA20]`. O IFR14 ≤ 40 quase nunca ocorre dentro de tendência de alta, porque os osciladores deslocam a faixa em tendências fortes (Murphy, cap. 10, "A Importância da Tendência"). Tratar o limite como parâmetro (faixa de busca 40 a 50) e validar fora da amostra. Entrada: ordem stop de compra em `H[T] + 1 tick`, válida só no pregão T+1. No backtest, o preço de execução é `max(Open[T+1], H[T] + tick)` para tratar gaps.

**Setup C — Donchian 20/10.** Fórmulas corretas (excluem t). Falta definir precedência de saída: a mínima de 10 dias é o trailing do setup e substitui a EMA9 para C. Murphy descreve a regra semanal de Donchian e sua variante de 2 semanas (cap. 9, "A Regra Semanal").

**Setup D — Bollinger Squeeze.** Três correções:

- O squeeze é medido em `t-1`: `BW[t-1] <= percentil_q(BW[t-120..t-1])`, com `q = 10` e quantil pelo método `lower` (sem interpolação, para resultado estável). O `.md` usava o mínimo de 60 dias, que equivale ao percentil zero e gera poucos sinais.
- Desvio-padrão populacional (`ddof=0`), como Bollinger define. O padrão do pandas é `ddof=1` e muda as bandas.
- Gatilho em t: `C[t] > BandaSup[t]` e `RVol[t] >= 1,5`. Murphy e Lemos descrevem a largura de banda baixa como prelúdio de nova tendência (Murphy, cap. 9, "A Largura da Banda Mede a Volatilidade"; Lemos, cap. 7, Bollinger BandWidth).

**Setup E — Divergência de alta.** Trocar a defasagem fixa por dois fundos pivô confirmados `Pa` (antigo) e `Pb` (recente), com 5 a 30 barras entre eles: `L[Pb] <= L[Pa] × 1,005` e `IFR14[Pb] >= IFR14[Pa] + 3`. Gatilho somente após a confirmação de Pb, com histograma MACD virando para cima (`Hist[t] > Hist[t-1]`), e entrada por ordem stop acima da máxima da barra de confirmação. Murphy (cap. 10) e Pring (cap. 13) tratam a divergência como sinal de alerta que precisa de confirmação do preço. Ponto de atenção: o setup é de reversão e a Camada 1 exige tendência de alta, então ele deve ser raro. Se o backtest de 5 anos gerar menos de 30 operações, o setup sai da versão 1.

### 1.1.4 Camada 1 por setup

Um ADX ≥ 25 global elimina justamente o Squeeze, que nasce de volatilidade e direcionalidade baixas, e corta os pullbacks, porque o ADX cai durante a correção. Proposta: filtros de mercado (IBOV) e de tendência primária (C > SMA200) valem para todos; o filtro de ADX vale só para A e C; para B, exigir `ADX >= 20` em algum ponto dos últimos 10 pregões; para D, nenhum filtro de ADX. O ADX usa a suavização de Wilder (RMA), declarada explicitamente.

### 1.1.5 Dados: ajustes, sobrevivência e aquecimento

- **Proventos e eventos corporativos.** Os candles do MT5 costumam vir sem ajuste. Um grupamento 10:1 vira um "rompimento" falso e um dividendo grande dispara stops. Manter duas séries: `adj` para indicadores e sinais, `raw` para preço de ordem e stop no book. O fator de ajuste vem de uma tabela de eventos corporativos versionada; diferenças acima de 1% entre fontes geram alerta.
- **Viés de sobrevivência.** Usar a composição histórica do IBX-100 (carteiras teóricas quadrimestrais da B3) no backtest, incluindo ativos deslistados.
- **Volume financeiro.** O MT5 entrega `real_volume` em ações; o ADTV deve usar `real_volume × preço típico` como aproximação documentada.
- **Aquecimento.** EMA e RMA dependem do ponto inicial. Calcular sempre com pelo menos 3× o período de aquecimento e descartar esse trecho; ativos com menos de 260 barras ficam fora do universo.

### 1.1.6 Valores unificados propostos (versão 1 do `strategy.yaml`)

| Parâmetro | PDF | `.md` | Proposto | Motivo |
| --- | --- | --- | --- | --- |
| Filtro IBOV | C > SMA200 | C > SMA50 e SMA200 subindo | C > SMA200 e SMA50 > SMA200 | Maré primária de Dow sem reagir a cada correção curta |
| ADX | ≥ 25 global | ≥ 20 global | ≥ 20 só para A e C | Ver 1.1.4 |
| Stop por ATR | 2,0 × ATR14 | 1,5 × ATR14 | 2,0 × ATR14 como piso de distância | Ver 1.2.1 |
| Squeeze | P10 de 120 dias | mínimo de 60 dias | P10 de 120 dias, medido em t−1 | Ver Setup D |
| Trailing de tendência | Fechamento < EMA9 | Mínima de 3 dias ou EMA9 | Chandelier 3 × ATR22 | Ver 1.2.2 |
| Time stop | 8 pregões (todos) | 8 pregões (todos) | 8 pregões só para B e E | Ver 1.2.2 |

## Etapa 1.2 — Risco, carteira e quarentena

O dimensionamento por risco fixo está correto na essência, mas tem um erro de sinal no stop, ignora o risco de gap e soma riscos como se as ações fossem independentes. A quarentena de 3 perdas quase nunca dispara durante o paper e, quando dispara, mede azar mais do que perda de vantagem.

### 1.2.1 Dimensionamento (Q) e stop

**Erro de sinal.** O `.md` define `P_stop = min(Suporte, P_entrada − 1,5×ATR)`. O mínimo de dois preços escolhe o preço mais baixo, ou seja, o stop mais **longe**. O PDF diz "o que for menor, para evitar stops muito longos", que é a intenção oposta. Proposta explícita, em distância:

```latex
D = \mathrm{clip}\big(P_e - (S_{\text{téc}} - \text{tick}),\; 1{,}0\,\mathrm{ATR}_{14},\; 3{,}0\,\mathrm{ATR}_{14}\big)
```

O stop fica um tick abaixo do suporte técnico (pivô P3 ou fundo confirmado), como Murphy recomenda (cap. 16, "Combinando Fatores Técnicos e Gerenciamento de Dinheiro"), mas nunca a menos de 1 ATR (ruído) nem a mais de 3 ATR. Se o suporte estiver a mais de 3 ATR, a operação é descartada em vez de ter o stop encurtado para um ponto sem significado técnico.

**Quantidade.** Manter `Q = floor(R / D)` arredondado ao lote de 100, com três travas adicionais:

1. **Teto de capital:** `Q × P_e <= 20% do PL` (já existe).
2. **Teto de gap:** `Q × P_e × |gap_p01| <= 2% do PL`, onde `gap_p01` é o 1º percentil histórico do gap de abertura do ativo. O stop diário não protege contra abertura abaixo dele; Spina mostra que stop loss não elimina o risco de cauda (cap. 6, discussão de stop loss e CVaR).
3. **Perda por arredondamento:** se o lote de 100 reduzir o risco efetivo para menos de 70% do alvo, registrar o motivo. Em contas abaixo de R$ 100 mil isso será frequente; o mercado fracionário fica como opção configurável, desligada no início.

Exemplo com PL de R$ 100 mil e risco de 1% (R$ 1.000): ação a R$ 50, ATR R$ 1,00, suporte a R$ 47,90. D = 2,11, Q = 473, arredondado para 400 ações (R$ 20 mil, no teto de 20%). O risco efetivo cai para R$ 844, 84% do alvo.

**Risco de 1% a 2%.** Lemos recomenda no máximo 1% do capital de risco por operação (cap. 13, "Controle do Risco"). Recomendação: 1% fixo até o fim do piloto real; 2% só se a estimativa de Kelly fracionário (Lemos, cap. 13, fórmula `% = G − (1 − G) / V`) calculada com o limite inferior do intervalo de confiança do paper justificar, usando no máximo um quarto do Kelly.

### 1.2.2 Saídas: 3:1, parcial em 1,5R e trailing

A parcial não cria vantagem; ela troca cauda direita por frequência de ganho. Com stop em −1R, parcial de 50% em +1,5R, stop da sobra no zero a zero e alvo em 3R, a esperança por operação é:

```latex
E = -(1 - p) + p\,(0{,}75 + 1{,}5\,q)
```

onde `p` é a chance de tocar 1,5R antes do stop e `q` a chance de ir de 1,5R a 3R antes de voltar à entrada. Num passeio aleatório sem tendência, p = 0,40 e q = 0,50, e E = 0, exatamente como sem parcial. O sistema só ganha se os setups tiverem vantagem real; a parcial muda o formato da distribuição, não o sinal da esperança. Lemos faz o mesmo alerta: realizar metade e subir o stop altera a relação risco/retorno original (cap. 13, "Stop de preço e/ou stop de tempo"). Custos de 2 ordens extras pesam contra.

Propostas:

- **Reversão à média (B, E):** manter parcial em 1,5R e alvo em 3R, mas o stop da sobra vai para entrada + custos, não para a entrada seca. Time stop de 8 pregões só aqui.
- **Tendência (A, C, D):** sem alvo e sem time stop fixo. Trailing Chandelier (`máxima desde a entrada − 3 × ATR22`) ativado após +1R; para C, a mínima de 10 dias do próprio Donchian. A EMA9 no fechamento é apertada demais: sai em qualquer correção normal e contradiz a ideia de deixar a cauda gorda crescer. Murphy chama isso de "unidades de tendência" com stops longos, que são as que produzem os maiores lucros (cap. 16, "Operando Posições Múltiplas"). Para liberar capital parado, usar uma regra de estagnação: sai se após 15 pregões o MFE for menor que 1R.
- **Filtro 3:1 ex-ante:** Murphy pede retorno potencial de pelo menos 3 vezes o risco (cap. 16, "Relação Risco/Retorno"). Transformar isso em filtro: para B e E, se o topo pivô confirmado mais próximo acima da entrada estiver a menos de 3R, o sinal é descartado.
- **Execução do stop:** no modo real, stop no servidor da corretora (ordem stop enviada junto com a entrada). No paper e no backtest, quando `L[t] <= stop`, o preço de saída é `min(Open[t], stop)`, para refletir gaps.

### 1.2.3 Carteira e correlação

Cinco posições a 1% não são 5 riscos independentes. Ações da B3 andam juntas com o IBOV, e a correlação sobe justamente nas quedas. Spina mostra que a variância da carteira depende da covariância entre as posições e que correlação alta deixa a carteira exposta ao risco direcional (cap. 1, "Covariância e Correlação"; cap. 8 sobre diversificação). Murphy limita a exposição por grupo de mercados correlacionados a 20–25% (cap. 16, Diretrizes Gerais, item 4).

Regras propostas:

| Regra | Valor inicial | Como medir |
| --- | --- | --- |
| Heat total (soma dos riscos abertos) | ≤ 5% do PL | Σ (preço atual − stop) × Q |
| Risco de carteira ajustado por correlação | ≤ 3% do PL | √(rᵀ Σ r), com Σ = correlação de 60 dias encolhida (Ledoit-Wolf) |
| Clusters | Máx. 2 posições por cluster | Agrupamento hierárquico com corte em correlação 0,7; substitui o limite por setor B3, que é grosso demais |
| Beta da carteira | ≤ 1,2 | Σ peso × beta 120 dias contra o IBOV |
| Desempate entre sinais | Maior força relativa 60 dias | Pring, cap. 19; Lemos, cap. 8 |

### 1.2.4 Regime de volatilidade

Spina dimensiona a alocação máxima da carteira pela faixa de volatilidade do mercado (cap. 7, tabela de faixa do VIX contra alocação máxima). O equivalente para a B3 é o percentil da volatilidade realizada de 20 dias do IBOV numa janela de 5 anos (alternativa: o S&P/B3 Ibovespa VIX, se o dado estiver disponível no MT5 da corretora).

| Percentil da vol. do IBOV | Risco por trade | Heat máximo |
| --- | --- | --- |
| Abaixo de 50 | 1,00% | 5% |
| 50 a 80 | 0,75% | 4% |
| 80 a 95 | 0,50% | 2,5% |
| Acima de 95 | Sem novas entradas | Mantém as abertas |

### 1.2.5 Quarentena: diagnóstico e substituição

Com taxa de acerto de 40%, a chance de três perdas seguidas em qualquer trinca é 0,6³ = 21,6%. Em 20 operações no mesmo ativo, a chance de ocorrer ao menos uma sequência de 3 perdas é de cerca de 93% (70% em apenas 10 operações), mesmo com a estratégia funcionando. Ao mesmo tempo, cada ativo gera poucas operações por ano, então nos 60–90 pregões de paper a regra praticamente não será testada. Os dois documentos ainda divergem na liberação (BW abaixo do P20 contra 20 pregões mais novo pivô acima da SMA50).

Substituir por três mecanismos, cada um medindo uma coisa diferente:

1. **Cooldown por ativo (anti-whipsaw):** 2 stops no mesmo ativo em 30 pregões pausam o ativo por 10 pregões. É barato e evita insistir num papel em distribuição. Mantido só se o backtest mostrar ganho; Pring pede regras simples e sem casos especiais inventados para melhorar o teste (cap. 34).
2. **Monitor estatístico por setup (proteção real):** a cada operação fechada, calcular a média de R das últimas 30 operações do setup. Se ficar abaixo do percentil 5 da distribuição de médias de 30 operações do backtest (bootstrap), o setup é suspenso e gera alerta para revisão humana. Complementar com CUSUM sobre R para detectar mudança de média mais cedo.
3. **Circuit breaker da carteira com curva sombra:** os níveis de 8% e 12% de drawdown ficam, mas a regra de volta ("curva de capital acima da própria MM20") não funciona quando o sistema para de operar, porque a curva fica parada. O sistema continua registrando os sinais não executados numa **curva sombra** e retoma quando a curva sombra cruza acima da sua MM20. Murphy discute a tensão entre reduzir ou aumentar a mão depois de perdas (cap. 16, "O que Fazer Depois de Períodos de Sucesso e Adversidade"); aqui a escolha é reduzir, porque a fase é de validação.

### 1.2.6 Custos B3 (entram no backtest e no paper)

Modelar por ordem: emolumentos e liquidação da B3 (cerca de 0,03% por lado, conferir tabela vigente), corretagem do contrato, slippage medido, e IR de 15% sobre lucro líquido mensal de swing trade com isenção para vendas até R$ 20 mil no mês em ações. O IR não muda sinais, mas entra no relatório de resultado líquido. Os percentuais ficam em `config/costs.yaml`, com data de vigência.

## Etapa 1.3 — Confiabilidade no Windows: MT5, persistência e agendamento

O maior risco operacional do desenho atual é o `@retry` do Tenacity aplicado a envio de ordem: se a ordem chegou à corretora e a resposta se perdeu no IPC, o retry compra de novo. Retry automático vale para leitura; para escrita, só com chave de idempotência e reconciliação antes de reenviar.

### 1.3.1 Pontos de falha do MT5 e tratamento

| Falha | Como aparece | Detecção | Tratamento |
| --- | --- | --- | --- |
| Terminal aberto mas sem login | `initialize()` retorna True, `account_info()` retorna None | Checar `account_info()` logo após conectar | Chamar `initialize(path, login, server, password, timeout)` com credenciais do Gerenciador de Credenciais do Windows (`keyring`), nunca do `settings.json` |
| Desconexão silenciosa | `terminal_info().connected` True, cotação parada | Idade do último tick de um ativo líquido maior que 120 s durante o pregão | `shutdown()` e `initialize()`; após 3 falhas, encerrar `terminal64.exe` e reabrir; alerta |
| Erro de IPC | `last_error()` entre -10001 e -10005 (envio, recebimento, init, conexão, timeout) | Código de erro | Leitura: retry com backoff (3 tentativas, 1 s a 8 s). Escrita: reconciliar antes de qualquer reenvio |
| Ativo fora da Observação de Mercado | `copy_rates_*` retorna None | `symbol_info(s).visible` False | `symbol_select(s, True)` antes de ler |
| Histórico incompleto | Menos barras que o pedido, ou última barra antiga | Comparar com o calendário B3 | Usar `copy_rates_range` com datas explícitas; aguardar e repetir; marcar o ativo como `STALE` e excluí-lo do dia |
| Fuso horário | Horário da barra no fuso do servidor da corretora | Teste de sanidade na instalação | Gravar `ts_utc` e `session_date`; nunca usar o relógio local para identificar a barra |
| Concorrência | Duas rotinas chamando a API ao mesmo tempo | Pacote `MetaTrader5` não é thread-safe | Um processo por vez: mutex nomeado do Windows ou lock file; agendamentos sem sobreposição |
| AutoTrading desligado | retcode 10027 | `terminal_info().trade_allowed` | Não tentar de novo; alerta imediato |
| Modo de preenchimento | retcode 10030 | `symbol_info().filling_mode` | Derivar o `type_filling` do símbolo (na B3 geralmente `ORDER_FILLING_RETURN`) |
| Leilão | Ordem a mercado rejeitada no call | retcode 10018 ou rejeição | Ordem limitada com preço de proteção no leilão de abertura; validar com a corretora |

Política por código de retorno do `order_send`:

| retcode | Significado | Ação |
| --- | --- | --- |
| 10008, 10009 | Colocada, executada | Gravar e reconciliar |
| 10010 | Execução parcial | Gravar parcial; reavaliar o restante |
| 10004, 10020, 10021 | Requote, preços mudaram, sem cotação | Até 2 novas tentativas com novo preço, após reconciliação |
| 10024 | Requisições demais | Backoff de 5 s, 1 tentativa |
| 10006, 10014, 10015, 10016, 10019 | Rejeição, volume, preço ou stop inválidos, sem saldo | Sem retry; erro de negócio, alerta |
| 10018, 10027, 10030 | Mercado fechado, AutoTrading desligado, preenchimento inválido | Sem retry; erro de configuração, alerta |
| None ou erro de IPC | Resposta perdida | Reconciliar por `comment` antes de decidir |

**Idempotência:** cada intenção de ordem recebe um `client_order_id` curto (cabe nos 31 caracteres do campo `comment`) e um `magic` fixo do sistema. Antes de qualquer reenvio, consultar `orders_get`, `positions_get` e `history_deals_get` filtrando por esse id. Sempre rodar `order_check` antes de `order_send`. As contas de ações da B3 são *netting*: a parcial é uma ordem de venda que reduz a posição, e o stop fica na posição.

### 1.3.2 Persistência

**SQLite** é adequado ao volume (dezenas de ordens por mês), desde que configurado: `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000`, `foreign_keys=ON`. Só o processo principal escreve; o Streamlit abre em modo somente leitura (`file:trading.db?mode=ro`). Ordens viram uma tabela de eventos só de inclusão (`order_events`) mais uma tabela de estado atual, para que a auditoria reconstrua qualquer dia. Backup diário pela API `sqlite3.Connection.backup`, nunca copiando o arquivo aberto.

**Parquet:** usar `pyarrow` em vez de `fastparquet` (é o motor padrão do pandas e tem melhor suporte a tipos e metadados). Um arquivo por ativo e por timeframe (`data/ohlcv/d1/PETR4.parquet`), com `schema_version` nos metadados. Escrita atômica: gravar `PETR4.parquet.tmp` na mesma pasta e chamar `os.replace`. No Windows, `os.replace` falha com `PermissionError` se outro processo (Streamlit, antivírus, indexador) estiver com o arquivo aberto; tratar com 5 tentativas e backoff curto.

**NTFS e pastas:** raiz curta (`C:\TradingSystem`) para evitar o limite de 260 caracteres; nomes de arquivo em ASCII; nada dentro de OneDrive ou Dropbox, cuja sincronização trava arquivos abertos.

### 1.3.3 Agendador de Tarefas do Windows

O Agendador não conhece feriados nem a mudança de horário da B3. Então as tarefas rodam todo dia útil e o Python consulta `b3_calendar.yaml`: em dia sem pregão, registra e sai com código 0.

| Tarefa | Horário | Função |
| --- | --- | --- |
| `TS_PreOpen` | 08:45 | Health check do terminal, reconciliação de posições, envio das ordens planejadas para o leilão de abertura |
| `TS_Watchdog` | A cada 15 min, 09:30–18:30 | Heartbeat, idade do tick, processo `terminal64.exe` vivo |
| `TS_EOD` | 19:30 | Sincronização D1, indicadores, sinais, risco, plano de ordens do dia seguinte, relatório Telegram |
| `TS_Backup` | 23:00 | Backup do SQLite e dos Parquet |

Configurações obrigatórias de cada tarefa: "Executar somente quando o usuário estiver conectado" (o MT5 é aplicativo gráfico e não roda na sessão 0 de serviços); "Não iniciar nova instância"; "Parar a tarefa se executar por mais de 30 minutos"; "Executar o mais cedo possível após uma inicialização agendada perdida"; ação apontando para o `python.exe` do ambiente virtual com caminho absoluto e pasta inicial definida. As tarefas ficam versionadas como XML em `deploy/tasks/` e são registradas com `schtasks /Create /XML`.

Na máquina: logon automático com tela bloqueada, plano de energia sem suspensão, economia de energia da placa de rede desligada, Windows Update com horário ativo cobrindo 08:00–21:00 e sincronização de hora (`w32tm`) diária. Um **dead-man switch** fecha o ciclo: se o Telegram não receber "EOD concluído" até 20:30, o watchdog dispara alerta.

## Etapa 1.4 — Paper trading e critérios Go/No-Go

Os critérios atuais não são estatisticamente blindados: 60 a 90 pregões não produzem 50 operações com 6 posições simultâneas, e mesmo 50 operações não distinguem vantagem real de sorte. A saída é mudar o papel do paper: a vantagem se prova no backtest walk-forward (antes do paper); o paper prova que a implementação real reproduz o backtest.

### 1.4.1 Por que os números atuais não bastam

- **Capacidade:** com no máximo 6 posições e permanência média de 6 a 10 pregões, 90 pregões comportam no teto 54 a 90 operações, e na prática (filtros, heat, regime) algo entre 25 e 45. O critério de 50 operações provavelmente não fecha no prazo.
- **Poder estatístico:** com desvio-padrão típico de 1,5R por operação, o erro-padrão da média em 50 operações é 0,21R. Uma esperança verdadeira de +0,25R tem intervalo de 95% entre −0,17R e +0,67R: o paper não consegue provar vantagem.
- **Sharpe em 90 pregões:** o erro-padrão do Sharpe anualizado medido em cerca de 0,36 ano é da ordem de 1,7. Um Sharpe medido de 1,3 é compatível com Sharpe verdadeiro zero. Fica como métrica de relatório, não como portão.
- **Overconfidence de configuração:** se parâmetros mudarem durante o paper, o resultado deixa de valer. Murphy lembra que parâmetros devem ser escolhidos numa parte dos dados e testados em outra (cap. 9, "Otimizar ou Não") e que quem acompanha muitos ativos deve usar os mesmos parâmetros para todos. Pring adverte que regras especiais inventadas para melhorar o teste indicam sistema que não funcionará (cap. 34).

### 1.4.2 Portões em três níveis

| Portão | Quando | Critério | Tipo |
| --- | --- | --- | --- |
| G0 — Vantagem | Fim da Sprint 5, antes do paper | Walk-forward de 5+ anos, fora da amostra, com custos: esperança > 0 com limite inferior do bootstrap de 95% > 0 | Obrigatório |
| G0 |  | Deflated Sharpe Ratio com probabilidade ≥ 0,95, considerando o número de configurações testadas | Obrigatório |
| G0 |  | Probabilidade de overfitting (PBO, validação combinatória) < 0,20 | Obrigatório |
| G0 |  | Platô: esperança positiva com todos os parâmetros a ±20% do valor escolhido | Obrigatório |
| G0 |  | Nenhuma operação responde por mais de 20% do lucro total (Pring, cap. 34) | Obrigatório |
| G0 |  | Resultado positivo em pelo menos 2 de 3 regimes (alta, lateral, queda filtrada) | Obrigatório |
| G1 — Operação | Durante e no fim do paper | 100% dos pregões processados; ≥ 98% sem intervenção manual | Obrigatório |
| G1 |  | Zero ordens duplicadas; zero divergências de posição não explicadas na reconciliação diária | Obrigatório |
| G1 |  | Paridade: reprocessar cada dia com os dados finais reproduz 100% das decisões ao vivo (ou cada diferença tem causa registrada) | Obrigatório |
| G2 — Consistência | Fim do paper (mínimo 60 pregões e 30 operações; até 120 pregões) | Média de R, taxa de acerto e operações por mês dentro da faixa de 90% do bootstrap do backtest para o mesmo N | Obrigatório |
| G2 |  | Slippage medido ≤ slippage assumido; se maior, refazer G0 com o valor medido | Obrigatório |
| G2 |  | Drawdown ≤ 10% (critério de interrupção imediata, não de aprovação) | Kill switch |
| G3 — Piloto real | 30 pregões a 0,5% de risco e 25% do capital | Diferença entre resultado real e a curva paper paralela (tracking error) dentro do custo modelado | Obrigatório |

### 1.4.3 O que medir além de Sharpe, drawdown, PF e slippage

- **Esperança em R** com intervalo de confiança (bootstrap), por setup e total. É a métrica que Lemos chama de esperança matemática positiva (cap. 13).
- **Distribuição dos R:** assimetria, curtose, maior ganho e maior perda em R. Perdas abaixo de −1,5R indicam gap ou falha de execução.
- **CVaR 5% por operação e por dia de carteira**, como Spina usa para comparar estratégias de gestão (cap. 6).
- **MAE e MFE** em múltiplos de ATR, para calibrar stop e trailing.
- **Sequência máxima de perdas** contra a esperada pela taxa de acerto.
- **Ulcer Index, Calmar e tempo de recuperação** do drawdown.
- **Custos sobre lucro bruto** (alvo: < 25%) e **gap de abertura** pago por operação.
- **Taxa de conversão** de ordens stop (B, E) e **arrependimento**: resultado dos sinais descartados por heat, cluster ou regime.
- **Exposição:** % do tempo posicionado, beta médio, heat médio.

### 1.4.4 Regras de condução do paper

1. **Pré-registro:** o hash do `strategy.yaml` é congelado no dia 1. Qualquer alteração de parâmetro reinicia a contagem; correções de bug que não mudam sinais não reiniciam, mas ficam no diário de alterações.
2. **Motor de paper próprio:** `PaperBroker` interno com dados reais da conta e modelo de execução conservador (leilão de abertura + slippage + custos). A conta demo B3 da corretora pode não simular leilão e book de forma realista; ela serve para testar o encanamento de ordens em paralelo, não para medir resultado.
3. **Relatório semanal automático** com as métricas acima e a posição de cada uma dentro das faixas do backtest.

## Etapa 2 — Plano modular para o Antigravity

O plano tem 7 sprints. Cada sprint só termina quando `pytest` passa sem o MT5 aberto (testes de integração com o terminal ficam marcados e rodam à parte). A ordem garante que nenhuma lógica de estratégia seja escrita antes de os dados estarem confiáveis.

### 2.1 Roadmap de sprints

| Sprint | Entregas | Critério de aceite |
| --- | --- | --- |
| S1 — Conexão e dados D1 | Config, calendário B3, `MT5Gateway`, `FakeMT5`, `OhlcvStore` Parquet, sincronização incremental, validação, CLI `sync` e `doctor` | Base de 5 anos do universo gravada; reexecutar `sync` não muda nenhum arquivo; 100% dos testes de falha do FakeMT5 passam |
| S2 — Indicadores e pivôs | SMA, EMA, RMA, ATR, ADX/DI, IFR, MACD, Bollinger/BW, Donchian, RVol, `PivotTracker`, ajuste por proventos | Valores batem com referência (TA-Lib ou planilha) até 1e-8; teste de prefixo sem diferenças |
| S3 — Sinais | Camada 1 por setup, setups A a E, `SignalEngine` | Teste de prefixo nos sinais; casos de borda documentados com fixtures |
| S4 — Risco e carteira | Sizing, stops, saídas por classe de trade, heat, clusters, regime, cooldown, monitor por setup, circuit breaker com curva sombra | Testes de propriedade (Q nunca viola tetos; risco efetivo ≤ alvo) |
| S5 — Backtest e G0 | Backtester orientado a eventos que reusa S2–S4, custos B3, walk-forward, bootstrap, DSR, PBO | Relatório G0 gerado; mesmo resultado em duas execuções |
| S6 — Paper e orquestração | `PaperBroker`, `MT5Broker` (desligado), máquina de estados de ordem, reconciliação, rotinas `preopen`/`eod`, tarefas do Agendador, watchdog | 10 pregões seguidos sem intervenção em máquina de teste |
| S7 — Observabilidade | Telegram, CLI Rich, dashboard Streamlit somente leitura, relatório semanal G1/G2 | Relatório semanal gerado automaticamente |

&#91;embedded content: roadmap · 7 sprints, paper e piloto, 3 portões\]

Cada portão é obrigatório: reprovar em G0 volta para S3–S4; reprovar em G1 ou G2 reinicia a contagem do paper após a correção.

### 2.2 Estrutura de pastas

```text
C:\TradingSystem\
  pyproject.toml            # dependências e ferramentas (ruff, mypy, pytest)
  requirements.lock         # versões fixadas após o primeiro build
  config\
    settings.yaml           # caminhos, modo (paper/live), conta MT5 (sem senha)
    strategy.yaml           # todos os parâmetros de sinal e risco
    costs.yaml              # emolumentos, corretagem, slippage, vigência
    b3_calendar.yaml        # feriados, pregões especiais, horários por período
    universe\               # composição histórica do IBX-100 por quadrimestre
  src\ts\
    core\       models.py, errors.py, clock.py, calendar.py, config.py, logging.py
    data\       mt5_gateway.py, fake_mt5.py, ohlcv_store.py, sync.py, validation.py, corporate_actions.py
    indicators\ ta.py, pivots.py
    strategy\   regime.py, setups.py, signal_engine.py
    risk\       sizing.py, exits.py, portfolio.py, protections.py
    execution\  broker.py, paper_broker.py, mt5_broker.py, order_state.py, reconcile.py
    persistence\ db.py, migrations\
    backtest\   engine.py, metrics.py, walkforward.py
    app\        cli.py, routines.py, notify.py, dashboard.py
  tests\  unit\, property\, integration_mt5\, fixtures\
  deploy\ tasks\*.xml, scripts\*.ps1
  data\   ohlcv\d1\, ohlcv\m5\, db\trading.db, backups\
  logs\
```

### 2.3 Contratos de interfaces

Todo código de domínio depende destas interfaces, nunca do pacote `MetaTrader5` diretamente. O pacote só é importado dentro de `mt5_gateway.py`, que recebe o módulo por injeção.

```python
# src/ts/core/models.py
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Literal

class SetupId(str, Enum):
    A_PIVOT = "A"; B_PULLBACK = "B"; C_DONCHIAN = "C"; D_SQUEEZE = "D"; E_DIVERGENCE = "E"

class TradeClass(str, Enum):
    TREND = "TREND"; MEAN_REVERSION = "MEAN_REVERSION"

@dataclass(frozen=True, slots=True)
class Bar:
    ticker: str; session_date: date; ts_utc: datetime
    open: float; high: float; low: float; close: float
    real_volume: int; tick_volume: int; spread: int

@dataclass(frozen=True, slots=True)
class PivotEvent:
    ticker: str; kind: Literal["HIGH", "LOW"]
    pivot_date: date; confirmed_date: date; price: float; atr_at_confirm: float

@dataclass(frozen=True, slots=True)
class Signal:
    signal_id: str; ticker: str; setup: SetupId; trade_class: TradeClass
    decision_date: date; entry_type: Literal["OPEN_AUCTION", "BUY_STOP"]
    entry_ref: float; stop_price: float; target_price: float | None
    features: dict[str, float]; config_hash: str

@dataclass(frozen=True, slots=True)
class OrderIntent:
    client_order_id: str; signal_id: str | None; ticker: str
    side: Literal["BUY", "SELL"]; qty: int
    order_type: Literal["MARKET", "LIMIT", "STOP", "STOP_LIMIT"]
    price: Decimal | None; stop_loss: Decimal | None
    valid_until: date; reason: str

@dataclass(frozen=True, slots=True)
class Fill:
    client_order_id: str; broker_deal_id: str | None
    ts_utc: datetime; qty: int; price: Decimal; fees: Decimal
```

```python
# src/ts/core/interfaces.py
from typing import Protocol, Sequence
import pandas as pd

class Clock(Protocol):
    def now_utc(self) -> datetime: ...

class MarketData(Protocol):
    def daily_bars(self, ticker: str, start: date, end: date) -> pd.DataFrame: ...
    def last_tick_age_seconds(self, ticker: str) -> float: ...
    def health(self) -> "HealthReport": ...

class Broker(Protocol):
    mode: Literal["paper", "live"]
    def submit(self, intent: OrderIntent) -> "SubmitResult": ...
    def cancel(self, client_order_id: str) -> None: ...
    def open_positions(self) -> Sequence["BrokerPosition"]: ...
    def find_by_client_id(self, client_order_id: str) -> "BrokerOrderState | None": ...
    def fills_since(self, ts_utc: datetime) -> Sequence[Fill]: ...

class SignalEngine(Protocol):
    def evaluate(self, asof: date, bars: dict[str, pd.DataFrame],
                 index_bars: pd.DataFrame) -> list[Signal]: ...

class RiskEngine(Protocol):
    def plan(self, asof: date, signals: list[Signal],
             book: "PortfolioState") -> list[OrderIntent]: ...
    def manage(self, asof: date, book: "PortfolioState",
               bars: dict[str, pd.DataFrame]) -> list[OrderIntent]: ...
```

Regra de implementação: `SignalEngine.evaluate(asof, ...)` deve levantar `LookaheadError` se receber qualquer barra com `session_date > asof`. O backtester, o paper e o modo real chamam exatamente as mesmas classes de S2 a S4.

### 2.4 Schemas de dados

**Parquet D1** (`data/ohlcv/d1/{TICKER}.parquet`, motor `pyarrow`, compressão `zstd`):

| Coluna | Tipo Arrow | Regra |
| --- | --- | --- |
| `session_date` | `date32` | Chave única, ordenada, sem duplicatas |
| `ts_utc` | `timestamp[s, tz=UTC]` | Horário da barra convertido do servidor |
| `open`, `high`, `low`, `close` | `float64` | Brutos (sem ajuste) |
| `real_volume` | `int64` | Quantidade de ações |
| `tick_volume` | `int64` | Negócios |
| `spread` | `int32` | Em pontos |
| `adj_factor` | `float64` | Fator cumulativo de proventos; 1,0 na barra mais recente |
| `source` | `string` | `mt5:<servidor>` |
| `ingested_at` | `timestamp[s, tz=UTC]` | Momento da gravação |

Metadados do arquivo: `schema_version=1`, `ticker`, `timeframe=D1`, `tz_server`. As colunas ajustadas (`adj_close` etc.) são calculadas em memória a partir de `adj_factor`, nunca gravadas, para que um evento corporativo novo não exija reescrever o histórico.

**SQLite** (`data/db/trading.db`, migrações numeradas em `persistence/migrations/`):

```sql
CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);

CREATE TABLE runs (
  run_id TEXT PRIMARY KEY, routine TEXT NOT NULL CHECK (routine IN ('sync','preopen','eod','watchdog','backtest')),
  session_date TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
  status TEXT NOT NULL CHECK (status IN ('RUNNING','OK','FAILED','SKIPPED_HOLIDAY')),
  config_hash TEXT NOT NULL, code_version TEXT NOT NULL, error TEXT);

CREATE TABLE config_snapshots (config_hash TEXT PRIMARY KEY, created_at TEXT NOT NULL, yaml TEXT NOT NULL);

CREATE TABLE signals (
  signal_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id),
  decision_date TEXT NOT NULL, ticker TEXT NOT NULL, setup TEXT NOT NULL,
  trade_class TEXT NOT NULL, entry_type TEXT NOT NULL, entry_ref REAL NOT NULL,
  stop_price REAL NOT NULL, target_price REAL, features_json TEXT NOT NULL,
  disposition TEXT NOT NULL CHECK (disposition IN ('ACCEPTED','REJECTED_HEAT','REJECTED_CLUSTER',
    'REJECTED_REGIME','REJECTED_COOLDOWN','REJECTED_SIZE','REJECTED_RR','SHADOW')),
  UNIQUE (decision_date, ticker, setup));

CREATE TABLE order_intents (
  client_order_id TEXT PRIMARY KEY, signal_id TEXT REFERENCES signals(signal_id),
  position_id TEXT, ticker TEXT NOT NULL, side TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
  qty INTEGER NOT NULL CHECK (qty > 0), order_type TEXT NOT NULL, price REAL, stop_loss REAL,
  valid_until TEXT NOT NULL, reason TEXT NOT NULL, mode TEXT NOT NULL CHECK (mode IN ('paper','live')),
  created_at TEXT NOT NULL);

CREATE TABLE order_events (                       -- somente INSERT
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  client_order_id TEXT NOT NULL REFERENCES order_intents(client_order_id),
  ts_utc TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('PLANNED','SUBMITTED','ACCEPTED','PARTIALLY_FILLED',
    'FILLED','CANCELLED','EXPIRED','REJECTED','UNKNOWN')),
  broker_order_id TEXT, retcode INTEGER, filled_qty INTEGER, avg_price REAL, fees REAL, detail TEXT);

CREATE TABLE positions (
  position_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, setup TEXT NOT NULL, trade_class TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('OPEN','PARTIAL','CLOSED')),
  opened_on TEXT NOT NULL, closed_on TEXT, entry_price REAL NOT NULL, initial_stop REAL NOT NULL,
  current_stop REAL NOT NULL, target_price REAL, qty_initial INTEGER NOT NULL, qty_open INTEGER NOT NULL,
  risk_brl REAL NOT NULL, exit_reason TEXT CHECK (exit_reason IN ('STOP','TARGET','TRAILING','TIME',
    'STAGNATION','BREAKEVEN','MANUAL','CIRCUIT_BREAKER')),
  pnl_brl REAL, r_multiple REAL, mae_r REAL, mfe_r REAL, bars_held INTEGER);

CREATE TABLE equity_daily (
  session_date TEXT NOT NULL, curve TEXT NOT NULL CHECK (curve IN ('REAL','PAPER','SHADOW')),
  equity REAL NOT NULL, cash REAL NOT NULL, heat_pct REAL NOT NULL, drawdown_pct REAL NOT NULL,
  PRIMARY KEY (session_date, curve));

CREATE TABLE protections (
  scope TEXT NOT NULL CHECK (scope IN ('TICKER','SETUP','PORTFOLIO')), key TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('COOLDOWN','SETUP_SUSPENDED','DD_LEVEL1','DD_LEVEL2','REGIME_BLOCK')),
  active_from TEXT NOT NULL, active_until TEXT, reason TEXT NOT NULL,
  PRIMARY KEY (scope, key, kind, active_from));

CREATE TABLE data_quality (
  session_date TEXT NOT NULL, ticker TEXT NOT NULL, check_name TEXT NOT NULL,
  severity TEXT NOT NULL CHECK (severity IN ('INFO','WARN','BLOCK')), detail TEXT,
  PRIMARY KEY (session_date, ticker, check_name));
```

PRAGMAs aplicados em toda conexão: `journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`, `busy_timeout=5000`. Datas em ISO 8601; valores monetários em `REAL` só para relatório, com cálculo de ordem em `Decimal` arredondado ao tick do símbolo.

### 2.5 Mocks para testes sem MT5

O pacote `MetaTrader5` só instala no Windows, então ele entra no `pyproject.toml` com marcador `sys_platform == "win32"` e o `MT5Gateway` recebe o módulo por parâmetro (`MT5Gateway(mt5_module=FakeMT5(...))`).

`src/ts/data/fake_mt5.py` reproduz a superfície usada da API:

- `initialize`, `shutdown`, `login`, `last_error`, `terminal_info`, `account_info`, `version`.
- `symbol_select`, `symbol_info` (com `visible`, `trade_tick_size`, `filling_mode`, `volume_min`, `volume_step`), `symbol_info_tick`.
- `copy_rates_range` e `copy_rates_from_pos` retornando `numpy` structured array com o mesmo dtype do MT5 (`time, open, high, low, close, tick_volume, spread, real_volume`).
- `order_check`, `order_send` (retorno com `retcode`, `order`, `deal`, `volume`, `price`, `comment`), `orders_get`, `positions_get`, `history_deals_get`.
- Constantes usadas (`TIMEFRAME_D1`, `TIMEFRAME_M5`, `ORDER_TYPE_*`, `TRADE_ACTION_*`, `ORDER_FILLING_*`, `TRADE_RETCODE_*`).

Injeção de falhas por roteiro, para que cada cenário da tabela 1.3.1 vire um teste:

```python
fake = FakeMT5(bars=load_fixture("petr4_d1_2019_2024.csv"))
fake.script("copy_rates_range", [None, None, OK])        # 2 falhas de IPC e depois sucesso
fake.script("order_send", [LOST_RESPONSE])               # ordem executada, resposta perdida
fake.disconnect_after(calls=5)                           # desconexão silenciosa
fake.set_last_error(-10005, "IPC timeout")
fake.truncate_history("VALE3", keep_last=40)            # histórico incompleto
```

Outros dublês: `FakeClock` (tempo controlado, inclusive troca de horário da B3), `InMemoryStore` para o SQLite (`:memory:` com as mesmas migrações), `RecordingNotifier` no lugar do Telegram. Testes que precisam do terminal real ficam em `tests/integration_mt5/` com `@pytest.mark.mt5` e só rodam com `pytest -m mt5` na máquina Windows.

Fixtures: CSVs de 5 anos de 3 a 5 ativos reais, mais séries sintéticas desenhadas à mão para cada setup (um caso que dispara e um que quase dispara), com o resultado esperado gravado ao lado.

### 2.6 Exceções e políticas de retry

```text
TSError
  ConfigError                  # config inválida: aborta a rotina, sem retry
  CalendarError                # data fora do calendário conhecido: aborta
  DataError
    DataUnavailableError       # MT5 não devolveu barras: retry de leitura
    DataQualityError           # barra inconsistente: ativo marcado BLOCK no dia
    LookaheadError             # bug de programação: aborta e alerta
  ConnectivityError
    TerminalNotRunningError    # reinicia o terminal (watchdog)
    IPCError                   # retry de leitura; reconciliação na escrita
    NotLoggedInError           # novo login; depois alerta
  BrokerError
    OrderRejectedError         # erro de negócio: sem retry, alerta
    OrderStateUnknownError     # resposta perdida: reconciliar antes de qualquer ação
    TradingDisabledError       # AutoTrading desligado: sem retry, alerta
  RiskLimitError               # ordem violaria teto: descarta o sinal e registra
```

| Operação | Tentativas | Espera | Após esgotar |
| --- | --- | --- | --- |
| `initialize`/login | 5 | Exponencial 2 s a 30 s | Reiniciar terminal uma vez; depois `TerminalNotRunningError` e alerta |
| Leitura de barras ou tick | 3 | Exponencial 1 s a 8 s, com jitter | Ativo marcado `STALE` no dia; rotina segue com os demais |
| Escrita Parquet (`os.replace`) | 5 | 0,2 s a 2 s | Falha da rotina e alerta |
| SQLite ocupado | Coberto pelo `busy_timeout` | 5 s | Falha da rotina |
| `order_send` | 1 envio + até 2 reenvios, só para retcodes retentáveis | Novo preço a cada vez | Intenção marcada `REJECTED` ou `UNKNOWN` e alerta |
| Resposta de ordem perdida | 0 reenvios cegos | Reconciliar por `client_order_id` em 2 s, 5 s e 15 s | Se ainda desconhecida: `UNKNOWN`, bloqueia novas ordens do ativo e alerta |
| Telegram | 3 | 2 s a 10 s | Grava em `logs/` e segue |

A máquina de estados da ordem aceita só estas transições: `PLANNED → SUBMITTED → ACCEPTED → (PARTIALLY_FILLED →) FILLED`, e de qualquer estado não final para `CANCELLED`, `EXPIRED`, `REJECTED` ou `UNKNOWN`; `UNKNOWN` só sai por reconciliação. Transição fora disso levanta exceção e é testada.

### 2.7 Dependências

| Pacote | Uso | Observação |
| --- | --- | --- |
| `MetaTrader5` | Gateway do terminal | Só Windows (marcador de plataforma) |
| `numpy`, `pandas` | Cálculo | — |
| `pyarrow` | Parquet | Substitui `fastparquet` |
| `pydantic` v2, `PyYAML` | Validação de config | Erro de config falha no início, não no meio do pregão |
| `tenacity` | Retry | Só em leituras e escrita de arquivo |
| `keyring` | Senha do MT5 e token do Telegram | Gerenciador de Credenciais do Windows |
| `httpx` | Telegram (API HTTP direta) | Mais simples que `python-telegram-bot` para só enviar mensagens |
| `rich`, `streamlit`, `plotly` | CLI e dashboard | S7 |
| `pytest`, `hypothesis`, `ruff`, `mypy` | Qualidade | — |

Fora da versão 1: `pandas-ta` (manutenção irregular e incompatibilidades com versões novas do numpy; os 10 indicadores necessários cabem em um módulo próprio testado contra valores de referência) e `vectorbt` (o backtest de validação precisa reusar o código de produção; o vectorbt pode entrar depois só para varreduras de parâmetros). As versões exatas são fixadas no `requirements.lock` gerado no primeiro build que passar nos testes.

## Sprint 1 — Roteiro passo a passo para o Antigravity

Objetivo da Sprint 1: ter, na máquina Windows, 5 anos de barras D1 do universo IBX-100 (mais o IBOV) gravadas em Parquet, validadas e atualizadas de forma incremental e idempotente, com todo o código testado sem o MT5 aberto. Nenhuma lógica de estratégia entra nesta sprint.

**Passo 0 — Ambiente (manual, uma vez).**

1. Instalar Python 3.12 64-bit e confirmar: `py -3.12 -c "import struct; print(struct.calcsize('P')*8)"` deve imprimir `64`.
2. Criar `C:\TradingSystem`, inicializar git e o ambiente virtual: `py -3.12 -m venv .venv` e `.venv\Scripts\Activate.ps1`.
3. Instalar o MT5 da corretora, fazer login na conta, ligar "Algo Trading" e marcar em Opções → Expert Advisors "Permitir negociação algorítmica". Anotar o caminho do `terminal64.exe` e o nome do servidor.
4. Guardar a senha no Gerenciador de Credenciais: `python -c "import keyring; keyring.set_password('ts-mt5', '<login>', '<senha>')"` (digitar localmente; nunca versionar).

**Passo 1 — Esqueleto do projeto.** Criar `pyproject.toml` com as dependências da seção 2.7 (`MetaTrader5; sys_platform == "win32"`), configuração de `ruff`, `mypy --strict` em `src/ts/core` e `src/ts/data`, e `pytest` com o marcador `mt5`. Criar a árvore de pastas da seção 2.2. Aceite: `pytest` roda (zero testes) e `ruff check` passa.

**Passo 2 — `core/config.py`.** Modelos `pydantic` para `settings.yaml` (caminho do terminal, servidor, login, modo `paper`, raiz de dados, nível de log) e um carregador que calcula `config_hash` (SHA-256 do YAML normalizado). Aceite: config inválida gera `ConfigError` com a chave problemática na mensagem.

**Passo 3 — `core/calendar.py` e `b3_calendar.yaml`.** Funções `is_session(d)`, `sessions_between(a, b)`, `previous_session(d)`, `close_time(d)`. Preencher feriados de 2019 a 2027 e os períodos de horário da B3 a partir do site oficial; cada período com data de início e fim. Aceite: testes com Carnaval, Corpus Christi, véspera de Ano-Novo e as duas datas de troca de horário de 2025.

**Passo 4 — `core/errors.py` e `core/logging.py`.** A hierarquia da seção 2.6 e log em JSON por linha em `logs/ts-YYYY-MM-DD.jsonl` (rotação diária), sempre com `run_id`.

**Passo 5 — `data/fake_mt5.py`.** Implementar o dublê da seção 2.5 antes do gateway real. Aceite: testes do próprio fake (dtype idêntico ao do MT5, roteiro de falhas consumido na ordem).

**Passo 6 — `data/mt5_gateway.py`.** Classe `MT5Gateway(mt5_module, settings, clock)` com:

- `connect()`: `initialize(path=..., login=..., server=..., password=keyring, timeout=60000)`; checa `account_info()` e `terminal_info().connected`; retry da seção 2.6.
- `ensure_symbol(ticker)`: `symbol_select` quando `visible` for falso.
- `daily_bars(ticker, start, end)`: `copy_rates_range(ticker, TIMEFRAME_D1, start_dt, end_dt)`; converte para DataFrame com o schema 2.4; levanta `DataUnavailableError` em None ou vazio.
- `health()`: versão do terminal, conectado, trade permitido, idade do tick do IBOV e latência de uma chamada simples.
- `shutdown()` sempre chamado em `finally`.

Aceite (todos com FakeMT5): sucesso após 2 falhas de IPC; `NotLoggedInError` quando `account_info()` é None; `symbol_select` chamado para ativo invisível; conversão de fuso correta.

**Passo 7 — `data/validation.py`.** Checagens por barra e por série, gravadas na tabela `data_quality`:

| Checagem | Severidade |
| --- | --- |
| `high >= max(open, close)` e `low <= min(open, close)` | BLOCK |
| Preços > 0 e volume ≥ 0 | BLOCK |
| `session_date` duplicada | BLOCK |
| Pregão do calendário sem barra | WARN (BLOCK se for a última) |
| Barra em dia sem pregão | BLOCK |
| Variação de fechamento acima de 25% em um dia | WARN: provável evento corporativo, vai para revisão |
| Última barra mais antiga que o pregão anterior | BLOCK (`STALE`) |

**Passo 8 — `data/ohlcv_store.py`.** `read(ticker) -> DataFrame` e `upsert(ticker, df)`: lê o arquivo existente, une, remove duplicatas por `session_date` (a barra nova vence), ordena, valida o schema e grava de forma atômica (`.tmp` + `os.replace` com retry). Metadados da seção 2.4. Aceite: gravar duas vezes o mesmo conteúdo não altera o hash do arquivo; arquivo travado por outro processo é tratado.

**Passo 9 — `persistence/db.py` e migração 001.** Conexão com os PRAGMAs, executor de migrações e a DDL da seção 2.4 (todas as tabelas, mesmo as usadas só depois). Aceite: migração roda em `:memory:` e em arquivo; rodar duas vezes não falha.

**Passo 10 — `data/sync.py`.** `sync_universe(asof, tickers)`:

1. Registra um `run` com status `RUNNING`.
2. Para cada ativo: lê a última `session_date` gravada; pede barras desde 5 pregões antes dela (sobreposição para capturar correções do servidor) até `asof`; na primeira carga, desde `2019-01-01`.
3. Valida; se houver BLOCK, não grava aquele ativo e registra o motivo.
4. Faz `upsert` e registra contagens.
5. Ativos com falha não interrompem os demais; a rotina termina `OK` se pelo menos 95% do universo e o IBOV estiverem atualizados, senão `FAILED`.

Aceite: rodar `sync` duas vezes seguidas não muda nenhum arquivo; com 3 ativos falhando no FakeMT5, os demais são gravados e o resumo lista os 3.

**Passo 11 — Universo.** `config/universe/ibx100_YYYY_Qn.csv` com a composição de cada carteira teórica (fonte B3) e uma função `universe_on(d)`. O símbolo do IBOV no servidor da corretora fica em `settings.yaml`, porque o nome muda entre corretoras.

**Passo 12 — CLI `app/cli.py`.** Comandos:

- `python -m ts doctor`: health check completo (Python 64-bit, versão do pacote, terminal, login, AutoTrading, fuso do servidor, espaço em disco, escrita na pasta de dados).
- `python -m ts sync --asof 2026-11-17 [--tickers PETR4,VALE3] [--full]`.
- `python -m ts data-report`: tabela Rich com cobertura e problemas por ativo.

Códigos de saída: 0 ok, 0 com `SKIPPED_HOLIDAY`, 2 erro de config, 3 erro de conectividade, 4 qualidade de dados abaixo do mínimo.

**Passo 13 — Testes de integração com o terminal real.** Em `tests/integration_mt5/`: conectar, baixar 30 barras do IBOV e de 3 ativos, conferir o fuso e comparar 5 fechamentos com o histórico oficial da B3. Rodar com `pytest -m mt5` na máquina Windows.

**Passo 14 — Agendamento provisório.** `deploy/tasks/TS_Sync.xml` (dias úteis, 19:30, configurações da seção 1.3.3) e `deploy/scripts/register_tasks.ps1`. Rodar por 5 pregões e conferir `runs` e `data_quality`.

**Definição de pronto da Sprint 1:**

- [ ] `pytest` (sem `-m mt5`) passa em máquina sem MT5; cobertura ≥ 90% em `src/ts/data` e `src/ts/core`.
- [ ] `pytest -m mt5` passa na máquina Windows com o terminal logado.
- [ ] Base de 2019 até hoje gravada para o universo histórico e o IBOV, sem BLOCK em aberto.
- [ ] Dois `sync` seguidos não alteram nenhum arquivo.
- [ ] `doctor` reporta tudo verde.
- [ ] 5 pregões de sync agendado sem intervenção.

## Decisões tomadas

Em 17/11/2026 ricardo aprovou a opção recomendada em todos os itens. Corretora definida: XP. Rever uma decisão altera a Sprint indicada.

| Decisão | Escolhido | Descartado (por ora) | Afeta |
| --- | --- | --- | --- |
| Momento de decisão e execução | Barra D1 fechada às 19:30, execução no leilão de abertura do dia seguinte (fechamento como confirmação, como em Dow, Murphy, Pring e Lemos) | Execução perto do fechamento; pode virar experimento depois do Go | S3, S5, S6 |
| Capital de referência do paper | Mesmo valor previsto para o piloto real | Valor redondo | S4 |
| Mercado fracionário | Desligado | Ligado | S4, S6 |
| Corretora | XP. No Sprint 1, confirmar no MT5 o modo de preenchimento, as ordens no leilão, o símbolo do IBOV e se o histórico é ajustado (teste do degrau numa data de grupamento conhecida) | — | S1, S6 |
| Setup E | Fica no código; sai da versão 1 se o backtest gerar menos de 30 operações em 5 anos | Remover já | S3, S5 |
| Fonte de eventos corporativos | Tabela própria conferida com a B3, mesmo que a XP entregue histórico ajustado | Depender só do ajuste da corretora | S2 |

## Referências

Os livros abaixo estão na pasta do projeto e foram consultados para as citações deste documento.

- **John J. Murphy**, *Análise Técnica do Mercado Financeiro*. Cap. 2 (Teoria de Dow), cap. 9 (Bandas de Bollinger, "A Largura da Banda Mede a Volatilidade", "A Regra Semanal", "Otimizar ou Não"), cap. 10 (IFR, MACD, "A Importância da Tendência"), cap. 16 (diretrizes de gerenciamento de dinheiro, relação risco/retorno 3:1, unidades de tendência e de trading, períodos de adversidade, tipos de ordem).
- **Martin J. Pring**, *Análise Técnica Explicada* (5ª ed.). Cap. 1 (análise de picos e vales, critério de magnitude), cap. 3 (Teoria de Dow), cap. 12 (Bollinger), cap. 13 (divergências de momentum), cap. 19 (força relativa), cap. 34 (sistemas automatizados: regras de backtest, simplicidade, concentração do lucro em poucos sinais).
- **Flávio Lemos e Celso Cardoso**, *Análise Técnica Clássica*. Cap. 7 (Bollinger %b e BandWidth, ADX, parametrização), cap. 8 (força relativa entre ativos), cap. 12 (estratégias de pivô: pivô de alta de 3 pontos), cap. 13 (controle de risco: 1% por operação, retorno/risco 3:1, stop de preço e de tempo, fórmula de Kelly, esperança matemática).
- **Julia Spina**, *O Guia do Investidor Azarado para o Trade de Opções*. Cap. 1 (valor esperado, distribuições e caudas, covariância e correlação), cap. 6 (CVaR 5% e limites do stop loss contra risco de cauda), cap. 7 (alocação dinâmica pela volatilidade do mercado), cap. 8 (diversificação e correlação de P/L na carteira). O livro trata de opções; aqui foram aproveitados apenas os conceitos estatísticos e de alocação.
- **Bailey e López de Prado** (2014), *The Deflated Sharpe Ratio*, e **Bailey, Borwein, López de Prado e Zhu** (2016), *The Probability of Backtest Overfitting*: base dos critérios DSR e PBO do portão G0 (citados de memória, não estão na pasta).
- Documentos de origem: `Arquitetura_de_Trading_Algoritmico_B3.pdf` (anexo) e `prd-sistema-swing-trade-b3.md` (pasta do projeto).
