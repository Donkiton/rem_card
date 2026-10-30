using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Markup;
using System.Windows.Media;
using System.Windows.Media.Effects;
using System.Windows.Media.Imaging;
using System.Windows.Shapes;
using System.Windows.Threading;
using IOPath = System.IO.Path;

namespace RemCard.Setup
{
    public sealed class InstallerWindow : Window
    {
        private readonly InstallerContext _context;
        private readonly string[] _steps = { "Знакомство", "Условия", "Размещение", "Всё готово", "Установка", "Завершение" };
        private readonly BitmapImage _logo, _darkPhoto, _lightPhoto;
        private Grid _shell, _body, _right, _overlay;
        private StackPanel _rail, _page, _footerActions, _dialogContent;
        private Border _surface, _header, _footer;
        private TextBlock _footerHint, _title, _lead, _phase, _percentage, _detail, _pathError;
        private Button _themeButton, _nextButton;
        private Grid _headerActions;
        private StackPanel _windowActions;
        private ProgressBar _bar;
        private StackPanel _journal;
        private ScrollViewer _journalScroll;
        private Image _welcomePhoto;
        private EcgMonitor _ecg;
        private TextBox _pathBox;
        private CheckBox _acceptBox;
        private RadioButton _defaultRadio, _customRadio;
        private string _target, _lastPhase, _errorCode;
        private int _step, _percent, _lastLogPercent;
        private bool _dark = true, _accepted, _desktop = true, _menu = true, _launchAfter;
        private bool _installing, _failed, _allowClose, _closeAfterCancel, _dialogVisible;
        private bool _faultInjectionConsumed;
        private volatile int _runId;
        private Action _dismissDialog;
        private IInputElement _previousFocus;
        private InstallResult _result;
        private CancellationTokenSource _cancel;
        private readonly List<JournalEntry> _log = new List<JournalEntry>();
        private readonly object _progressLock = new object();
        private InstallProgress _pendingProgress;
        private InstallProgress _currentProgress;
        private string _folderPickerPath, _pathErrorSource;
        private bool _journalHidden, _customLocation;
        private DialogState _dialogState;
        private int _pendingRunId;
        private bool _progressScheduled;

        public InstallerWindow(InstallerContext context)
        {
            if (context == null) throw new ArgumentNullException("context");
            _context = context;
            InstallerText.SetLanguage(context.Language);
            _logo = InstallerResources.Image("RemCard.logo.png");
            _darkPhoto = InstallerResources.Image("RemCard.background-dark.png");
            _lightPhoto = InstallerResources.Image("RemCard.background-light.png");
            _launchAfter = !context.SuppressLaunch;
            _target = !String.IsNullOrWhiteSpace(context.AutomationTargetDirectory) ? context.AutomationTargetDirectory : String.IsNullOrWhiteSpace(context.ExistingInstallDirectory) ? DefaultTarget() : context.ExistingInstallDirectory;
            _customLocation = !String.Equals(_target.TrimEnd('\\'), DefaultTarget().TrimEnd('\\'), StringComparison.OrdinalIgnoreCase);
            RefreshWindowTitle();
            Icon = _logo;
            FontFamily = new FontFamily("Segoe UI"); FontSize = 14;
            WindowStyle = WindowStyle.None; AllowsTransparency = true;
            ResizeMode = ResizeMode.NoResize; Background = Brushes.Transparent;
            WindowStartupLocation = WindowStartupLocation.CenterScreen;
            Width = Math.Min(960, Math.Max(600, SystemParameters.WorkArea.Width - 28));
            Height = Math.Min(750, Math.Max(560, SystemParameters.WorkArea.Height - 28));
            MinWidth = Math.Min(Width, 720); MinHeight = Math.Min(Height, 600);
            UseLayoutRounding = true;
            TextOptions.SetTextFormattingMode(this, TextFormattingMode.Display);
            SetPalette(); AddStyles(); BuildShell(); Render();
            Closing += OnWindowClosing;
            Closed += delegate { if (_ecg != null) _ecg.Dispose(); };
            PreviewKeyDown += OnKeyDown;
            Loaded += delegate { ApplyAutomationPreview(); };
            AutomationProperties.SetName(this, Title);
        }

        public void CloseForAutomation()
        {
            if (_installing) { _closeAfterCancel = true; if (_cancel != null) _cancel.Cancel(); return; }
            _allowClose = true; Close();
        }

        private static string DefaultTarget()
        { return IOPath.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", "RemCard"); }

        private void PutBrush(string name, string color)
        { SolidColorBrush brush = new SolidColorBrush((Color)ColorConverter.ConvertFromString(color)); brush.Freeze(); Resources[name] = brush; }

        private void SetPalette()
        {
            PutBrush("Bg", _dark ? "#0C1824" : "#F5F8FA");
            PutBrush("Panel", _dark ? "#10202E" : "#FFFFFF");
            PutBrush("Well", _dark ? "#09141F" : "#EAF0F4");
            PutBrush("Line", _dark ? "#28404F" : "#CCD9E1");
            PutBrush("Text", _dark ? "#EDF5FA" : "#173448");
            PutBrush("Muted", _dark ? "#A0B5C3" : "#526B7E");
            PutBrush("Accent", _dark ? "#70E4D5" : "#08766D");
            PutBrush("AccentInk", _dark ? "#082E2B" : "#FFFFFF");
            PutBrush("Soft", _dark ? "#193A40" : "#DFEFEB");
            PutBrush("Hover", _dark ? "#1A2D3D" : "#E7EEF2");
            PutBrush("AccentHover", _dark ? "#91F0E4" : "#09695F");
            PutBrush("Error", _dark ? "#FFB2B9" : "#AD283C");
            PutBrush("HeroCaption", _dark ? "#CDE8F4" : "#173448");
            PutBrush("PreviewAccent", "#70E4D5");
        }

        private void AddStyles()
        {
            const string styles = @"<ResourceDictionary xmlns='http://schemas.microsoft.com/winfx/2006/xaml/presentation' xmlns:x='http://schemas.microsoft.com/winfx/2006/xaml'>
              <Style x:Key='Focus' TargetType='Control'><Setter Property='Template'><Setter.Value><ControlTemplate><Border Margin='-4' BorderBrush='{DynamicResource Accent}' BorderThickness='2' CornerRadius='11'/></ControlTemplate></Setter.Value></Setter></Style>
              <Style x:Key='Button' TargetType='Button'>
                <Setter Property='Foreground' Value='{DynamicResource Text}'/><Setter Property='Background' Value='Transparent'/><Setter Property='BorderBrush' Value='{DynamicResource Line}'/><Setter Property='BorderThickness' Value='1'/><Setter Property='Padding' Value='17,10'/><Setter Property='MinHeight' Value='40'/><Setter Property='FontSize' Value='13'/><Setter Property='FontWeight' Value='SemiBold'/><Setter Property='Cursor' Value='Hand'/><Setter Property='FocusVisualStyle' Value='{StaticResource Focus}'/>
                <Setter Property='Template'><Setter.Value><ControlTemplate TargetType='Button'><Border x:Name='Frame' Background='{TemplateBinding Background}' BorderBrush='{TemplateBinding BorderBrush}' BorderThickness='{TemplateBinding BorderThickness}' CornerRadius='9' Padding='{TemplateBinding Padding}'><ContentPresenter HorizontalAlignment='{TemplateBinding HorizontalContentAlignment}' VerticalAlignment='Center' RecognizesAccessKey='True'/></Border><ControlTemplate.Triggers><Trigger Property='IsMouseOver' Value='True'><Setter Property='Background' Value='{DynamicResource Hover}'/></Trigger><Trigger Property='IsPressed' Value='True'><Setter Property='Opacity' Value='.78'/></Trigger><Trigger Property='IsEnabled' Value='False'><Setter Property='Opacity' Value='.4'/><Setter Property='Cursor' Value='Arrow'/></Trigger></ControlTemplate.Triggers></ControlTemplate></Setter.Value></Setter>
              </Style>
              <Style x:Key='Primary' TargetType='Button' BasedOn='{StaticResource Button}'><Setter Property='Background' Value='{DynamicResource Accent}'/><Setter Property='BorderBrush' Value='{DynamicResource Accent}'/><Setter Property='Foreground' Value='{DynamicResource AccentInk}'/><Style.Triggers><Trigger Property='IsMouseOver' Value='True'><Setter Property='Background' Value='{DynamicResource AccentHover}'/></Trigger></Style.Triggers></Style>
              <Style x:Key='Quiet' TargetType='Button' BasedOn='{StaticResource Button}'><Setter Property='BorderThickness' Value='0'/><Setter Property='Foreground' Value='{DynamicResource Muted}'/><Setter Property='Padding' Value='8,7'/></Style>
              <Style x:Key='Link' TargetType='Button' BasedOn='{StaticResource Quiet}'><Setter Property='Foreground' Value='{DynamicResource Accent}'/><Setter Property='FontWeight' Value='Normal'/><Setter Property='FontSize' Value='12'/><Setter Property='Padding' Value='0'/><Setter Property='MinHeight' Value='26'/><Setter Property='HorizontalContentAlignment' Value='Left'/></Style>
              <Style TargetType='TextBox'><Setter Property='Foreground' Value='{DynamicResource Text}'/><Setter Property='Background' Value='{DynamicResource Well}'/><Setter Property='BorderBrush' Value='{DynamicResource Line}'/><Setter Property='BorderThickness' Value='1'/><Setter Property='Padding' Value='12,10'/><Setter Property='FontSize' Value='13'/><Setter Property='VerticalContentAlignment' Value='Center'/><Setter Property='CaretBrush' Value='{DynamicResource Accent}'/><Setter Property='SelectionBrush' Value='{DynamicResource Soft}'/><Setter Property='FocusVisualStyle' Value='{StaticResource Focus}'/><Setter Property='Template'><Setter.Value><ControlTemplate TargetType='TextBox'><Border CornerRadius='8' BorderBrush='{TemplateBinding BorderBrush}' BorderThickness='{TemplateBinding BorderThickness}' Background='{TemplateBinding Background}' Padding='{TemplateBinding Padding}'><ScrollViewer x:Name='PART_ContentHost'/></Border></ControlTemplate></Setter.Value></Setter></Style>
              <Style TargetType='CheckBox'><Setter Property='Foreground' Value='{DynamicResource Text}'/><Setter Property='FontSize' Value='13'/><Setter Property='Cursor' Value='Hand'/><Setter Property='FocusVisualStyle' Value='{StaticResource Focus}'/><Setter Property='Template'><Setter.Value><ControlTemplate TargetType='CheckBox'><Grid><Grid.ColumnDefinitions><ColumnDefinition Width='20'/><ColumnDefinition Width='*'/></Grid.ColumnDefinitions><Border x:Name='Box' Width='17' Height='17' VerticalAlignment='Top' Margin='0,2,0,0' BorderBrush='{DynamicResource Muted}' BorderThickness='1' CornerRadius='4' Background='{DynamicResource Well}'/><Path x:Name='Tick' Data='M 3 8 L 7 12 L 14 4' Stroke='{DynamicResource AccentInk}' StrokeThickness='2' Width='15' Height='14' VerticalAlignment='Top' Margin='0,3,0,0' Visibility='Collapsed'/><ContentPresenter Grid.Column='1' Margin='8,0,0,0' RecognizesAccessKey='True'/></Grid><ControlTemplate.Triggers><Trigger Property='IsChecked' Value='True'><Setter TargetName='Box' Property='Background' Value='{DynamicResource Accent}'/><Setter TargetName='Box' Property='BorderBrush' Value='{DynamicResource Accent}'/><Setter TargetName='Tick' Property='Visibility' Value='Visible'/></Trigger><Trigger Property='IsEnabled' Value='False'><Setter Property='Opacity' Value='.45'/></Trigger></ControlTemplate.Triggers></ControlTemplate></Setter.Value></Setter></Style>
              <Style TargetType='RadioButton'><Setter Property='Foreground' Value='{DynamicResource Text}'/><Setter Property='FontSize' Value='13'/><Setter Property='Cursor' Value='Hand'/><Setter Property='FocusVisualStyle' Value='{StaticResource Focus}'/><Setter Property='Template'><Setter.Value><ControlTemplate TargetType='RadioButton'><Grid><Grid.ColumnDefinitions><ColumnDefinition Width='20'/><ColumnDefinition Width='*'/></Grid.ColumnDefinitions><Ellipse x:Name='Outer' Width='17' Height='17' VerticalAlignment='Top' Margin='0,2,0,0' Stroke='{DynamicResource Muted}' StrokeThickness='1'/><Ellipse x:Name='Dot' Width='9' Height='9' VerticalAlignment='Top' Margin='0,6,0,0' Fill='{DynamicResource Accent}' Visibility='Collapsed'/><ContentPresenter Grid.Column='1' Margin='8,0,0,0'/></Grid><ControlTemplate.Triggers><Trigger Property='IsChecked' Value='True'><Setter TargetName='Dot' Property='Visibility' Value='Visible'/><Setter TargetName='Outer' Property='Stroke' Value='{DynamicResource Accent}'/></Trigger></ControlTemplate.Triggers></ControlTemplate></Setter.Value></Setter></Style>
              <Style TargetType='ProgressBar'><Setter Property='Foreground' Value='{DynamicResource Accent}'/><Setter Property='Background' Value='{DynamicResource Line}'/><Setter Property='Height' Value='7'/><Setter Property='Template'><Setter.Value><ControlTemplate TargetType='ProgressBar'><Grid x:Name='PART_Track'><Border Background='{TemplateBinding Background}' CornerRadius='4'/><Border x:Name='PART_Indicator' Background='{TemplateBinding Foreground}' CornerRadius='4' HorizontalAlignment='Left'/></Grid></ControlTemplate></Setter.Value></Setter></Style>
              <Style TargetType='ListBox'><Setter Property='Background' Value='{DynamicResource Well}'/><Setter Property='Foreground' Value='{DynamicResource Text}'/><Setter Property='BorderBrush' Value='{DynamicResource Line}'/><Setter Property='BorderThickness' Value='1'/><Setter Property='Padding' Value='5'/></Style>
              <Style TargetType='ListBoxItem'><Setter Property='Padding' Value='7'/><Setter Property='HorizontalContentAlignment' Value='Stretch'/><Setter Property='Template'><Setter.Value><ControlTemplate TargetType='ListBoxItem'><Border x:Name='Frame' Background='Transparent' CornerRadius='5' Padding='{TemplateBinding Padding}'><ContentPresenter/></Border><ControlTemplate.Triggers><Trigger Property='IsSelected' Value='True'><Setter TargetName='Frame' Property='Background' Value='{DynamicResource Soft}'/><Setter Property='Foreground' Value='{DynamicResource Accent}'/></Trigger><Trigger Property='IsMouseOver' Value='True'><Setter TargetName='Frame' Property='Background' Value='{DynamicResource Hover}'/></Trigger></ControlTemplate.Triggers></ControlTemplate></Setter.Value></Setter></Style>
            </ResourceDictionary>";
            ResourceDictionary dictionary = (ResourceDictionary)XamlReader.Parse(styles);
            Resources.MergedDictionaries.Add(dictionary);
        }

        private void Color(DependencyObject element, DependencyProperty property, string key)
        {
            FrameworkElement framework = element as FrameworkElement;
            if (framework != null) framework.SetResourceReference(property, key);
        }

        private TextBlock Text(string value, double size, string color)
        {
            TextBlock block = new TextBlock { Text = InstallerText.Get(value), FontSize = size, TextWrapping = TextWrapping.Wrap };
            Color(block, TextBlock.ForegroundProperty, color); return block;
        }
        private TextBlock Text(string value, double size) { return Text(value, size, "Text"); }
        private TextBlock RawText(string value, double size, string color)
        { TextBlock block = Text("", size, color); block.Text = value; return block; }

        private void RefreshWindowTitle()
        {
            Title = InstallerText.Get(_context.UninstallMode ? "Удаление Ремкарты" : "Установка Ремкарты " + _context.Version);
            if (_context.NoRegistration) Title += InstallerText.Get(" — проверка интерфейса");
            AutomationProperties.SetName(this, Title);
        }

        public void ChangeLanguage(string language)
        {
            if (_context.Language == language) return;
            InstallerText.SetLanguage(language);
            if (_pathBox != null) _target = _pathBox.Text;
            DialogState dialog = _dialogState;
            _context.Language = language; RefreshWindowTitle();
            _dialogVisible = false; BuildShell(); Render();
            if (dialog != null) ShowDialog(dialog.Title, dialog.Body, dialog.Error, dialog.Extra, dialog.Actions);
        }

        public string[] FindLayoutIssues()
        {
            UpdateLayout(); List<string> issues = new List<string>();
            InspectLayout((DependencyObject)Content, issues);
            return issues.Distinct().ToArray();
        }

        private static void InspectLayout(DependencyObject parent, List<string> issues)
        {
            TextBlock text = parent as TextBlock;
            if (text != null && text.IsVisible && text.ActualWidth > 1 && text.ActualHeight > 1 && text.TextTrimming == TextTrimming.None)
            {
                if (text.DesiredSize.Height - text.Margin.Top - text.Margin.Bottom > text.ActualHeight + 2)
                    issues.Add("text-height: " + text.Text.Substring(0, Math.Min(60, text.Text.Length)));
            }
            for (int index = 0; index < VisualTreeHelper.GetChildrenCount(parent); index++) InspectLayout(VisualTreeHelper.GetChild(parent, index), issues);
        }

        public object RunLanguageStateSmoke()
        {
            if (!_context.NoRegistration) throw new InvalidOperationException("UI state checks require --ui-smoke.");
            List<string> checks = new List<string>();
            string originalLanguage = _context.Language;
            _target = IOPath.Combine(IOPath.GetTempPath(), "RemCard-language-preview", "A long selected application folder");
            _customLocation = true; _accepted = true; _desktop = false; _menu = true; _launchAfter = false; _dark = false; SetPalette();
            _step = 2; Render();
            foreach (string language in new[] { "en", "ru" })
            {
                ChangeLanguage(language);
                if (_pathBox.Text != _target || _pathBox.IsReadOnly || !_accepted || _desktop || !_menu || _launchAfter || _dark || _step != 2)
                    throw new InvalidOperationException("Language switch changed location, options, consent or theme.");
            }
            checks.Add("location-options-consent-theme");
            _step = 1; Render(); ChangeLanguage("en");
            if (_acceptBox.IsChecked != true || _nextButton.IsEnabled != true) throw new InvalidOperationException("License consent was lost.");
            checks.Add("license-consent");
            _step = 4; _installing = true; _currentProgress = new InstallProgress(58, "Подготовка файлов", "Проверяем готовность пакета");
            AppendLog("Проверяем готовность пакета", false); _journalHidden = true; Render();
            int logCount = _log.Count; ChangeLanguage("ru"); ChangeLanguage("en");
            if (!_installing || _step != 4 || _percent != 58 || _bar.Value != 58 || _log.Count != logCount || !_journalHidden || _phase.Text != "Preparing files")
                throw new InvalidOperationException("Language switch changed installation progress or journal.");
            checks.Add("active-progress-and-journal");
            _installing = false; PresentFailure(new InstallFailureException("RC-INSTALL-005", "Нет доступа к записи в папку приложения. Проверьте права на выбранную папку или выберите другой каталог."));
            ChangeLanguage("ru"); ChangeLanguage("en");
            if (!_failed || !_dialogVisible || _errorCode != "RC-INSTALL-005" || _percent != 58 || _dialogState == null || _windowActions.IsEnabled)
                throw new InvalidOperationException("Language switch changed the error state.");
            checks.Add("error-dialog-and-progress");
            byte[] beforeLicense = InstallerResources.ReadBytes("RemCard.LICENSE");
            HideDialog(); ShowLicense(); ChangeLanguage("ru"); ChangeLanguage("en");
            if (!beforeLicense.SequenceEqual(InstallerResources.ReadBytes("RemCard.LICENSE"))) throw new InvalidOperationException("License bytes changed.");
            checks.Add("original-license-bytes"); HideDialog();
            bool rejected = false; try { ChangeLanguage("fr"); } catch (ArgumentException) { rejected = true; }
            if (!rejected || _context.Language != "en") throw new InvalidOperationException("An unsupported language was accepted.");
            checks.Add("exactly-two-languages");
            bool uninstall = _context.UninstallMode;
            _context.UninstallMode = true; _failed = false; _step = 0; Render();
            ChangeLanguage("ru"); ChangeLanguage("en");
            if (!Title.StartsWith("Uninstall RemCard", StringComparison.Ordinal) || _target == null || !_accepted)
                throw new InvalidOperationException("Uninstaller language state was lost.");
            _step = 4; _currentProgress = new InstallProgress(58, "Удаление программы", "Данные и настройки сохраняются."); Render();
            ChangeLanguage("ru"); ChangeLanguage("en");
            if (_percent != 58 || _phase.Text != "Uninstall application") throw new InvalidOperationException("Uninstaller progress was lost.");
            checks.Add("uninstaller-language-and-progress"); _context.UninstallMode = uninstall;
            _failed = false; _step = 3; ChangeLanguage(originalLanguage); Render();
            string licenseHash;
            using (System.Security.Cryptography.SHA256 sha = System.Security.Cryptography.SHA256.Create()) licenseHash = PayloadArchive.Hex(sha.ComputeHash(beforeLicense));
            return new { ok = true, mode = "ui-language-state", checks = checks, language = _context.Language, license_sha256 = licenseHash, layout_issues = FindLayoutIssues() };
        }

        private Image Logo(double size)
        { return new Image { Source = _logo, Width = size, Height = size, Stretch = Stretch.Uniform }; }

        private Button Button(string label, Action action, bool primary)
        {
            Button button = new Button { Content = InstallerText.Get(label), Style = (Style)FindResource(primary ? "Primary" : "Button"), HorizontalContentAlignment = HorizontalAlignment.Center };
            button.Click += delegate { action(); };
            AutomationProperties.SetName(button, InstallerText.Get(label)); return button;
        }

        private Button Link(string label, Action action)
        { Button result = Button(label, action, false); result.Style = (Style)FindResource("Link"); return result; }

        private Border Box(UIElement child, string background, bool outline, Thickness padding)
        {
            Border border = new Border { Child = child, CornerRadius = new CornerRadius(11), Padding = padding };
            Color(border, Border.BackgroundProperty, background);
            if (outline) { Color(border, Border.BorderBrushProperty, "Line"); border.BorderThickness = new Thickness(1); }
            return border;
        }

        private void BuildShell()
        {
            _shell = new Grid();
            _shell.RowDefinitions.Add(new RowDefinition { Height = new GridLength(66) });
            _shell.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            _shell.RowDefinitions.Add(new RowDefinition { Height = new GridLength(34) });
            _surface = new Border { CornerRadius = new CornerRadius(20), BorderThickness = new Thickness(1), Margin = new Thickness(8), Child = _shell };
            Color(_surface, Border.BackgroundProperty, "Bg"); Color(_surface, Border.BorderBrushProperty, "Line");
            _surface.Effect = new DropShadowEffect { BlurRadius = 18, Opacity = .22, ShadowDepth = 4, Color = Colors.Black };
            _shell.SizeChanged += delegate { _shell.Clip = new RectangleGeometry(new Rect(0, 0, _shell.ActualWidth, _shell.ActualHeight), 19, 19); };
            Content = _surface;
            BuildHeader();
            _body = new Grid(); _body.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(188) }); _body.ColumnDefinitions.Add(new ColumnDefinition());
            Grid.SetRow(_body, 1); _shell.Children.Add(_body);
            Border railBorder = new Border { BorderThickness = new Thickness(0, 0, 1, 0), Padding = new Thickness(16, 25, 16, 20) };
            Color(railBorder, Border.BackgroundProperty, "Well"); Color(railBorder, Border.BorderBrushProperty, "Line");
            DockPanel railDock = new DockPanel(); railBorder.Child = railDock;
            StackPanel railBottom = new StackPanel { Margin = new Thickness(8, 15, 8, 0), VerticalAlignment = VerticalAlignment.Bottom };
            railBottom.Children.Add(StaticPulse(140, 35));
            TextBlock intensive = Text("INTENSIVE CARE", 11); intensive.FontWeight = FontWeights.SemiBold; intensive.Margin = new Thickness(0, 13, 0, 5); railBottom.Children.Add(intensive);
            railBottom.Children.Add(Text("Для тех, кто рядом\nв самые важные минуты.", 12, "Muted")); DockPanel.SetDock(railBottom, Dock.Bottom); railDock.Children.Add(railBottom);
            _rail = new StackPanel(); railDock.Children.Add(_rail); _body.Children.Add(railBorder);
            _right = new Grid(); _right.RowDefinitions.Add(new RowDefinition()); _right.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto }); Grid.SetColumn(_right, 1); _body.Children.Add(_right);
            ScrollViewer scroll = new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled, Padding = new Thickness(29, 25, 29, 23), CanContentScroll = false };
            _page = new StackPanel(); scroll.Content = _page; _right.Children.Add(scroll);
            _footer = new Border { BorderThickness = new Thickness(0, 1, 0, 0), Padding = new Thickness(29, 16, 29, 18) }; Color(_footer, Border.BorderBrushProperty, "Line"); Grid.SetRow(_footer, 1); _right.Children.Add(_footer);
            Grid footerGrid = new Grid(); footerGrid.ColumnDefinitions.Add(new ColumnDefinition()); footerGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto }); _footer.Child = footerGrid;
            _footerHint = Text("", 11, "Muted"); _footerHint.VerticalAlignment = VerticalAlignment.Center; _footerHint.Margin = new Thickness(0, 0, 14, 0); footerGrid.Children.Add(_footerHint);
            _footerActions = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right, VerticalAlignment = VerticalAlignment.Center }; Grid.SetColumn(_footerActions, 1); footerGrid.Children.Add(_footerActions);
            Grid links = new Grid { Margin = new Thickness(21, 0, 21, 0) }; Grid.SetRow(links, 2); _shell.Children.Add(links);
            TextBlock copyright = Text("© 2026 Ремкарта", 11, "Muted"); copyright.VerticalAlignment = VerticalAlignment.Center; links.Children.Add(copyright);
            StackPanel linkRow = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right };
            Button site = Link("Сайт Ремкарты ↗", delegate { OpenLink("https://donkiton.github.io/rem_site/"); }); site.Margin = new Thickness(0, 0, 18, 0); linkRow.Children.Add(site);
            linkRow.Children.Add(Link("О проекте ↗", delegate { OpenLink("https://github.com/Donkiton/rem_card"); })); links.Children.Add(linkRow);
            _overlay = new Grid { Visibility = Visibility.Collapsed, Background = Brushes.Transparent };
            Grid.SetRowSpan(_overlay, 3); Panel.SetZIndex(_overlay, 50); _shell.Children.Add(_overlay);
            KeyboardNavigation.SetTabNavigation(_overlay, KeyboardNavigationMode.Cycle);
        }

        private void BuildHeader()
        {
            Grid headerGrid = new Grid { Margin = new Thickness(20, 0, 16, 0) }; headerGrid.ColumnDefinitions.Add(new ColumnDefinition()); headerGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            _header = new Border { BorderThickness = new Thickness(0, 0, 0, 1), Child = headerGrid }; Color(_header, Border.BackgroundProperty, "Panel"); Color(_header, Border.BorderBrushProperty, "Line"); _shell.Children.Add(_header);
            Panel.SetZIndex(_header, 100);
            StackPanel brand = new StackPanel { Orientation = Orientation.Horizontal, VerticalAlignment = VerticalAlignment.Center };
            Image logo = Logo(32); logo.Margin = new Thickness(0, 0, 10, 0); brand.Children.Add(logo);
            StackPanel names = new StackPanel(); TextBlock name = Text("РЕМКАРТА", 14); name.FontWeight = FontWeights.SemiBold; names.Children.Add(name); names.Children.Add(Text(_context.UninstallMode ? "Удаление программы" : "Мастер установки", 11, "Muted")); brand.Children.Add(names); headerGrid.Children.Add(brand);
            brand.MouseLeftButtonDown += delegate(object sender, MouseButtonEventArgs args) { if (args.ButtonState == MouseButtonState.Pressed) { try { DragMove(); } catch (InvalidOperationException) { } } };
            _headerActions = new Grid { VerticalAlignment = VerticalAlignment.Center }; Grid.SetColumn(_headerActions, 1); headerGrid.Children.Add(_headerActions);
            StackPanel actions = new StackPanel { Orientation = Orientation.Horizontal, VerticalAlignment = VerticalAlignment.Center }; _headerActions.Children.Add(actions);
            TextBlock version = Text("v" + (_context.Version ?? ""), 12, "Muted"); version.VerticalAlignment = VerticalAlignment.Center; version.Margin = new Thickness(0, 0, 12, 0); actions.Children.Add(version);
            foreach (string language in new[] { "ru", "en" })
            {
                string selectedLanguage = language;
                Button languageButton = Button(language == "ru" ? "Русский" : "English", delegate { ChangeLanguage(selectedLanguage); }, language == _context.Language);
                languageButton.Padding = new Thickness(10, 6, 10, 6); languageButton.MinHeight = 34; languageButton.Margin = new Thickness(0, 0, 5, 0);
                AutomationProperties.SetAutomationId(languageButton, "language-" + language);
                actions.Children.Add(languageButton);
            }
            _windowActions = new StackPanel { Orientation = Orientation.Horizontal }; actions.Children.Add(_windowActions);
            _themeButton = Button(_dark ? "☼" : "☾", ToggleTheme, false); _themeButton.Style = (Style)FindResource("Quiet"); _themeButton.FontSize = 21; _themeButton.Width = 36; _themeButton.MinHeight = 34; _themeButton.Padding = new Thickness(0); AutomationProperties.SetName(_themeButton, InstallerText.Get("Переключить светлую и тёмную тему")); _windowActions.Children.Add(_themeButton);
            Button minimize = Button("−", delegate { WindowState = WindowState.Minimized; }, false); minimize.Style = (Style)FindResource("Quiet"); minimize.Width = 34; minimize.MinHeight = 34; minimize.Padding = new Thickness(0); AutomationProperties.SetName(minimize, InstallerText.Get("Свернуть установщик")); _windowActions.Children.Add(minimize);
            Button close = Button("×", RequestClose, false); close.Style = (Style)FindResource("Quiet"); close.FontSize = 21; close.Width = 34; close.MinHeight = 34; close.Padding = new Thickness(0); AutomationProperties.SetName(close, InstallerText.Get("Закрыть установщик")); _windowActions.Children.Add(close);
        }

        private void ToggleTheme()
        {
            _dark = !_dark; SetPalette(); _themeButton.Content = _dark ? "☼" : "☾";
            if (_welcomePhoto != null) _welcomePhoto.Source = _dark ? _darkPhoto : _lightPhoto;
        }

        private FrameworkElement StaticPulse(double width, double height)
        {
            System.Windows.Shapes.Path pulse = new System.Windows.Shapes.Path { Data = Geometry.Parse("M0 34 H53 L66 29 L77 34 H109 L119 40 L132 10 L146 53 L155 34 H207 L218 28 L229 34 H260 L271 40 L284 10 L298 53 L307 34 H358 L369 29 L381 34 H400"), StrokeThickness = 1.5, Stretch = Stretch.Fill, Width = width, Height = height, Opacity = .8 };
            Color(pulse, Shape.StrokeProperty, "Accent"); return pulse;
        }

        private void RenderRail()
        {
            _rail.Children.Clear(); TextBlock caption = Text("ВАШЕ РАБОЧЕЕ\nМЕСТО", 11, "Muted"); caption.Margin = new Thickness(8, 0, 0, 20); _rail.Children.Add(caption);
            string[] labels = _context.UninstallMode ? new[] { "Подтверждение", "Удаление", "Завершение" } : _steps;
            int selected = _context.UninstallMode ? (_step == 5 ? 2 : _step == 4 ? 1 : 0) : _step;
            for (int i = 0; i < labels.Length; i++)
            {
                int destination = i; bool current = i == selected, past = i < selected;
                StackPanel row = new StackPanel { Orientation = Orientation.Horizontal };
                Border circle = new Border { Width = 26, Height = 26, CornerRadius = new CornerRadius(13), BorderThickness = new Thickness(past ? 0 : 1), Margin = new Thickness(0, 0, 10, 0) };
                Color(circle, Border.BorderBrushProperty, "Line"); if (current) Color(circle, Border.BackgroundProperty, "Accent");
                TextBlock number = Text(past ? "✓" : (i + 1).ToString("00"), 11, current ? "AccentInk" : past ? "Accent" : "Muted"); number.HorizontalAlignment = HorizontalAlignment.Center; number.VerticalAlignment = VerticalAlignment.Center; circle.Child = number; row.Children.Add(circle);
                TextBlock text = Text(labels[i], 12, current ? "Accent" : "Muted"); text.VerticalAlignment = VerticalAlignment.Center; row.Children.Add(text);
                Button button = Button("", delegate { if (!_context.UninstallMode && destination < _step && _step < 4) { _step = destination; Render(); } }, false);
                button.Content = row; button.Padding = new Thickness(8, 7, 7, 7); button.BorderThickness = new Thickness(0); button.HorizontalContentAlignment = HorizontalAlignment.Left; button.Margin = new Thickness(0, 0, 0, 7); button.MinHeight = 42;
                if (current) Color(button, Control.BackgroundProperty, "Soft");
                button.IsEnabled = !_installing && !_context.UninstallMode && i < _step && _step < 4;
                button.Opacity = current || past ? 1 : .55;
                AutomationProperties.SetName(button, (i + 1).ToString() + ". " + InstallerText.Get(labels[i])); _rail.Children.Add(button);
            }
        }

        private void Header(string heading, string lead)
        {
            TextBlock counter = Text(_context.UninstallMode ? (_step == 4 ? "УДАЛЕНИЕ ПРОГРАММЫ" : _step == 5 ? "ЗАВЕРШЕНО" : "ВАШЕ РАБОЧЕЕ МЕСТО") : "ШАГ " + (_step + 1).ToString("00") + " / 06", 11, "Accent"); counter.Margin = new Thickness(0, 0, 0, 12); _page.Children.Add(counter);
            _title = Text(heading, 28); _title.FontWeight = FontWeights.SemiBold; _title.LineHeight = 34; _title.Margin = new Thickness(0, 0, 0, 12); _page.Children.Add(_title);
            _lead = Text(lead, 13, "Muted"); _lead.LineHeight = 21; _lead.Margin = new Thickness(0, 0, 0, 19); _page.Children.Add(_lead);
        }

        private void Note(string text)
        {
            Grid grid = new Grid { Margin = new Thickness(0, 16, 0, 0) }; grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(23) }); grid.ColumnDefinitions.Add(new ColumnDefinition());
            TextBlock symbol = Text("ⓘ", 15, "Accent"); grid.Children.Add(symbol); TextBlock label = Text(text, 12, "Muted"); label.LineHeight = 18; Grid.SetColumn(label, 1); grid.Children.Add(label); _page.Children.Add(grid);
        }

        private void Render()
        {
            if (_ecg != null) { _ecg.Dispose(); _ecg = null; }
            _welcomePhoto = null; _page.Children.Clear(); _footerActions.Children.Clear(); _pathBox = null;
            RenderRail();
            if (_context.UninstallMode && _step == 0) RenderUninstallWelcome();
            else if (_step == 0) RenderWelcome();
            else if (_step == 1) RenderLicense();
            else if (_step == 2) RenderLocation();
            else if (_step == 3) RenderReview();
            else if (_step == 4) RenderProgress();
            else RenderSuccess();
            RenderFooter();
            if (_step == 4 && _failed) ApplyFailureState();
        }

        private void RenderWelcome()
        {
            Header("Больше времени\nдля пациента.", "Ремкарта — рабочее пространство команды реанимации. Назначения, наблюдение и документация в одной системе.");
            Grid hero = new Grid { Height = Height < 700 ? 166 : 182, ClipToBounds = true };
            _welcomePhoto = new Image { Source = _dark ? _darkPhoto : _lightPhoto, Stretch = Stretch.UniformToFill }; hero.Children.Add(_welcomePhoto);
            TextBlock upper = new TextBlock { Text = InstallerText.Get("АНЕСТЕЗИОЛОГИЯ И РЕАНИМАТОЛОГИЯ"), TextWrapping = TextWrapping.Wrap, FontSize = 10.5, Margin = new Thickness(17, 14, 17, 0), VerticalAlignment = VerticalAlignment.Top }; Color(upper, TextBlock.ForegroundProperty, "HeroCaption"); hero.Children.Add(upper);
            StackPanel preview = new StackPanel(); StackPanel brand = new StackPanel { Orientation = Orientation.Horizontal }; Image smallLogo = Logo(20); smallLogo.Margin = new Thickness(0, 0, 7, 0); brand.Children.Add(smallLogo); brand.Children.Add(new TextBlock { Text = InstallerText.Get("Реанимационная карта"), Foreground = Brushes.White, FontSize = 11, VerticalAlignment = VerticalAlignment.Center }); preview.Children.Add(brand);
            FrameworkElement pulse = StaticPulse(180, 28); Color(pulse, Shape.StrokeProperty, "PreviewAccent"); pulse.Margin = new Thickness(0, 9, 0, 6); preview.Children.Add(pulse); preview.Children.Add(new TextBlock { Text = InstallerText.Get("Наблюдение · Назначения · Баланс"), TextWrapping = TextWrapping.Wrap, Foreground = new SolidColorBrush(ColorFrom("#A9C1D2")), FontSize = 10.5 });
            Border previewBox = new Border { Child = preview, Width = 220, Padding = new Thickness(14, 12, 14, 12), Background = new SolidColorBrush(ColorFrom("#EE0C1B2D")), BorderBrush = new SolidColorBrush(ColorFrom("#55738FA1")), BorderThickness = new Thickness(1), CornerRadius = new CornerRadius(9), HorizontalAlignment = HorizontalAlignment.Right, VerticalAlignment = VerticalAlignment.Bottom, Margin = new Thickness(15) }; hero.Children.Add(previewBox);
            Border heroBox = new Border { Child = hero, CornerRadius = new CornerRadius(12), ClipToBounds = true, Margin = new Thickness(0, 2, 0, 17) }; hero.SizeChanged += delegate { hero.Clip = new RectangleGeometry(new Rect(0, 0, hero.ActualWidth, hero.ActualHeight), 12, 12); }; _page.Children.Add(heroBox);
            WrapPanel features = new WrapPanel();
            features.Children.Add(Feature("Ремкарты", "M5,3 L5,1 L13,1 L13,3 M4,3 L2,3 L2,17 L16,17 L16,3 L14,3 M6,7 L12,7 M6,11 L12,11"));
            features.Children.Add(Feature("Работа команды", "M6,8 A3,3 0 1 0 6,2 A3,3 0 1 0 6,8 M1,17 L1,14 C1,10 11,10 11,14 L11,17 M12,3 C17,3 17,8 12,9 M14,11 C18,12 18,14 18,17"));
            features.Children.Add(Feature("Оперблок", "M1,10 L5,10 L8,2 L11,17 L14,7 L16,10 L20,10"));
            _page.Children.Add(features);
            Note("Настроим программу на этом компьютере. Папку данных можно выбрать при первом запуске.");
        }

        private FrameworkElement Feature(string label, string data)
        {
            StackPanel row = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 0, 22, 0) };
            System.Windows.Shapes.Path icon = new System.Windows.Shapes.Path { Data = Geometry.Parse(data), Width = 16, Height = 16, Stretch = Stretch.Uniform, StrokeThickness = 1.4, StrokeStartLineCap = PenLineCap.Round, StrokeEndLineCap = PenLineCap.Round, Margin = new Thickness(0, 0, 7, 0) };
            Color(icon, Shape.StrokeProperty, "Accent"); row.Children.Add(icon); row.Children.Add(Text(label, 12, "Muted")); return row;
        }

        private void RenderUninstallWelcome()
        {
            Header("Освободим рабочее место.", "Будут удалены файлы Ремкарты и созданные установщиком ярлыки. Настройки и ваши данные сохранятся.");
            AddPathSummary("Установленная программа", _target);
            Note("Сетевая база, медицинские документы, локальные пользовательские данные и настройка пути к базе не удаляются.");
        }

        private void RenderLicense()
        {
            Header("Договоримся об условиях.", "Перед установкой ознакомьтесь с лицензией Ремкарты.");
            StackPanel terms = new StackPanel(); TextBlock licenseName = Text("RemCard Proprietary Source License · 1.1", 12); licenseName.FontWeight = FontWeights.SemiBold; licenseName.Margin = new Thickness(0, 0, 0, 12); terms.Children.Add(licenseName);
            AddTerm(terms, "Личное некоммерческое использование", "Разрешено на условиях лицензии проекта.");
            AddTerm(terms, "Работа в организации", "Использование в клинике или другой организации требует письменного разрешения автора и отдельного соглашения.");
            AddTerm(terms, "Авторские права", "Битюцкий Вадим Алексеевич · 2026");
            terms.Children.Add(Link("Прочитать полный текст лицензии", ShowLicense));
            _page.Children.Add(Box(terms, "Panel", true, new Thickness(18, 16, 18, 16)));
            _acceptBox = Check("Я ознакомился и принимаю условия лицензионного соглашения.", _accepted, delegate(bool value) { _accepted = value; if (_nextButton != null) _nextButton.IsEnabled = value; }); _acceptBox.Margin = new Thickness(0, 19, 0, 0); _page.Children.Add(_acceptBox);
        }

        private void AddTerm(StackPanel parent, string title, string body)
        {
            TextBlock heading = Text(title, 12); heading.FontWeight = FontWeights.SemiBold; heading.Margin = new Thickness(0, 3, 0, 3); parent.Children.Add(heading);
            TextBlock text = Text(body, 12, "Muted"); text.LineHeight = 19; text.Margin = new Thickness(0, 0, 0, 11); parent.Children.Add(text);
        }

        private CheckBox Check(string label, bool selected, Action<bool> change)
        {
            CheckBox box = new CheckBox { Content = Text(label, 13), IsChecked = selected, VerticalContentAlignment = VerticalAlignment.Top };
            box.Checked += delegate { change(true); }; box.Unchecked += delegate { change(false); };
            AutomationProperties.SetName(box, InstallerText.Get(label)); return box;
        }

        private void RenderLocation()
        {
            Header("Где разместим Ремкарту?", "Выберите локальную папку программы. Общая база данных подключается отдельно.");
            bool isDefault = !_customLocation;
            _defaultRadio = Choice("Рекомендуемая папка", "Для текущего пользователя · без прав администратора", isDefault);
            _customRadio = Choice("Другая папка", "Например, D:\\RemCard", !isDefault);
            _page.Children.Add(TextWithMargin("Папка установки", 13, "Text", new Thickness(0, 17, 0, 8)));
            Grid pathRow = new Grid(); pathRow.ColumnDefinitions.Add(new ColumnDefinition()); pathRow.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            _pathBox = new TextBox { Text = _target, FontFamily = new FontFamily("Consolas"), IsReadOnly = isDefault, MinHeight = 42 };
            AutomationProperties.SetName(_pathBox, InstallerText.Get("Папка установки")); _pathBox.TextChanged += delegate { _target = _pathBox.Text; _pathErrorSource = null; if (_pathError != null) _pathError.Visibility = Visibility.Collapsed; }; pathRow.Children.Add(_pathBox);
            Button browse = Button("Обзор  ▱", ShowFolderPicker, false); browse.Margin = new Thickness(8, 0, 0, 0); Grid.SetColumn(browse, 1); pathRow.Children.Add(browse); _page.Children.Add(pathRow);
            _defaultRadio.Checked += delegate { _customLocation = false; _target = DefaultTarget(); _pathBox.Text = _target; _pathBox.IsReadOnly = true; };
            _customRadio.Checked += delegate { _customLocation = true; _pathBox.IsReadOnly = false; _pathBox.Focus(); _pathBox.SelectAll(); };
            _pathError = Text(_pathErrorSource ?? "", 12, "Error"); _pathError.Margin = new Thickness(0, 8, 0, 0); _pathError.Visibility = _pathErrorSource == null ? Visibility.Collapsed : Visibility.Visible; _page.Children.Add(_pathError);
            WrapPanel options = new WrapPanel { Margin = new Thickness(0, 17, 0, 0) };
            CheckBox desktop = Check("Ярлык на рабочем столе", _desktop, delegate(bool value) { _desktop = value; }); desktop.Margin = new Thickness(0, 0, 22, 9); options.Children.Add(desktop);
            options.Children.Add(Check("В меню «Пуск»", _menu, delegate(bool value) { _menu = value; })); _page.Children.Add(options);
            Note("Папка программы должна быть доступна для записи: в неё будут устанавливаться последующие обновления.");
        }

        private TextBlock TextWithMargin(string value, double size, string color, Thickness margin)
        { TextBlock text = Text(value, size, color); text.Margin = margin; return text; }

        private RadioButton Choice(string title, string detail, bool selected)
        {
            StackPanel labels = new StackPanel(); TextBlock heading = Text(title, 13); heading.FontWeight = FontWeights.SemiBold; labels.Children.Add(heading); labels.Children.Add(TextWithMargin(detail, 12, "Muted", new Thickness(0, 4, 0, 0)));
            RadioButton radio = new RadioButton { Content = labels, IsChecked = selected, GroupName = "InstallLocation" };
            Border panel = Box(radio, selected ? "Soft" : "Panel", true, new Thickness(15)); panel.Margin = new Thickness(0, 0, 0, 10);
            radio.Checked += delegate { Color(panel, Border.BackgroundProperty, "Soft"); Color(panel, Border.BorderBrushProperty, "Accent"); };
            radio.Unchecked += delegate { Color(panel, Border.BackgroundProperty, "Panel"); Color(panel, Border.BorderBrushProperty, "Line"); };
            if (selected) Color(panel, Border.BorderBrushProperty, "Accent"); _page.Children.Add(panel); return radio;
        }

        private void RenderReview()
        {
            Header("Всё готово к установке.", "Проверьте параметры. Дальше мы подготовим ваше рабочее пространство.");
            StackPanel summary = new StackPanel();
            SummaryRow(summary, "Приложение", "Ремкарта " + _context.Version, true);
            SummaryRow(summary, "Установка", "Для текущего пользователя", true);
            List<string> shortcuts = new List<string>(); if (_desktop) shortcuts.Add("Рабочий стол"); if (_menu) shortcuts.Add("Меню «Пуск»");
            SummaryRow(summary, "Ярлыки", shortcuts.Count > 0 ? String.Join(", ", shortcuts) : "Без ярлыков", true);
            SummaryRow(summary, "Папка данных", "При первом запуске", false);
            _page.Children.Add(Box(summary, "Panel", true, new Thickness(18, 0, 18, 0))); AddPathSummary("Программа будет установлена в", _target);
            Note("Существующая сетевая база и документы в установку не входят.");
        }

        private void SummaryRow(StackPanel panel, string label, string value, bool line)
        {
            Grid row = new Grid(); row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(145) }); row.ColumnDefinitions.Add(new ColumnDefinition());
            row.Children.Add(Text(label, 13, "Muted")); TextBlock data = Text(value, 13); data.TextAlignment = TextAlignment.Right; Grid.SetColumn(data, 1); row.Children.Add(data);
            Border border = new Border { Padding = new Thickness(0, 13, 0, 13), BorderThickness = new Thickness(0, 0, 0, line ? 1 : 0), Child = row }; Color(border, Border.BorderBrushProperty, "Line"); panel.Children.Add(border);
        }

        private void AddPathSummary(string label, string path)
        {
            StackPanel details = new StackPanel(); details.Children.Add(Text(label, 11, "Muted")); TextBlock location = RawText(path, 12, "Text"); location.FontFamily = new FontFamily("Consolas"); location.Margin = new Thickness(0, 5, 0, 0); details.Children.Add(location);
            Border folder = Box(details, "Soft", false, new Thickness(17, 14, 17, 14)); folder.Margin = new Thickness(0, 18, 0, 0); _page.Children.Add(folder);
        }

        private void RenderProgress()
        {
            Header(_context.UninstallMode ? "Удаляем программу." : "Готовим рабочее место.", _context.UninstallMode ? "Данные и настройки сохраняются." : "Ещё немного — и можно приступать к работе.");
            Grid progressHeading = new Grid { Margin = new Thickness(0, 3, 0, 10) }; progressHeading.ColumnDefinitions.Add(new ColumnDefinition()); progressHeading.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            _phase = Text("Подготовка", 13); _phase.VerticalAlignment = VerticalAlignment.Center; progressHeading.Children.Add(_phase);
            _percentage = Text("0%", 28, "Accent"); Grid.SetColumn(_percentage, 1); progressHeading.Children.Add(_percentage); _page.Children.Add(progressHeading);
            _bar = new ProgressBar { Minimum = 0, Maximum = 100, Value = _percent }; AutomationProperties.SetName(_bar, InstallerText.Get("Ход установки")); _page.Children.Add(_bar);
            _detail = Text("Проверяем готовность пакета", 11, "Muted"); _detail.TextTrimming = TextTrimming.CharacterEllipsis; _detail.TextWrapping = TextWrapping.NoWrap; _detail.Margin = new Thickness(0, 9, 0, 17); _page.Children.Add(_detail);
            Grid logHeading = new Grid { Margin = new Thickness(0, 0, 0, 8) }; logHeading.Children.Add(Text(_context.UninstallMode ? "ЖУРНАЛ УДАЛЕНИЯ" : "ЖУРНАЛ УСТАНОВКИ", 11, "Muted"));
            Button toggle = Link(_journalHidden ? "Показать" : "Скрыть", delegate { _journalHidden = !_journalHidden; _journalScroll.Visibility = _journalHidden ? Visibility.Collapsed : Visibility.Visible; }); toggle.HorizontalAlignment = HorizontalAlignment.Right; toggle.MinHeight = 18;
            toggle.Click += delegate { toggle.Content = InstallerText.Get(_journalHidden ? "Показать" : "Скрыть"); }; logHeading.Children.Add(toggle); _page.Children.Add(logHeading);
            _journal = new StackPanel(); _journalScroll = new ScrollViewer { Content = _journal, VerticalScrollBarVisibility = ScrollBarVisibility.Auto, HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled, Height = Height < 700 ? 112 : 140 };
            _journalScroll.Visibility = _journalHidden ? Visibility.Collapsed : Visibility.Visible;
            foreach (JournalEntry entry in _log.Skip(Math.Max(0, _log.Count - 24))) RenderJournalEntry(entry);
            Border journalBox = Box(_journalScroll, "Well", false, new Thickness(14, 11, 14, 11)); _page.Children.Add(journalBox);
            _ecg = new EcgMonitor(); _ecg.SetResourceReference(EcgMonitor.StrokeBrushProperty, "Accent");
            Border monitor = Box(_ecg, "Well", false, new Thickness(0)); monitor.CornerRadius = new CornerRadius(6); monitor.ClipToBounds = true; monitor.Margin = new Thickness(0, 17, 0, 0); _page.Children.Add(monitor);
            if (_currentProgress != null) ApplyProgressVisuals(_currentProgress);
        }

        private void RenderSuccess()
        {
            Border seal = Box(Logo(49), "Soft", false, new Thickness(12)); seal.Width = 74; seal.Height = 74; seal.CornerRadius = new CornerRadius(23); seal.HorizontalAlignment = HorizontalAlignment.Left; seal.Margin = new Thickness(0, 0, 0, 22); _page.Children.Add(seal);
            Header(_context.UninstallMode ? "Ремкарта удалена." : "Готово к новой смене.", _context.UninstallMode ? "Файлы программы удалены. Пользовательские данные и настройки сохранены." : "Ремкарта установлена. Спасибо, что выбираете инструменты, созданные для работы рядом с пациентом.");
            if (!_context.UninstallMode)
            {
                AddPathSummary("Ваше рабочее пространство", _target);
                TextBlock facts = Text("✓  Файлы подготовлены       ✓  Обновления готовы", 12, "Muted"); facts.Margin = new Thickness(0, 18, 0, 0); _page.Children.Add(facts);
                CheckBox launch = Check("Запустить Ремкарту после завершения", _launchAfter, delegate(bool value) { _launchAfter = value; }); launch.IsEnabled = !_context.SuppressLaunch; launch.Margin = new Thickness(0, 20, 0, 0); _page.Children.Add(launch);
                Note("При первом запуске укажите общую папку данных вашего отделения.");
            }
            else Note("Программу можно установить снова в ту же папку. Настройка подключения к базе будет доступна при следующей установке.");
        }

        private void FooterButton(string label, Action action, bool primary)
        { Button button = Button(label, action, primary); button.Margin = new Thickness(_footerActions.Children.Count == 0 ? 0 : 9, 0, 0, 0); _footerActions.Children.Add(button); _nextButton = primary ? button : _nextButton; }

        private void RenderFooter()
        {
            _footerActions.Children.Clear(); _nextButton = null;
            if (_step == 4)
            {
                _footerHint.Text = InstallerText.Get(_failed ? (_context.UninstallMode ? "Удаление остановлено" : "Установка остановлена") : "Пожалуйста, дождитесь завершения");
                if (_failed)
                {
                    if (!_context.UninstallMode) FooterButton("Другая папка", delegate { _failed = false; _step = 2; Render(); }, false);
                    FooterButton("Повторить  ↻", BeginInstall, true);
                }
                else FooterButton("Отмена", RequestClose, false);
                return;
            }
            if (_step == 5) { _footerHint.Text = InstallerText.Get("Хорошей и спокойной смены!"); FooterButton("Завершить  ✓", Finish, true); return; }
            if (_context.UninstallMode) { _footerHint.Text = InstallerText.Get("Пользовательские данные сохранятся"); FooterButton("Отмена", ExitWindow, false); FooterButton("Удалить Ремкарту", BeginInstall, true); return; }
            string[] hints = { "Настройка за несколько шагов", "Пожалуйста, ознакомьтесь с условиями", "Папка программы на этом компьютере", "Можно вернуться и изменить параметры" };
            _footerHint.Text = InstallerText.Get(hints[Math.Min(3, _step)]);
            if (_step > 0) FooterButton("Назад", delegate { _step--; Render(); }, false);
            FooterButton(_step == 3 ? "Установить  ↓" : "Далее  →", delegate { if (_step == 3) BeginInstall(); else Next(); }, true);
            if (_step == 1) _nextButton.IsEnabled = _accepted;
        }

        private void Next()
        {
            if (_step == 1 && !_accepted) return;
            if (_step == 2)
            {
                try { _target = InstallerEngine.ValidateTarget(Environment.ExpandEnvironmentVariables(_pathBox.Text.Trim())); }
                catch (Exception error) { _pathErrorSource = error.Message; _pathError.Text = InstallerText.Get(error.Message); _pathError.Visibility = Visibility.Visible; return; }
            }
            _step++; Render();
        }

        private async void BeginInstall()
        {
            if (_installing) return;
            if (!_context.UninstallMode && !_accepted) { _step = 1; Render(); return; }
            HideDialog(); _failed = false; _errorCode = null; _currentProgress = null; _percent = 0; _lastPhase = null; _lastLogPercent = -10; _log.Clear();
            _installing = true; _step = 4; _closeAfterCancel = false; _cancel = new CancellationTokenSource(); Render();
            CancellationToken token = _cancel.Token;
            int runId = ++_runId;
            Action<InstallProgress> report = delegate(InstallProgress progress) { ReportProgress(progress, runId); };
            InstallOptions options = new InstallOptions { TargetDirectory = _target, DesktopShortcut = _desktop, StartMenuShortcut = _menu, NoRegistration = _context.NoRegistration, SimulateFailureAfterFiles = _faultInjectionConsumed ? -1 : _context.SimulateFailureAfterFiles };
            _faultInjectionConsumed = true;
            try
            {
                _result = await Task.Run(delegate
                {
                    if (_context.UninstallMode) { InstallerEngine.Uninstall(_context, _target, report, _context.NoRegistration, token); return new InstallResult { InstallDirectory = _target }; }
                    return InstallerEngine.Install(_context, options, report, token);
                });
                _installing = false; _step = 5; Render();
            }
            catch (OperationCanceledException)
            {
                _installing = false;
                if (_closeAfterCancel) { ExitWindow(); return; }
                _step = _context.UninstallMode ? 0 : 3; Render();
                ShowMessage("Операция отменена.", "Можно изменить параметры или попробовать снова.");
            }
            catch (Exception error)
            {
                PresentFailure(error);
            }
            finally { if (_cancel != null) { _cancel.Dispose(); _cancel = null; } }
        }

        private void ReportProgress(InstallProgress progress, int runId)
        {
            if (runId != _runId) return;
            lock (_progressLock)
            {
                _pendingProgress = progress; _pendingRunId = runId;
                if (_progressScheduled) return;
                _progressScheduled = true;
            }
            Dispatcher.BeginInvoke(new Action(delegate
            {
                InstallProgress latest; int generation;
                lock (_progressLock) { latest = _pendingProgress; generation = _pendingRunId; _progressScheduled = false; }
                if (generation != _runId || !_installing || _failed || latest == null || _bar == null) return;
                _currentProgress = latest; ApplyProgressVisuals(latest);
                if (!String.Equals(_lastPhase, latest.Phase, StringComparison.Ordinal) || _percent - _lastLogPercent >= 12)
                { AppendLog(latest.Message, false); _lastPhase = latest.Phase; _lastLogPercent = _percent; }
            }), DispatcherPriority.Background);
        }

        private void ApplyProgressVisuals(InstallProgress latest)
        {
            _percent = Math.Max(0, Math.Min(100, latest.Percent)); _bar.Value = _percent; _percentage.Text = _percent.ToString() + "%";
            _phase.Text = InstallerText.Get(latest.Phase); _detail.Text = InstallerText.Get(latest.Message); _detail.ToolTip = _detail.Text;
        }

        private sealed class JournalEntry
        { public string Time, Message; public bool Error; }
        private void RenderJournalEntry(JournalEntry entry)
        {
            TextBlock line = RawText(entry.Time + (entry.Error ? "  ERR  " : "  OK   ") + InstallerText.Get(entry.Message), 11, entry.Error ? "Error" : "Muted"); line.FontFamily = new FontFamily("Consolas"); line.LineHeight = 18; line.Margin = new Thickness(0, 0, 0, 2); _journal.Children.Add(line);
        }
        private void AppendLog(string message, bool error)
        {
            JournalEntry entry = new JournalEntry { Time = DateTime.Now.ToString("HH:mm:ss"), Message = message, Error = error };
            _log.Add(entry); if (_journal == null) return;
            RenderJournalEntry(entry);
            while (_journal.Children.Count > 24) _journal.Children.RemoveAt(0);
            _journalScroll.ScrollToEnd();
        }

        private void ShowFailure(string message)
        {
            ShowDialog(_context.UninstallMode ? "Удаление прервано." : "Установка прервана.", message, true,
                delegate(StackPanel panel)
                {
                    TextBlock path = RawText(_target, 11, "Muted"); path.FontFamily = new FontFamily("Consolas"); path.TextWrapping = TextWrapping.Wrap; path.ToolTip = _target; panel.Children.Add(Box(path, "Well", false, new Thickness(12, 9, 12, 9)));
                    panel.Children.Add(TextWithMargin(_errorCode, 11, "Error", new Thickness(0, 10, 0, 0)));
                },
                new DialogAction("Показать журнал", delegate { HideDialog(); _journalScroll.Visibility = Visibility.Visible; _journalScroll.ScrollToEnd(); }, false),
                new DialogAction("Повторить  ↻", delegate { HideDialog(); BeginInstall(); }, true));
        }

        private void PresentFailure(Exception error)
        {
            _installing = false; _failed = true;
            InstallFailureException installationError = error as InstallFailureException;
            _errorCode = installationError == null ? "RC-INSTALL-UNEXPECTED" : installationError.Code;
            ApplyFailureState();
            AppendLog(_errorCode + " · " + error.Message, true); RenderFooter(); ShowFailure(error.Message);
        }

        private void ApplyFailureState()
        {
            if (_ecg != null) _ecg.MarkFailed();
            _title.Text = InstallerText.Get(_context.UninstallMode ? "Удаление остановлено." : "Установка остановлена.");
            _lead.Text = InstallerText.Get("Посмотрите причину ошибки и повторите попытку.");
            _phase.Text = InstallerText.Get(_context.UninstallMode ? "Удаление прервано" : "Установка прервана");
            Color(_phase, TextBlock.ForegroundProperty, "Error"); Color(_percentage, TextBlock.ForegroundProperty, "Error"); Color(_bar, Control.ForegroundProperty, "Error");
            _detail.Text = _errorCode; Color(_detail, TextBlock.ForegroundProperty, "Error");
        }

        private void ApplyAutomationPreview()
        {
            if (!_context.NoRegistration || String.IsNullOrWhiteSpace(_context.AutomationPreview)) return;
            if (String.Equals(_context.AutomationPreview, "review", StringComparison.OrdinalIgnoreCase))
            { _step = 3; Render(); return; }
            if (String.Equals(_context.AutomationPreview, "license", StringComparison.OrdinalIgnoreCase))
            { _step = 1; Render(); return; }
            if (String.Equals(_context.AutomationPreview, "location", StringComparison.OrdinalIgnoreCase))
            { _step = 2; Render(); return; }
            if (String.Equals(_context.AutomationPreview, "progress", StringComparison.OrdinalIgnoreCase))
            { _step = 4; _currentProgress = new InstallProgress(58, "Подготовка файлов", "Проверяем готовность пакета"); Render(); return; }
            if (!String.Equals(_context.AutomationPreview, "error", StringComparison.OrdinalIgnoreCase)) return;
            _step = 4; _installing = false; _failed = false; Render();
            _currentProgress = new InstallProgress(58, "Общие компоненты", "Проверка экрана ошибки без записи на диск"); ApplyProgressVisuals(_currentProgress);
            AppendLog("Пакет Ремкарты " + _context.Version + " подготовлен к проверке интерфейса", false);
            AppendLog("Проверка оформления сообщения об ошибке", false);
            Dispatcher.BeginInvoke(new Action(delegate
            {
                PresentFailure(new InstallFailureException("RC-INSTALL-005", "Нет доступа к записи в папку приложения. Проверьте права на выбранную папку или выберите другой каталог."));
            }), DispatcherPriority.ContextIdle);
        }

        private sealed class DialogAction
        {
            public readonly string Label; public readonly Action Action; public readonly bool Primary;
            public DialogAction(string label, Action action, bool primary) { Label = label; Action = action; Primary = primary; }
        }
        private sealed class DialogState
        {
            public string Title, Body; public bool Error; public Action<StackPanel> Extra; public DialogAction[] Actions;
        }

        private void ShowDialog(string title, string body, bool error, Action<StackPanel> extra, params DialogAction[] actions)
        {
            _dialogState = new DialogState { Title = title, Body = body, Error = error, Extra = extra, Actions = actions };
            _previousFocus = Keyboard.FocusedElement; _dialogVisible = true; _overlay.Children.Clear();
            _overlay.Background = error ? Brushes.Transparent : new SolidColorBrush(ColorFrom(_dark ? "#BB020B17" : "#80233443"));
            _dialogContent = new StackPanel(); StackPanel brand = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 0, 0, error ? 14 : 20) }; Image logo = Logo(error ? 23 : 27); logo.Margin = new Thickness(0, 0, 10, 0); brand.Children.Add(logo); brand.Children.Add(Text("РЕМКАРТА", error ? 11 : 12, "Muted")); _dialogContent.Children.Add(brand);
            TextBlock heading = Text(title, error ? 20 : 21); heading.FontWeight = FontWeights.SemiBold; heading.Margin = new Thickness(0, 0, 0, error ? 8 : 10); _dialogContent.Children.Add(heading);
            if (!String.IsNullOrWhiteSpace(body)) { TextBlock message = Text(body, error ? 12 : 13, "Muted"); message.LineHeight = error ? 19 : 21; message.Margin = new Thickness(0, 0, 0, error ? 12 : 18); _dialogContent.Children.Add(message); }
            if (extra != null) extra(_dialogContent);
            WrapPanel buttons = new WrapPanel { HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, error ? 16 : 22, 0, 0) }; Button initial = null;
            foreach (DialogAction action in actions) { DialogAction selected = action; Button button = Button(selected.Label, selected.Action, selected.Primary); button.Margin = new Thickness(8, 4, 0, 0); buttons.Children.Add(button); if (selected.Primary) initial = button; }
            _dialogContent.Children.Add(buttons);
            ScrollViewer dialogScroll = new ScrollViewer { Content = _dialogContent, MaxHeight = Math.Max(280, Height - 170), VerticalScrollBarVisibility = ScrollBarVisibility.Auto, HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled };
            Border dialog = Box(dialogScroll, "Panel", true, new Thickness(error ? 22 : 25)); dialog.CornerRadius = new CornerRadius(17); dialog.Width = Math.Min(500, Math.Max(300, Width - 70)); dialog.HorizontalAlignment = HorizontalAlignment.Center; dialog.VerticalAlignment = VerticalAlignment.Top;
            dialog.Margin = new Thickness(20, 82, 20, 20); dialog.Effect = new DropShadowEffect { BlurRadius = 40, ShadowDepth = 10, Opacity = .34, Color = Colors.Black };
            _overlay.Children.Add(dialog); _overlay.Visibility = Visibility.Visible; _body.IsEnabled = false; _windowActions.IsEnabled = false;
            _dismissDialog = HideDialog;
            if (_ecg != null) _ecg.Paused = !error;
            AutomationProperties.SetName(dialog, InstallerText.Get(title));
            if (initial != null) Dispatcher.BeginInvoke(new Action(delegate { initial.Focus(); }), DispatcherPriority.Input);
        }

        private void HideDialog()
        {
            _dialogVisible = false; _dialogState = null; _overlay.Visibility = Visibility.Collapsed; _overlay.Children.Clear(); _body.IsEnabled = true; _windowActions.IsEnabled = true;
            if (_ecg != null) _ecg.Paused = false;
            _dismissDialog = null;
            if (_previousFocus != null) { try { Keyboard.Focus(_previousFocus); } catch (InvalidOperationException) { } }
        }

        private void ShowMessage(string title, string body)
        { ShowDialog(title, body, false, null, new DialogAction("Понятно", HideDialog, true)); }

        private void ShowLicense()
        {
            ShowDialog("Лицензия Ремкарты", "Полный текст действующей лицензии проекта.", false,
                delegate(StackPanel panel)
                {
                    TextBlock text = RawText(InstallerResources.ReadText("RemCard.LICENSE"), 12, "Muted"); text.LineHeight = 19;
                    ScrollViewer scroll = new ScrollViewer { Content = text, MaxHeight = Math.Max(170, Height - 370), VerticalScrollBarVisibility = ScrollBarVisibility.Auto, HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled, Padding = new Thickness(13) };
                    panel.Children.Add(Box(scroll, "Well", false, new Thickness(0)));
                }, new DialogAction("К условиям", HideDialog, true));
        }

        private void ShowFolderPicker()
        {
            _folderPickerPath = _target;
            TextBox folderPath = null; TextBlock error = null; ListBox folders = null;
            Action refresh = delegate
            {
                folders.Items.Clear(); error.Text = "";
                try
                {
                    string current = Environment.ExpandEnvironmentVariables(folderPath.Text.Trim());
                    if (current.StartsWith("\\\\", StringComparison.Ordinal)) { error.Text = InstallerText.Get("Для программы нужна локальная папка."); return; }
                    if (!Directory.Exists(current)) { error.Text = InstallerText.Get("Эта папка будет создана при установке."); return; }
                    foreach (string child in Directory.GetDirectories(current).OrderBy(delegate(string path) { return path; }, StringComparer.CurrentCultureIgnoreCase).Take(200))
                    { ListBoxItem item = new ListBoxItem { Content = IOPath.GetFileName(child), Tag = child }; folders.Items.Add(item); }
                }
                catch (Exception exception) { error.Text = InstallerText.Get(exception.Message); }
            };
            ShowDialog("Выберите папку", "Укажите место для программы. Общая папка базы данных выбирается после установки.", false,
                delegate(StackPanel panel)
                {
                    folderPath = new TextBox { Text = _folderPickerPath, FontFamily = new FontFamily("Consolas"), Margin = new Thickness(0, 0, 0, 10) }; folderPath.TextChanged += delegate { _folderPickerPath = folderPath.Text; }; AutomationProperties.SetName(folderPath, InstallerText.Get("Путь к папке программы")); panel.Children.Add(folderPath);
                    WrapPanel presets = new WrapPanel();
                    Button personal = Button("Личная папка", delegate { folderPath.Text = DefaultTarget(); refresh(); }, false); personal.Margin = new Thickness(0, 0, 8, 8); presets.Children.Add(personal);
                    Button up = Button("На уровень выше", delegate { try { DirectoryInfo parent = Directory.GetParent(folderPath.Text.TrimEnd('\\')); if (parent != null) { folderPath.Text = parent.FullName; refresh(); } } catch (Exception exception) { error.Text = InstallerText.Get(exception.Message); } }, false); up.Margin = new Thickness(0, 0, 0, 8); presets.Children.Add(up); panel.Children.Add(presets);
                    folders = new ListBox { Height = Math.Max(90, Math.Min(150, Height - 540)), FontSize = 12 }; panel.Children.Add(folders);
                    folders.MouseDoubleClick += delegate { ListBoxItem selected = folders.SelectedItem as ListBoxItem; if (selected != null) { folderPath.Text = (string)selected.Tag; refresh(); } };
                    folderPath.PreviewKeyDown += delegate(object sender, KeyEventArgs args) { if (args.Key == Key.Enter) { refresh(); args.Handled = true; } };
                    error = Text("", 11, "Muted"); error.Margin = new Thickness(0, 9, 0, 0); panel.Children.Add(error); refresh();
                },
                new DialogAction("Отмена", HideDialog, false),
                new DialogAction("Выбрать папку", delegate
                {
                    try { _target = InstallerEngine.ValidateTarget(Environment.ExpandEnvironmentVariables(folderPath.Text.Trim())); _customLocation = true; HideDialog(); _step = 2; Render(); }
                    catch (Exception exception) { error.Text = InstallerText.Get(exception.Message); Color(error, TextBlock.ForegroundProperty, "Error"); }
                }, true));
        }

        private void RequestClose()
        {
            if (_dialogVisible) return;
            if (_step == 5) { ExitWindow(); return; }
            if (_installing)
            {
                ShowDialog("Остановить операцию?", "Дождитесь остановки и завершения восстановления файлов. Не закрывайте установщик принудительно.", false, null,
                    new DialogAction("Остановить", delegate { HideDialog(); _closeAfterCancel = true; _footerHint.Text = InstallerText.Get("Остановка операции…"); if (_cancel != null) _cancel.Cancel(); }, false),
                    new DialogAction("Продолжить", HideDialog, true));
            }
            else ShowDialog("Закрыть установщик?", "Вы можете вернуться к настройке в любой момент.", false, null, new DialogAction("Выйти", ExitWindow, false), new DialogAction("Остаться", HideDialog, true));
        }

        private void OnWindowClosing(object sender, CancelEventArgs args)
        {
            if (_allowClose) return;
            args.Cancel = true;
            if (_dialogVisible) HideDialog(); else RequestClose();
        }

        private void OnKeyDown(object sender, KeyEventArgs args)
        {
            if (args.Key == Key.Escape) { args.Handled = true; if (_dialogVisible && _dismissDialog != null) _dismissDialog(); else RequestClose(); }
        }

        private void ExitWindow() { _allowClose = true; Close(); }

        private void Finish()
        {
            if (!_context.UninstallMode && _launchAfter && !_context.SuppressLaunch && _result != null)
            {
                try { Process.Start(new ProcessStartInfo(_result.ExecutablePath) { WorkingDirectory = _result.InstallDirectory, UseShellExecute = true }); }
                catch (Exception error) { ShowDialog("Не удалось открыть Ремкарту.", error.Message, false, null, new DialogAction("Готово", ExitWindow, true)); return; }
            }
            ExitWindow();
        }

        private void OpenLink(string url)
        { try { Process.Start(new ProcessStartInfo(url) { UseShellExecute = true }); } catch (Exception error) { ShowMessage("Не удалось открыть ссылку.", error.Message); } }

        private static Color ColorFrom(string value) { return (Color)ColorConverter.ConvertFromString(value); }
    }
}
