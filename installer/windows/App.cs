using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows;
using System.Windows.Threading;
using Microsoft.Win32;

namespace RemCard.Setup
{
    internal static class Program
    {
        private const string RegistryPath = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\RemCard";

        [STAThread]
        public static int Main(string[] args)
        {
            // The accepted Qt payload contains deep resource paths. These process-local
            // .NET switches avoid the legacy 260-character normalization limit without
            // changing Windows registry or system policy.
            AppContext.SetSwitch("Switch.System.IO.UseLegacyPathHandling", false);
            AppContext.SetSwitch("Switch.System.IO.BlockLongPaths", false);
            Dictionary<string, string> options = Parse(args);
            string reportPath = Get(options, "report", null);
            bool testInstall = options.ContainsKey("install-test"), testUninstall = options.ContainsKey("uninstall-test");
            bool uninstall = options.ContainsKey("uninstall") || testUninstall || Path.GetFileName(Assembly.GetExecutingAssembly().Location).StartsWith("RemCardUninstall", StringComparison.OrdinalIgnoreCase);
            InstallerContext context = null;
            try
            {
                InstallerText.SetLanguage(Get(options, "language", "ru"));
                if (options.ContainsKey("ui-smoke") && Get(options, "preview", "") == "uninstall") uninstall = true;
                string executable = Assembly.GetExecutingAssembly().Location;
                string version = Assembly.GetExecutingAssembly().GetName().Version.ToString(3);
                string target = Get(options, "target", null);
                if (uninstall && !testUninstall && String.IsNullOrWhiteSpace(target)) target = Path.GetDirectoryName(executable);
                int parentPid;
                if (uninstall && Int32.TryParse(Get(options, "wait-parent", "0"), out parentPid) && parentPid > 0)
                {
                    try { using (Process parent = Process.GetProcessById(parentPid)) if (!parent.WaitForExit(15000)) throw new IOException("Предыдущий процесс удаления ещё завершает работу. Повторите запуск."); }
                    catch (ArgumentException) { }
                }
                if (uninstall && !testUninstall && !options.ContainsKey("from-temp") && !String.IsNullOrEmpty(target) && SameDirectory(Path.GetDirectoryName(executable), target))
                    return RelaunchUninstaller(executable, target, InstallerText.Language);
                context = new InstallerContext(); context.Version = version; context.UninstallMode = uninstall;
                context.Language = InstallerText.Language;
                context.SuppressLaunch = options.ContainsKey("ui-smoke") || testInstall || testUninstall;
                context.NoRegistration = options.ContainsKey("ui-smoke");
                context.AutomationTargetDirectory = options.ContainsKey("ui-smoke") ? target : null;
                context.AutomationPreview = options.ContainsKey("ui-smoke") ? Get(options, "preview", null) : null;
                int uiFailureAfter;
                if (options.ContainsKey("ui-smoke") && Int32.TryParse(Get(options, "fail-after", "-1"), out uiFailureAfter)) context.SimulateFailureAfterFiles = uiFailureAfter;
                context.UninstallerBytes = InstallerResources.ReadBytes("RemCard.uninstaller.exe");
                context.ExistingInstallDirectory = uninstall ? target : ReadInstallDirectory();
                if (!uninstall)
                {
                    context.Payload = PayloadArchive.Open(executable); context.Version = context.Payload.Version; context.SourceCommit = context.Payload.SourceCommit;
                }
                List<string> operations = new List<string>();
                Action<InstallProgress> record = delegate(InstallProgress progress) { operations.Add(progress.Percent + "% " + progress.Phase + ": " + progress.Message); };
                Stopwatch elapsed = Stopwatch.StartNew();
                if (options.ContainsKey("verify-payload"))
                {
                    context.Payload.VerifyAll(record, CancellationToken.None);
                    WriteReport(reportPath, new { ok = true, mode = "verify", version = context.Version, source_commit = context.SourceCommit, files = context.Payload.Files.Count, unpacked_bytes = context.Payload.TotalUnpackedBytes, package_sha256 = context.Payload.PackageSha256, elapsed_ms = elapsed.ElapsedMilliseconds });
                    return 0;
                }
                if (testInstall)
                {
                    if (String.IsNullOrWhiteSpace(target)) throw new ArgumentException("Тестовая установка требует явный --target.");
                    int failAfter; if (!Int32.TryParse(Get(options, "fail-after", "-1"), out failAfter)) failAfter = -1;
                    InstallResult result = InstallerEngine.Install(context, new InstallOptions { TargetDirectory = target, NoRegistration = true, DesktopShortcut = false, StartMenuShortcut = false, SimulateFailureAfterFiles = failAfter }, record, CancellationToken.None);
                    WriteReport(reportPath, new { ok = true, mode = "install-test", version = context.Version, target = result.InstallDirectory, executable = result.ExecutablePath, elapsed_ms = elapsed.ElapsedMilliseconds, operations = operations });
                    return 0;
                }
                if (testUninstall)
                {
                    if (String.IsNullOrWhiteSpace(target)) throw new ArgumentException("Тестовое удаление требует явный --target.");
                    InstallerEngine.Uninstall(context, target, record, true, CancellationToken.None);
                    WriteReport(reportPath, new { ok = true, mode = "uninstall-test", target = target, elapsed_ms = elapsed.ElapsedMilliseconds, operations = operations });
                    return 0;
                }
                Application application = new Application(); application.ShutdownMode = ShutdownMode.OnMainWindowClose;
                application.DispatcherUnhandledException += delegate(object sender, DispatcherUnhandledExceptionEventArgs eventArgs)
                {
                    eventArgs.Handled = true; ShowFatal(application, eventArgs.Exception);
                };
                InstallerWindow window = new InstallerWindow(context);
                application.MainWindow = window;
                if (options.ContainsKey("ui-smoke") && options.ContainsKey("language-state-smoke"))
                {
                    window.Loaded += delegate
                    {
                        window.Dispatcher.BeginInvoke(new Action(delegate
                        {
                            try { WriteReport(reportPath, window.RunLanguageStateSmoke()); }
                            catch (Exception error) { WriteReport(reportPath, new { ok = false, mode = "ui-language-state", message = error.ToString() }); }
                            finally { window.CloseForAutomation(); }
                        }), DispatcherPriority.ApplicationIdle);
                    };
                }
                string renderPreview = options.ContainsKey("ui-smoke") ? Get(options, "render-preview", null) : null;
                if (!String.IsNullOrWhiteSpace(renderPreview))
                {
                    window.Loaded += delegate
                    {
                        window.Dispatcher.BeginInvoke(new Action(delegate
                        {
                            try
                            {
                                window.UpdateLayout();
                                FrameworkElement content = (FrameworkElement)window.Content;
                                int width = Math.Max(1, (int)Math.Ceiling(content.ActualWidth));
                                int height = Math.Max(1, (int)Math.Ceiling(content.ActualHeight));
                                System.Windows.Media.Imaging.RenderTargetBitmap bitmap = new System.Windows.Media.Imaging.RenderTargetBitmap(width, height, 96, 96, System.Windows.Media.PixelFormats.Pbgra32);
                                bitmap.Render(content);
                                System.Windows.Media.Imaging.PngBitmapEncoder encoder = new System.Windows.Media.Imaging.PngBitmapEncoder();
                                encoder.Frames.Add(System.Windows.Media.Imaging.BitmapFrame.Create(bitmap));
                                string destination = Path.GetFullPath(renderPreview); Directory.CreateDirectory(Path.GetDirectoryName(destination));
                                using (FileStream image = File.Create(destination)) encoder.Save(image);
                                WriteReport(reportPath, new { ok = true, mode = "ui-render", preview = context.AutomationPreview ?? "welcome", language = context.Language, width = width, height = height, image = destination, layout_issues = window.FindLayoutIssues() });
                            }
                            catch (Exception error) { WriteReport(reportPath, new { ok = false, mode = "ui-render", message = error.ToString() }); }
                            finally { window.CloseForAutomation(); }
                        }), DispatcherPriority.ApplicationIdle);
                    };
                }
                int autoClose;
                if (Int32.TryParse(Get(options, "auto-close-ms", "0"), out autoClose) && autoClose > 0)
                {
                    DispatcherTimer timer = new DispatcherTimer(); timer.Interval = TimeSpan.FromMilliseconds(autoClose);
                    timer.Tick += delegate { timer.Stop(); window.CloseForAutomation(); }; timer.Start();
                }
                return application.Run(window);
            }
            catch (Exception error)
            {
                InstallFailureException failure = error as InstallFailureException;
                WriteReport(reportPath, new { ok = false, code = failure == null ? "RC-SETUP-UNEXPECTED" : failure.Code, message = error.Message, detail = error.ToString() });
                if (!testInstall && !testUninstall && !options.ContainsKey("verify-payload"))
                {
                    Application app = Application.Current ?? new Application(); ShowFatal(app, error);
                    if (app.MainWindow != null) return app.Run();
                }
                return 1;
            }
            finally { if (context != null) context.Dispose(); }
        }

        private static int RelaunchUninstaller(string executable, string target, string language)
        {
            string directory = Path.Combine(Path.GetTempPath(), "RemCard-uninstall-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(directory); string helper = Path.Combine(directory, "RemCardUninstall.exe");
            File.Copy(executable, helper);
            Process.Start(new ProcessStartInfo(helper, "--uninstall --from-temp --language " + language + " --wait-parent " + Process.GetCurrentProcess().Id + " --target " + Quote(target)) { UseShellExecute = false, WorkingDirectory = directory });
            return 0;
        }

        private static bool SameDirectory(string first, string second)
        { return String.Equals(Path.GetFullPath(first).TrimEnd('\\'), Path.GetFullPath(second).TrimEnd('\\'), StringComparison.OrdinalIgnoreCase); }
        private static string ReadInstallDirectory()
        { using (RegistryKey key = Registry.CurrentUser.OpenSubKey(RegistryPath)) return key == null ? null : key.GetValue("InstallLocation") as string; }
        private static Dictionary<string, string> Parse(string[] args)
        {
            Dictionary<string, string> result = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            for (int index = 0; index < args.Length; index++)
            {
                if (!args[index].StartsWith("--")) continue;
                string key = args[index].Substring(2); string value = "true";
                if (index + 1 < args.Length && !args[index + 1].StartsWith("--")) value = args[++index];
                result[key] = value;
            }
            return result;
        }
        private static string Get(Dictionary<string, string> options, string key, string fallback)
        { string value; return options.TryGetValue(key, out value) ? value : fallback; }
        private static string Quote(string value) { return "\"" + value.Replace("\"", "") + "\""; }
        private static void WriteReport(string path, object value)
        {
            if (String.IsNullOrEmpty(path)) return;
            string absolute = Path.GetFullPath(path); Directory.CreateDirectory(Path.GetDirectoryName(absolute));
            File.WriteAllText(absolute, new JavaScriptSerializer().Serialize(value), new UTF8Encoding(false));
        }

        private static void ShowFatal(Application application, Exception error)
        {
            // This window is also branded: startup errors never use MessageBox.
            System.Windows.Media.SolidColorBrush background = new System.Windows.Media.SolidColorBrush(System.Windows.Media.Color.FromRgb(12, 24, 36));
            System.Windows.Controls.StackPanel panel = new System.Windows.Controls.StackPanel { Margin = new Thickness(30) };
            panel.Children.Add(new System.Windows.Controls.Image { Source = InstallerResources.Image("RemCard.logo.png"), Width = 42, Height = 42, HorizontalAlignment = HorizontalAlignment.Left, Margin = new Thickness(0, 0, 0, 20) });
            System.Windows.Controls.TextBlock heading = new System.Windows.Controls.TextBlock { Text = InstallerText.Get("Не удалось открыть установщик"), FontSize = 23, Foreground = System.Windows.Media.Brushes.White, TextWrapping = TextWrapping.Wrap };
            System.Windows.Controls.TextBlock message = new System.Windows.Controls.TextBlock { Text = InstallerText.Get(error.Message), FontSize = 14, Foreground = System.Windows.Media.Brushes.LightGray, TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 16, 0, 22) };
            panel.Children.Add(heading); panel.Children.Add(message);
            System.Windows.Controls.Border frame = new System.Windows.Controls.Border { Child = panel, Background = background, CornerRadius = new CornerRadius(16), BorderBrush = new System.Windows.Media.SolidColorBrush(System.Windows.Media.Color.FromRgb(40, 64, 79)), BorderThickness = new Thickness(1) };
            Window window = new Window { Title = InstallerText.Get("Ремкарта — ошибка установщика"), Content = frame, Width = 520, SizeToContent = SizeToContent.Height, WindowStyle = WindowStyle.None, AllowsTransparency = true, ResizeMode = ResizeMode.NoResize, WindowStartupLocation = WindowStartupLocation.CenterScreen, Background = System.Windows.Media.Brushes.Transparent, Icon = InstallerResources.Image("RemCard.logo.png"), FontFamily = new System.Windows.Media.FontFamily("Segoe UI") };
            frame.MouseLeftButtonDown += delegate { try { window.DragMove(); } catch (InvalidOperationException) { } };
            System.Windows.Controls.Button close = new System.Windows.Controls.Button { Content = InstallerText.Get("Закрыть"), Padding = new Thickness(20, 10, 20, 10), HorizontalAlignment = HorizontalAlignment.Right, Background = new System.Windows.Media.SolidColorBrush(System.Windows.Media.Color.FromRgb(112, 228, 213)), Foreground = background };
            FrameworkElementFactory closeBorder = new FrameworkElementFactory(typeof(System.Windows.Controls.Border));
            closeBorder.SetValue(System.Windows.Controls.Border.BackgroundProperty, close.Background); closeBorder.SetValue(System.Windows.Controls.Border.CornerRadiusProperty, new CornerRadius(8));
            FrameworkElementFactory closeContent = new FrameworkElementFactory(typeof(System.Windows.Controls.ContentPresenter));
            closeContent.SetValue(FrameworkElement.MarginProperty, new Thickness(20, 10, 20, 10)); closeContent.SetValue(FrameworkElement.HorizontalAlignmentProperty, HorizontalAlignment.Center);
            closeBorder.AppendChild(closeContent); close.Template = new System.Windows.Controls.ControlTemplate(typeof(System.Windows.Controls.Button)) { VisualTree = closeBorder };
            close.Click += delegate { window.Close(); }; panel.Children.Add(close);
            System.Windows.Controls.StackPanel languages = new System.Windows.Controls.StackPanel { Orientation = System.Windows.Controls.Orientation.Horizontal, Margin = new Thickness(0, 0, 0, 16) };
            foreach (string language in new[] { "ru", "en" })
            {
                string selected = language;
                System.Windows.Controls.Button languageButton = new System.Windows.Controls.Button { Content = language == "ru" ? "Русский" : "English", Padding = new Thickness(12, 7, 12, 7), Margin = new Thickness(0, 0, 8, 0) };
                languageButton.Click += delegate { InstallerText.SetLanguage(selected); heading.Text = InstallerText.Get("Не удалось открыть установщик"); message.Text = InstallerText.Get(error.Message); close.Content = InstallerText.Get("Закрыть"); window.Title = InstallerText.Get("Ремкарта — ошибка установщика"); };
                languages.Children.Add(languageButton);
            }
            panel.Children.Insert(1, languages);
            if (application.MainWindow == null) { application.MainWindow = window; window.Show(); } else window.ShowDialog();
        }
    }
}
