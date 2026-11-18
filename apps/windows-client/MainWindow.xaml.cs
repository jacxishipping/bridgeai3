using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Forms;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Media.Imaging;
using System.Windows.Shapes;
using System.Windows.Threading;
using AIBridgeClient.Models;
using AIBridgeClient.Services;
using AIBridgeClient.Themes;

namespace AIBridgeClient;

public partial class MainWindow : Window
{
    private sealed record PaletteCommand(string Label, Action Execute);

    private readonly ConfigService _configService = new();
    private readonly SystemMonitorService _monitor = new();
    private readonly NativeToolExecutor _executor;
    private readonly ControlPlaneClient _client;

    private AgentGuiConfig _config;
    private string _deviceToken;
    private List<TaskHistoryRecord> _taskHistory;
    private readonly List<LogEntry> _logBuffer = new();
    private readonly ObservableCollection<LogEntry> _visibleLogs = new();
    private readonly ObservableCollection<CaptureHistoryItem> _captureHistory = new();
    private readonly ObservableCollection<string> _policyAuditHistory = new();
    private readonly ObservableCollection<string> _deviceTags = new();

    private string _logFilter = "ALL";
    private string _taskFilter = "all";
    private int _taskPage = 1;
    private const int TaskPageSize = 12;
    private string _lastScreenshotPath = "";
    private int _visionZoomMode; // 0=Fit, 1=100%, 2=150%
    private bool? _sidebarManualCompact;
    private bool _tokenVisible;
    private bool _allowRealClose;
    private bool _consoleExpanded;
    private Storyboard? _pulseStoryboard;

    private readonly Queue<double> _cpuHistory = new();
    private readonly Queue<double> _memHistory = new();
    private const int SparklineCapacity = 60;

    private readonly DispatcherTimer _monitorTimer;
    private readonly DispatcherTimer _toastTimer;
    private NotifyIcon? _trayIcon;
    private readonly System.Windows.Controls.Button[] _navButtons;
    private readonly TextBlock[] _navIcons;
    private readonly List<PaletteCommand> _paletteCommands = new();
    private TaskCompletionSource<bool>? _pendingApprovalTcs;

    [DllImport("dwmapi.dll", PreserveSig = true)]
    private static extern int DwmSetWindowAttribute(IntPtr hwnd, int attr, ref int attrValue, int attrSize);

    private static readonly (string Title, string Subtitle)[] TabMeta =
    {
        ("Overview", "Agent status, connection, and live activity"),
        ("Task Explorer", "Every task this device received, with full payloads"),
        ("System Monitor", "Live hardware telemetry, GPU & process manager"),
        ("Desktop Vision", "Multi-monitor Windows GDI+ desktop screenshot capture"),
        ("Security & Policy", "4-tier guardrails, allowlist matrix & policy audit"),
        ("Settings", "Control Plane endpoint, DPAPI token, updates & appearance"),
    };

    public MainWindow(int initialTab = 0, bool suppressAutoSignIn = false)
    {
        InitializeComponent();

        _navButtons = new[] { NavOverviewBtn, NavTasksBtn, NavMonitorBtn, NavVisionBtn, NavSecurityBtn, NavSettingsBtn };
        _navIcons = new[] { NavIcon0, NavIcon1, NavIcon2, NavIcon3, NavIcon4, NavIcon5 };

        ActivityLogList.ItemsSource = _visibleLogs;
        CaptureHistoryList.ItemsSource = _captureHistory;
        PolicyAuditList.ItemsSource = _policyAuditHistory;
        SetTagsItems.ItemsSource = _deviceTags;

        _config = _configService.LoadConfig();
        _deviceToken = _configService.LoadToken();
        _taskHistory = _configService.LoadTaskHistory();
        foreach (var entry in _configService.LoadPolicyAudit())
        {
            _policyAuditHistory.Add(entry);
        }

        if (_config.WindowWidth >= MinWidth && _config.WindowHeight >= MinHeight)
        {
            Width = _config.WindowWidth;
            Height = _config.WindowHeight;
        }
        if (_config.WindowMaximized)
        {
            WindowState = WindowState.Maximized;
        }

        var screenshotDir = _config.ScreenshotDirectory;
        _executor = new NativeToolExecutor(_monitor, screenshotDir);
        _client = new ControlPlaneClient(_monitor, _executor);

        _client.LogMessage += (level, msg) => Dispatcher.Invoke(() => AppendLog(level, msg));
        _client.StatusChanged += (status, tone) => Dispatcher.Invoke(() => UpdateConnectionStatus(status, tone));
        _client.TaskRecorded += rec => Dispatcher.Invoke(() => RecordTask(rec));
        _client.EmergencyStopChanged += active => Dispatcher.Invoke(() =>
        {
            EmergencyBanner.Visibility = active ? Visibility.Visible : Visibility.Collapsed;
            if (active)
            {
                _trayIcon?.ShowBalloonTip(3000, "AI Bridge — Emergency All-Stop", "Remote task execution halted by Control Plane.", ToolTipIcon.Warning);
            }
        });
        _client.LocalApprovalRequested = RequestLocalApprovalAsync;

        _monitorTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(Math.Clamp(_config.MonitorIntervalMs, 500, 5000)) };
        _monitorTimer.Tick += (_, _) => RefreshSystemMonitor();

        _toastTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(2.5) };
        _toastTimer.Tick += (_, _) => { ToastBanner.Visibility = Visibility.Collapsed; _toastTimer.Stop(); };

        BuildPaletteCommands();
        SetupSystemTray();
        ApplyTheme(_config.Theme);
        ApplyRuntimeSettings();
        LoadHardwareInfo();
        LoadConfigIntoUi();
        RefreshMonitorList();
        LoadSavedCaptureHistory(screenshotDir);
        RefreshTaskGrid();
        RefreshProcessesGrid();
        RefreshSystemMonitor();
        ApplyResponsiveLayout(Width);
        int startTab = suppressAutoSignIn
            ? (initialTab >= 0 && initialTab < TabMeta.Length ? initialTab : 0)
            : (_config.LastTab > 0 && _config.LastTab < TabMeta.Length ? _config.LastTab : 0);
        if (startTab > 0)
        {
            SelectTab(startTab);
        }
        AppendLog("info", "AI Bridge Native C# Client initialized.");

        if (_config.AutoConnect && !string.IsNullOrWhiteSpace(_deviceToken))
        {
            _client.Start(_config, _deviceToken);
            SetAgentButtonState(running: true, paused: false);
        }
        if (_config.AutoCheckUpdates)
        {
            OnCheckUpdatesClick(this, new RoutedEventArgs());
        }

        if (!suppressAutoSignIn && string.IsNullOrWhiteSpace(_deviceToken))
        {
            ShowSignInPage(accountMode: true);
        }
    }

    protected override void OnSourceInitialized(EventArgs e)
    {
        base.OnSourceInitialized(e);
        UpdateDwmWindowChrome();
    }

    private void UpdateDwmWindowChrome()
    {
        var hwnd = new WindowInteropHelper(this).Handle;
        if (hwnd == IntPtr.Zero) return;
        int dark = string.Equals(ThemeManager.ResolveEffectiveTheme(_config.Theme), "light", StringComparison.OrdinalIgnoreCase) ? 0 : 1;
        DwmSetWindowAttribute(hwnd, 20, ref dark, sizeof(int));
        int round = 2;
        DwmSetWindowAttribute(hwnd, 33, ref round, sizeof(int));
    }

    private void ApplyRuntimeSettings()
    {
        NamedPipeBridge.ActivePipeName = string.IsNullOrWhiteSpace(_config.DesktopPipeName)
            ? NamedPipeBridge.PipeName
            : _config.DesktopPipeName.Trim();
        var hmac = _configService.LoadPipeHmacSecret();
        NamedPipeBridge.ActiveHmacSecret = string.IsNullOrWhiteSpace(hmac) ? null : hmac;

        _monitorTimer.Interval = TimeSpan.FromMilliseconds(Math.Clamp(_config.MonitorIntervalMs, 500, 5000));
        if (_config.AutoRefreshMonitor)
        {
            if (!_monitorTimer.IsEnabled) _monitorTimer.Start();
        }
        else if (_monitorTimer.IsEnabled)
        {
            _monitorTimer.Stop();
        }

        if (LogAutoScrollChk is not null)
        {
            LogAutoScrollChk.IsChecked = _config.AutoScrollLogs;
        }
        _sidebarManualCompact = _config.CompactSidebar ? true : null;
    }

    private void OnWindowSizeChanged(object sender, SizeChangedEventArgs e) => ApplyResponsiveLayout(e.NewSize.Width);

    private void OnToggleSidebarClick(object sender, RoutedEventArgs e)
    {
        bool currentlyCompact = SidebarColumn.Width.Value < 120;
        _sidebarManualCompact = !currentlyCompact;
        ApplyResponsiveLayout(ActualWidth > 0 ? ActualWidth : Width);
    }

    private void ApplyResponsiveLayout(double width)
    {
        if (width <= 0) return;

        bool compactSidebar = _sidebarManualCompact ?? (width < 900);
        SidebarColumn.Width = new GridLength(compactSidebar ? 68 : 224);
        var labelVis = compactSidebar ? Visibility.Collapsed : Visibility.Visible;
        SidebarBrandText.Visibility = labelVis;
        SidebarLinkTitle.Visibility = labelVis;
        SidebarLinkSub.Visibility = labelVis;
        SidebarStatusText.Visibility = labelVis;
        if (SidebarFooterVersion is not null) SidebarFooterVersion.Visibility = labelVis;
        NavGroupOpsText.Visibility = labelVis;
        NavGroupGovText.Visibility = labelVis;
        NavLblOverview.Visibility = labelVis;
        NavLblTasks.Visibility = labelVis;
        NavLblMonitor.Visibility = labelVis;
        NavLblVision.Visibility = labelVis;
        NavLblSecurity.Visibility = labelVis;
        NavLblSettings.Visibility = labelVis;

        if (width < 980)
        {
            Grid.SetRow(HeaderActionsWrap, 1);
            Grid.SetColumn(HeaderActionsWrap, 0);
            Grid.SetColumnSpan(HeaderActionsWrap, 2);
            HeaderActionsWrap.HorizontalAlignment = System.Windows.HorizontalAlignment.Left;
            HeaderActionsWrap.Margin = new Thickness(0, 10, 0, 0);
        }
        else
        {
            Grid.SetRow(HeaderActionsWrap, 0);
            Grid.SetColumn(HeaderActionsWrap, 1);
            Grid.SetColumnSpan(HeaderActionsWrap, 1);
            HeaderActionsWrap.HorizontalAlignment = System.Windows.HorizontalAlignment.Right;
            HeaderActionsWrap.Margin = new Thickness(0);
        }

        OverviewKpiGrid.Columns = width >= 1060 ? 4 : (width >= 720 ? 2 : 1);

        if (width < 960)
        {
            OverviewCpuCol.Width = new GridLength(0);
            Grid.SetRow(CpuTelemetryCard, 1);
            Grid.SetColumn(CpuTelemetryCard, 0);
            EndpointCard.Margin = new Thickness(0, 0, 0, 12);
        }
        else
        {
            OverviewCpuCol.Width = new GridLength(5, GridUnitType.Star);
            Grid.SetRow(CpuTelemetryCard, 0);
            Grid.SetColumn(CpuTelemetryCard, 1);
            EndpointCard.Margin = new Thickness(0, 0, 14, 0);
        }

        MonitorSparkGrid.Columns = width >= 920 ? 2 : 1;
        MonitorStatsGrid.Columns = width >= 1020 ? 3 : 1;
        SecurityPostureGrid.Columns = width >= 1020 ? 4 : (width >= 720 ? 2 : 1);
        SecurityBottomGrid.Columns = width >= 1080 ? 3 : 1;
        VisionHistoryCol.Width = new GridLength(width >= 900 ? 3 : 0, width >= 900 ? GridUnitType.Star : GridUnitType.Pixel);
        VisionHistoryCard.Visibility = width >= 900 ? Visibility.Visible : Visibility.Collapsed;
    }

    private void ShowToast(string message)
    {
        ToastText.Text = message;
        ToastBanner.Visibility = Visibility.Visible;
        _toastTimer.Stop();
        _toastTimer.Start();
    }

    private void LoadHardwareInfo()
    {
        var hw = _monitor.GetHardwareInfo();
        var (product, displayVer, build) = DiagnosticsService.GetWindowsEditionAndBuild();
        var verLabel = string.IsNullOrWhiteSpace(displayVer) ? $"Build {build}" : $"{displayVer} (Build {build})";
        MonGpuText.Text = hw.GpuName;
        MonCpuModelText.Text = $"{hw.CpuName} ({hw.LogicalCores} threads) · {product} {verLabel}";
    }

    private void BuildPaletteCommands()
    {
        _paletteCommands.Clear();
        _paletteCommands.AddRange(new[]
        {
            new PaletteCommand("Overview", () => SelectTab(0)),
            new PaletteCommand("Task Explorer", () => SelectTab(1)),
            new PaletteCommand("System Monitor", () => SelectTab(2)),
            new PaletteCommand("Desktop Vision", () => SelectTab(3)),
            new PaletteCommand("Security & Policy", () => SelectTab(4)),
            new PaletteCommand("Settings", () => SelectTab(5)),
            new PaletteCommand("Action: Start / Stop Agent", () => OnToggleAgentClick(this, new RoutedEventArgs())),
            new PaletteCommand("Action: Pause / Resume", () => OnTogglePauseClick(this, new RoutedEventArgs())),
            new PaletteCommand("Action: Ping Control Plane", () => OnPingClick(this, new RoutedEventArgs())),
            new PaletteCommand("Action: Capture Screenshot", () => { SelectTab(3); OnCaptureScreenshotClick(this, new RoutedEventArgs()); }),
            new PaletteCommand("Action: Open Workstation Inspector Drawer", () => OpenNodeDrawer()),
            new PaletteCommand("Action: Expand / Collapse Console", () => { SelectTab(0); OnToggleExpandConsoleClick(this, new RoutedEventArgs()); }),
            new PaletteCommand("Action: Copy Diagnostics Report", () => OnCopyDiagnosticsClick(this, new RoutedEventArgs())),
            new PaletteCommand("Action: Toggle Light / Dark Theme", () => OnToggleThemeClick(this, new RoutedEventArgs())),
        });
    }

    private void OnWindowPreviewKeyDown(object sender, System.Windows.Input.KeyEventArgs e)
    {
        if (e.Key == Key.K && (Keyboard.Modifiers & ModifierKeys.Control) == ModifierKeys.Control)
        {
            e.Handled = true;
            OpenCommandPalette();
        }
        else if (e.Key == Key.Escape && CommandPaletteOverlay.Visibility == Visibility.Visible)
        {
            e.Handled = true;
            CommandPaletteOverlay.Visibility = Visibility.Collapsed;
        }
    }

    private void OnOpenCommandPaletteClick(object sender, RoutedEventArgs e) => OpenCommandPalette();
    private void OnCloseCommandPaletteClick(object sender, RoutedEventArgs e) => CommandPaletteOverlay.Visibility = Visibility.Collapsed;

    private void OpenCommandPalette()
    {
        CommandSearchBox.Text = "";
        CommandResultsList.ItemsSource = _paletteCommands;
        CommandResultsList.SelectedIndex = 0;
        CommandPaletteOverlay.Visibility = Visibility.Visible;
        CommandSearchBox.Focus();
    }

    private void OnOverlayBackdropClick(object sender, MouseButtonEventArgs e)
    {
        if (e.OriginalSource == CommandPaletteOverlay)
        {
            CommandPaletteOverlay.Visibility = Visibility.Collapsed;
        }
    }

    private void OnCommandSearchChanged(object sender, TextChangedEventArgs e)
    {
        var q = CommandSearchBox.Text.Trim();
        var filtered = string.IsNullOrEmpty(q)
            ? _paletteCommands
            : _paletteCommands.Where(c => c.Label.Contains(q, StringComparison.OrdinalIgnoreCase)).ToList();
        CommandResultsList.ItemsSource = filtered;
        if (filtered.Count > 0) CommandResultsList.SelectedIndex = 0;
    }

    private void OnCommandBoxKeyDown(object sender, System.Windows.Input.KeyEventArgs e)
    {
        if (e.Key == Key.Down && CommandResultsList.SelectedIndex < CommandResultsList.Items.Count - 1)
        {
            CommandResultsList.SelectedIndex++;
            e.Handled = true;
        }
        else if (e.Key == Key.Up && CommandResultsList.SelectedIndex > 0)
        {
            CommandResultsList.SelectedIndex--;
            e.Handled = true;
        }
        else if (e.Key == Key.Enter && CommandResultsList.SelectedItem is PaletteCommand cmd)
        {
            CommandPaletteOverlay.Visibility = Visibility.Collapsed;
            cmd.Execute();
            e.Handled = true;
        }
    }

    private void OnCommandItemDoubleClick(object sender, MouseButtonEventArgs e)
    {
        if (CommandResultsList.SelectedItem is PaletteCommand cmd)
        {
            CommandPaletteOverlay.Visibility = Visibility.Collapsed;
            cmd.Execute();
        }
    }

    private void OnToggleThemeClick(object sender, RoutedEventArgs e)
    {
        _config.Theme = string.Equals(_config.Theme, "light", StringComparison.OrdinalIgnoreCase) ? "dark" : "light";
        _configService.SaveConfig(_config);
        ApplyTheme(_config.Theme);
        UpdateDwmWindowChrome();
        ShowToast($"Switched to {_config.Theme} mode");
    }

    private void ApplyTheme(string theme)
    {
        ThemeManager.ApplyTheme(theme);
        bool isLight = string.Equals(ThemeManager.ResolveEffectiveTheme(theme), "light", StringComparison.OrdinalIgnoreCase);
        var icon = isLight ? "\uE706" : "\uE708";
        ThemeToggleIcon.Text = icon;
        if (SignInThemeIcon is not null) SignInThemeIcon.Text = icon;
        if (MainTabs.SelectedIndex >= 0)
        {
            SelectTab(MainTabs.SelectedIndex);
        }
    }

    private void OnProfileMenuClick(object sender, RoutedEventArgs e)
    {
        if (ProfileContextMenu is not null)
        {
            ProfileContextMenu.PlacementTarget = ProfileMenuBtn;
            ProfileContextMenu.IsOpen = true;
        }
    }

    private void OnGoToSettingsMenuClick(object sender, RoutedEventArgs e) => SelectTab(5);
    private void OnOpenNodeDrawerMenuClick(object sender, RoutedEventArgs e) => OpenNodeDrawer();
    private void OnOpenNodeDrawerClick(object sender, MouseButtonEventArgs e) => OpenNodeDrawer();

    private void OpenNodeDrawer()
    {
        var (product, displayVer, build) = DiagnosticsService.GetWindowsEditionAndBuild();
        var hw = _monitor.GetHardwareInfo();
        var t = _monitor.Sample();
        var tagsText = _config.Tags is { Count: > 0 } ? string.Join(", ", _config.Tags) : "none";
        var summary =
            $"Workstation: {Environment.MachineName} ({_config.DeviceId})\n" +
            $"User:        {(_config.SignedInUser is { Length: > 0 } u ? u : Environment.UserName)}\n" +
            $"Tenant:      {_config.TenantId}\n" +
            $"Status:      {SidebarStatusText.Text}\n" +
            $"Endpoint:    {_config.ControlUrl}\n" +
            $"Tags:        {tagsText}\n\n" +
            $"OS:          {product} {displayVer} (Build {build})\n" +
            $"CPU:         {hw.CpuName} ({hw.LogicalCores} threads)\n" +
            $"GPU:         {hw.GpuName}\n" +
            $"Memory:      {t.MemoryUsedGb:F1} / {t.MemoryTotalGb:F1} GB ({t.MemoryPercent:F0}%)\n" +
            $"Disk:        {t.DiskUsedGb:F1} / {t.DiskTotalGb:F1} GB ({t.DiskPercent:F1}%)\n" +
            $"C++ Pipe:    {(NamedPipeBridge.IsDesktopAgentAvailable(_config.DesktopPipeName) ? "Connected" : "Offline")}\n" +
            $"Token Vault: {(string.IsNullOrWhiteSpace(_deviceToken) ? "Not enrolled" : "DPAPI Sealed")}";
        OpenDrawer("Workstation Inspector", $"{_config.DeviceId} · {_config.TenantId}", summary);
    }

    private void OpenDrawer(string title, string subtitle, string detailText)
    {
        DrawerTitleText.Text = title;
        DrawerSubtitleText.Text = subtitle;
        DrawerDetailBox.Text = detailText;
        InspectorDrawerOverlay.Visibility = Visibility.Visible;

        DrawerSlideTransform.X = 430;
        var anim = new DoubleAnimation(430, 0, TimeSpan.FromMilliseconds(220))
        {
            EasingFunction = new CubicEase { EasingMode = EasingMode.EaseOut },
        };
        DrawerSlideTransform.BeginAnimation(TranslateTransform.XProperty, anim);
    }

    private void OnCloseDrawerClick(object sender, RoutedEventArgs e) =>
        InspectorDrawerOverlay.Visibility = Visibility.Collapsed;

    private void OnDrawerBackdropClick(object sender, MouseButtonEventArgs e)
    {
        if (e.OriginalSource == InspectorDrawerOverlay)
        {
            InspectorDrawerOverlay.Visibility = Visibility.Collapsed;
        }
    }

    private void OnCopyDrawerDetailClick(object sender, RoutedEventArgs e)
    {
        if (!string.IsNullOrWhiteSpace(DrawerDetailBox.Text))
        {
            System.Windows.Clipboard.SetText(DrawerDetailBox.Text);
            ShowToast("Copied inspector details");
        }
    }
    private void OnCardGoToSettings(object sender, MouseButtonEventArgs e) => SelectTab(5);
    private void OnCardGoToTasks(object sender, MouseButtonEventArgs e) => SelectTab(1);
    private void OnCardGoToSecurity(object sender, MouseButtonEventArgs e) => SelectTab(4);

    private void OnMinimizeWindowClick(object sender, RoutedEventArgs e) => WindowState = WindowState.Minimized;
    private void OnMaximizeRestoreClick(object sender, RoutedEventArgs e) =>
        WindowState = WindowState == WindowState.Maximized ? WindowState.Normal : WindowState.Maximized;
    private void OnCloseToTrayClick(object sender, RoutedEventArgs e) => Close();

    private void SetupSystemTray()
    {
        var menu = new ContextMenuStrip();
        menu.Items.Add("Show Window", null, (_, _) => Dispatcher.Invoke(ShowFromTray));
        menu.Items.Add("Start / Stop Agent", null, (_, _) => Dispatcher.Invoke(() => OnToggleAgentClick(this, new RoutedEventArgs())));
        menu.Items.Add("Pause / Resume", null, (_, _) => Dispatcher.Invoke(() => OnTogglePauseClick(this, new RoutedEventArgs())));
        menu.Items.Add("Ping Control Plane", null, (_, _) => Dispatcher.Invoke(() => OnPingClick(this, new RoutedEventArgs())));
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("Capture Screenshot", null, (_, _) => Dispatcher.Invoke(() => { SelectTab(3); OnCaptureScreenshotClick(this, new RoutedEventArgs()); }));
        menu.Items.Add("Toggle Light / Dark Theme", null, (_, _) => Dispatcher.Invoke(() => OnToggleThemeClick(this, new RoutedEventArgs())));
        menu.Items.Add("Open Logs Folder", null, (_, _) => Dispatcher.Invoke(() => OnOpenLogsFolderClick(this, new RoutedEventArgs())));
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("Exit", null, (_, _) => Dispatcher.Invoke(ExitApplication));

        _trayIcon = new NotifyIcon
        {
            Text = "AI Bridge Windows Client",
            Icon = TrayIconGenerator.CreateStatusIcon("muted"),
            Visible = true,
            ContextMenuStrip = menu,
        };
        _trayIcon.DoubleClick += (_, _) => Dispatcher.Invoke(ShowFromTray);
    }

    private void ShowFromTray()
    {
        Show();
        if (WindowState == WindowState.Minimized)
        {
            WindowState = WindowState.Normal;
        }
        Activate();
    }

    private void PersistWindowBounds()
    {
        _config.WindowMaximized = WindowState == WindowState.Maximized;
        if (WindowState == WindowState.Normal)
        {
            _config.WindowWidth = ActualWidth > 0 ? ActualWidth : Width;
            _config.WindowHeight = ActualHeight > 0 ? ActualHeight : Height;
        }
        _config.LastTab = MainTabs.SelectedIndex >= 0 ? MainTabs.SelectedIndex : 0;
        _configService.SaveConfig(_config);
    }

    protected override void OnClosing(System.ComponentModel.CancelEventArgs e)
    {
        PersistWindowBounds();
        if (!_allowRealClose && _config.MinimizeToTray)
        {
            e.Cancel = true;
            Hide();
            if (_config.NotificationsEnabled)
            {
                _trayIcon?.ShowBalloonTip(2000, "AI Bridge", "Client minimized to system tray.", ToolTipIcon.Info);
            }
            return;
        }
        if (!_allowRealClose)
        {
            e.Cancel = true;
            ExitApplication();
            return;
        }
        base.OnClosing(e);
    }

    private async void ExitApplication()
    {
        _allowRealClose = true;
        PersistWindowBounds();
        _monitorTimer.Stop();
        await _client.StopAsync();
        _client.Dispose();
        if (_trayIcon is not null)
        {
            _trayIcon.Visible = false;
            _trayIcon.Dispose();
        }
        System.Windows.Application.Current.Shutdown();
    }

    private void OnNavClick(object sender, RoutedEventArgs e)
    {
        if (sender is System.Windows.Controls.Button btn && int.TryParse(btn.Tag?.ToString(), out var idx))
        {
            SelectTab(idx);
        }
    }

    private void SelectTab(int idx)
    {
        MainTabs.SelectedIndex = idx;
        if (idx >= 0 && idx < TabMeta.Length)
        {
            PageTitleText.Text = TabMeta[idx].Title;
            PageSubtitleText.Text = TabMeta[idx].Subtitle;
            BreadcrumbPageText.Text = TabMeta[idx].Title;
        }

        var activeStyle = (Style)FindResource("NavButtonActiveStyle");
        var normalStyle = (Style)FindResource("NavButtonStyle");
        var activeAccent = (System.Windows.Media.Brush)FindResource("NavActiveAccent");
        var mutedBrush = (System.Windows.Media.Brush)FindResource("TextSecondary");

        for (int i = 0; i < _navButtons.Length; i++)
        {
            _navButtons[i].Style = i == idx ? activeStyle : normalStyle;
            _navIcons[i].Foreground = i == idx ? activeAccent : mutedBrush;
        }
    }

    private void LoadConfigIntoUi()
    {
        var label = !string.IsNullOrWhiteSpace(_config.SignedInUser)
            ? _config.SignedInUser.Trim()
            : (string.IsNullOrWhiteSpace(_config.DeviceId) ? Environment.MachineName : _config.DeviceId);
        ProfileInitialsText.Text = label.Length >= 2 ? label[..2].ToUpperInvariant() : "WI";
        OverviewEndpointText.Text = _config.ControlUrl;
        KpiTasksText.Text = _taskHistory.Count.ToString();
        LoadPolicyIntoUi();
        LoadSettingsIntoUi();
    }

    private void LoadPolicyIntoUi()
    {
        PolAllowWritesChk.IsChecked = _config.Policy.AllowWrites;
        PolAllowTier3Chk.IsChecked = _config.Policy.AllowTier3;
        PolAllowTier4Chk.IsChecked = _config.Policy.AllowTier4;
        PolAllowShellChk.IsChecked = _config.Policy.AllowShell;
        PolAllowDesktopChk.IsChecked = _config.Policy.AllowDesktopControl;
        PolAllowPackageInstallChk.IsChecked = _config.Policy.AllowPackageInstall;
        PolAllowClipboardChk.IsChecked = _config.Policy.AllowClipboard;
        PolRequireLocalApprovalChk.IsChecked = _config.Policy.RequireLocalApproval;

        int maxTierIdx = Math.Clamp(_config.Policy.MaxTierUnattended, 1, 4) - 1;
        PolMaxTierCombo.SelectedIndex = maxTierIdx;

        AllowedAppsList.Items.Clear();
        foreach (var app in _config.Policy.AllowedApps)
        {
            AllowedAppsList.Items.Add(app);
        }

        AllowedCommandsList.Items.Clear();
        foreach (var cmd in _config.Policy.AllowedCommands)
        {
            AllowedCommandsList.Items.Add(cmd);
        }
        UpdateSecurityPostureSummary();
    }

    private void UpdateSecurityPostureSummary()
    {
        bool lockedDown = !_config.Policy.AllowWrites && !_config.Policy.AllowTier3 && !_config.Policy.AllowTier4;
        SecModeSummaryText.Text = lockedDown
            ? "Lockdown (Read-Only)"
            : $"Unattended ≤ Tier {Math.Clamp(_config.Policy.MaxTierUnattended, 1, 4)}";
        SecLocalApprovalText.Text = _config.Policy.RequireLocalApproval ? "Required" : "Off";
        SecAllowedCountText.Text = $"{_config.Policy.AllowedApps.Count} apps · {_config.Policy.AllowedCommands.Count} cmds";
    }

    private void LoadSettingsIntoUi()
    {
        // General
        SetWorkspaceBox.Text = _config.Workspace;
        SetScreenshotDirBox.Text = _config.ScreenshotDirectory;
        SetAutoStartChk.IsChecked = _configService.IsAutoStartEnabled();
        SetMinimizeToTrayChk.IsChecked = _config.MinimizeToTray;
        SetNotificationsChk.IsChecked = _config.NotificationsEnabled;
        SetConfirmDestructiveChk.IsChecked = _config.ConfirmDestructiveActions;

        // Control Plane
        SetControlUrlBox.Text = _config.ControlUrl;
        SetDeviceIdBox.Text = _config.DeviceId;
        SetTenantIdBox.Text = _config.TenantId;
        SetPollIntervalBox.Text = _config.PollIntervalSeconds.ToString();
        SetHeartbeatIntervalBox.Text = _config.HeartbeatIntervalSeconds.ToString();
        SetHttpTimeoutBox.Text = _config.HttpTimeoutSeconds.ToString();
        SetWsReconnectBox.Text = _config.WebSocketReconnectSeconds.ToString();
        SetAutoConnectChk.IsChecked = _config.AutoConnect;
        SetWsEnabledChk.IsChecked = _config.WebSocketEnabled;

        // Agent Execution & Telemetry
        SetDefaultTimeoutBox.Text = _config.DefaultTimeoutSeconds.ToString();
        SetMaxTimeoutBox.Text = _config.MaxTimeoutSeconds.ToString();
        SetMonitorIntervalBox.Text = _config.MonitorIntervalMs.ToString();
        SetMaxReadBytesBox.Text = _config.MaxReadBytes.ToString();
        SetMaxOutputBytesBox.Text = _config.MaxOutputBytes.ToString();
        SetMaxListEntriesBox.Text = _config.MaxListEntries.ToString();
        SetTaskHistoryLimitBox.Text = _config.TaskHistoryLimit.ToString();
        SetAutoRefreshMonChk.IsChecked = _config.AutoRefreshMonitor;

        // Logging
        SelectComboByTag(SetLogLevelCombo, _config.LogLevel, 1);
        SetMaxLogEntriesBox.Text = _config.MaxLogEntries.ToString();
        SetMaxLogFileMbBox.Text = _config.MaxLogFileMb.ToString();
        SetFileLoggingChk.IsChecked = _config.FileLoggingEnabled;
        SetAutoScrollDefaultChk.IsChecked = _config.AutoScrollLogs;

        // Security
        SetTokenPasswordBox.Password = _deviceToken;
        SetTokenPlainBox.Text = _deviceToken;
        SetPipeNameBox.Text = _config.DesktopPipeName;
        SetPipeHmacSecretBox.Password = _configService.LoadPipeHmacSecret();

        // Appearance
        SelectComboByTag(SetThemeCombo, _config.Theme, 0);
        SetCompactSidebarChk.IsChecked = _config.CompactSidebar;

        // Tags
        _deviceTags.Clear();
        foreach (var tg in _config.Tags ?? new List<string>())
        {
            if (!string.IsNullOrWhiteSpace(tg)) _deviceTags.Add(tg);
        }

        // Advanced
        SetUpdateManifestUrlBox.Text = _config.UpdateManifestUrl;
        SetAutoCheckUpdatesChk.IsChecked = _config.AutoCheckUpdates;
    }

    private static void SelectComboByTag(System.Windows.Controls.ComboBox combo, string? tag, int fallbackIndex = 0)
    {
        for (int i = 0; i < combo.Items.Count; i++)
        {
            if (combo.Items[i] is ComboBoxItem item &&
                string.Equals(item.Tag?.ToString(), tag, StringComparison.OrdinalIgnoreCase))
            {
                combo.SelectedIndex = i;
                return;
            }
        }
        combo.SelectedIndex = fallbackIndex;
    }

    private void RefreshMonitorList()
    {
        var monitors = ScreenCaptureService.GetMonitors();
        VisionMonitorCombo.ItemsSource = monitors;
        if (monitors.Count > 0) VisionMonitorCombo.SelectedIndex = 0;

        var (_, _, vw, vh, _, _) = DesktopInputService.GetVirtualScreenSize();
        var summary = string.Join("\n", monitors.Select(m =>
            $"• Display {m.Index}{(m.IsPrimary ? " [Primary]" : "")}: {m.Width}x{m.Height} @ ({m.BoundsX},{m.BoundsY})"));
        VisionDisplayInfoText.Text = $"{monitors.Count} display(s) · Virtual {vw}x{vh}\n{summary}";
    }

    private void LoadSavedCaptureHistory(string screenshotDir)
    {
        try
        {
            if (!Directory.Exists(screenshotDir)) return;
            var files = new DirectoryInfo(screenshotDir)
                .GetFiles("*.png")
                .OrderByDescending(f => f.LastWriteTimeUtc)
                .Take(30);
            foreach (var f in files)
            {
                long kb = Math.Max(1, f.Length / 1024);
                _captureHistory.Add(new CaptureHistoryItem(
                    f.FullName,
                    0,
                    $"{f.LastWriteTime:MM-dd HH:mm} · {f.Name} ({kb} KB)",
                    $"{kb} KB",
                    f.LastWriteTime.ToString("HH:mm:ss")));
            }
            if (_captureHistory.Count > 0)
            {
                CaptureHistoryEmptyText.Visibility = Visibility.Collapsed;
            }
        }
        catch
        {
            // Ignore unreadable screenshots directory
        }
    }

    private void SetAgentButtonState(bool running, bool paused)
    {
        HeaderAgentIcon.Text = running ? "\uE71A" : "\uE768";
        HeaderAgentLabel.Text = running ? "Stop Agent" : "Start Agent";
        HeaderPauseBtn.IsEnabled = running;
        HeaderPauseLabel.Text = paused ? "Resume" : "Pause";
    }

    private void UpdateConnectionStatus(string status, string tone)
    {
        SidebarStatusText.Text = status;
        KpiStatusText.Text = status;
        TopStatusText.Text = status;

        var brush = tone switch
        {
            "ok" => new SolidColorBrush(ColorFromHex(16, 185, 129)),
            "warn" => new SolidColorBrush(ColorFromHex(245, 158, 11)),
            "err" => new SolidColorBrush(ColorFromHex(239, 68, 68)),
            _ => new SolidColorBrush(ColorFromHex(148, 163, 184)),
        };

        SidebarStatusDot.Fill = brush;
        KpiStatusDot.Fill = brush;
        TopStatusDot.Fill = brush;
        var mutedTextBrush = (System.Windows.Media.Brush)FindResource("TextSecondary");
        SidebarStatusText.Foreground = tone == "muted" ? mutedTextBrush : brush;
        TopStatusText.Foreground = tone == "muted" ? mutedTextBrush : brush;
        UpdatePulseBeacon(tone == "ok");
        SetAgentButtonState(_client.IsRunning, _client.IsPaused);

        if (_trayIcon is not null)
        {
            var old = _trayIcon.Icon;
            _trayIcon.Icon = TrayIconGenerator.CreateStatusIcon(tone);
            old?.Dispose();
        }
    }

    private void UpdatePulseBeacon(bool active)
    {
        if (TopStatusPulse is null) return;
        _pulseStoryboard?.Stop();
        if (!active)
        {
            TopStatusPulse.Opacity = 0;
            return;
        }

        var sb = new Storyboard { RepeatBehavior = RepeatBehavior.Forever };
        var sx = new DoubleAnimation(0.7, 1.85, TimeSpan.FromSeconds(1.5));
        var sy = new DoubleAnimation(0.7, 1.85, TimeSpan.FromSeconds(1.5));
        var op = new DoubleAnimation(0.75, 0.0, TimeSpan.FromSeconds(1.5));
        Storyboard.SetTarget(sx, TopStatusPulse);
        Storyboard.SetTargetProperty(sx, new PropertyPath("(UIElement.RenderTransform).(ScaleTransform.ScaleX)"));
        Storyboard.SetTarget(sy, TopStatusPulse);
        Storyboard.SetTargetProperty(sy, new PropertyPath("(UIElement.RenderTransform).(ScaleTransform.ScaleY)"));
        Storyboard.SetTarget(op, TopStatusPulse);
        Storyboard.SetTargetProperty(op, new PropertyPath("Opacity"));
        sb.Children.Add(sx);
        sb.Children.Add(sy);
        sb.Children.Add(op);
        _pulseStoryboard = sb;
        sb.Begin();
    }

    private static System.Windows.Media.Color ColorFromHex(byte r, byte g, byte b) =>
        System.Windows.Media.Color.FromRgb(r, g, b);

    private static int LogSeverity(string lvl) => lvl.ToUpperInvariant() switch
    {
        "DEBUG" => 0,
        "INFO" or "OK" => 1,
        "WARN" => 2,
        "ERR" or "ERROR" => 3,
        _ => 1,
    };

    private void AppendLog(string level, string message)
    {
        var lvl = level.ToUpperInvariant();
        if (LogSeverity(lvl) < LogSeverity(_config.LogLevel))
        {
            return;
        }
        var color = lvl switch
        {
            "OK" or "INFO" => "#34D399",
            "WARN" => "#FBBF24",
            "ERR" or "ERROR" => "#F87171",
            "DEBUG" => "#94A3B8",
            _ => "#60A5FA",
        };
        var entry = new LogEntry
        {
            Timestamp = DateTimeOffset.Now.ToString("HH:mm:ss"),
            Level = lvl,
            Message = message,
            LevelColor = color,
        };
        int maxEntries = Math.Clamp(_config.MaxLogEntries, 100, 5000);
        _logBuffer.Add(entry);
        while (_logBuffer.Count > maxEntries) _logBuffer.RemoveAt(0);
        if (_config.FileLoggingEnabled)
        {
            _configService.AppendLogLine($"{DateTimeOffset.Now:yyyy-MM-dd} {entry.FormattedLine}", _config.MaxLogFileMb);
        }
        RenderLogs();
    }

    private void RenderLogs()
    {
        var query = LogSearchBox?.Text?.Trim() ?? "";
        var filtered = _logBuffer.Where(x =>
            (_logFilter == "ALL" || x.Level == _logFilter || (_logFilter == "INFO" && x.Level == "OK")) &&
            (string.IsNullOrEmpty(query) ||
             x.Message.Contains(query, StringComparison.OrdinalIgnoreCase) ||
             x.Level.Contains(query, StringComparison.OrdinalIgnoreCase)));

        _visibleLogs.Clear();
        foreach (var item in filtered)
        {
            _visibleLogs.Add(item);
        }

        if (LogAutoScrollChk?.IsChecked == true && _visibleLogs.Count > 0)
        {
            ActivityLogList.ScrollIntoView(_visibleLogs[^1]);
        }
    }

    private void OnLogSearchChanged(object sender, TextChangedEventArgs e) => RenderLogs();

    private void OnToggleExpandConsoleClick(object sender, RoutedEventArgs e)
    {
        _consoleExpanded = !_consoleExpanded;
        var vis = _consoleExpanded ? Visibility.Collapsed : Visibility.Visible;
        OverviewKpiGrid.Visibility = vis;
        OverviewMiddleGrid.Visibility = vis;
        ExpandConsoleBtn.Content = _consoleExpanded ? "\uE73F" : "\uE740";
    }

    private void OnCopySelectedLogBtnClick(object sender, RoutedEventArgs e)
    {
        var target = ActivityLogList.SelectedItem as LogEntry ?? _visibleLogs.LastOrDefault();
        if (target is not null)
        {
            System.Windows.Clipboard.SetText(target.FormattedLine);
            ShowToast("Copied log entry");
        }
    }

    private void OnLogFilterClick(object sender, RoutedEventArgs e)
    {
        if (sender is System.Windows.Controls.Button btn && btn.Tag is string tag)
        {
            _logFilter = tag;
            var active = (Style)FindResource("TerminalChipActiveStyle");
            var normal = (Style)FindResource("TerminalChipStyle");
            LogFilterAllBtn.Style = tag == "ALL" ? active : normal;
            LogFilterInfoBtn.Style = tag == "INFO" ? active : normal;
            LogFilterWarnBtn.Style = tag == "WARN" ? active : normal;
            LogFilterErrBtn.Style = tag == "ERR" ? active : normal;
            RenderLogs();
        }
    }

    private void OnCopySelectedLogEntry(object sender, MouseButtonEventArgs e)
    {
        if (ActivityLogList.SelectedItem is LogEntry entry)
        {
            System.Windows.Clipboard.SetText(entry.FormattedLine);
            ShowToast("Copied log line");
        }
    }

    private void OnExportLogClick(object sender, RoutedEventArgs e)
    {
        var dlg = new Microsoft.Win32.SaveFileDialog
        {
            FileName = $"ai-bridge-client-{DateTimeOffset.Now:yyyyMMdd-HHmmss}.log",
            Filter = "Log files (*.log)|*.log|Text files (*.txt)|*.txt",
        };
        if (dlg.ShowDialog() == true)
        {
            File.WriteAllLines(dlg.FileName, _logBuffer.Select(x => x.FormattedLine));
            ShowToast("Exported activity logs");
        }
    }

    private void RecordTask(TaskHistoryRecord rec)
    {
        int limit = Math.Clamp(_config.TaskHistoryLimit, 50, 2000);
        _taskHistory.Insert(0, rec);
        if (_taskHistory.Count > limit)
        {
            _taskHistory.RemoveRange(limit, _taskHistory.Count - limit);
        }
        _configService.SaveTaskHistory(_taskHistory, limit);
        KpiTasksText.Text = _taskHistory.Count.ToString();
        RefreshTaskGrid();
        if (_config.NotificationsEnabled && !IsActive)
        {
            _trayIcon?.ShowBalloonTip(
                2500,
                $"AI Bridge · {rec.Tool}",
                $"Task {rec.Id} {rec.Status} ({rec.DurationMs} ms)",
                rec.Status == "succeeded" ? ToolTipIcon.Info : ToolTipIcon.Warning);
        }
    }

    private void RefreshTaskGrid()
    {
        var q = TaskSearchBox?.Text?.Trim() ?? "";
        var filtered = _taskHistory.Where(t =>
            (_taskFilter == "all" ||
             string.Equals(t.Status, _taskFilter, StringComparison.OrdinalIgnoreCase) ||
             (_taskFilter == "succeeded" && string.Equals(t.Status, "completed", StringComparison.OrdinalIgnoreCase))) &&
            (string.IsNullOrEmpty(q) ||
             t.Id.Contains(q, StringComparison.OrdinalIgnoreCase) ||
             t.Tool.Contains(q, StringComparison.OrdinalIgnoreCase)))
            .ToList();

        int totalPages = Math.Max(1, (int)Math.Ceiling(filtered.Count / (double)TaskPageSize));
        if (_taskPage > totalPages) _taskPage = totalPages;
        if (_taskPage < 1) _taskPage = 1;

        var pageSlice = filtered
            .Skip((_taskPage - 1) * TaskPageSize)
            .Take(TaskPageSize)
            .ToList();

        TasksGrid.ItemsSource = pageSlice;
        TasksEmptyState.Visibility = filtered.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
        if (TaskPageInfoText is not null)
        {
            TaskPageInfoText.Text = $"{filtered.Count} task(s) · Page {_taskPage} of {totalPages}";
            TaskPrevBtn.IsEnabled = _taskPage > 1;
            TaskNextBtn.IsEnabled = _taskPage < totalPages;
        }
    }

    private void OnTaskPrevPageClick(object sender, RoutedEventArgs e)
    {
        if (_taskPage > 1) { _taskPage--; RefreshTaskGrid(); }
    }

    private void OnTaskNextPageClick(object sender, RoutedEventArgs e)
    {
        _taskPage++;
        RefreshTaskGrid();
    }

    private void RefreshSystemMonitor()
    {
        var t = _monitor.Sample();

        _cpuHistory.Enqueue(t.CpuPercent);
        if (_cpuHistory.Count > SparklineCapacity) _cpuHistory.Dequeue();
        _memHistory.Enqueue(t.MemoryPercent);
        if (_memHistory.Count > SparklineCapacity) _memHistory.Dequeue();

        OverviewCpuLiveText.Text = $"{t.CpuPercent:F1}%";
        MonCpuText.Text = $"{t.CpuPercent:F1}%";

        MonMemText.Text = $"{t.MemoryUsedGb:F1} / {t.MemoryTotalGb:F1} GB ({t.MemoryPercent:F0}%)";
        MonDiskText.Text = $"{t.DiskUsedGb:F1} / {t.DiskTotalGb:F1} GB ({t.DiskPercent:F1}%)";
        MonDiskBar.Value = t.DiskPercent;

        MonNetText.Text = $"{t.NetworkRecvKbps:F1} KB/s ↓ · {t.NetworkSentKbps:F1} KB/s ↑";
        MonProcText.Text = $"{t.ProcessCount} processes · {(int)t.SystemUptime.TotalHours}h {t.SystemUptime.Minutes}m uptime";

        bool pipeOk = NamedPipeBridge.IsDesktopAgentAvailable();
        MonPipeStatusText.Text = pipeOk ? $"C++ Desktop Pipe: Connected ({NamedPipeBridge.PipeName})" : "C++ Desktop Pipe: Offline";
        MonPipeStatusText.Foreground = new SolidColorBrush(pipeOk ? ColorFromHex(16, 185, 129) : ColorFromHex(100, 116, 139));

        RenderSparkline(OverviewCpuSparkCanvas, OverviewCpuSparkLine, OverviewCpuSparkFill, _cpuHistory);
        RenderSparkline(MonCpuSparkCanvas, MonCpuSparkLine, MonCpuSparkFill, _cpuHistory);
        RenderSparkline(MonMemSparkCanvas, MonMemSparkLine, MonMemSparkFill, _memHistory);
    }

    private static void RenderSparkline(Canvas canvas, Polyline line, Polygon fill, IReadOnlyCollection<double> values)
    {
        var w = canvas.ActualWidth;
        var h = canvas.ActualHeight;
        if (w <= 4 || h <= 4 || values.Count < 2) return;

        var linePts = new PointCollection(values.Count);
        var fillPts = new PointCollection(values.Count + 2);
        int i = 0;
        double stepX = w / (SparklineCapacity - 1);
        double startX = w - (values.Count - 1) * stepX;

        fillPts.Add(new System.Windows.Point(startX, h));
        foreach (var v in values)
        {
            double x = startX + i * stepX;
            double y = h - 3 - (Math.Clamp(v, 0, 100) / 100.0) * (h - 6);
            var pt = new System.Windows.Point(x, y);
            linePts.Add(pt);
            fillPts.Add(pt);
            i++;
        }
        fillPts.Add(new System.Windows.Point(startX + (values.Count - 1) * stepX, h));

        line.Points = linePts;
        fill.Points = fillPts;
    }

    private async void OnToggleAgentClick(object sender, RoutedEventArgs e)
    {
        if (_client.IsRunning)
        {
            await _client.StopAsync();
            SetAgentButtonState(running: false, paused: false);
            return;
        }

        if (string.IsNullOrWhiteSpace(_deviceToken))
        {
            OnOpenEnrollModalClick(this, new RoutedEventArgs());
            return;
        }

        _client.Start(_config, _deviceToken);
        SetAgentButtonState(running: true, paused: false);
    }

    private void OnTogglePauseClick(object sender, RoutedEventArgs e)
    {
        if (!_client.IsRunning) return;
        _client.SetPaused(!_client.IsPaused);
        SetAgentButtonState(running: true, paused: _client.IsPaused);
    }

    private void OnOpenEnrollModalClick(object sender, RoutedEventArgs e)
    {
        ShowSignInPage(accountMode: true);
    }

    private void ShowSignInPage(bool accountMode = true)
    {
        SignInUrlBox.Text = _config.ControlUrl;
        SignInDeviceNameBox.Text = string.IsNullOrWhiteSpace(_config.DeviceId) ? Environment.MachineName.ToLowerInvariant() : _config.DeviceId;
        SignInUsernameBox.Text = _config.SignedInUser;
        SignInPasswordBox.Password = "";
        SignInTokenBox.Password = "";
        WizAllowWritesChk.IsChecked = _config.Policy.AllowWrites;
        WizAllowTier3Chk.IsChecked = _config.Policy.AllowTier3;
        WizAllowDesktopChk.IsChecked = _config.Policy.AllowDesktopControl;
        WizAllowShellChk.IsChecked = _config.Policy.AllowShell;
        SignInStatusText.Visibility = Visibility.Collapsed;
        SignInStatusText.Text = "";
        SetWizardStep(1);
        SetSignInMode(accountMode);
        SignInPageOverlay.Visibility = Visibility.Visible;
        if (accountMode) SignInUsernameBox.Focus();
    }

    private void SetWizardStep(int step)
    {
        bool s1 = step == 1;
        SignInStep1Panel.Visibility = s1 ? Visibility.Visible : Visibility.Collapsed;
        SignInStep2Panel.Visibility = s1 ? Visibility.Collapsed : Visibility.Visible;
        SignInWizProgress.Value = s1 ? 50 : 100;
        var active = (Style)FindResource("ChipActiveStyle");
        var normal = (Style)FindResource("ChipBtnStyle");
        WizTab1Btn.Style = s1 ? active : normal;
        WizTab2Btn.Style = s1 ? normal : active;
    }

    private void OnWizStepTabClick(object sender, RoutedEventArgs e)
    {
        if (sender is System.Windows.Controls.Button btn && int.TryParse(btn.Tag?.ToString(), out var step))
        {
            SetWizardStep(step);
        }
    }

    private void OnWizApplyAndSubmitClick(object sender, RoutedEventArgs e)
    {
        _config.Policy.AllowWrites = WizAllowWritesChk.IsChecked == true;
        _config.Policy.AllowTier3 = WizAllowTier3Chk.IsChecked == true;
        _config.Policy.AllowDesktopControl = WizAllowDesktopChk.IsChecked == true;
        _config.Policy.AllowShell = WizAllowShellChk.IsChecked == true;
        SetWizardStep(1);
        if (SignInAccountPanel.Visibility == Visibility.Visible)
        {
            OnSignInAccountSubmitClick(this, new RoutedEventArgs());
        }
        else
        {
            OnSignInTokenSubmitClick(this, new RoutedEventArgs());
        }
    }

    private void SetSignInMode(bool accountMode)
    {
        var active = (Style)FindResource("ChipActiveStyle");
        var normal = (Style)FindResource("ChipBtnStyle");
        SignInModeAccountBtn.Style = accountMode ? active : normal;
        SignInModeTokenBtn.Style = accountMode ? normal : active;
        SignInAccountPanel.Visibility = accountMode ? Visibility.Visible : Visibility.Collapsed;
        SignInTokenPanel.Visibility = accountMode ? Visibility.Collapsed : Visibility.Visible;
    }

    private void OnSignInModeClick(object sender, RoutedEventArgs e)
    {
        if (sender is System.Windows.Controls.Button btn && btn.Tag is string tag)
        {
            SetSignInMode(tag == "ACCOUNT");
            SignInStatusText.Visibility = Visibility.Collapsed;
        }
    }

    private void OnSignInKeyDown(object sender, System.Windows.Input.KeyEventArgs e)
    {
        if (e.Key != System.Windows.Input.Key.Enter) return;
        e.Handled = true;
        if (SignInAccountPanel.Visibility == Visibility.Visible)
        {
            OnSignInAccountSubmitClick(this, new RoutedEventArgs());
        }
        else
        {
            OnSignInTokenSubmitClick(this, new RoutedEventArgs());
        }
    }

    private void SetSignInStatus(string message, bool isError = true)
    {
        SignInStatusText.Text = message;
        SignInStatusText.Foreground = (System.Windows.Media.Brush)FindResource(isError ? "ErrorText" : "SuccessText");
        SignInStatusText.Visibility = string.IsNullOrWhiteSpace(message) ? Visibility.Collapsed : Visibility.Visible;
    }

    private async void OnSignInPingClick(object sender, RoutedEventArgs e)
    {
        var url = ControlPlaneClient.NormalizeUrl(SignInUrlBox.Text);
        if (!Uri.TryCreate(url, UriKind.Absolute, out _))
        {
            SetSignInStatus("Enter a valid Control Plane URL (e.g. https://...).");
            return;
        }
        SetSignInStatus("Pinging Control Plane...", isError: false);
        var (ok, latencyMs, detail) = await _client.PingAsync(url);
        SetSignInStatus(ok ? $"Reachable ({latencyMs:F1} ms)" : $"Unreachable: {detail}", isError: !ok);
    }

    private async void OnSignInAccountSubmitClick(object sender, RoutedEventArgs e)
    {
        var url = ControlPlaneClient.NormalizeUrl(SignInUrlBox.Text);
        var devName = SignInDeviceNameBox.Text.Trim();
        var username = SignInUsernameBox.Text.Trim();
        var password = SignInPasswordBox.Password;

        if (!Uri.TryCreate(url, UriKind.Absolute, out _))
        {
            SetSignInStatus("Enter a valid Control Plane URL.");
            return;
        }
        if (string.IsNullOrWhiteSpace(username) || string.IsNullOrEmpty(password))
        {
            SetSignInStatus("Username and password are required.");
            return;
        }

        SignInAccountSubmitBtn.IsEnabled = false;
        SetSignInStatus("Signing in and enrolling workstation...", isError: false);
        try
        {
            var (ok, deviceToken, resolvedId, resolvedTenant, resolvedUser, detail) =
                await _client.LoginAndEnrollAsync(url, username, password, devName, _config.TenantId);
            if (!ok)
            {
                SetSignInStatus(detail);
                return;
            }
            CompleteSignInEnrollment(url, resolvedId, deviceToken, resolvedTenant, resolvedUser);
        }
        finally
        {
            SignInAccountSubmitBtn.IsEnabled = true;
        }
    }

    private async void OnSignInTokenSubmitClick(object sender, RoutedEventArgs e)
    {
        var url = ControlPlaneClient.NormalizeUrl(SignInUrlBox.Text);
        var devName = SignInDeviceNameBox.Text.Trim();
        var tok = SignInTokenBox.Password.Trim();

        if (!Uri.TryCreate(url, UriKind.Absolute, out _))
        {
            SetSignInStatus("Enter a valid Control Plane URL.");
            return;
        }
        if (string.IsNullOrWhiteSpace(tok))
        {
            SetSignInStatus("Device Token or Admin Token is required.");
            return;
        }

        SignInTokenSubmitBtn.IsEnabled = false;
        SetSignInStatus("Verifying token with Control Plane...", isError: false);
        try
        {
            var (ok, deviceToken, resolvedId, detail) = await _client.EnrollAsync(url, tok, devName);
            if (!ok)
            {
                SetSignInStatus(detail);
                return;
            }
            CompleteSignInEnrollment(url, resolvedId, deviceToken, _config.TenantId, _config.SignedInUser);
        }
        finally
        {
            SignInTokenSubmitBtn.IsEnabled = true;
        }
    }

    private void CompleteSignInEnrollment(string url, string deviceId, string deviceToken, string tenant, string username)
    {
        _config.ControlUrl = url;
        _config.DeviceId = deviceId;
        if (!string.IsNullOrWhiteSpace(tenant)) _config.TenantId = tenant;
        if (!string.IsNullOrWhiteSpace(username)) _config.SignedInUser = username;
        _deviceToken = deviceToken;
        _configService.SaveConfig(_config);
        _configService.SaveToken(deviceToken);
        LoadConfigIntoUi();

        SignInPasswordBox.Password = "";
        SignInTokenBox.Password = "";
        SignInPageOverlay.Visibility = Visibility.Collapsed;
        AppendLog("ok", $"Enrolled workstation '{deviceId}' and sealed token with DPAPI.");
        ShowToast($"Enrolled '{deviceId}'");

        if (!_client.IsRunning)
        {
            _client.Start(_config, _deviceToken);
            SetAgentButtonState(running: true, paused: false);
        }
    }

    private void OnSignInContinueOfflineClick(object sender, RoutedEventArgs e)
    {
        SignInPageOverlay.Visibility = Visibility.Collapsed;
        AppendLog("warn", "Entered offline mode (device is not enrolled).");
        ShowToast("Device not enrolled — limited offline mode");
    }

    private void OnCloseEnrollModalClick(object sender, RoutedEventArgs e) =>
        EnrollModalOverlay.Visibility = Visibility.Collapsed;

    private async void OnSubmitEnrollClick(object sender, RoutedEventArgs e)
    {
        var url = ControlPlaneClient.NormalizeUrl(EnrollUrlBox.Text);
        var devId = EnrollDeviceIdBox.Text.Trim();
        var tok = EnrollTokenBox.Text.Trim();
        if (string.IsNullOrEmpty(url) || string.IsNullOrEmpty(tok))
        {
            EnrollStatusText.Text = "Control Plane URL and Enrollment Token are required.";
            return;
        }

        EnrollStatusText.Text = "Enrolling device...";
        var (ok, deviceToken, resolvedId, detail) = await _client.EnrollAsync(url, tok, devId);
        if (!ok)
        {
            EnrollStatusText.Text = $"Failed: {detail}";
            return;
        }

        _config.ControlUrl = url;
        _config.DeviceId = resolvedId;
        _deviceToken = deviceToken;
        _configService.SaveConfig(_config);
        _configService.SaveToken(deviceToken);
        LoadConfigIntoUi();

        EnrollModalOverlay.Visibility = Visibility.Collapsed;
        AppendLog("ok", $"Enrolled device '{resolvedId}' and sealed token with DPAPI.");
        ShowToast($"Enrolled device '{resolvedId}'");
        if (!_client.IsRunning)
        {
            _client.Start(_config, _deviceToken);
            SetAgentButtonState(running: true, paused: false);
        }
    }

    private async void OnPingClick(object sender, RoutedEventArgs e)
    {
        var (ok, latencyMs, detail) = await _client.PingAsync(_config.ControlUrl);
        KpiLatencyText.Text = ok ? $"{latencyMs:F1} ms" : "ERR";
        AppendLog(ok ? "ok" : "err", $"Ping {_config.ControlUrl}: {detail} ({latencyMs:F1} ms)");
        ShowToast(ok ? $"Ping: {latencyMs:F1} ms" : "Control Plane unreachable");
    }

    private async void OnTestConnectionClick(object sender, RoutedEventArgs e)
    {
        var url = ControlPlaneClient.NormalizeUrl(SetControlUrlBox.Text);
        if (!Uri.TryCreate(url, UriKind.Absolute, out _))
        {
            SetValidationBadge.Foreground = new SolidColorBrush(ColorFromHex(239, 68, 68));
            SetValidationBadge.Text = "Invalid URL";
            return;
        }
        var (ok, latencyMs, _) = await _client.PingAsync(url);
        SetValidationBadge.Foreground = new SolidColorBrush(ok ? ColorFromHex(16, 185, 129) : ColorFromHex(239, 68, 68));
        SetValidationBadge.Text = ok ? $"Reachable ({latencyMs:F1} ms)" : "Unreachable";
    }

    private async void OnCheckUpdatesClick(object sender, RoutedEventArgs e)
    {
        SetUpdateStatusText.Text = "Checking signed release manifest...";
        var (_, _, detail) = await _client.CheckUpdateAsync(_config.ControlUrl, "0.3.0", customManifestUrl: _config.UpdateManifestUrl);
        SetUpdateStatusText.Text = detail;
    }

    private void OnBrowseWorkspaceClick(object sender, RoutedEventArgs e)
    {
        var dlg = new Microsoft.Win32.OpenFolderDialog
        {
            Title = "Select Workspace Confinement Directory",
            InitialDirectory = Directory.Exists(SetWorkspaceBox.Text) ? SetWorkspaceBox.Text : Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
        };
        if (dlg.ShowDialog() == true)
        {
            SetWorkspaceBox.Text = dlg.FolderName;
        }
    }

    private void OnBrowseScreenshotDirClick(object sender, RoutedEventArgs e)
    {
        var dlg = new Microsoft.Win32.OpenFolderDialog
        {
            Title = "Select Screenshot Storage Directory",
            InitialDirectory = Directory.Exists(SetScreenshotDirBox.Text) ? SetScreenshotDirBox.Text : _configService.DefaultScreenshotDir,
        };
        if (dlg.ShowDialog() == true)
        {
            SetScreenshotDirBox.Text = dlg.FolderName;
        }
    }

    private void OnOpenDashboardClick(object sender, RoutedEventArgs e)
    {
        try
        {
            Process.Start(new ProcessStartInfo(_config.ControlUrl) { UseShellExecute = true });
        }
        catch (Exception ex)
        {
            AppendLog("err", $"Could not open dashboard: {ex.Message}");
        }
    }

    private async void OnRotateTokenClick(object sender, RoutedEventArgs e)
    {
        if (string.IsNullOrWhiteSpace(_deviceToken))
        {
            AppendLog("warn", "Cannot rotate token: no device token configured.");
            ShowToast("No device token configured");
            return;
        }
        var (ok, newTok, detail) = await _client.RotateTokenAsync(_config.ControlUrl, _deviceToken);
        if (ok)
        {
            _deviceToken = newTok;
            _configService.SaveToken(newTok);
            LoadSettingsIntoUi();
            AppendLog("ok", "Device token rotated and sealed with DPAPI.");
            ShowToast("DPAPI token rotated");
        }
        else
        {
            AppendLog("err", $"Token rotation failed: {detail}");
        }
    }

    private void OnClearLogClick(object sender, RoutedEventArgs e)
    {
        _logBuffer.Clear();
        _visibleLogs.Clear();
    }

    private void OnTaskFilterClick(object sender, RoutedEventArgs e)
    {
        if (sender is System.Windows.Controls.Button btn && btn.Tag is string f)
        {
            _taskFilter = f;
            _taskPage = 1;
            var active = (Style)FindResource("ChipActiveStyle");
            var normal = (Style)FindResource("ChipBtnStyle");
            TaskFilterAllBtn.Style = f == "all" ? active : normal;
            TaskFilterSuccBtn.Style = f == "succeeded" ? active : normal;
            TaskFilterFailBtn.Style = f == "failed" ? active : normal;
            TaskFilterCancBtn.Style = f == "cancelled" ? active : normal;
            RefreshTaskGrid();
        }
    }

    private void OnTaskSearchChanged(object sender, TextChangedEventArgs e)
    {
        _taskPage = 1;
        RefreshTaskGrid();
    }

    private void OnTaskSelected(object sender, SelectionChangedEventArgs e)
    {
        if (TasksGrid.SelectedItem is TaskHistoryRecord rec)
        {
            TaskInspectorBox.Text = $"ID: {rec.Id}\nTool: {rec.Tool}\nStatus: {rec.Status}\nArgs:\n{PrettyJson(rec.ArgsJson)}\n\nResult:\n{PrettyJson(rec.ResultPreview)}";
        }
        else
        {
            TaskInspectorBox.Text = "Select a task row above to inspect payload and result details.";
        }
    }

    private void OnOpenTaskDrawerClick(object sender, RoutedEventArgs e)
    {
        if (TasksGrid.SelectedItem is TaskHistoryRecord rec)
        {
            OpenDrawer(
                $"Task · {rec.Tool}",
                $"ID {rec.Id} · {rec.Status} · {rec.DurationMs} ms · {rec.Timestamp}",
                $"Arguments:\n{PrettyJson(rec.ArgsJson)}\n\nResult:\n{PrettyJson(rec.ResultPreview)}");
        }
        else
        {
            ShowToast("Select a task row first");
        }
    }

    private static string PrettyJson(string raw)
    {
        try
        {
            using var doc = JsonDocument.Parse(raw);
            return JsonSerializer.Serialize(doc.RootElement, new JsonSerializerOptions { WriteIndented = true });
        }
        catch
        {
            return raw;
        }
    }

    private void OnCopyTaskPayloadClick(object sender, RoutedEventArgs e)
    {
        if (!string.IsNullOrEmpty(TaskInspectorBox.Text))
        {
            System.Windows.Clipboard.SetText(TaskInspectorBox.Text);
            ShowToast("Payload copied");
        }
    }

    private void OnClearTasksClick(object sender, RoutedEventArgs e)
    {
        if (_config.ConfirmDestructiveActions)
        {
            var confirm = System.Windows.MessageBox.Show(
                "Clear all locally persisted task history?",
                "Confirm Clear History",
                MessageBoxButton.YesNo,
                MessageBoxImage.Question);
            if (confirm != MessageBoxResult.Yes) return;
        }

        _taskHistory.Clear();
        _configService.SaveTaskHistory(_taskHistory, _config.TaskHistoryLimit);
        KpiTasksText.Text = "0";
        TaskInspectorBox.Text = "Select a task row above to inspect payload and result details.";
        RefreshTaskGrid();
        ShowToast("Cleared task history");
    }

    private void OnCaptureScreenshotClick(object sender, RoutedEventArgs e)
    {
        try
        {
            int monIdx = VisionMonitorCombo.SelectedItem is MonitorDescriptor m ? m.Index : 0;
            var dir = string.IsNullOrWhiteSpace(_config.ScreenshotDirectory) ? _configService.DefaultScreenshotDir : _config.ScreenshotDirectory;
            var shot = ScreenCaptureService.CapturePrimaryScreen(dir, monIdx);
            RecordCaptureResult(shot, $"M{shot.MonitorIndex}");
            AppendLog("ok", $"Captured monitor {monIdx} screenshot: {shot.FilePath}");
            ShowToast("Desktop screenshot captured");
        }
        catch (Exception ex)
        {
            AppendLog("err", $"Screenshot capture failed: {ex.Message}");
        }
    }

    private void OnCaptureFullDesktopClick(object sender, RoutedEventArgs e)
    {
        try
        {
            var dir = string.IsNullOrWhiteSpace(_config.ScreenshotDirectory) ? _configService.DefaultScreenshotDir : _config.ScreenshotDirectory;
            var shot = ScreenCaptureService.CaptureVirtualDesktop(dir);
            RecordCaptureResult(shot, "Virtual");
            AppendLog("ok", $"Captured full virtual desktop ({shot.Width}x{shot.Height}): {shot.FilePath}");
            ShowToast("Full virtual desktop captured");
        }
        catch (Exception ex)
        {
            AppendLog("err", $"Virtual desktop capture failed: {ex.Message}");
        }
    }

    private void OnOpenScreenshotsFolderClick(object sender, RoutedEventArgs e)
    {
        var dir = string.IsNullOrWhiteSpace(_config.ScreenshotDirectory) ? _configService.DefaultScreenshotDir : _config.ScreenshotDirectory;
        Directory.CreateDirectory(dir);
        Process.Start(new ProcessStartInfo(dir) { UseShellExecute = true });
    }

    private void RecordCaptureResult(ScreenshotResult shot, string label)
    {
        LoadScreenshotPreview(shot.FilePath);
        VisionMetaText.Text = $"Captured {DateTimeOffset.Now:HH:mm:ss} · {shot.Width}x{shot.Height} ({Math.Max(1, shot.SizeBytes / 1024)} KB)";

        _captureHistory.Insert(0, new CaptureHistoryItem(
            shot.FilePath,
            shot.MonitorIndex,
            $"{DateTimeOffset.Now:HH:mm:ss} · {label} ({shot.Width}x{shot.Height})",
            $"{Math.Max(1, shot.SizeBytes / 1024)} KB",
            DateTimeOffset.Now.ToString("HH:mm:ss")));
        CaptureHistoryEmptyText.Visibility = Visibility.Collapsed;
    }

    private void LoadScreenshotPreview(string path)
    {
        _lastScreenshotPath = path;
        var bmp = new BitmapImage();
        bmp.BeginInit();
        bmp.CacheOption = BitmapCacheOption.OnLoad;
        bmp.UriSource = new Uri(path);
        bmp.EndInit();
        VisionPreviewImage.Source = bmp;
        VisionEmptyState.Visibility = Visibility.Collapsed;
    }

    private void OnCaptureHistorySelected(object sender, SelectionChangedEventArgs e)
    {
        if (CaptureHistoryList.SelectedItem is CaptureHistoryItem item && File.Exists(item.FilePath))
        {
            LoadScreenshotPreview(item.FilePath);
            VisionMetaText.Text = item.Resolution;
        }
    }

    private void OnVisionZoomToggleClick(object sender, RoutedEventArgs e)
    {
        _visionZoomMode = (_visionZoomMode + 1) % 3;
        if (_visionZoomMode == 0)
        {
            VisionPreviewImage.Stretch = Stretch.Uniform;
            VisionPreviewImage.LayoutTransform = Transform.Identity;
            VisionZoomBtn.Content = "Zoom: Fit";
        }
        else if (_visionZoomMode == 1)
        {
            VisionPreviewImage.Stretch = Stretch.None;
            VisionPreviewImage.LayoutTransform = Transform.Identity;
            VisionZoomBtn.Content = "Zoom: 100%";
        }
        else
        {
            VisionPreviewImage.Stretch = Stretch.None;
            VisionPreviewImage.LayoutTransform = new ScaleTransform(1.5, 1.5);
            VisionZoomBtn.Content = "Zoom: 150%";
        }
    }

    private void OnOpenScreenshotClick(object sender, RoutedEventArgs e)
    {
        if (!string.IsNullOrEmpty(_lastScreenshotPath) && File.Exists(_lastScreenshotPath))
        {
            Process.Start(new ProcessStartInfo(_lastScreenshotPath) { UseShellExecute = true });
        }
    }

    private void OnCopyScreenshotPathClick(object sender, RoutedEventArgs e)
    {
        if (!string.IsNullOrEmpty(_lastScreenshotPath))
        {
            System.Windows.Clipboard.SetText(_lastScreenshotPath);
            ShowToast("Screenshot path copied");
        }
    }

    private void RecordPolicyAudit(string message)
    {
        _policyAuditHistory.Insert(0, $"[{DateTimeOffset.Now:yyyy-MM-dd HH:mm:ss}] {message}");
        while (_policyAuditHistory.Count > 200)
        {
            _policyAuditHistory.RemoveAt(_policyAuditHistory.Count - 1);
        }
        _configService.SavePolicyAudit(_policyAuditHistory);
    }

    private void OnPresetFullClick(object sender, RoutedEventArgs e)
    {
        PolAllowWritesChk.IsChecked = true;
        PolAllowTier3Chk.IsChecked = true;
        PolAllowTier4Chk.IsChecked = false;
        PolAllowShellChk.IsChecked = false;
        PolAllowDesktopChk.IsChecked = true;
        PolAllowClipboardChk.IsChecked = true;
        PolMaxTierCombo.SelectedIndex = 2; // Tier 3
        RecordPolicyAudit("Applied preset: Full Desktop");
        ShowToast("Applied Full Desktop preset");
    }

    private void OnPresetWorkstationClick(object sender, RoutedEventArgs e)
    {
        PolAllowWritesChk.IsChecked = true;
        PolAllowTier3Chk.IsChecked = true;
        PolAllowTier4Chk.IsChecked = false;
        PolAllowShellChk.IsChecked = false;
        PolAllowDesktopChk.IsChecked = false;
        PolAllowPackageInstallChk.IsChecked = false;
        PolMaxTierCombo.SelectedIndex = 2; // Tier 3
        RecordPolicyAudit("Applied preset: Workstation");
        ShowToast("Applied Workstation preset");
    }

    private void OnPresetLockdownClick(object sender, RoutedEventArgs e)
    {
        PolAllowWritesChk.IsChecked = false;
        PolAllowTier3Chk.IsChecked = false;
        PolAllowTier4Chk.IsChecked = false;
        PolAllowShellChk.IsChecked = false;
        PolAllowDesktopChk.IsChecked = false;
        PolAllowPackageInstallChk.IsChecked = false;
        PolAllowClipboardChk.IsChecked = false;
        PolMaxTierCombo.SelectedIndex = 0; // Tier 1
        RecordPolicyAudit("Applied preset: Lockdown");
        ShowToast("Applied Lockdown preset");
    }

    private void OnAddAllowedAppClick(object sender, RoutedEventArgs e)
    {
        var app = NewAppBox.Text.Trim();
        if (!string.IsNullOrEmpty(app) && !_config.Policy.AllowedApps.Contains(app, StringComparer.OrdinalIgnoreCase))
        {
            _config.Policy.AllowedApps.Add(app);
            AllowedAppsList.Items.Add(app);
            NewAppBox.Clear();
            UpdateSecurityPostureSummary();
            RecordPolicyAudit($"Allowed app added: {app}");
        }
    }

    private void OnRemoveAllowedAppClick(object sender, RoutedEventArgs e)
    {
        if (AllowedAppsList.SelectedItem is string app)
        {
            _config.Policy.AllowedApps.Remove(app);
            AllowedAppsList.Items.Remove(app);
            UpdateSecurityPostureSummary();
            RecordPolicyAudit($"Allowed app removed: {app}");
        }
    }

    private void OnAddAllowedCommandClick(object sender, RoutedEventArgs e)
    {
        var cmd = NewCommandBox.Text.Trim();
        if (!string.IsNullOrEmpty(cmd) && !_config.Policy.AllowedCommands.Contains(cmd, StringComparer.OrdinalIgnoreCase))
        {
            _config.Policy.AllowedCommands.Add(cmd);
            AllowedCommandsList.Items.Add(cmd);
            NewCommandBox.Clear();
            UpdateSecurityPostureSummary();
            RecordPolicyAudit($"Allowed command added: {cmd}");
        }
    }

    private void OnRemoveAllowedCommandClick(object sender, RoutedEventArgs e)
    {
        if (AllowedCommandsList.SelectedItem is string cmd)
        {
            _config.Policy.AllowedCommands.Remove(cmd);
            AllowedCommandsList.Items.Remove(cmd);
            UpdateSecurityPostureSummary();
            RecordPolicyAudit($"Allowed command removed: {cmd}");
        }
    }

    private void SavePolicyFromUi()
    {
        _config.Policy.AllowWrites = PolAllowWritesChk.IsChecked == true;
        _config.Policy.AllowTier3 = PolAllowTier3Chk.IsChecked == true;
        _config.Policy.AllowTier4 = PolAllowTier4Chk.IsChecked == true;
        _config.Policy.AllowShell = PolAllowShellChk.IsChecked == true;
        _config.Policy.AllowDesktopControl = PolAllowDesktopChk.IsChecked == true;
        _config.Policy.AllowPackageInstall = PolAllowPackageInstallChk.IsChecked == true;
        _config.Policy.AllowClipboard = PolAllowClipboardChk.IsChecked == true;
        _config.Policy.RequireLocalApproval = PolRequireLocalApprovalChk.IsChecked == true;
        _config.Policy.MaxTierUnattended = Math.Clamp(PolMaxTierCombo.SelectedIndex + 1, 1, 4);
        _configService.SaveConfig(_config);
        UpdateSecurityPostureSummary();
        if (NamedPipeBridge.IsDesktopAgentAvailable())
        {
            _ = Task.Run(() => NamedPipeBridge.UpdateSessionPermissionsAsync(_config.Policy));
        }
    }

    private void RefreshProcessesGrid()
    {
        var filter = ProcSearchBox?.Text?.Trim();
        ProcessesGrid.ItemsSource = _monitor.GetTopProcesses(filter);
    }

    private void OnProcSearchChanged(object sender, TextChangedEventArgs e) => RefreshProcessesGrid();
    private void OnRefreshProcessesClick(object sender, RoutedEventArgs e)
    {
        RefreshProcessesGrid();
        ShowToast("Process list refreshed");
    }

    private void OnKillProcessClick(object sender, RoutedEventArgs e)
    {
        if (ProcessesGrid.SelectedItem is not ProcessSummary proc) return;
        if (_config.ConfirmDestructiveActions)
        {
            var confirm = System.Windows.MessageBox.Show(
                $"Terminate process '{proc.Name}' (PID {proc.Pid})?",
                "Confirm End Process",
                MessageBoxButton.YesNo,
                MessageBoxImage.Warning);
            if (confirm != MessageBoxResult.Yes) return;
        }

        if (_monitor.TryKillProcess(proc.Pid, out var err))
        {
            AppendLog("warn", $"Terminated process {proc.Name} (PID {proc.Pid}).");
            RefreshProcessesGrid();
        }
        else
        {
            AppendLog("err", $"Could not terminate PID {proc.Pid}: {err}");
        }
    }

    private Task<bool> RequestLocalApprovalAsync(string taskId, string tool, string argsJson)
    {
        return Dispatcher.InvokeAsync(() =>
        {
            _pendingApprovalTcs?.TrySetResult(false);
            _pendingApprovalTcs = new TaskCompletionSource<bool>();
            ApprovalTitleText.Text = $"Allow '{tool}' ({taskId})?";
            ApprovalArgsBox.Text = PrettyJson(argsJson);
            ApprovalModalOverlay.Visibility = Visibility.Visible;
            ShowFromTray();
            return _pendingApprovalTcs.Task;
        }).Task.Unwrap();
    }

    private void OnApproveLocalApprovalClick(object sender, RoutedEventArgs e)
    {
        ApprovalModalOverlay.Visibility = Visibility.Collapsed;
        _pendingApprovalTcs?.TrySetResult(true);
    }

    private void OnDenyLocalApprovalClick(object sender, RoutedEventArgs e)
    {
        ApprovalModalOverlay.Visibility = Visibility.Collapsed;
        _pendingApprovalTcs?.TrySetResult(false);
    }

    private void OnSavePolicyClick(object sender, RoutedEventArgs e)
    {
        SavePolicyFromUi();
        RecordPolicyAudit("Saved local policy");
        AppendLog("ok", "Local security policy saved.");
        ShowToast("Local policy saved");
    }

    private async void OnSyncPolicyClick(object sender, RoutedEventArgs e)
    {
        SavePolicyFromUi();
        if (string.IsNullOrWhiteSpace(_deviceToken))
        {
            AppendLog("warn", "Policy saved locally; set a Device Token to sync with Control Plane.");
            ShowToast("Policy saved locally (no token)");
            return;
        }
        var (ok, detail) = await _client.SyncPolicyAsync(_config.ControlUrl, _deviceToken, _config.Policy);
        RecordPolicyAudit($"Sync to server: {(ok ? "OK" : "FAILED")}");
        AppendLog(ok ? "ok" : "err", detail);
        ShowToast(ok ? "Policy synced to Control Plane" : "Policy sync failed");
    }

    private async void OnFetchPolicyClick(object sender, RoutedEventArgs e)
    {
        if (string.IsNullOrWhiteSpace(_deviceToken))
        {
            AppendLog("warn", "Device Token required to fetch policy from Control Plane.");
            return;
        }
        var (ok, policy, detail) = await _client.FetchPolicyAsync(_config.ControlUrl, _deviceToken, _config.Policy);
        if (ok && policy is not null)
        {
            _config.Policy = policy;
            _configService.SaveConfig(_config);
            LoadPolicyIntoUi();
            RecordPolicyAudit("Fetched policy from Control Plane");
            AppendLog("ok", detail);
            ShowToast("Fetched policy from Control Plane");
        }
        else
        {
            AppendLog("err", detail);
        }
    }

    private void OnToggleTokenVisClick(object sender, RoutedEventArgs e)
    {
        _tokenVisible = !_tokenVisible;
        if (_tokenVisible)
        {
            SetTokenPlainBox.Text = SetTokenPasswordBox.Password;
            SetTokenPasswordBox.Visibility = Visibility.Collapsed;
            SetTokenPlainBox.Visibility = Visibility.Visible;
            ToggleTokenVisBtn.Content = "Hide";
        }
        else
        {
            SetTokenPasswordBox.Password = SetTokenPlainBox.Text;
            SetTokenPlainBox.Visibility = Visibility.Collapsed;
            SetTokenPasswordBox.Visibility = Visibility.Visible;
            ToggleTokenVisBtn.Content = "Show";
        }
    }

    private void OnCopyTokenClick(object sender, RoutedEventArgs e)
    {
        var tok = _tokenVisible ? SetTokenPlainBox.Text : SetTokenPasswordBox.Password;
        if (!string.IsNullOrEmpty(tok))
        {
            System.Windows.Clipboard.SetText(tok);
            ShowToast("Device token copied");
        }
    }

    private void OnReloadTokenClick(object sender, RoutedEventArgs e)
    {
        _deviceToken = _configService.LoadToken();
        LoadSettingsIntoUi();
        AppendLog("info", "Reloaded DPAPI-sealed token from disk.");
        ShowToast("Reloaded token from disk");
    }

    private async void OnClearTokenClick(object sender, RoutedEventArgs e)
    {
        if (_config.ConfirmDestructiveActions)
        {
            var confirm = System.Windows.MessageBox.Show(
                "Clear the DPAPI-sealed device token and sign out?",
                "Confirm Sign Out",
                MessageBoxButton.YesNo,
                MessageBoxImage.Warning);
            if (confirm != MessageBoxResult.Yes) return;
        }
        if (_client.IsRunning)
        {
            await _client.StopAsync();
            SetAgentButtonState(running: false, paused: false);
        }
        _deviceToken = string.Empty;
        _config.SignedInUser = string.Empty;
        _configService.ClearToken();
        _configService.SaveConfig(_config);
        LoadConfigIntoUi();
        AppendLog("warn", "Signed out and cleared DPAPI device token.");
        ShowToast("Signed out");
        ShowSignInPage(accountMode: true);
    }

    private void OnSettingsCategoryClick(object sender, RoutedEventArgs e)
    {
        if (sender is not System.Windows.Controls.Button btn || btn.Tag is not string tag) return;
        var active = (Style)FindResource("ChipActiveStyle");
        var normal = (Style)FindResource("ChipBtnStyle");
        SetCatAllBtn.Style = tag == "ALL" ? active : normal;
        SetCatGenBtn.Style = tag == "GENERAL" ? active : normal;
        SetCatCpBtn.Style = tag == "CONTROL_PLANE" ? active : normal;
        SetCatAgentBtn.Style = tag == "AGENT" ? active : normal;
        SetCatLogBtn.Style = tag == "LOGGING" ? active : normal;
        SetCatSecBtn.Style = tag == "SECURITY" ? active : normal;
        SetCatAppBtn.Style = tag == "APPEARANCE" ? active : normal;
        SetCatAdvBtn.Style = tag == "ADVANCED" ? active : normal;

        SetSectionGeneral.Visibility = tag is "ALL" or "GENERAL" ? Visibility.Visible : Visibility.Collapsed;
        SetSectionControlPlane.Visibility = tag is "ALL" or "CONTROL_PLANE" ? Visibility.Visible : Visibility.Collapsed;
        SetSectionAgent.Visibility = tag is "ALL" or "AGENT" ? Visibility.Visible : Visibility.Collapsed;
        SetSectionLogging.Visibility = tag is "ALL" or "LOGGING" ? Visibility.Visible : Visibility.Collapsed;
        SetSectionSecurity.Visibility = tag is "ALL" or "SECURITY" ? Visibility.Visible : Visibility.Collapsed;
        SetSectionAppearance.Visibility = tag is "ALL" or "APPEARANCE" ? Visibility.Visible : Visibility.Collapsed;
        SetSectionAdvanced.Visibility = tag is "ALL" or "ADVANCED" ? Visibility.Visible : Visibility.Collapsed;
    }

    private async void OnSaveSettingsClick(object sender, RoutedEventArgs e)
    {
        // General
        _config.Workspace = SetWorkspaceBox.Text.Trim();
        _config.ScreenshotDirectory = string.IsNullOrWhiteSpace(SetScreenshotDirBox.Text)
            ? _configService.DefaultScreenshotDir
            : SetScreenshotDirBox.Text.Trim();
        _config.MinimizeToTray = SetMinimizeToTrayChk.IsChecked == true;
        _config.NotificationsEnabled = SetNotificationsChk.IsChecked == true;
        _config.ConfirmDestructiveActions = SetConfirmDestructiveChk.IsChecked == true;

        // Control Plane
        _config.ControlUrl = ControlPlaneClient.NormalizeUrl(SetControlUrlBox.Text);
        _config.DeviceId = SetDeviceIdBox.Text.Trim();
        _config.TenantId = string.IsNullOrWhiteSpace(SetTenantIdBox.Text) ? "default" : SetTenantIdBox.Text.Trim();
        if (int.TryParse(SetPollIntervalBox.Text, out var poll)) _config.PollIntervalSeconds = Math.Clamp(poll, 5, 60);
        if (int.TryParse(SetHeartbeatIntervalBox.Text, out var hb)) _config.HeartbeatIntervalSeconds = Math.Clamp(hb, 5, 120);
        if (int.TryParse(SetHttpTimeoutBox.Text, out var httpTo)) _config.HttpTimeoutSeconds = Math.Clamp(httpTo, 5, 120);
        if (int.TryParse(SetWsReconnectBox.Text, out var wsRec)) _config.WebSocketReconnectSeconds = Math.Clamp(wsRec, 3, 120);
        _config.AutoConnect = SetAutoConnectChk.IsChecked == true;
        _config.WebSocketEnabled = SetWsEnabledChk.IsChecked == true;

        // Agent Execution & Telemetry
        if (int.TryParse(SetDefaultTimeoutBox.Text, out var defTo)) _config.DefaultTimeoutSeconds = Math.Clamp(defTo, 5, 600);
        if (int.TryParse(SetMaxTimeoutBox.Text, out var maxTo)) _config.MaxTimeoutSeconds = Math.Clamp(maxTo, _config.DefaultTimeoutSeconds, 3600);
        if (int.TryParse(SetMonitorIntervalBox.Text, out var monMs)) _config.MonitorIntervalMs = Math.Clamp(monMs, 500, 5000);
        if (int.TryParse(SetMaxReadBytesBox.Text, out var maxRead)) _config.MaxReadBytes = Math.Clamp(maxRead, 4096, 2_000_000);
        if (int.TryParse(SetMaxOutputBytesBox.Text, out var maxOut)) _config.MaxOutputBytes = Math.Clamp(maxOut, 4096, 2_000_000);
        if (int.TryParse(SetMaxListEntriesBox.Text, out var maxList)) _config.MaxListEntries = Math.Clamp(maxList, 10, 1000);
        if (int.TryParse(SetTaskHistoryLimitBox.Text, out var histLim)) _config.TaskHistoryLimit = Math.Clamp(histLim, 50, 2000);
        _config.AutoRefreshMonitor = SetAutoRefreshMonChk.IsChecked == true;

        // Logging
        if (SetLogLevelCombo.SelectedItem is ComboBoxItem logItem && logItem.Tag is string lvl)
        {
            _config.LogLevel = lvl;
        }
        if (int.TryParse(SetMaxLogEntriesBox.Text, out var maxLogs)) _config.MaxLogEntries = Math.Clamp(maxLogs, 100, 5000);
        if (int.TryParse(SetMaxLogFileMbBox.Text, out var maxMb)) _config.MaxLogFileMb = Math.Clamp(maxMb, 1, 50);
        _config.FileLoggingEnabled = SetFileLoggingChk.IsChecked == true;
        _config.AutoScrollLogs = SetAutoScrollDefaultChk.IsChecked == true;

        // Security & Pipe
        var tok = (_tokenVisible ? SetTokenPlainBox.Text : SetTokenPasswordBox.Password).Trim();
        _deviceToken = tok;
        if (!string.IsNullOrEmpty(tok))
        {
            _configService.SaveToken(tok);
        }
        _config.DesktopPipeName = string.IsNullOrWhiteSpace(SetPipeNameBox.Text) ? NamedPipeBridge.PipeName : SetPipeNameBox.Text.Trim();
        _configService.SavePipeHmacSecret(SetPipeHmacSecretBox.Password);

        // Appearance
        if (SetThemeCombo.SelectedItem is ComboBoxItem themeItem && themeItem.Tag is string th)
        {
            _config.Theme = th;
        }
        _config.CompactSidebar = SetCompactSidebarChk.IsChecked == true;
        _config.Tags = _deviceTags.ToList();

        // Advanced
        _config.UpdateManifestUrl = SetUpdateManifestUrlBox.Text.Trim();
        _config.AutoCheckUpdates = SetAutoCheckUpdatesChk.IsChecked == true;

        _configService.SaveConfig(_config);
        _configService.SetAutoStart(SetAutoStartChk.IsChecked == true);
        ApplyTheme(_config.Theme);
        UpdateDwmWindowChrome();
        ApplyRuntimeSettings();
        ApplyResponsiveLayout(ActualWidth > 0 ? ActualWidth : Width);
        LoadConfigIntoUi();
        if (_client.IsRunning && !string.IsNullOrWhiteSpace(_deviceToken))
        {
            await _client.StopAsync();
            _client.Start(_config, _deviceToken);
            SetAgentButtonState(running: true, paused: _client.IsPaused);
            AppendLog("info", "Reloaded active agent loops with updated settings.");
        }
        AppendLog("ok", "All Windows Agent settings and DPAPI secrets saved.");
        ShowToast("Configuration saved");
    }

    private void OnNewTagKeyDown(object sender, System.Windows.Input.KeyEventArgs e)
    {
        if (e.Key != System.Windows.Input.Key.Enter) return;
        e.Handled = true;
        var tag = NewTagBox.Text.Trim();
        if (!string.IsNullOrEmpty(tag) && !_deviceTags.Contains(tag, StringComparer.OrdinalIgnoreCase))
        {
            _deviceTags.Add(tag);
            _config.Tags = _deviceTags.ToList();
        }
        NewTagBox.Clear();
    }

    private void OnRemoveTagClick(object sender, RoutedEventArgs e)
    {
        if (sender is System.Windows.Controls.Button btn && btn.Tag is string tag)
        {
            _deviceTags.Remove(tag);
            _config.Tags = _deviceTags.ToList();
        }
    }

    private void OnCopyDiagnosticsClick(object sender, RoutedEventArgs e)
    {
        try
        {
            var json = DiagnosticsService.BuildDiagnosticsJson(
                _config,
                _monitor,
                !string.IsNullOrWhiteSpace(_deviceToken),
                _taskHistory.Count,
                SidebarStatusText.Text);
            var recent = string.Join(Environment.NewLine, _logBuffer.TakeLast(50).Select(x => x.FormattedLine));
            System.Windows.Clipboard.SetText($"{json}\n\n--- recent activity ---\n{recent}");
            AppendLog("ok", "Copied diagnostics report to clipboard (token redacted).");
            ShowToast("Diagnostics copied");
        }
        catch (Exception ex)
        {
            AppendLog("err", $"Failed to copy diagnostics: {ex.Message}");
        }
    }

    private void OnExportDiagnosticsBundleClick(object sender, RoutedEventArgs e)
    {
        var dlg = new Microsoft.Win32.SaveFileDialog
        {
            FileName = $"ai-bridge-diagnostics-{DateTimeOffset.Now:yyyyMMdd-HHmmss}.zip",
            Filter = "Zip archive (*.zip)|*.zip",
        };
        if (dlg.ShowDialog() != true) return;

        try
        {
            var recent = string.Join(Environment.NewLine, _logBuffer.Select(x => x.FormattedLine));
            DiagnosticsService.ExportDiagnosticsBundle(
                dlg.FileName,
                _configService,
                _config,
                _monitor,
                recent,
                !string.IsNullOrWhiteSpace(_deviceToken),
                _taskHistory.Count,
                SidebarStatusText.Text);
            AppendLog("ok", $"Exported diagnostics bundle: {dlg.FileName}");
            ShowToast("Diagnostics bundle exported");
        }
        catch (Exception ex)
        {
            AppendLog("err", $"Diagnostics export failed: {ex.Message}");
        }
    }

    private void OnOpenLogsFolderClick(object sender, RoutedEventArgs e)
    {
        Directory.CreateDirectory(_configService.LogDir);
        Process.Start(new ProcessStartInfo(_configService.LogDir) { UseShellExecute = true });
    }

    private void OnOpenConfigDirClick(object sender, RoutedEventArgs e)
    {
        Directory.CreateDirectory(_configService.ConfigDir);
        Process.Start(new ProcessStartInfo(_configService.ConfigDir) { UseShellExecute = true });
    }

    private void OnResetSettingsClick(object sender, RoutedEventArgs e)
    {
        if (_config.ConfirmDestructiveActions)
        {
            var confirm = System.Windows.MessageBox.Show(
                "Reset all local settings to defaults?",
                "Confirm Reset",
                MessageBoxButton.YesNo,
                MessageBoxImage.Warning);
            if (confirm != MessageBoxResult.Yes) return;
        }

        _config = new AgentGuiConfig
        {
            Workspace = System.IO.Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                "ai-bridge-workspace"),
            ScreenshotDirectory = _configService.DefaultScreenshotDir,
        };
        _configService.SaveConfig(_config);
        ApplyTheme(_config.Theme);
        UpdateDwmWindowChrome();
        ApplyRuntimeSettings();
        ApplyResponsiveLayout(ActualWidth > 0 ? ActualWidth : Width);
        LoadConfigIntoUi();
        AppendLog("warn", "Configuration reset to defaults.");
        ShowToast("Defaults restored");
    }
}
