# Regras do Sistema — Trading System B3

Versão das regras: 17/11/2026 · configuração `config/strategy.yaml` (hash começa com `d2a8ef70`) · modo **PAPER**

Este documento explica, em linguagem simples, **tudo o que o sistema faz para decidir uma compra, quanto comprar,
quando vender e quando parar**. Os números aqui são os que estão configurados hoje. Se você mudar o
`strategy.yaml`, este documento precisa ser atualizado junto.

> **Situação atual:** o sistema **não passou** no portão de validação G0 (seção 9). Ele roda só em PAPER
> (simulação com dados reais, sem ordens na corretora). Nada aqui é recomendação de investimento.

---

## 1. Visão geral do dia

O sistema é de **swing trade diário**: decide com o pregão fechado e executa no pregão seguinte.
Ele não acompanha o mercado durante o dia e **não faz day trade** (seção 10).

| Horário (dias úteis) | Rotina | O que faz |
| --- | --- | --- |
| 08:45 | Pré-abertura | Prepara o pacote de ordens do dia (entradas, stops, alvos, saídas na abertura) |
| 09:30 | Watchdog | Confere se as rotinas rodaram e se os dados estão em dia |
| 10:00 | Pregão | Ordens executam no **leilão de abertura** ou quando o preço de gatilho é atingido |
| 19:30 | Fim do dia (EOD) | Baixa as cotações, atualiza stops, decide saídas, gera os sinais de amanhã |
| 20:30 | Dead-man | Avisa se o EOD não rodou |
| 23:00 | Backup | Cópia do banco de dados |

**Regra de ouro:** toda decisão usa só barras **já fechadas**. Nenhuma regra olha o futuro (há testes
automáticos que garantem isso).

---

## 2. Universo: quais ações o sistema olha

- Ações do **IBrX-100** vigentes na data (lista oficial da B3, atualizada por `ts universe-update`).
- Preços **ajustados por proventos** (dividendos e JCP) para os indicadores; as ordens usam o preço real do dia.
- Ação com menos de **260 pregões** de histórico fica de fora.
- Ação sem a barra do último pregão (suspensa, dado faltando) não gera decisão naquele dia.

---

## 3. Filtros gerais (valem para todos os setups de compra em tendência)

### 3.1 Regime do mercado (IBOV)

O sistema classifica o IBOV todo dia, com o fechamento e as médias de 50 e 200 pregões:

| Regime | Condição | O que o sistema faz |
| --- | --- | --- |
| **ALTA** | IBOV acima da MM200 **e** MM50 acima da MM200 | Setups de tendência (A, B, E) podem comprar |
| **BAIXA** | IBOV abaixo da MM200 **e** MM50 abaixo da MM200 | A, B, E bloqueados; o setup **F** pode comprar |
| **LATERAL** | Qualquer outra combinação | A, B, E, F não compram |

O setup **G** é a exceção: opera **em qualquer regime**, porque o seu próprio filtro de longo prazo (IFR mensal)
já exige que a ação esteja forte.

Posições já abertas seguem com seus stops e saídas em qualquer regime.

### 3.2 Tendência do ativo
Para A, B e E: o fechamento da ação precisa estar **acima da sua média de 200 pregões**. (O setup F não usa
este filtro.)

### 3.3 Roteador de regime (ligado)
Cada setup só compra na fase do **Método do Diamante** (fechamento × MM50 × MM200 da própria ação) em que tem
vantagem, e alguns exigem que o preço esteja andando em tendência (eficiência de Kaufman de 10 pregões ≥ 0,30):

| Setup | Fases permitidas | Exige tendência (eficiência ≥ 0,30)? |
| --- | --- | --- |
| A | 2 Acumulação, 3 Altista | Sim |
| B | 3 Altista | Não |
| E | 3 Altista, 4 Aviso | Não |
| F, G | — (não passam pelo roteador) | Não |

Fases: 1 Recuperação · 2 Acumulação · 3 Altista · 4 Aviso · 5 Distribuição · 6 Baixista.

---

## 4. Os setups (gatilhos de compra)

O sistema **só compra** (não vende a descoberto). Situação de cada setup:

| Setup | Nome | Situação | Quando opera |
| --- | --- | --- | --- |
| **A** | Pivô de alta | Ligado | Mercado em ALTA |
| **B** | Pullback | Ligado | Mercado em ALTA |
| C | Donchian 20/10 | **Desligado** (perdia dinheiro nos testes) | — |
| D | Bollinger squeeze | **Desligado** (perdia dinheiro nos testes) | — |
| **E** | Divergência de alta | Ligado (raramente dispara) | Mercado em ALTA |
| **F** | Reversão na baixa | **Ligado em caráter experimental** | Mercado em BAIXA |
| **G** | Força mensal + MACD | **Ligado em caráter experimental** | Qualquer regime |

### Setup A — Pivô de alta (o principal)
Todas as condições no fechamento do dia:
1. Os três últimos **pivôs confirmados** formam **fundo → topo → fundo mais alto** (o segundo fundo acima do primeiro).
   Pivô = topo ou fundo com 2 barras de cada lado e amplitude de pelo menos 1 ATR.
2. O fechamento passa **acima do topo** do meio (rompimento).
3. **Volume relativo** (volume do dia ÷ média de 20 dias) **≥ 1,5**.
4. **ADX(14) ≥ 20** (há tendência).
5. **Histograma do MACD positivo e maior que o de ontem** (momento a favor).

- **Entrada:** leilão de abertura do dia seguinte.
- **Stop inicial:** 1 centavo abaixo do último fundo (ajustado pelas regras de tamanho da seção 5).

### Setup B — Pullback
1. Fechamento e média exponencial de 20 acima da média de 50.
2. Correção de **2 a 5 pregões** com máximas descendentes seguidas.
3. A mínima do dia fica **entre a MME20 e a MM50**.
4. **IFR(14) ≤ 45**.
5. **ADX ≥ 20** em algum dos últimos 10 pregões.
6. Espaço até o próximo topo de pelo menos **3 vezes o risco**.

- **Entrada:** ordem de **stop de compra** 1 centavo acima da máxima do dia (só entra se o preço subir).
- **Stop:** 1 centavo abaixo da mínima do dia. **Alvo:** 3R.

### Setup E — Divergência de alta
1. Dois fundos confirmados, com 5 a 30 pregões entre eles; o segundo **no mesmo nível ou abaixo** do primeiro (tolerância 0,5%).
2. O **IFR(14)** no segundo fundo é **pelo menos 3 pontos maior** (o preço caiu, a força vendedora não).
3. Histograma do MACD subindo.
4. Espaço até o próximo topo de pelo menos 3R.

- **Entrada:** stop de compra 1 centavo acima da máxima do candle que confirmou o fundo.
- **Stop:** abaixo do segundo fundo. **Alvo:** 3R.

### Setup F — Reversão em mercado de baixa (experimental)
Base: estudo de 17/11/2026 (IFR2 de Larry Connors condicionado ao regime). Comprar quedas fortes **só funcionou
com o mercado em baixa**; em alta ou lateral a mesma regra perdeu dinheiro.
1. IBOV em regime de **BAIXA** (seção 3.1).
2. **IFR de 2 períodos < 10** (queda forte e rápida).

- **Entrada:** leilão de abertura do dia seguinte.
- **Stop de proteção:** 2,8 ATR abaixo do fechamento.
- **Saída:** quando o fechamento passa **acima da média de 5 pregões** (vende na abertura seguinte), ou no máximo
  em **10 pregões**.

### Setup G — Força mensal + virada do MACD diário (experimental)
Base: laboratório de múltiplos tempos gráficos de 17/11/2026 (ideia do trader). Combina um filtro de **longo
prazo** com um gatilho **diário**:
1. **IFR(14) no gráfico mensal ≥ 60** — a ação está forte há meses. Usa só **meses encerrados**: em qualquer
   dia de novembro, o valor considerado é o calculado até o fim de outubro.
2. **Histograma do MACD(12, 26, 9) diário cruza acima de zero** (hoje > 0 e ontem ≤ 0) — o preço voltou a subir.

- Não usa filtro de regime do IBOV nem de tendência do ativo (o IFR mensal faz esse papel).
- **Entrada:** leilão de abertura do dia seguinte. **Stop de proteção:** 2,8 ATR(14) abaixo do fechamento.
- **Saída:** stop móvel Chandelier (máxima atingida − 3 × ATR(22)), ativo desde o primeiro dia.
- Perfil: acerta menos da metade das vezes, mas os ganhos são maiores que as perdas.

---

## 5. Quanto comprar (tamanho da posição)

1. **Risco por operação:** **1% do patrimônio** = quanto você perde se o stop for atingido.
   Exemplo: patrimônio R$ 100.000, entrada R$ 20,00, stop R$ 19,00 → risco R$ 1,00 por ação →
   R$ 1.000 ÷ R$ 1,00 = 1.000 ações (arredondado para lotes de 100).
2. **Distância do stop:** no mínimo **1 ATR** (se o stop técnico for mais curto, ele é afastado); se passar de
   **3 ATR**, a operação **é recusada**.
3. **Teto de capital:** nenhuma posição passa de **20% do patrimônio**.
4. **Teto de gap:** o tamanho é reduzido para que um gap de abertura extremo (pior 1% histórico do ativo) não
   custe mais que **2% do patrimônio**.
5. **Volatilidade do mercado** (percentil da volatilidade de 20 dias do IBOV nos últimos 5 anos):

| Percentil | Risco por operação | Risco total máximo da carteira |
| --- | --- | --- |
| até 50 | 100% (1%) | 5% |
| 50 a 80 | 75% | 4% |
| 80 a 95 | 50% | 2,5% |
| acima de 95 | **sem compras novas** | 0% |

---

## 6. Limites da carteira

Quando há mais sinais do que espaço, o sistema prioriza as ações com **maior força relativa** (retorno de 60
pregões acima do IBOV) e aceita enquanto todos os limites abaixo forem respeitados:

| Limite | Valor |
| --- | --- |
| Risco aberto total (soma dos riscos até os stops) | ≤ **5%** do patrimônio (menos em volatilidade alta) |
| Risco em ações muito correlacionadas entre si | ≤ **3%** |
| Ações do mesmo grupo de correlação (> 0,70) | no máximo **2** |
| Beta da carteira contra o IBOV | ≤ **1,2** |
| Uma posição por ação | não compra o que já está na carteira |

---

## 7. Como as ordens são executadas

- **Leilão de abertura (A e F):** ordem **limitada** a no máximo **3% acima** do fechamento de referência, e nunca
  acima do preço em que a perda no stop passaria do risco planejado. Abriu mais alto que isso → **não compra**.
  Abriu **abaixo do stop** → **não compra** (o setup foi invalidado pelo gap).
- **Stop de compra (B e E):** vale só para o dia seguinte, como **stop-limitada** (não executa se o gap levar o
  risco além do planejado).
- **Stop de venda:** fica registrado na corretora junto com a entrada desde o primeiro dia. Se a ação abrir abaixo
  do stop, a venda sai na abertura (pode perder um pouco mais que 1R).
- **Custos considerados:** taxas B3 0,03% por ordem + 0,05% de escorregamento; corretagem zero.
- **IR:** 15% sobre o lucro mensal de swing trade; isento quando as vendas de ações do mês ficam até R$ 20.000.

### 7.1 Filtro fundamentalista: o Cérebro (TradingAgents B3) — opcional

Quando a integração está ligada (`cerebro.enabled`), toda **compra nova** precisa também da autorização do
Cérebro, que analisa os fundamentos da empresa (especificação: `INTEGRACAO_ROBO_TECNICO.md`).

| Passo | Quando | O que acontece |
| --- | --- | --- |
| Pedido de análise | Fim do EOD (19:30) | Para cada compra planejada para amanhã sem autorização válida, o sistema pede análise ao Cérebro (prioridade alta) |
| Decisão | Segundos a ~1 min depois | O Cérebro responde pelo webhook: **autorizado**, **rejeitado** ou **pausa** (dados indisponíveis), com convicção e validade em dias |
| Compra | Leilão de abertura (10:00) | Só executa se houver **"autorizado" ainda válido às 10:00** daquele dia. Decisões que chegaram depois das 10:00 não contam para aquele pregão |
| Resultado | Quando o trade fecha | O sistema informa ao Cérebro o resultado (ganho/perda, %, motivo da saída) para ele aprender |

- O Cérebro **só veta ou libera compras** que as regras técnicas já escolheram. Ele não cria ordens e não mexe em
  stops, alvos ou saídas de posições abertas.
- Sem decisão válida (Cérebro desligado, fora do ar ou atrasado) → a compra **não acontece** (falha segura).
  No modo `advisory` (consultivo) ela acontece e o sistema só avisa.
- O backtest e o G0 **não** usam o Cérebro: não existe histórico de decisões dele. O efeito do filtro só pode ser
  medido no paper.
- Ver na tela: coluna **Fund.** no Market Watch (✓ libera · ✗ rejeitado/vencido · ⏸ pausa · — sem análise), cartão
  **Cérebro** no Raio-X (tese, indicadores, botão "Pedir análise agora") e linha **Cérebro** em "Saúde do sistema".

---

## 8. Quando vender

| Setup | Stop | Saída com lucro | Outras saídas |
| --- | --- | --- | --- |
| **A** | Inicial no último fundo | **Stop móvel (Chandelier):** depois de andar +1R, o stop sobe para a máxima atingida − 3 × ATR(22) | **Estagnação:** 15 pregões sem chegar a +1R → vende na abertura |
| **B / E** | Inicial na mínima / fundo | **Metade em +1,5R** (a partir do pregão seguinte à entrada) e o stop vai para o preço de entrada + custos; o resto no **alvo de 3R** | **Tempo:** 8 pregões → vende na abertura |
| **F** | 2,8 ATR | Fechamento **acima da média de 5** → vende na abertura seguinte | **Tempo:** 10 pregões |
| **G** | 2,8 ATR | **Stop móvel (Chandelier)** desde a entrada: máxima atingida − 3 × ATR(22) | — |

- O stop **só sobe**, nunca desce.
- Decisões tomadas no fechamento (estagnação, tempo, saída do F) executam no **leilão de abertura** do dia seguinte.
- "R" = o risco da operação. +2R = ganhou 2 vezes o que arriscou.

---

## 9. Proteções automáticas

| Proteção | Quando dispara | O que acontece | Como volta |
| --- | --- | --- | --- |
| **Cooldown do ativo** | 2 stops no mesmo ativo em 30 dias | Ativo fica 10 pregões sem compras | Sozinho, após 10 pregões |
| **Monitor do setup** | Média das últimas 30 operações do setup significativamente negativa | Setup **suspenso** | **Sozinho**, quando as últimas 20 operações acompanhadas "na sombra" (sem dinheiro) têm média positiva |
| **Circuit breaker nível 1** | Queda de **8%** desde o pico | Risco por operação cai pela metade | Sozinho, quando o drawdown diminui |
| **Quarentena** (nível 2) | Queda de **12%** desde o pico **ou 6 perdas seguidas** | Nenhuma compra nova; sinais seguem numa carteira-sombra; o sistema roda uma **reotimização** dentro das travas | Quando a curva-sombra fecha acima da sua média de 20 pregões, **ou** quando você aprova a configuração candidata |

**O sistema nunca muda uma regra sozinho.** A reotimização só propõe uma configuração candidata, dentro de limites
que você definiu (`config/strategy_default.yaml`); ela só vale depois da sua aprovação no terminal. Os parâmetros
de risco, carteira e proteções **nunca** são reotimizados.

---

## 10. O que o sistema NÃO faz (e por quê)

- **Day trade:** testado em 17/11/2026 com 21 regras clássicas, separadas também por regime do mercado.
  Nenhuma passou. A mais promissora ("comprar gap de baixa > 2% com o mercado em baixa") deu +0,7% por operação,
  mas o resultado vinha de poucos dias de pânico do mercado inteiro (75 ações no mesmo dia em 2020): contando
  um resultado por dia, a estatística ficou indistinguível de sorte (t ≈ 1), com perdas de até 15% em um dia.
  Detalhes em `Estudo — Day Trade B3.md`.
- **Venda a descoberto**, opções, alavancagem, mini-índice/mini-dólar.
- **Prever preço com inteligência artificial:** filtros de aprendizado de máquina (meta-labeling) e previsão de
  volatilidade (GARCH) foram testados e não melhoraram o resultado.
- **Outras combinações de tempos gráficos** testadas e reprovadas: Tela Tripla de Elder (MACD semanal + IFR2),
  estágio 2 de Weinstein (MM de 30 semanas + rompimento), IFR mensal + IFR2 diário, MM de 10 meses + IFR semanal.

---

## 11. Validação: o portão G0

Antes de dinheiro real, o sistema precisa passar em **todos** os critérios, medidos fora da amostra e com custos:

| Critério | Meta | Situação (17/11/2026, A+B+F+G) |
| --- | --- | --- |
| Ganho médio por operação | > 0 | ✓ +0,088R |
| Limite inferior do intervalo de confiança de 95% | > 0 | ✓ +0,01R (no limite) |
| Deflated Sharpe (desconta as ~73 variações testadas) | ≥ 0,95 | ✗ 0,70 |
| Robustez entre configurações (PBO) | < 0,20 | a medir no relatório oficial |
| Maior operação / lucro total | ≤ 20% | a medir |
| Parâmetros vizinhos também positivos | todos | ✓ F (IFR2 5–20) e G (IFR mensal 50/60/70) |
| Positivo em pelo menos 2 de 3 regimes | 2/3 | a medir |

Evolução do backtest completo 2019–nov/2026 (com todas as proteções):

| Configuração | Operações | Acerto | Retorno 7 anos | Queda máxima | Prova 2024+ |
| --- | --- | --- | --- | --- | --- |
| A + B | 99 | 46% | −1,9% | 8,4% | — |
| A + B + F | 401 | 62% | +11,7% | 14,7% | +3,0% |
| **A + B + F + G (atual)** | **520** | 53% | **+27,6%** | 15,4% | **+6,6%** |

O setup G teve um ano ruim (2024: −0,33R por operação); os demais anos foram positivos ou neutros.

**Limitações conhecidas:** o histórico só contém ações que ainda existem (faltam casos como Americanas), o que
favorece estratégias de comprar quedas; o período tem poucos ciclos completos de baixa.

---

## 12. Qualquer mudança de regra reinicia o paper

Cada configuração tem uma impressão digital (hash). Mudou um número no `strategy.yaml` → o hash muda → a contagem
de dias do paper **recomeça do zero**, porque os resultados antigos não valem mais para as regras novas.

Para entender a decisão de um ativo específico, abra o **Raio-X** no terminal: ele mostra cada condição de cada
setup com o valor medido e o limite exigido.
