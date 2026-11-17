# Trading System B3 — Swing Trade D1

Implementação do *PRD — Sistema de Swing Trade Automatizado B3*. Decisão com a barra D1 fechada,
execução no leilão de abertura, mesmo código em backtest, paper e real.

## Instalação no Windows

**Para instalar em qualquer Windows 10/11 64-bit:** envie só o arquivo `TradingSystemB3-Setup.exe`. A pessoa abre e
clica em **Instalar / Atualizar**: a URL do repositório (público) já vem embutida, Git e Python são instalados se
faltarem, as cotações são carregadas e um atalho **Trading System B3** é criado na Área de Trabalho e no Menu Iniciar.
O atalho abre o painel; na barra lateral, **Atualizações** mostra a versão instalada e o botão **Atualizar agora**
(também `python -m ts version` / `python -m ts update`). A atualização roda os testes e volta sozinha à versão anterior
se algo falhar.

**Instalador gráfico:** execute `TradingSystemB3-Setup.exe` (baixe em GitHub > Actions > Artifacts, ou gere
localmente com `powershell -ExecutionPolicy Bypass -File deploy\installer\build_installer.ps1`). Informe a URL do
repositório, escolha a fonte de dados e clique em **Instalar / Atualizar**. Não precisa de administrador. O EXE não
é assinado digitalmente: na primeira execução o SmartScreen pode avisar ("Mais informações" > "Executar assim mesmo").

Pré-requisito: o código num repositório GitHub (pode ser privado). Alternativa por linha de comando:

```powershell
# opção 1: duplo clique em deploy\install.cmd (pede a URL do repositório)
# opção 2: PowerShell
powershell -ExecutionPolicy Bypass -File deploy\install.ps1 -RepoUrl https://github.com/SEU_USUARIO/SEU_REPO.git -Provider yahoo
```

O instalador:

1. instala Git e Python 3.12 64-bit se faltarem;
2. clona em `C:\TradingSystem`;
3. cria o `.venv` e roda os testes;
4. gera `config\settings.local.yaml` (fora do git);
5. registra as tarefas do Agendador, inclusive a **TS_Update**.

Parâmetros úteis: `-Provider mt5 -Mt5Login 123456 -Mt5Server XP-PRD` (a senha vai para o Gerenciador de
Credenciais), `-TelegramChatId 123` (pede o token do bot), `-UpdateEveryMinutes 5`, `-NoTasks`.

## Atualização automática

A tarefa `TS_Update` roda `deploy\scripts\update.ps1` a cada N minutos e no logon:

- `git fetch`; se houver commit novo em `main`, aplica (`git reset --hard`);
- reinstala dependências só se `pyproject.toml`/`requirements.lock` mudaram;
- roda a suíte de testes; **se falhar, volta ao commit anterior**, avisa no Telegram e não tenta de novo o
  mesmo commit;
- não atualiza nas janelas críticas (08:30–09:15 e 19:00–21:00 em dias úteis) e usa o mesmo lock das
  rotinas, então nunca troca o código no meio de um EOD;
- avisa se `config/strategy.yaml` mudou (PRD 1.4.4: mudança de parâmetro reinicia a contagem do paper).

Ajustes da máquina vão em `config\settings.local.yaml`, nunca nos arquivos versionados — o update recusa
atualizar se encontrar arquivo versionado modificado localmente.

## Dados

| `data.provider` | Uso | Observações |
| --- | --- | --- |
| `yahoo` | Testes e paper sem corretora | Grátis, D1 completo desde 2019, preços já ajustados por desdobramento, dividendos via `python -m ts corp-actions`. Endpoint não oficial. |
| `mt5` | Produção (XP) | Obrigatório para `execution.mode: live`. |

## Comandos

```text
python -m ts doctor            # diagnóstico
python -m ts sync --full       # primeira carga desde 2019
python -m ts eod               # rotina de fim de dia (paper)
python -m ts backtest --g0     # walk-forward + PBO + DSR + platô + regimes
python -m ts corp-actions      # dividendos do Yahoo -> config/corporate_actions.csv
python -m ts weekly-report
streamlit run src/ts/app/dashboard.py
```
