// Trading System B3 — instalador gráfico (Setup.exe).
// Compilado com o csc.exe do .NET Framework 4.x que acompanha o Windows (ver build_installer.ps1).
// O EXE embute deploy\install.ps1 e deploy\uninstall.ps1; ao clicar em Instalar ele extrai o script para
// %TEMP% e o executa, mostrando o progresso. Senhas vão por variável de ambiente do processo filho,
// nunca pela linha de comando (que fica visível para outros processos).
// Linguagem: C# 5 (o compilador do Framework não aceita sintaxe mais nova).
using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Text;
using System.Text.RegularExpressions;
using System.Windows.Forms;

[assembly: AssemblyTitle("Trading System B3 - Instalador")]
[assembly: AssemblyProduct("Trading System B3")]
[assembly: AssemblyVersion("0.2.0.0")]
[assembly: AssemblyFileVersion("0.2.0.0")]

namespace TradingSystemSetup
{
    public class SetupForm : Form
    {
        TextBox txtRepo, txtRoot, txtBranch, txtLogin, txtServer, txtMt5Pwd, txtChat, txtTgToken, txtLog;
        ComboBox cmbProvider;
        NumericUpDown numUpdate;
        CheckBox chkTasks, chkTests, chkRemoveFiles;
        Button btnInstall, btnUninstall, btnBrowse, btnClose;
        ProgressBar progress;
        Label lblStatus;
        Process proc;
        StringBuilder fullLog = new StringBuilder();
        static readonly Regex Ansi = new Regex(@"\x1B\[[0-9;?]*[A-Za-z]");
        static readonly string AppDir = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "TradingSystemB3");

        public SetupForm()
        {
            SuspendLayout();
            // Coordenadas abaixo são em pixels a 96 DPI; o WinForms escala tudo para a DPI real da tela.
            AutoScaleDimensions = new SizeF(96F, 96F);
            AutoScaleMode = AutoScaleMode.Dpi;
            Text = "Trading System B3 — Instalador";
            Font = new Font("Segoe UI", 9F);
            ClientSize = new Size(720, 700);
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false;
            StartPosition = FormStartPosition.CenterScreen;

            var title = new Label { Text = "Trading System B3 — Swing Trade D1", Font = new Font("Segoe UI", 13F, FontStyle.Bold),
                                    Location = new Point(16, 12), AutoSize = true };
            var sub = new Label { Text = "Instala Git/Python, clona o repositório, roda os testes e agenda as rotinas e o auto-update.",
                                  Location = new Point(18, 44), AutoSize = true };
            Controls.Add(title); Controls.Add(sub);

            int y = 84;
            txtRepo = AddText("Repositório GitHub *", "https://github.com/SEU_USUARIO/SEU_REPO.git", ref y, 554);
            txtRoot = AddText("Pasta de instalação", @"C:\TradingSystem", ref y, 480);
            btnBrowse = new Button { Text = "Procurar...", Location = new Point(632, y - 30), Size = new Size(72, 25) };
            btnBrowse.Click += delegate {
                using (var d = new FolderBrowserDialog()) { if (d.ShowDialog(this) == DialogResult.OK) txtRoot.Text = d.SelectedPath; }
            };
            Controls.Add(btnBrowse);
            txtBranch = AddText("Branch", "main", ref y, 160);

            AddLabel("Fonte de dados", y);
            cmbProvider = new ComboBox { Location = new Point(150, y), Size = new Size(380, 24), DropDownStyle = ComboBoxStyle.DropDownList };
            cmbProvider.Items.AddRange(new object[] { "yahoo  — grátis, para testar sem corretora", "mt5  — terminal MetaTrader 5 da corretora" });
            cmbProvider.SelectedIndex = 0;
            cmbProvider.SelectedIndexChanged += delegate { UpdateMt5Fields(); };
            Controls.Add(cmbProvider);
            y += 32;

            txtLogin = AddText("Login MT5", "", ref y, 160);
            txtServer = AddText("Servidor MT5", "XP-PRD", ref y, 160);
            txtMt5Pwd = AddText("Senha MT5", "", ref y, 160);
            txtMt5Pwd.UseSystemPasswordChar = true;
            txtChat = AddText("Telegram chat_id", "", ref y, 160);
            txtTgToken = AddText("Token do bot", "", ref y, 300);
            txtTgToken.UseSystemPasswordChar = true;

            AddLabel("Auto-update (min)", y);
            numUpdate = new NumericUpDown { Location = new Point(150, y), Size = new Size(70, 24), Minimum = 1, Maximum = 120, Value = 5 };
            Controls.Add(numUpdate);
            chkTasks = new CheckBox { Text = "Registrar tarefas do Agendador", Location = new Point(240, y), AutoSize = true, Checked = true };
            chkTests = new CheckBox { Text = "Rodar testes", Location = new Point(460, y), AutoSize = true, Checked = true };
            Controls.Add(chkTasks); Controls.Add(chkTests);
            y += 34;

            btnInstall = new Button { Text = "Instalar / Atualizar", Location = new Point(16, y), Size = new Size(160, 32), Font = new Font("Segoe UI", 9.5F, FontStyle.Bold) };
            btnInstall.Click += delegate { StartInstall(); };
            btnUninstall = new Button { Text = "Desinstalar", Location = new Point(186, y), Size = new Size(110, 32) };
            btnUninstall.Click += delegate { StartUninstall(); };
            chkRemoveFiles = new CheckBox { Text = "apagar também a pasta e os dados", Location = new Point(304, y + 7), AutoSize = true };
            btnClose = new Button { Text = "Fechar", Location = new Point(624, y), Size = new Size(80, 32) };
            btnClose.Click += delegate { Close(); };
            Controls.Add(btnInstall); Controls.Add(btnUninstall); Controls.Add(chkRemoveFiles); Controls.Add(btnClose);
            y += 40;

            progress = new ProgressBar { Location = new Point(16, y), Size = new Size(688, 14), Style = ProgressBarStyle.Marquee, Visible = false };
            Controls.Add(progress);
            lblStatus = new Label { Location = new Point(16, y + 16), AutoSize = true, Text = "Pronto." };
            Controls.Add(lblStatus);
            y += 38;

            txtLog = new TextBox { Location = new Point(16, y), Size = new Size(688, ClientSize.Height - y - 12), Multiline = true,
                                   ScrollBars = ScrollBars.Vertical, ReadOnly = true, BackColor = Color.FromArgb(20, 20, 24),
                                   ForeColor = Color.Gainsboro, Font = new Font("Consolas", 8.5F), WordWrap = true };
            Controls.Add(txtLog);

            LoadPrefs();
            UpdateMt5Fields();
            ResumeLayout(false);
            PerformLayout();
            FormClosing += delegate(object s, FormClosingEventArgs e) {
                if (proc != null && !proc.HasExited &&
                    MessageBox.Show(this, "A instalação ainda está rodando. Fechar mesmo assim?", Text, MessageBoxButtons.YesNo, MessageBoxIcon.Warning) != DialogResult.Yes)
                    e.Cancel = true;
            };
        }

        TextBox AddText(string label, string value, ref int y, int width)
        {
            AddLabel(label, y);
            var t = new TextBox { Location = new Point(150, y), Size = new Size(width, 24), Text = value };
            Controls.Add(t);
            y += 30;
            return t;
        }

        void AddLabel(string text, int y)
        {
            Controls.Add(new Label { Text = text, Location = new Point(16, y + 3), AutoSize = true });
        }

        bool IsMt5 { get { return cmbProvider.SelectedIndex == 1; } }

        void UpdateMt5Fields()
        {
            txtLogin.Enabled = txtServer.Enabled = txtMt5Pwd.Enabled = IsMt5;
        }

        // ------------------------------------------------------------------ prefs (sem segredos)
        string PrefsPath { get { return Path.Combine(AppDir, "setup.ini"); } }

        void LoadPrefs()
        {
            try
            {
                if (!File.Exists(PrefsPath)) return;
                foreach (var line in File.ReadAllLines(PrefsPath))
                {
                    int i = line.IndexOf('=');
                    if (i < 0) continue;
                    string k = line.Substring(0, i), v = line.Substring(i + 1);
                    if (k == "repo") txtRepo.Text = v;
                    else if (k == "root") txtRoot.Text = v;
                    else if (k == "branch") txtBranch.Text = v;
                    else if (k == "provider") cmbProvider.SelectedIndex = v == "mt5" ? 1 : 0;
                    else if (k == "login") txtLogin.Text = v;
                    else if (k == "server") txtServer.Text = v;
                    else if (k == "chat") txtChat.Text = v;
                    else if (k == "update") { decimal d; if (decimal.TryParse(v, out d) && d >= 1 && d <= 120) numUpdate.Value = d; }
                }
            }
            catch { }
        }

        void SavePrefs()
        {
            try
            {
                Directory.CreateDirectory(AppDir);
                File.WriteAllLines(PrefsPath, new[] {
                    "repo=" + txtRepo.Text.Trim(), "root=" + txtRoot.Text.Trim(), "branch=" + txtBranch.Text.Trim(),
                    "provider=" + (IsMt5 ? "mt5" : "yahoo"), "login=" + txtLogin.Text.Trim(), "server=" + txtServer.Text.Trim(),
                    "chat=" + txtChat.Text.Trim(), "update=" + numUpdate.Value });
            }
            catch { }
        }

        // ------------------------------------------------------------------ run scripts
        static string Q(string s) { return "\"" + s.Replace("\"", "\\\"") + "\""; }

        string ExtractScript(string resourceName)
        {
            string dir = Path.Combine(Path.GetTempPath(), "TradingSystemB3Setup");
            Directory.CreateDirectory(dir);
            string path = Path.Combine(dir, resourceName);
            using (var s = Assembly.GetExecutingAssembly().GetManifestResourceStream(resourceName))
            {
                if (s == null) throw new InvalidOperationException("Recurso não encontrado no EXE: " + resourceName);
                using (var f = File.Create(path)) s.CopyTo(f);
            }
            return path;
        }

        bool ValidateInputs()
        {
            string repo = txtRepo.Text.Trim();
            if (repo.Length == 0 || repo.Contains("SEU_USUARIO") ||
                !(repo.StartsWith("https://") || repo.StartsWith("git@") || Directory.Exists(repo)))
            {
                MessageBox.Show(this, "Informe a URL do repositório no GitHub (ex.: https://github.com/usuario/repo.git).", Text,
                                MessageBoxButtons.OK, MessageBoxIcon.Warning);
                txtRepo.Focus();
                return false;
            }
            string root = txtRoot.Text.Trim();
            if (root.Length == 0 || Regex.IsMatch(root, "OneDrive|Dropbox|Google Drive", RegexOptions.IgnoreCase))
            {
                MessageBox.Show(this, "Escolha uma pasta fora de OneDrive/Dropbox (a sincronização trava arquivos abertos).", Text,
                                MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return false;
            }
            int login;
            if (IsMt5 && txtLogin.Text.Trim().Length > 0 && !int.TryParse(txtLogin.Text.Trim(), out login))
            {
                MessageBox.Show(this, "Login do MT5 deve ser numérico.", Text, MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return false;
            }
            return true;
        }

        void StartInstall()
        {
            if (!ValidateInputs()) return;
            SavePrefs();
            var args = new StringBuilder();
            args.Append("-RepoUrl ").Append(Q(txtRepo.Text.Trim()));
            args.Append(" -Root ").Append(Q(txtRoot.Text.Trim()));
            args.Append(" -Branch ").Append(Q(txtBranch.Text.Trim().Length > 0 ? txtBranch.Text.Trim() : "main"));
            args.Append(" -Provider ").Append(IsMt5 ? "mt5" : "yahoo");
            args.Append(" -UpdateEveryMinutes ").Append(numUpdate.Value);
            if (IsMt5 && txtLogin.Text.Trim().Length > 0) args.Append(" -Mt5Login ").Append(txtLogin.Text.Trim());
            if (IsMt5 && txtServer.Text.Trim().Length > 0) args.Append(" -Mt5Server ").Append(Q(txtServer.Text.Trim()));
            if (txtChat.Text.Trim().Length > 0) args.Append(" -TelegramChatId ").Append(Q(txtChat.Text.Trim()));
            if (!chkTasks.Checked) args.Append(" -NoTasks");
            if (!chkTests.Checked) args.Append(" -SkipTests");

            var secrets = new System.Collections.Generic.Dictionary<string, string>();
            if (IsMt5 && txtMt5Pwd.Text.Length > 0) secrets["TS_MT5_PASSWORD"] = txtMt5Pwd.Text;
            if (txtTgToken.Text.Length > 0) secrets["TS_TELEGRAM_TOKEN"] = txtTgToken.Text;
            RunScript("install.ps1", args.ToString(), secrets, "Instalação");
        }

        void StartUninstall()
        {
            string msg = chkRemoveFiles.Checked
                ? "Remover as tarefas e APAGAR a pasta " + txtRoot.Text + " (banco do paper, dados e logs)?"
                : "Remover as tarefas do Agendador (TS_*)? A pasta e os dados são mantidos.";
            if (MessageBox.Show(this, msg, Text, MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes) return;
            string args = "-Root " + Q(txtRoot.Text.Trim()) + (chkRemoveFiles.Checked ? " -RemoveFiles" : "");
            RunScript("uninstall.ps1", args, null, "Desinstalação");
        }

        void RunScript(string resource, string scriptArgs, System.Collections.Generic.Dictionary<string, string> env, string what)
        {
            string script;
            try { script = ExtractScript(resource); }
            catch (Exception ex) { MessageBox.Show(this, ex.Message, Text, MessageBoxButtons.OK, MessageBoxIcon.Error); return; }

            var psi = new ProcessStartInfo
            {
                FileName = "powershell.exe",
                Arguments = "-NoProfile -ExecutionPolicy Bypass -File " + Q(script) + " " + scriptArgs,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                RedirectStandardInput = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8,
            };
            psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
            psi.EnvironmentVariables["NO_COLOR"] = "1";
            if (env != null) foreach (var kv in env) psi.EnvironmentVariables[kv.Key] = kv.Value;

            txtLog.Clear();
            fullLog.Clear();
            Append(what + " iniciada: " + DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss"));
            SetBusy(true, what + " em andamento... (a primeira vez pode levar alguns minutos)");

            proc = new Process { StartInfo = psi, EnableRaisingEvents = true };
            proc.OutputDataReceived += delegate(object s, DataReceivedEventArgs e) { if (e.Data != null) Append(e.Data); };
            proc.ErrorDataReceived += delegate(object s, DataReceivedEventArgs e) { if (e.Data != null) Append(e.Data); };
            proc.Exited += delegate
            {
                int code = proc.ExitCode;
                BeginInvoke((MethodInvoker)delegate
                {
                    SetBusy(false, code == 0 ? what + " concluída com sucesso." : what + " falhou (código " + code + "). Veja o log abaixo.");
                    Append("");
                    Append(code == 0 ? "==> SUCESSO" : "==> FALHOU (código " + code + ")");
                    string logPath = SaveLog(what);
                    if (code == 0)
                        MessageBox.Show(this, what + " concluída.\n\nLog: " + logPath, Text, MessageBoxButtons.OK, MessageBoxIcon.Information);
                    else
                        MessageBox.Show(this, what + " falhou. O log completo foi salvo em:\n" + logPath, Text, MessageBoxButtons.OK, MessageBoxIcon.Error);
                });
            };
            try
            {
                proc.Start();
                proc.StandardInput.Close();  // nada interativo: segredos vieram por variável de ambiente
                proc.BeginOutputReadLine();
                proc.BeginErrorReadLine();
            }
            catch (Exception ex)
            {
                SetBusy(false, "Não foi possível iniciar o PowerShell.");
                MessageBox.Show(this, ex.Message, Text, MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
        }

        string SaveLog(string what)
        {
            try
            {
                Directory.CreateDirectory(AppDir);
                string p = Path.Combine(AppDir, (what.StartsWith("Instal") ? "install" : "uninstall") + "-" + DateTime.Now.ToString("yyyyMMdd-HHmmss") + ".log");
                File.WriteAllText(p, fullLog.ToString(), new UTF8Encoding(false));
                return p;
            }
            catch { return "(não foi possível salvar o log)"; }
        }

        void Append(string line)
        {
            line = Ansi.Replace(line, "");
            lock (fullLog) fullLog.AppendLine(line);
            if (InvokeRequired) { BeginInvoke((MethodInvoker)delegate { AppendUi(line); }); return; }
            AppendUi(line);
        }

        void AppendUi(string line)
        {
            txtLog.AppendText(line + Environment.NewLine);
            if (line.StartsWith("==>")) lblStatus.Text = line.Substring(3).Trim();
        }

        void SetBusy(bool busy, string status)
        {
            btnInstall.Enabled = btnUninstall.Enabled = !busy;
            progress.Visible = busy;
            lblStatus.Text = status;
            UseWaitCursor = busy;
        }

        [STAThread]
        static void Main()
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new SetupForm());
        }
    }
}
