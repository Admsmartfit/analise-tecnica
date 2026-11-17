# Estudo — Day Trade em ações da B3

Data: 17/11/2026. Pergunta: vale a pena o sistema também operar day trade?

**Resposta curta: não, por enquanto.** Nenhuma das 21 regras clássicas de day trade testadas em 100 ações do IBrX teve lucro depois dos custos com significância estatística. Os custos (≈0,15% por operação, ida e volta) são maiores que a vantagem bruta de quase todas elas, e o day trade ainda paga 20% de IR sobre o lucro, contra 15% no swing.

---

## 1. Dados usados

| Fonte | Período | Uso |
| --- | --- | --- |
| Barras diárias (base do sistema) | 2019 – nov/2026, 100 ações | Estratégias que compram/vendem na abertura e zeram no fechamento |
| Yahoo, candles de 60 min | dez/2023 – nov/2026 (≈730 pregões) | Rompimento da 1ª hora, momentum intradiário |
| Yahoo, candles de 5 min | ago – nov/2026 (60 pregões) | Rompimento dos primeiros 15/30 min e checagem cruzada |

Cuidados aplicados:

- O pregão de hoje (em andamento) foi excluído.
- Datas ex-dividendo foram excluídas: o "gap" nesses dias é o dividendo, não um movimento do mercado.
- A abertura diária bate com o 1º candle de 5 min (diferença mediana de 0,000%), então é um preço negociável no leilão de abertura.
- Quando um candle toca o stop e a entrada (ou o alvo) ao mesmo tempo, considerei o pior caso.

## 2. Custos assumidos

| Item | Valor | Observação |
| --- | --- | --- |
| Taxas B3 de day trade (ida + volta) | ≈0,046% | Estimativa; confirme na tabela vigente da B3 e da corretora |
| Slippage | 0,05% por lado | Ações líquidas; em ações menos líquidas é maior |
| **Total por operação** | **0,15%** | Coluna "Custo p/ empatar" = custo máximo que a regra aguenta |
| Corretagem | 0 | Corretoras de taxa zero |
| IR | 20% sobre o lucro mensal + 1% retido na fonte | Não descontado na tabela (só pioraria) |

## 3. Resultados

"Líquido/op." = retorno médio por operação depois dos custos. "t" acima de 2 indicaria algo além do acaso, mas como testei 21 regras, o limite honesto fica perto de 3. "Anos +" = anos com resultado líquido positivo.

| Estratégia | Op. | Acerto líq. | Bruto/op. | Líquido/op. | t | Anos + |
| --- | --- | --- | --- | --- | --- | --- |
| Referência: comprar na abertura, vender no fechamento, todo dia | 179.125 | 45% | −0,026% | −0,176% | −31,4 | 0/8 |
| Gap de baixa > 1%: comprar (contra o gap) | 16.128 | 51% | +0,171% | +0,021% | +0,8 | 5/8 |
| Gap de alta > 1%: vender (contra o gap) | 21.326 | 52% | +0,181% | +0,031% | +1,5 | 4/8 |
| Gap de baixa > 2%: comprar | 4.571 | 52% | +0,217% | +0,067% | +1,0 | 4/8 |
| Gap de alta > 2%: vender | 5.962 | 53% | +0,237% | +0,087% | +1,7 | 4/8 |
| Gap de baixa > 3%: comprar | 1.946 | 51% | +0,096% | −0,054% | −0,4 | 3/8 |
| Gap de alta > 3%: vender | 2.486 | 53% | +0,287% | +0,137% | +1,5 | 4/8 |
| Gap de alta > 2%: comprar (a favor do gap) | 5.962 | 43% | −0,237% | −0,387% | −7,7 | 1/8 |
| Gap de baixa > 2%: vender (a favor do gap) | 4.571 | 44% | −0,217% | −0,367% | −5,7 | 2/8 |
| Gap > 1 desvio diário: contra o gap | 7.155 | 52% | +0,110% | −0,040% | −1,0 | 5/8 |
| 60 min: rompimento da 1ª hora, só compra | 38.027 | 39% | −0,074% | −0,224% | −31,8 | 0/4 |
| 60 min: rompimento da 1ª hora, só venda | 39.546 | 39% | −0,079% | −0,229% | −33,6 | 0/4 |
| 60 min: rompimento da 1ª hora, lado que romper primeiro | 62.171 | 43% | +0,041% | −0,109% | −19,2 | 0/4 |
| 60 min: idem, alvo 2R | 62.171 | 43% | +0,040% | −0,110% | −19,9 | 0/4 |
| 60 min: momentum (5 primeiras horas → última hora) | 50.747 | 36% | 0,000% | −0,150% | −55,7 | 0/4 |
| 5 min: rompimento dos primeiros 15 min | 5.723 | 36% | −0,115% | −0,265% | −9,3 | 0/1 |
| 5 min: idem, alvo 2R | 5.723 | 37% | −0,098% | −0,248% | −9,2 | 0/1 |
| 5 min: rompimento dos primeiros 30 min | 5.675 | 41% | −0,043% | −0,193% | −7,2 | 0/1 |
| 5 min: idem, alvo 2R | 5.675 | 42% | −0,041% | −0,191% | −7,2 | 0/1 |

## 4. O que os números dizem

1. **Na média, quem compra na abertura e vende no fechamento perde.** Nessas ações o ganho acontece de um dia para o outro, não dentro do pregão. Mesmo antes dos custos o resultado é −0,026% por operação.
2. **Seguir a direção do gap é a pior ideia testada** (−0,37% a −0,39% por operação). Operar contra o gap é o único efeito com sinal consistente: grandes gaps tendem a devolver parte do movimento no mesmo dia. Esse efeito bruto (+0,17% a +0,29%) mal cobre o custo de 0,15%. O líquido fica entre +0,02% e +0,14%, com t abaixo de 2 e lucro em só metade dos anos. Com 21 regras testadas, esse resultado é compatível com o acaso.
3. **Rompimento de abertura (ORB) não funciona em ações da B3** em nenhuma versão (60 min ou 5 min, com ou sem alvo). O acerto de 36% a 43% não compensa o tamanho das perdas.
4. **O momentum intradiário** (estudado no S&P 500) não aparece aqui: a vantagem bruta é zero.

### Um erro que eu peguei no meio do caminho

A primeira versão do teste "gap > 1 desvio, contra o gap" mostrou **+0,256% líquido por operação, t = 7,2, 7 de 8 anos positivos**, o que parecia uma estratégia excelente. Eu tinha errado: o filtro de volatilidade usava o fechamento do próprio dia, que ninguém conhece na abertura. Corrigido, o resultado virou **−0,040%**. É o tipo de erro que faz um backtest prometer o que o mercado não paga. O sistema de swing já se protege disso com `FeatureView` causal, e um módulo de day trade precisaria da mesma disciplina.

## 5. Por que o day trade é mais difícil aqui do que no swing

| Ponto | Swing (sistema atual) | Day trade |
| --- | --- | --- |
| Custo relativo ao movimento | 0,15% contra alvos de 5% a 15% | 0,15% contra movimentos de 0,5% a 1,5% |
| IR | 15% (isenção até R$ 20 mil/mês de vendas não se aplica a DT) | 20% + 1% retido na fonte |
| Dados | Diário, gratuito, já validado | Yahoo atrasa cerca de 15 min, inútil para operar ao vivo; precisa do feed do MT5 da corretora |
| Infraestrutura | 2 rotinas por dia (pré-abertura e fim do dia) | Motor rodando o pregão inteiro, reconexão, ordens a mercado, zeragem obrigatória até 16:50 |
| Risco operacional | Stop no servidor da corretora | Queda de internet ou PC com posição aberta; a corretora zera compulsoriamente (e cobra) |
| Validação | 7 anos de histórico | 5 min só tem 60 dias grátis; histórico longo exige o MT5 |

## 6. O que não foi testado (e como testar, se quiser continuar)

- **Mini-índice (WIN) e mini-dólar (WDO).** São os ativos mais usados no day trade na B3, com custo por contrato muito menor que em ações. O Yahoo não tem esse histórico. O MT5 da corretora (XP, Clear, Genial etc.) fornece WIN$ / WDO$ em 1 e 5 min por vários anos. Esse é o próximo teste que faz sentido, e o `MT5Gateway` do sistema já sabe se conectar.
- **Contra o gap com stop intradiário e saída antes do fechamento** (ex.: alvo no fechamento do dia anterior). O efeito bruto existe; uma saída melhor poderia aumentar a margem sobre o custo. Com 21 regras já testadas, uma nova versão precisaria de t > 3 para ser levada a sério.
- **Leilões e microestrutura** (book, agressão, VWAP). Exigem dados tick a tick, que só o MT5 ou feeds pagos fornecem.

## 7. Recomendação

1. **Não construir um módulo de day trade em ações agora.** Nenhuma regra passou num critério equivalente ao G0 do swing.
2. **Se quiser insistir no day trade**, o próximo passo barato é um laboratório de **WIN/WDO com histórico do MT5** (≈1 dia de trabalho), usando o mesmo padrão deste estudo: custos reais, pior caso nos candles ambíguos, filtros sem look-ahead e contagem de tentativas no DSR.
3. **Manter o foco do sistema no swing.** Com A + B e o roteador, o swing já está perto do zero a zero com metade do rebaixamento. A melhoria que falta está no sinal de entrada do setup A, não no prazo da operação.

---

Scripts do estudo (pasta temporária da sessão): `dt_fetch.py` (download), `dt_study.py` (testes), `dt_check.py` (checagens cruzadas).
