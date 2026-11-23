# Integração: Robô Técnico ⇄ TradingAgents B3 (Cérebro Fundamentalista)

Especificação de tudo o que o **Robô Técnico** precisa **enviar** e **receber** para trabalhar com o
TradingAgents B3. Válida para a versão 8.0 do Cérebro.

---

## 1. Visão geral

São **3 mensagens**, todas HTTP + JSON (UTF-8):

```
 ROBÔ TÉCNICO                                          CÉREBRO (TradingAgents B3)
      │                                                          │
      │ ① POST /api/queue/demand   {"ativos": ["PETR4", ...]}     │
      │ ───────────────────────────────────────────────────────▶ │  responde na hora (HTTP 200)
      │ ◀─────────────────────────────────────────────────────── │  e analisa em segundo plano
      │                       (segundos a ~1 min por ativo)       │
      │ ② POST <URL do robô>   {decisão do ativo}  (webhook)      │
      │ ◀─────────────────────────────────────────────────────── │  1 webhook por ativo analisado
      │ ─── resposta HTTP 2xx em até 10 s ──────────────────────▶ │
      │                                                          │
      │   ... robô opera, encerra o trade ...                    │
      │                                                          │
      │ ③ POST /api/memory/feedback  {"ativo","resultado",...}    │
      │ ───────────────────────────────────────────────────────▶ │  IA registra a "lição aprendida"
      │ ◀─────────────────────────────────────────────────────── │  e usa nas próximas análises
```

| # | Direção | Quem chama | Endereço |
|---|---|---|---|
| ① | Robô → Cérebro | Robô | `POST http://<IP-do-Cérebro>:8000/api/queue/demand` |
| ② | Cérebro → Robô | Cérebro (webhook) | `POST <URL configurada no Cérebro>` (padrão `http://localhost:5000/webhook/decisao`) |
| ③ | Robô → Cérebro | Robô | `POST http://<IP-do-Cérebro>:8000/api/memory/feedback` |

---

## 2. Configuração dos dois lados

### No Cérebro (TradingAgents B3)
| O quê | Onde | Exemplo |
|---|---|---|
| URL que vai **receber** as decisões (webhook ②) | Cockpit → **Watchdog & Ajustes** → campo **"URL de Callback do Robô Técnico (Webhook)"**. (Alternativa: `TECHNICAL_ROBOT_WEBHOOK_URL` no `.env`; o valor da tela tem prioridade e vale na hora, sem reiniciar.) | `http://192.168.0.50:5000/webhook/decisao` |
| Porta/host do Cérebro | `.env`: `HOST=0.0.0.0`, `PORT=8000` | — |
| Tentativas de reenvio | `.env`: `WEBHOOK_MAX_RETRIES` (padrão 5), `WEBHOOK_TIMEOUT_SECONDS` (padrão 10) | — |

### No Robô Técnico
| O quê | Valor |
|---|---|
| URL base do Cérebro | `http://<IP-do-Cérebro>:8000` (no mesmo PC: `http://localhost:8000`) |
| Um endpoint HTTP **POST** que aceite JSON | O mesmo caminho configurado no Cérebro (ex.: `/webhook/decisao`) |
| Timeout ao chamar ① | 10 s é suficiente (resposta imediata) |
| Timeout ao chamar ③ | **90 s** (a IA escreve a reflexão antes de responder) |

> Se o Cérebro e o Robô estão em máquinas diferentes, libere a porta do Robô (ex.: 5000) e a porta 8000
> do Cérebro no firewall do Windows, só para a rede local.

---

## 3. ① Pedir análise de ativos — `POST /api/queue/demand`

O Robô pede ao Cérebro para (re)avaliar um ou mais ativos. A resposta é imediata. A decisão chega
depois, pelo webhook ②.

**Autenticação:** nenhuma (ver seção 7).
**Headers:** `Content-Type: application/json`

### Corpo enviado
| Campo | Tipo | Obrigatório | Descrição |
|---|---|---|---|
| `ativos` | lista de texto | **sim** (mín. 1) | Tickers da B3. Aceita `PETR4` ou `PETR4.SA`, maiúsculas ou minúsculas. |
| `prioridade` | texto | não (padrão `normal`) | `normal`, `alta` ou `urgente`. A fila processa `urgente` → `alta` → `normal`, 2 ativos por vez. |
| `origem` | texto | não (padrão `robo_tecnico`) | Identificação livre de quem pediu (aparece nos logs). |

```json
{
  "ativos": ["PETR4", "VALE3", "WEGE3"],
  "prioridade": "alta",
  "origem": "robo_tecnico"
}
```

### Resposta (HTTP 200)
```json
{
  "status": "queued",
  "message": "Ativos na Fila",
  "ativos": ["PETR4", "VALE3", "WEGE3"],
  "total_enfileirados": 3,
  "timestamp": "2026-11-23T14:47:56.317671"
}
```

| HTTP | Significado |
|---|---|
| 200 | Aceito e enfileirado |
| 422 | JSON inválido (ex.: `ativos` vazio ou ausente). O corpo traz o detalhe. |

**Regras:**
- Se o ativo já está na fila (aguardando ou em processamento), ele **não é duplicado**. Se o novo pedido for
  `alta` ou `urgente`, a prioridade é elevada.
- Os tickers voltam sem `.SA`.
- Cada ativo gera **um** webhook ② quando a análise termina.

---

## 4. ② Receber a decisão — webhook do Cérebro para o Robô

Quando termina a análise de cada ativo, o Cérebro faz:

```
POST <URL do robô>
Content-Type: application/json
```

Não há header de autenticação nem assinatura.

### Corpo recebido
| Campo | Tipo | Sempre presente | Descrição |
|---|---|---|---|
| `ativo` | texto | sim | Ticker sem `.SA` (ex.: `PETR4`) |
| `status` | texto | sim | **`autorizado`**, **`rejeitado`** ou **`pausa_analise`** (ver regras abaixo) |
| `conviccao` | número 0.00–1.00 | sim | Confiança da decisão. ≥ 0.70 é alta convicção. |
| `validade_dias` | inteiro ≥ 1 | sim | Por quantos dias a decisão vale |
| `tese_resumida` | texto | sim | Justificativa em português (até ~3 frases) |
| `indicadores_chave` | objeto | sim (pode vir `{}`) | Indicadores usados na decisão (tabela abaixo) |
| `data_autorizacao` | data-hora ISO | sim | Momento da decisão, **UTC sem fuso** (ex.: `2026-11-23T14:48:03.740668`) |
| `data_expiracao` | data-hora ISO | sim | `data_autorizacao + validade_dias`, **UTC sem fuso** |
| `timestamp` | data-hora ISO | sim | Geração da mensagem (UTC) |
| `id_analise` | inteiro | sim | ID único da análise. **Use para descartar duplicatas.** |
| `origem_decisao` | texto ou `null` | sim | Quem decidiu: `"IA: gemini/gemini-flash-lite-latest"`, `"Regra de contingência (sem IA)"` ou `"Trava de segurança"` |
| `fontes_consultadas` | texto ou `null` | sim | Fontes de dados que responderam (ex.: `"Fundamentus, Yahoo Finance, brapi.dev, CVM – Dados Abertos, ..."`) |

> **Datas:** vêm em **UTC sem indicação de fuso**. Para o horário de Brasília, subtraia 3 horas.
> Interprete sempre como UTC.

### Exemplo real (PETR4)
```json
{
  "ativo": "PETR4",
  "status": "autorizado",
  "conviccao": 0.88,
  "validade_dias": 10,
  "tese_resumida": "A PETR4 apresenta fundamentos sólidos, destacando-se por múltiplos atrativos como P/L de 3.92, ROIC robusto de 21.4% e um dividend yield expressivo de 8.5%. ...",
  "indicadores_chave": {
    "PL": 3.92, "PVP": 1.09, "DY_pct": 8.5, "ROE_pct": 27.8, "ROIC_pct": 21.4,
    "EV_EBITDA": 2.42, "Margem_Liquida_pct": 23.8, "Margem_Ebitda_pct": 45.7,
    "Margem_EBIT_pct": 44.5, "Margem_Bruta_pct": 52.1, "Div_Liq_Patrimonio": 0.59,
    "Liquidez_Corrente": 0.9, "Cresc_Receita_5a_pct": -1.8, "LPA": 10.89, "VPA": 39.16,
    "Valor_Mercado": 549962000000.0, "Volume_Medio_2m": 2611330000.0
  },
  "data_autorizacao": "2026-11-23T14:48:03.740668",
  "data_expiracao": "2026-12-03T14:48:03.740668",
  "timestamp": "2026-11-23T14:48:03.740668",
  "id_analise": 11,
  "origem_decisao": "IA: gemini/gemini-flash-lite-latest",
  "fontes_consultadas": "Fundamentus, Yahoo Finance, brapi.dev, CVM – Dados Abertos, Banco Central (SGS), Google News BR"
}
```

Exemplo de **pausa** (todas as fontes de dados falharam):
```json
{
  "ativo": "XPTO3",
  "status": "pausa_analise",
  "conviccao": 0.0,
  "validade_dias": 1,
  "tese_resumida": "Bloqueio temporário de segurança: dados insuficientes ou falha em todas as fontes da B3.",
  "indicadores_chave": {},
  "data_autorizacao": "2026-11-23T15:00:00.000000",
  "data_expiracao": "2026-11-24T15:00:00.000000",
  "timestamp": "2026-11-23T15:00:00.000000",
  "id_analise": 12,
  "origem_decisao": "Trava de segurança",
  "fontes_consultadas": "NENHUMA (Falha Total)"
}
```

### `indicadores_chave`: chaves possíveis
Nem todas vêm sempre: depende do que as fontes retornaram. Trate cada chave como opcional.

| Chave | Significado | Unidade |
|---|---|---|
| `PL` | Preço / Lucro | vezes |
| `PVP` | Preço / Valor Patrimonial | vezes |
| `DY_pct` | Dividend Yield 12m | % (8.5 = 8,5%) |
| `ROE_pct` / `ROIC_pct` | Retorno s/ patrimônio / capital investido | % |
| `Margem_Liquida_pct`, `Margem_Ebitda_pct`, `Margem_EBIT_pct`, `Margem_Bruta_pct` | Margens | % |
| `EV_EBITDA` | Valor da firma / EBITDA | vezes |
| `Div_Liq_Patrimonio` | Dívida líquida / Patrimônio | vezes (negativo = caixa líquido) |
| `Liquidez_Corrente` | Ativo circulante / Passivo circulante | vezes |
| `Cresc_Receita_5a_pct` | Crescimento da receita em 5 anos | % |
| `LPA` / `VPA` | Lucro / Valor patrimonial por ação | R$ |
| `Valor_Mercado` / `Volume_Medio_2m` | Valor de mercado / volume financeiro médio 2 meses | R$ |

### Como o Robô deve usar a decisão
| `status` | Ação recomendada |
|---|---|
| `autorizado` | Pode operar **compra** no ativo enquanto `agora (UTC) < data_expiracao`. Use `conviccao` para dimensionar a posição, se quiser. |
| `rejeitado` | **Não** abrir compra. |
| `pausa_analise` | **Não operar.** Dados indisponíveis. Peça nova análise depois (①). |
| decisão vencida (`agora ≥ data_expiracao`) | Tratar como sem autorização e pedir nova análise (①). |

Guarde a **última decisão por ativo**. Se `id_analise` maior chegar, ela substitui a anterior.

Se `origem_decisao` começar com `"Regra de contingência"`, a IA estava indisponível e quem decidiu foi uma
regra fixa de indicadores. O Robô pode querer ser mais conservador nesses casos.

### O que o Robô deve responder
- **HTTP 200, 201, 202 ou 204**, em até **10 segundos**. O corpo da resposta é ignorado.
- Qualquer outro código, erro de conexão ou demora acima de 10 s conta como **falha**.
- Responda rápido e processe depois, se necessário.

### Reenvio automático em caso de falha
| Tentativa | Quando |
|---|---|
| 1ª | Imediatamente após a análise |
| 2ª | +60 s |
| 3ª | +120 s |
| 4ª | +300 s (5 min) |
| 5ª | +900 s (15 min) |

Depois da 5ª falha a mensagem é marcada como `failed`. No Cockpit, em **Smart Queue & Webhooks →
Forçar Retentativas**, tudo o que não foi entregue (pendentes e falhas) é reenviado na hora, para a URL
configurada **no momento** (útil depois de religar o robô ou corrigir a URL). Três falhas definitivas
seguidas geram alerta de infraestrutura (log crítico e WhatsApp, se configurado).

> **Duplicatas são possíveis.** Se o Robô processar mas responder depois de 10 s, o Cérebro reenvia.
> Ignore mensagens cujo `id_analise` já foi processado.

---

## 5. ③ Informar o resultado do trade — `POST /api/memory/feedback`

Quando o Robô encerra uma operação, ele informa o resultado. A IA faz uma reflexão "pós-morte" cruzando o
resultado com a tese que autorizou o trade. A lição aprendida é usada nas próximas análises do mesmo ativo.

**Autenticação:** nenhuma (ver seção 7).
**Headers:** `Content-Type: application/json`
**Timeout recomendado:** 90 s (a resposta só volta depois que a IA escreve a reflexão).

### Corpo enviado
| Campo | Tipo | Obrigatório | Descrição |
|---|---|---|---|
| `ativo` | texto | **sim** | Ticker (`PETR4` ou `PETR4.SA`) |
| `resultado` | texto | **sim** | **`WIN`** (lucro) ou **`LOSS`** (prejuízo), em maiúsculas |
| `motivo_grafico` | texto | não | Por que o trade fechou, ex.: `"Alvo atingido"`, `"Stop por rompimento falso"` |
| `lucro_prejuizo_pct` | número | não | Resultado em %, ex.: `3.2` ou `-1.8` |
| `detalhes` | objeto | não | Qualquer dado extra (entrada, saída, setup...). É aceito mas hoje não é usado na reflexão. |

```json
{
  "ativo": "PETR4",
  "resultado": "LOSS",
  "motivo_grafico": "Stop acionado após rompimento falso da resistência",
  "lucro_prejuizo_pct": -1.8,
  "detalhes": {"entrada": 42.10, "saida": 41.34, "setup": "rompimento"}
}
```

### Resposta (HTTP 200)
```json
{
  "id": 7,
  "ticker": "PETR4.SA",
  "resultado": "LOSS",
  "motivo_grafico": "Stop acionado após rompimento falso da resistência",
  "lucro_prejuizo_pct": -1.8,
  "licao_aprendida": "Em PETR4, rompimentos sem confirmação de volume falharam; exigir ...",
  "tese_original": "A PETR4 apresenta fundamentos sólidos, ...",
  "created_at": "2026-11-23T16:10:00.123456"
}
```

| HTTP | Significado |
|---|---|
| 200 | Registrado. `licao_aprendida` traz a reflexão. |
| 422 | JSON inválido (falta `ativo` ou `resultado`) |

---

## 6. Opcional: consultar decisões sem webhook (polling)

Se o Robô não puder receber webhooks, ele pode **consultar** as decisões. Essas rotas exigem login.

1. Obter token (vale 24 h):
   ```
   POST /api/auth/login
   Content-Type: application/json
   {"username": "admin", "password": "<senha>"}
   ```
   Resposta: `{"access_token": "eyJ...", "token_type": "bearer", "expires_in_minutes": 1440}`

2. Usar o header `Authorization: Bearer <access_token>` em:
   | Rota | Retorna |
   |---|---|
   | `GET /api/assets` | Todos os ativos com `status_analise`, `conviccao`, `data_expiracao`, `dias_restantes`, `tese_resumida`, `indicadores`, `decisao_origem` |
   | `GET /api/queue/status` | `{"aguardando", "em_processamento", "concluidos_hoje", "total_ativo"}` |
   | `GET /api/queue/items?limit=50` | Itens da fila com status (`queued`, `processing`, `completed`, `failed`) |
   | `GET /api/assets/{ticker}/sources` | Detalhe das últimas análises: fontes, triagem e quem decidiu |

   Se a resposta for HTTP 401, o token venceu: faça login de novo.

---

## 7. Segurança: leia antes de expor na rede

- `POST /api/queue/demand` e `POST /api/memory/feedback` **não exigem autenticação**. Qualquer máquina que
  alcance a porta 8000 consegue enfileirar análises (gastando cota de IA) ou gravar resultados falsos.
- O webhook ② **não tem assinatura**. O Robô não tem como provar que a mensagem veio do Cérebro.
- **Recomendação:** mantenha os dois programas na **mesma máquina ou rede local**, com a porta 8000 e a do
  Robô **fechadas para a internet** no firewall. Troque a senha padrão `admin123` (`ADMIN_PASSWORD` no `.env`).
- Se precisar expor fora da rede local, peça a inclusão de uma chave compartilhada: header `X-API-Key` nas
  entradas e assinatura HMAC no webhook. Hoje isso não existe.

---

## 8. Roteiro de teste (copiar e colar)

Comandos para PowerShell (`curl.exe`), com o Cérebro em `localhost:8000`.

**a) Robô de teste para receber o webhook**: salve como `robo_teste.py` e rode
`pip install fastapi uvicorn` e depois `python robo_teste.py`:
```python
from fastapi import FastAPI, Request
import uvicorn

app = FastAPI()
vistos = set()

@app.post("/webhook/decisao")
async def decisao(req: Request):
    d = await req.json()
    if d["id_analise"] in vistos:          # descarta reenvio duplicado
        return {"ok": True, "duplicado": True}
    vistos.add(d["id_analise"])
    print(f'{d["ativo"]}: {d["status"]} ({d["conviccao"]:.0%}) até {d["data_expiracao"]} UTC '
          f'| {d["origem_decisao"]}')
    return {"ok": True}                    # responder 2xx rápido

uvicorn.run(app, host="0.0.0.0", port=5000)
```
No Cockpit, em **Watchdog & Ajustes → URL de Callback do Robô Técnico (Webhook)**, coloque
`http://localhost:5000/webhook/decisao` e salve.

**b) Pedir análise:**
```powershell
curl.exe -X POST http://localhost:8000/api/queue/demand -H "Content-Type: application/json" -d '{\"ativos\":[\"PETR4\"],\"prioridade\":\"alta\"}'
```
Em alguns segundos o `robo_teste.py` imprime a decisão. Também dá para ver em **Smart Queue & Webhooks**
(status `delivered`).

**c) Informar resultado:**
```powershell
curl.exe -X POST http://localhost:8000/api/memory/feedback -H "Content-Type: application/json" -d '{\"ativo\":\"PETR4\",\"resultado\":\"WIN\",\"motivo_grafico\":\"Alvo atingido\",\"lucro_prejuizo_pct\":3.2}'
```
A lição aparece em **Memória Pós-Morte**.

---

## 9. Resumo para o desenvolvedor do Robô

| O Robô **envia** | Quando | Para |
|---|---|---|
| `{"ativos": [...], "prioridade": "...", "origem": "..."}` | Quer autorização para operar ativos | `POST /api/queue/demand` |
| `{"ativo", "resultado": "WIN"/"LOSS", "motivo_grafico", "lucro_prejuizo_pct", "detalhes"}` | Encerrou um trade | `POST /api/memory/feedback` |

| O Robô **recebe** | Quando | Deve |
|---|---|---|
| Decisão `{ativo, status, conviccao, validade_dias, data_expiracao, id_analise, ...}` | Fim de cada análise | Responder 2xx em até 10 s; deduplicar por `id_analise`; operar só se `autorizado` e não vencido |
