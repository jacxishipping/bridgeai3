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
    private readonly DeviceAgentRuntimeService _agentRuntime;

    private AgentGuiConfig _config;
    private string _deviceToken;
    private string _agentApiKey = "";
    private bool _loadingAgentSettings;
    private List<TaskHistoryRecord> _taskHistory;
    private readonly List<LogEntry> _logBuffer = new();
    private readonly ObservableCollection<LogEntry> _visibleLogs = new();
    private readonly ObservableCollection<CaptureHistoryItem> _captureHistory = new();
    private readonly ObservableCollection<string> _policyAuditHistory = new();
    private readonly ObservableCollection<string> _deviceTags = new();
    private readonly ObservableCollection<CustomAgentItem> _customAgents = new();
    private readonly ObservableCollection<AgentChatThread> _chatThreads = new();
    private AgentChatThread _activeThread = new() { Title = "New Chat", Preview = "Ready on this Windows PC" };
    private bool _isChatBusy;
    private System.Threading.CancellationTokenSource? _activeChatCts;
    private string? _pendingAttachmentImageBase64;
    private string _pendingAttachmentFileContext = "";
    private string _operatorToken = "";
    private bool _operatorIsAdminToken;

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
    private Views.QuickCommandOverlayWindow? _quickOverlay;
    private HwndSource? _hwndSource;
    private const int HotkeyQuickCommandId = 0xA1B2;

    [DllImport("dwmapi.dll", PreserveSig = true)]
    private static extern int DwmSetWindowAttribute(IntPtr hwnd, int attr, ref int attrValue, int attrSize);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool RegisterHotKey(IntPtr hWnd, int id, uint fsModifiers, uint vk);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool UnregisterHotKey(IntPtr hWnd, int id);

    private static readonly (string Title, string Subtitle)[] TabMeta =
    {
        ("Overview", "Agent status, connection, and live activity"),
        ("Task Explorer", "Every task this device received, with full payloads"),
        ("System Monitor", "Live hardware telemetry, GPU & process manager"),
        ("Desktop Vision", "Multi-monitor C++20 DXGI Desktop Duplication, Media Foundation H.264 & GDI+ capture"),
        ("Security & Policy", "4-tier guardrails, allowlist matrix & policy audit"),
        ("Settings", "Control Plane endpoint, C++20 DXGI/H.264 engine, DPAPI token, updates & appearance"),
        ("Agents", "Local runtimes, C++ Named-Pipe bridge & agents scoped exclusively to this device"),
    };

    public MainWindow(int initialTab = 0, bool suppressAutoSignIn = false)
    {
        InitializeComponent();

        _navButtons = new[] { NavOverviewBtn, NavTasksBtn, NavMonitorBtn, NavVisionBtn, NavSecurityBtn, NavSettingsBtn, NavAgentsBtn };
        _navIcons = new[] { NavIcon0, NavIcon1, NavIcon2, NavIcon3, NavIcon4, NavIcon5, NavIcon6 };

        ActivityLogList.ItemsSource = _visibleLogs;
        CaptureHistoryList.ItemsSource = _captureHistory;
        PolicyAuditList.ItemsSource = _policyAuditHistory;
        SetTagsItems.ItemsSource = _deviceTags;
        AgCustomGrid.ItemsSource = _customAgents;
        var savedThreads = ChatHistoryService.Load();
        if (savedThreads.Count > 0)
        {
            foreach (var st in savedThreads) _chatThreads.Add(st);
            _activeThread = _chatThreads[0];
        }
        else
        {
            _chatThreads.Add(_activeThread);
        }
        AgThreadsList.ItemsSource = _chatThreads;
        AgThreadsList.SelectedItem = _activeThread;
        AgMessagesList.ItemsSource = _activeThread.Messages;
        SyncChatStreamVisibility();

        _config = _configService.LoadConfig();
        _deviceToken = _configService.LoadToken();
        _agentApiKey = _configService.LoadAgentApiKey();
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
        _agentRuntime = new DeviceAgentRuntimeService(_executor);

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
        _client.ServerPolicyReceived += serverPol => Dispatcher.Invoke(() => ApplyServerSyncedPolicy(serverPol));
        _client.LocalApprovalRequested = RequestLocalApprovalAsync;

        _monitorTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(Math.Clamp(_config.MonitorIntervalMs, 500, 5000)) };
        _monitorTimer.Tick += (_, _) => RefreshSystemMonitor();

        AgentTakeoverService.StateChanged += OnTakeoverStateChanged;
        AgentTakeoverService.ExtraAllowedRegions = GetTakeoverAllowedRegions;

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
        var hwnd = new WindowInteropHelper(this).Handle;
        if (hwnd != IntPtr.Zero)
        {
            _hwndSource = HwndSource.FromHwnd(hwnd);
            _hwndSource?.AddHook(WndProcHook);
            // Global Ctrl+Shift+Space launcher (MOD_CONTROL=0x0002 | MOD_SHIFT=0x0004 | MOD_NOREPEAT=0x4000, VK_SPACE=0x20)
            RegisterHotKey(hwnd, HotkeyQuickCommandId, 0x0002 | 0x0004 | 0x4000, 0x20);
        }
    }

    private IntPtr WndProcHook(IntPtr hwnd, int msg, IntPtr wParam, IntPtr lParam, ref bool handled)
    {
        const int WM_HOTKEY = 0x0312;
        if (msg == WM_HOTKEY && wParam.ToInt32() == HotkeyQuickCommandId)
        {
            OpenQuickCommandOverlay();
            handled = true;
        }
        return IntPtr.Zero;
    }

    private void OpenQuickCommandOverlay()
    {
        _quickOverlay ??= new Views.QuickCommandOverlayWindow(cmd =>
        {
            Dispatcher.BeginInvoke(async () =>
            {
                await SendAgentPromptAsync(cmd);
            });
        });
        _quickOverlay.ShowAndFocus();
    }

    private void UpdateDwmWindowChrome()
    {
        var hwnd = new WindowInteropHelper(this).Handle;
        if (hwnd == IntPtr.Zero) return;
        int dark = string.Equals(ThemeManager.ResolveEffectiveTheme(_config.Theme), "light", StringComparison.OrdinalIgnoreCase) ? 0 : 1;
        DwmSetWindowAttribute(hwnd, 20, ref dark, sizeof(int));
        int round = 2;
        DwmSetWindowAttribute(hwnd, 33, ref round, sizeof(int));
        // DWMWA_SYSTEMBACKDROP_TYPE (38): 2 = DWMSBT_MAINWINDOW (Mica), 1 = DWMSBT_NONE
        int backdrop = _config.EnableMicaBackdrop ? 2 : 1;
        DwmSetWindowAttribute(hwnd, 38, ref backdrop, sizeof(int));
    }

    private void ApplyRuntimeSettings()
    {
        _config.CppAgent ??= new CppAgentSettings();
        CppNativeBridge.PreferDxgiCapture = _config.CppAgent.PreferDxgiCapture;
        CppNativeBridge.DefaultH264Fps = Math.Clamp(_config.CppAgent.H264TargetFps, 1, 60);
        CppNativeBridge.DefaultH264BitrateKbps = Math.Clamp(_config.CppAgent.H264BitrateKbps, 250, 50_000);
        DesktopInputService.SetSpeedProfile(_config.CppAgent.InputSpeedProfile);
        Views.AgentGlowOverlayWindow.ShowCursorHalo = _config.CppAgent.ShowAgentCursorHalo;
        if (CppNativeBridge.IsAvailable())
        {
            CppNativeBridge.TrySetEncoderBitrate(CppNativeBridge.DefaultH264BitrateKbps);
        }

        NamedPipeBridge.ActivePipeName = string.IsNullOrWhiteSpace(_config.DesktopPipeName)
            ? NamedPipeBridge.PipeName
            : _config.DesktopPipeName.Trim();
        var hmac = _configService.LoadPipeHmacSecret();
        if (string.IsNullOrWhiteSpace(hmac) || NamedPipeBridge.IsInsecureDefaultSecret(hmac))
        {
            hmac = Convert.ToHexStringLower(System.Security.Cryptography.RandomNumberGenerator.GetBytes(24));
            _configService.SavePipeHmacSecret(hmac);
        }
        NamedPipeBridge.ActiveHmacSecret = NamedPipeBridge.RequireHmacSecret(hmac);
        CppNativeBridge.SyncInputPermissions(_config.Policy.AllowDesktopControl, _config.Policy.AllowClipboard);
        var cppCfg = _config.CppAgent;
        _ = Task.Run(() => NamedPipeBridge.EnsureCppSupervisorRunning(
            NamedPipeBridge.ActivePipeName,
            NamedPipeBridge.ActiveHmacSecret,
            _config.ControlUrl,
            _deviceToken,
            cppCfg));

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
        NavLblAgents.Visibility = labelVis;

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
        bool drawerOpen = AgSidePanel is not null && AgSidePanel.Visibility == Visibility.Visible;
        AgLeftRailCol.Width = (width < 920 || (drawerOpen && width < 1480)) ? new GridLength(0) : new GridLength(228);

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
            new PaletteCommand("Agents", () => SelectTab(6)),
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
            new PaletteCommand("Action: Test Agent Takeover Border Glow", () => AgentTakeoverService.StartAgentTakeover()),
            new PaletteCommand("Action: Open Global Quick-Command Bar (Ctrl+Shift+Space)", () => OpenQuickCommandOverlay()),
        });
        foreach (var pb in AgentPlaybookService.Load().Take(12))
        {
            var capturedPrompt = pb.Prompt;
            _paletteCommands.Add(new PaletteCommand($"Playbook: {pb.Name}", () =>
            {
                SelectTab(6);
                _ = SendAgentPromptAsync(capturedPrompt);
            }));
        }
    }

    private void OnWindowPreviewKeyDown(object sender, System.Windows.Input.KeyEventArgs e)
    {
        bool ctrl = (Keyboard.Modifiers & ModifierKeys.Control) == ModifierKeys.Control;
        bool shift = (Keyboard.Modifiers & ModifierKeys.Shift) == ModifierKeys.Shift;
        if (ctrl && !shift && e.Key == Key.K)
        {
            e.Handled = true;
            OpenCommandPalette();
        }
        else if (ctrl && !shift && e.Key == Key.N)
        {
            e.Handled = true;
            SelectTab(6);
            OnAgNewChatClick(this, new RoutedEventArgs());
        }
        else if (ctrl && !shift && e.Key is >= Key.D1 and <= Key.D7)
        {
            int tabIdx = (int)(e.Key - Key.D1);
            e.Handled = true;
            SelectTab(tabIdx);
        }
        else if (e.Key == Key.Escape && ScreenshotLightboxOverlay.Visibility == Visibility.Visible)
        {
            e.Handled = true;
            ScreenshotLightboxOverlay.Visibility = Visibility.Collapsed;
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

    private IEnumerable<Rect> GetTakeoverAllowedRegions()
    {
        if (TopTakeoverBtn is null || !TopTakeoverBtn.IsVisible || !IsVisible)
        {
            yield break;
        }
        Rect? rect = null;
        try
        {
            var dpi = VisualTreeHelper.GetDpi(TopTakeoverBtn);
            var tl = TopTakeoverBtn.PointToScreen(new System.Windows.Point(0, 0));
            rect = new Rect(tl.X, tl.Y, TopTakeoverBtn.ActualWidth * dpi.DpiScaleX, TopTakeoverBtn.ActualHeight * dpi.DpiScaleY);
        }
        catch { }
        if (rect.HasValue) yield return rect.Value;
    }

    private void OnTakeoverStateChanged(TakeoverState state)
    {
        if (TopTakeoverBtn is null) return;
        if (state == TakeoverState.AgentControlling)
        {
            TopTakeoverBtn.Content = "⚡ Take Over Screen";
            TopTakeoverBtn.Style = (Style)FindResource("DangerBtnStyle");
            TopTakeoverBtn.Visibility = Visibility.Visible;
            AppendLog("info", "AI Agent took over screen (physical input locked; border glow active). Click 'Take Over' or press Ctrl+Alt+T to reclaim.");
        }
        else if (state == TakeoverState.UserControl)
        {
            TopTakeoverBtn.Content = "Return Control to Agent";
            TopTakeoverBtn.Style = (Style)FindResource("PrimaryBtnStyle");
            TopTakeoverBtn.Visibility = Visibility.Visible;
            AppendLog("warn", "User reclaimed screen control — agent desktop input paused.");
            ShowToast("You have screen control (agent input paused)");
        }
        else
        {
            TopTakeoverBtn.Visibility = Visibility.Collapsed;
        }
    }

    private void OnTopTakeoverClick(object sender, RoutedEventArgs e)
    {
        if (AgentTakeoverService.State == TakeoverState.AgentControlling)
        {
            AgentTakeoverService.TakeOver();
        }
        else if (AgentTakeoverService.State == TakeoverState.UserControl)
        {
            AgentTakeoverService.ReturnControlToAgent();
            ShowToast("Returned screen control to AI Agent");
        }
    }

    private void SetupSystemTray()
    {
        var menu = new ContextMenuStrip();
        menu.Items.Add("Show Window", null, (_, _) => Dispatcher.Invoke(ShowFromTray));
        menu.Items.Add("Quick Agent Command (Ctrl+Shift+Space)", null, (_, _) => Dispatcher.Invoke(OpenQuickCommandOverlay));
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
        try
        {
            var hwnd = new WindowInteropHelper(this).Handle;
            if (hwnd != IntPtr.Zero) UnregisterHotKey(hwnd, HotkeyQuickCommandId);
            _hwndSource?.RemoveHook(WndProcHook);
            _quickOverlay?.Close();
        }
        catch { }
        AgentTakeoverService.Shutdown();
        NamedPipeBridge.StopManagedCppSupervisor();
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
        MainTabs.BeginAnimation(OpacityProperty, new DoubleAnimation(0.75, 1.0, TimeSpan.FromMilliseconds(140)));
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
        if (idx == 6)
        {
            RefreshLocalAgentsSummary();
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
        LoadDeviceAgentSettingsIntoUi();
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
        PolAllowFullPcChk.IsChecked = _config.Policy.AllowFullPcAccess;
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

        // C++20 Native Agent, DXGI Capture, Media Foundation H.264 & WebRTC Engine
        _config.CppAgent ??= new CppAgentSettings();
        SetCppAutoStartChk.IsChecked = _config.CppAgent.AutoStartSupervisor;
        SetCppPreferDxgiChk.IsChecked = _config.CppAgent.PreferDxgiCapture;
        SetCppPreferHwH264Chk.IsChecked = _config.CppAgent.H264PreferHardware;
        SetCppAdaptiveBitrateChk.IsChecked = _config.CppAgent.H264AdaptiveBitrate;
        SetCppFpsBox.Text = Math.Clamp(_config.CppAgent.H264TargetFps, 1, 60).ToString();
        SetCppBitrateBox.Text = Math.Clamp(_config.CppAgent.H264BitrateKbps, 250, 50_000).ToString();
        SetCppKeyframeBox.Text = Math.Clamp(_config.CppAgent.H264KeyframeIntervalSec, 1, 15).ToString();
        // Seed TURN URL from the control-plane host when unset so the C++ Engine
        // tab is never blank for a configured plane (credentials still come from ICE).
        if (string.IsNullOrWhiteSpace(_config.CppAgent.TurnUrl))
        {
            var derived = ControlPlaneClient.DeriveTurnUrlFromControlPlane(_config.ControlUrl);
            if (!string.IsNullOrWhiteSpace(derived))
            {
                _config.CppAgent.TurnUrl = derived;
            }
        }
        SetCppTurnUrlBox.Text = _config.CppAgent.TurnUrl ?? "";
        SetCppTurnUserBox.Text = _config.CppAgent.TurnUsername ?? "";
        SetCppTurnCredBox.Password = _config.CppAgent.TurnCredential ?? "";
        if (SetCppTurnHintText is not null)
        {
            SetCppTurnHintText.Text = string.IsNullOrWhiteSpace(_config.CppAgent.TurnUsername)
                ? "Username/credential empty — click Pull TURN from Control Plane."
                : "TURN settings loaded (ephemeral credentials expire; re-pull as needed).";
        }
        SetCppCursorHaloChk.IsChecked = _config.CppAgent.ShowAgentCursorHalo;
        SelectComboByTag(SetCppInputSpeedCombo, _config.CppAgent.InputSpeedProfile, 1);
        RefreshCppEngineStatusUi();
        _ = HydrateTurnSettingsIfNeededAsync(force: false);

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
        SetMicaBackdropChk.IsChecked = _config.EnableMicaBackdrop;

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
        RefreshLocalAgentsSummary();

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
        // Keep task metadata for the UI, but avoid persisting file contents,
        // command output, prompts, or other sensitive tool arguments/results.
        rec.ArgsJson = "{}";
        rec.ResultPreview = "Task content omitted from local history.";
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

    private static bool TryNormalizeControlPlaneUrl(string input, out string normalized)
    {
        try
        {
            normalized = ControlPlaneClient.NormalizeUrl(input);
            return true;
        }
        catch (ArgumentException)
        {
            normalized = "";
            return false;
        }
    }

    private async void OnSignInPingClick(object sender, RoutedEventArgs e)
    {
        if (!TryNormalizeControlPlaneUrl(SignInUrlBox.Text, out var url))
        {
            SetSignInStatus("Use an HTTPS Control Plane URL. HTTP is allowed only for localhost.");
            return;
        }
        SetSignInStatus("Pinging Control Plane...", isError: false);
        var (ok, latencyMs, detail) = await _client.PingAsync(url);
        SetSignInStatus(ok ? $"Reachable ({latencyMs:F1} ms)" : $"Unreachable: {detail}", isError: !ok);
    }

    private async void OnSignInAccountSubmitClick(object sender, RoutedEventArgs e)
    {
        if (!TryNormalizeControlPlaneUrl(SignInUrlBox.Text, out var url))
        {
            SetSignInStatus("Use an HTTPS Control Plane URL. HTTP is allowed only for localhost.");
            return;
        }
        var devName = SignInDeviceNameBox.Text.Trim();
        var username = SignInUsernameBox.Text.Trim();
        var password = SignInPasswordBox.Password;

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
        if (!TryNormalizeControlPlaneUrl(SignInUrlBox.Text, out var url))
        {
            SetSignInStatus("Use an HTTPS Control Plane URL. HTTP is allowed only for localhost.");
            return;
        }
        var devName = SignInDeviceNameBox.Text.Trim();
        var tok = SignInTokenBox.Password.Trim();

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
        if (!TryNormalizeControlPlaneUrl(EnrollUrlBox.Text, out var url))
        {
            EnrollStatusText.Text = "Use an HTTPS Control Plane URL. HTTP is allowed only for localhost.";
            return;
        }
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
        if (!TryNormalizeControlPlaneUrl(SetControlUrlBox.Text, out var url))
        {
            SetValidationBadge.Foreground = new SolidColorBrush(ColorFromHex(239, 68, 68));
            SetValidationBadge.Text = "Use HTTPS (or localhost HTTP)";
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
        var m = CppNativeBridge.LastMetrics;
        var backendLabel = (m.Valid != 0 && _config.CppAgent.PreferDxgiCapture)
            ? (m.FromDxgi != 0 ? $"C++ DXGI ({m.CaptureTimeMs:F1} ms)" : $"C++ GDI ({m.CaptureTimeMs:F1} ms)")
            : "GDI+";
        VisionMetaText.Text = $"Captured {DateTimeOffset.Now:HH:mm:ss} · {shot.Width}x{shot.Height} ({Math.Max(1, shot.SizeBytes / 1024)} KB) · {backendLabel}";
        RefreshVisionCppMetricsUi();

        _captureHistory.Insert(0, new CaptureHistoryItem(
            shot.FilePath,
            shot.MonitorIndex,
            $"{DateTimeOffset.Now:HH:mm:ss} · {label} ({shot.Width}x{shot.Height})",
            $"{Math.Max(1, shot.SizeBytes / 1024)} KB",
            DateTimeOffset.Now.ToString("HH:mm:ss")));
        CaptureHistoryEmptyText.Visibility = Visibility.Collapsed;
    }

    private void RefreshVisionCppMetricsUi()
    {
        if (VisionCppMetricsText is null) return;
        bool dllOk = CppNativeBridge.IsAvailable();
        var m = CppNativeBridge.LastMetrics;
        if (!dllOk)
        {
            VisionCppMetricsText.Text = "C++ DLL: Unavailable (using C# GDI+ fallback)";
            return;
        }
        if (m.Valid == 0)
        {
            VisionCppMetricsText.Text =
                $"DLL: ai_bridge_native.dll loaded\n" +
                $"Mode: {(_config.CppAgent.PreferDxgiCapture ? "DXGI Preferred" : "C# GDI+ Forced")}\n" +
                $"Target H.264: {_config.CppAgent.H264TargetFps} FPS @ {_config.CppAgent.H264BitrateKbps} kbps\n" +
                $"Held Keys: {CppNativeBridge.GetHeldKeyCount()}";
            return;
        }
        var enc = !string.IsNullOrWhiteSpace(m.EncoderName)
            ? $"{m.EncoderName} ({(m.IsHardwareEncoder != 0 ? "HW" : "SW")}, {m.EncodeTimeMs:F1} ms, {m.EncodedBytes} B)"
            : $"Ready ({_config.CppAgent.H264TargetFps} FPS @ {_config.CppAgent.H264BitrateKbps} kbps)";
        VisionCppMetricsText.Text =
            $"Backend: {(m.FromDxgi != 0 ? "DXGI Desktop Duplication" : "C++ GDI Fallback")}{(m.SecureDesktop != 0 ? " [Secure Desktop]" : "")}\n" +
            $"Frame:   {m.Width}x{m.Height} · {m.CaptureTimeMs:F2} ms · Dirty: {m.DirtyRectCount}\n" +
            $"Cursor:  ({m.CursorX}, {m.CursorY}) {(m.CursorVisible != 0 ? "Visible" : "Hidden")}\n" +
            $"H.264:   {enc}\n" +
            $"Input:   {CppNativeBridge.GetHeldKeyCount()} held keys";
    }

    private async void OnVisionCppProbeClick(object sender, RoutedEventArgs e)
    {
        int monIdx = VisionMonitorCombo.SelectedItem is MonitorDescriptor m ? m.Index : 0;
        int fps = Math.Clamp(_config.CppAgent.H264TargetFps, 1, 60);
        int kbps = Math.Clamp(_config.CppAgent.H264BitrateKbps, 250, 50_000);
        var (ok, json) = await NamedPipeBridge.RunCppDesktopProbeAsync(monIdx, fps, kbps);
        RefreshVisionCppMetricsUi();
        RefreshCppEngineStatusUi();
        if (SetCppConsoleBox is not null)
        {
            SetCppConsoleBox.Text = json;
        }
        var lm = CppNativeBridge.LastMetrics;
        VisionMetaText.Text = ok
            ? $"C++ H.264 Probe OK · {(lm.FromDxgi != 0 ? "DXGI" : "GDI")} {lm.CaptureTimeMs:F1} ms · H.264 {lm.EncodeTimeMs:F1} ms ({lm.EncodedBytes} B)"
            : "C++ H.264 Probe failed";
        ShowToast(ok ? $"C++ H.264 Probe: {lm.EncodedBytes} bytes ({lm.EncodeTimeMs:F1} ms)" : "C++ Probe failed");
    }

    private void OnVisionResetDxgiClick(object sender, RoutedEventArgs e)
    {
        int monIdx = VisionMonitorCombo.SelectedItem is MonitorDescriptor m ? m.Index : 0;
        bool ok = CppNativeBridge.ReinitializeCapture(monIdx);
        RefreshMonitorList();
        RefreshVisionCppMetricsUi();
        ShowToast(ok ? $"Reinitialized C++ DXGI on display {monIdx}" : "Reinitialized GDI fallback");
    }

    private void OnVisionToggleWebRtcClick(object sender, RoutedEventArgs e)
    {
        if (NamedPipeBridge.IsCppWebRtcRunning)
        {
            NamedPipeBridge.StopCppWebRtcSession();
            VisionWebRtcBtn.Content = "Start H.264 Stream";
            VisionMetaText.Text = "C++20 H.264 WebRTC stream stopped";
            ShowToast("Stopped C++20 H.264 stream");
            return;
        }
        int monIdx = VisionMonitorCombo.SelectedItem is MonitorDescriptor m ? m.Index : 0;
        int fps = Math.Clamp(_config.CppAgent?.H264TargetFps ?? 30, 1, 60);
        int kbps = Math.Clamp(_config.CppAgent?.H264BitrateKbps ?? 4500, 250, 50_000);
        var (ok, detail) = NamedPipeBridge.StartCppWebRtcSession(_config.ControlUrl, _deviceToken, $"live-{Guid.NewGuid():N}"[..12], monIdx, fps, kbps);
        VisionWebRtcBtn.Content = ok ? "Stop H.264 Stream" : "Start H.264 Stream";
        VisionMetaText.Text = detail;
        AppendLog(ok ? "ok" : "warn", detail);
        ShowToast(ok ? $"Started {fps} FPS H.264 stream" : detail);
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
        _config.Policy.AllowFullPcAccess = PolAllowFullPcChk.IsChecked == true;
        if (_config.Policy.AllowFullPcAccess)
        {
            _config.Policy.AllowUnrestrictedApps = true;
        }
        _config.Policy.RequireLocalApproval = PolRequireLocalApprovalChk.IsChecked == true;
        _config.Policy.MaxTierUnattended = Math.Clamp(PolMaxTierCombo.SelectedIndex + 1, 1, 4);
        _configService.SaveConfig(_config);
        UpdateSecurityPostureSummary();
        RefreshLocalAgentsSummary();
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

    private void OnApproveAlwaysLocalApprovalClick(object sender, RoutedEventArgs e)
    {
        ApprovalModalOverlay.Visibility = Visibility.Collapsed;
        _config.Policy.RequireLocalApproval = false;
        if (_config.DeviceAgent is not null)
        {
            _config.DeviceAgent.ConfirmMutatingFunctions = false;
        }
        _configService.SaveConfig(_config);
        LoadPolicyIntoUi();
        LoadDeviceAgentSettingsIntoUi();
        SyncPolicyInBackgroundIfConnected();
        _pendingApprovalTcs?.TrySetResult(true);
        ShowToast("Local approval prompts disabled");
    }

    private void OnDenyLocalApprovalClick(object sender, RoutedEventArgs e)
    {
        ApprovalModalOverlay.Visibility = Visibility.Collapsed;
        _pendingApprovalTcs?.TrySetResult(false);
    }

    private void ApplyServerSyncedPolicy(AgentPolicyConfig serverPol)
    {
        var cur = _config.Policy;
        bool changed =
            cur.AllowWrites != serverPol.AllowWrites ||
            cur.AllowTier3 != serverPol.AllowTier3 ||
            cur.AllowTier4 != serverPol.AllowTier4 ||
            cur.AllowShell != serverPol.AllowShell ||
            cur.AllowDesktopControl != serverPol.AllowDesktopControl ||
            cur.AllowPackageInstall != serverPol.AllowPackageInstall ||
            cur.AllowClipboard != serverPol.AllowClipboard ||
            cur.AllowFullPcAccess != serverPol.AllowFullPcAccess ||
            cur.AllowUnrestrictedApps != serverPol.AllowUnrestrictedApps ||
            cur.RequireLocalApproval != serverPol.RequireLocalApproval ||
            cur.MaxTierUnattended != serverPol.MaxTierUnattended;

        // Always realign Device Functions with effective policy (even when Policy
        // fields look unchanged) so IsToolAllowed cannot drift from NativeToolExecutor.
        _config.Policy = serverPol;
        _config.DeviceAgent ??= new DeviceAgentSettings();
        var prevDisabled = _config.DeviceAgent.Functions?.DisabledTools;
        _config.DeviceAgent.Functions = AgentFunctionPermissions.SyncFromEffectivePolicy(
            serverPol, prevDisabled);
        CppNativeBridge.SyncInputPermissions(serverPol.AllowDesktopControl, serverPol.AllowClipboard);

        if (!changed)
        {
            _configService.SaveConfig(_config);
            LoadDeviceAgentSettingsIntoUi();
            return;
        }

        _configService.SaveConfig(_config);
        LoadPolicyIntoUi();
        LoadDeviceAgentSettingsIntoUi();
        UpdateSecurityPostureSummary();
        RefreshLocalAgentsSummary();
        if (NamedPipeBridge.IsDesktopAgentAvailable())
        {
            _ = Task.Run(() => NamedPipeBridge.UpdateSessionPermissionsAsync(_config.Policy));
        }
        RecordPolicyAudit("Synced policy update from Online Control Plane");
        AppendLog("info", "Applied device policy update pushed from Control Plane.");
    }

    private void SyncPolicyInBackgroundIfConnected()
    {
        if (string.IsNullOrWhiteSpace(_deviceToken) || string.IsNullOrWhiteSpace(_config.ControlUrl))
        {
            return;
        }
        var url = _config.ControlUrl;
        var tok = _deviceToken;
        var pol = _config.Policy;
        _ = Task.Run(async () =>
        {
            var (ok, detail) = await _client.SyncPolicyAsync(url, tok, pol);
            Dispatcher.Invoke(() =>
            {
                if (ok)
                {
                    RecordPolicyAudit("Auto-synced policy to Control Plane");
                }
                else
                {
                    AppendLog("warn", $"Background policy sync: {detail}");
                }
            });
        });
    }

    private void OnSavePolicyClick(object sender, RoutedEventArgs e)
    {
        SavePolicyFromUi();
        RecordPolicyAudit("Saved local policy");
        AppendLog("ok", "Local security policy saved.");
        ShowToast("Local policy saved");
        SyncPolicyInBackgroundIfConnected();
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
            ApplyServerSyncedPolicy(policy);
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
        SetCatCppBtn.Style = tag == "CPP_AGENT" ? active : normal;
        SetCatLogBtn.Style = tag == "LOGGING" ? active : normal;
        SetCatSecBtn.Style = tag == "SECURITY" ? active : normal;
        SetCatAppBtn.Style = tag == "APPEARANCE" ? active : normal;
        SetCatAdvBtn.Style = tag == "ADVANCED" ? active : normal;

        SetSectionGeneral.Visibility = tag is "ALL" or "GENERAL" ? Visibility.Visible : Visibility.Collapsed;
        SetSectionControlPlane.Visibility = tag is "ALL" or "CONTROL_PLANE" ? Visibility.Visible : Visibility.Collapsed;
        SetSectionAgent.Visibility = tag is "ALL" or "AGENT" ? Visibility.Visible : Visibility.Collapsed;
        SetSectionCppAgent.Visibility = tag is "ALL" or "CPP_AGENT" ? Visibility.Visible : Visibility.Collapsed;
        SetSectionLogging.Visibility = tag is "ALL" or "LOGGING" ? Visibility.Visible : Visibility.Collapsed;
        SetSectionSecurity.Visibility = tag is "ALL" or "SECURITY" ? Visibility.Visible : Visibility.Collapsed;
        SetSectionAppearance.Visibility = tag is "ALL" or "APPEARANCE" ? Visibility.Visible : Visibility.Collapsed;
        SetSectionAdvanced.Visibility = tag is "ALL" or "ADVANCED" ? Visibility.Visible : Visibility.Collapsed;
        if (tag is "ALL" or "CPP_AGENT")
        {
            _ = HydrateTurnSettingsIfNeededAsync(force: false);
        }
    }

    private async void OnSaveSettingsClick(object sender, RoutedEventArgs e)
    {
        if (!TryNormalizeControlPlaneUrl(SetControlUrlBox.Text, out var normalizedControlUrl))
        {
            ShowToast("Use HTTPS for the Control Plane; HTTP is allowed only for localhost.");
            return;
        }

        // General
        _config.Workspace = SetWorkspaceBox.Text.Trim();
        _config.ScreenshotDirectory = string.IsNullOrWhiteSpace(SetScreenshotDirBox.Text)
            ? _configService.DefaultScreenshotDir
            : SetScreenshotDirBox.Text.Trim();
        _config.MinimizeToTray = SetMinimizeToTrayChk.IsChecked == true;
        _config.NotificationsEnabled = SetNotificationsChk.IsChecked == true;
        _config.ConfirmDestructiveActions = SetConfirmDestructiveChk.IsChecked == true;

        // Control Plane
        _config.ControlUrl = normalizedControlUrl;
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

        // C++20 Native Agent, DXGI Capture, Media Foundation H.264 & WebRTC Engine
        _config.CppAgent ??= new CppAgentSettings();
        _config.CppAgent.AutoStartSupervisor = SetCppAutoStartChk.IsChecked == true;
        _config.CppAgent.PreferDxgiCapture = SetCppPreferDxgiChk.IsChecked == true;
        _config.CppAgent.H264PreferHardware = SetCppPreferHwH264Chk.IsChecked == true;
        _config.CppAgent.H264AdaptiveBitrate = SetCppAdaptiveBitrateChk.IsChecked == true;
        if (int.TryParse(SetCppFpsBox.Text, out var cppFps)) _config.CppAgent.H264TargetFps = Math.Clamp(cppFps, 1, 60);
        if (int.TryParse(SetCppBitrateBox.Text, out var cppKbps)) _config.CppAgent.H264BitrateKbps = Math.Clamp(cppKbps, 250, 50_000);
        if (int.TryParse(SetCppKeyframeBox.Text, out var cppKf)) _config.CppAgent.H264KeyframeIntervalSec = Math.Clamp(cppKf, 1, 15);
        _config.CppAgent.TurnUrl = SetCppTurnUrlBox.Text.Trim();
        _config.CppAgent.TurnUsername = SetCppTurnUserBox.Text.Trim();
        _config.CppAgent.TurnCredential = SetCppTurnCredBox.Password.Trim();
        _config.CppAgent.ShowAgentCursorHalo = SetCppCursorHaloChk.IsChecked == true;
        if (SetCppInputSpeedCombo.SelectedItem is ComboBoxItem spdItem && spdItem.Tag is string spdTag)
        {
            _config.CppAgent.InputSpeedProfile = spdTag;
        }

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
        _config.EnableMicaBackdrop = SetMicaBackdropChk.IsChecked == true;
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

    // ==================== AGENTS PAGE (CHATGPT UI · THIS DEVICE ONLY) ====================

    private void RefreshLocalAgentsSummary()
    {
        var workerState = !_client.IsRunning ? "Stopped" : (_client.IsPaused ? "Paused" : "Running");
        AgPollerStateText.Text = $"Worker: {workerState}";
        AgPollerStateText.Foreground = (System.Windows.Media.Brush)FindResource(
            !_client.IsRunning ? "TextMuted" : (_client.IsPaused ? "WarningText" : "SuccessText"));

        var pipeName = string.IsNullOrWhiteSpace(_config.DesktopPipeName) ? NamedPipeBridge.PipeName : _config.DesktopPipeName;
        bool pipeUp = NamedPipeBridge.IsDesktopAgentAvailable(pipeName);
        bool dllUp = CppNativeBridge.IsAvailable();
        AgPipeStateText.Text = pipeUp && dllUp
            ? "C++ Agent: Pipe & DXGI DLL Active"
            : (pipeUp ? "Desktop Pipe: Available" : (dllUp ? "C++ DLL: Loaded (Starting Pipe)" : "Desktop Pipe: Not Running"));
        AgPipeStateText.Foreground = (System.Windows.Media.Brush)FindResource((pipeUp || dllUp) ? "SuccessText" : "WarningText");

        var hasHmac = !string.IsNullOrWhiteSpace(NamedPipeBridge.ActiveHmacSecret);
        var devLabel = string.IsNullOrWhiteSpace(_config.DeviceId) ? "Unenrolled" : _config.DeviceId;
        AgPollerMetaText.Text = $"{devLabel} · Poll {_config.PollIntervalSeconds}s";
        AgPipeMetaText.Text = $@"\\.\pipe\{pipeName} · {(hasHmac ? "HMAC Sealed" : "Open")} · {(dllUp ? "DXGI+MF H.264" : "GDI")}";

        if (AgScopedDeviceBadgeText is not null)
        {
            AgScopedDeviceBadgeText.Text = string.IsNullOrWhiteSpace(_config.DeviceId)
                ? "Local PC (Unenrolled)"
                : $"Scoped to: {_config.DeviceId}";
        }

        if (AgWelcomeSubText is not null)
        {
            var agent = _config.DeviceAgent ?? new DeviceAgentSettings();
            var instr = string.IsNullOrWhiteSpace(agent.InstructionName) ? "Default" : agent.InstructionName;
            AgWelcomeSubText.Text = string.IsNullOrWhiteSpace(_config.DeviceId)
                ? $"{agent.AgentName} ({instr}) · Scoped exclusively to {Environment.MachineName}."
                : $"{agent.AgentName} ({instr}) · Scoped exclusively to '{_config.DeviceId}'.";
        }

        if (AgChatStatusPillText is not null)
        {
            var agent = _config.DeviceAgent ?? new DeviceAgentSettings();
            var hasDirect = !string.IsNullOrWhiteSpace(_agentApiKey) ||
                agent.Provider.Equals("ollama", StringComparison.OrdinalIgnoreCase) ||
                agent.Provider.Equals("custom", StringComparison.OrdinalIgnoreCase);
            var statusLabel = (agent.ConnectionMode, hasDirect, !string.IsNullOrWhiteSpace(_operatorToken)) switch
            {
                ("control_plane", _, true) => $"{agent.AgentName} · Control Plane Ready",
                ("direct_api", true, _) or ("direct_or_local", true, _) => $"{agent.AgentName} · {agent.Provider}/{agent.Model}",
                ("local_only", _, _) => $"{agent.AgentName} · Local Tools Only",
                (_, _, true) => $"{agent.AgentName} · Cloud Auth Ready",
                _ => $"{agent.AgentName} · Local Ready",
            };
            AgChatStatusPillText.Text = statusLabel;
        }

        if (AgFullUiBtn is not null)
        {
            bool uiOn = _config.Policy.AllowDesktopControl;
            AgFullUiBtn.Content = uiOn ? "Full UI: ON" : "Full UI: Off";
            AgFullUiBtn.Foreground = (System.Windows.Media.Brush)FindResource(uiOn ? "SuccessText" : "WarningText");
        }

        if (AgFullPcBtn is not null)
        {
            bool pcOn = _config.Policy.AllowFullPcAccess;
            AgFullPcBtn.Content = pcOn ? "Full PC: ALLOWED" : "Full PC: Off";
            AgFullPcBtn.Foreground = (System.Windows.Media.Brush)FindResource(pcOn ? "SuccessText" : "TextSecondary");
        }

        if (!string.IsNullOrWhiteSpace(_config.SignedInUser) && string.IsNullOrWhiteSpace(AgUserBox.Text))
        {
            AgUserBox.Text = _config.SignedInUser;
        }
    }

    private bool PromptAndEnableFullPcAccess(string? requestedAction = null)
    {
        var actionLine = string.IsNullOrWhiteSpace(requestedAction)
            ? ""
            : $"Requested action: {requestedAction}\n\n";
        var res = System.Windows.MessageBox.Show(
            this,
            actionLine +
            "Allow AI Bridge Agents on this device full access to your entire Windows PC?\n\n" +
            "When allowed, this device agent can:\n" +
            "• Read, write, edit, move & delete files across all drives (C:\\, D:\\, user folders)\n" +
            "• Run CMD & PowerShell commands anywhere on this PC\n" +
            "• Launch any installed Windows application and terminate processes\n" +
            "• Control mouse, keyboard, windows, clipboard & screen captures\n\n" +
            "You can revoke this permission anytime using 'Full PC: ALLOWED' in the header or Security & Policy.",
            "Allow Full PC Access?",
            System.Windows.MessageBoxButton.YesNo,
            System.Windows.MessageBoxImage.Warning,
            System.Windows.MessageBoxResult.No);

        if (res != System.Windows.MessageBoxResult.Yes)
        {
            return false;
        }

        _config.Policy.AllowFullPcAccess = true;
        _config.Policy.AllowWrites = true;
        _config.Policy.AllowTier3 = true;
        _config.Policy.AllowTier4 = true;
        _config.Policy.AllowShell = true;
        _config.Policy.AllowDesktopControl = true;
        _config.Policy.AllowClipboard = true;
        _config.Policy.AllowPackageInstall = true;
        _config.Policy.AllowUnrestrictedApps = true;
        _config.Policy.MaxTierUnattended = 4;

        LoadPolicyIntoUi();
        _configService.SaveConfig(_config);
        UpdateSecurityPostureSummary();
        RefreshLocalAgentsSummary();
        if (NamedPipeBridge.IsDesktopAgentAvailable())
        {
            _ = Task.Run(() => NamedPipeBridge.UpdateSessionPermissionsAsync(_config.Policy));
        }
        RecordPolicyAudit("User allowed Full PC Access");
        AppendLog("ok", "Full PC Access ALLOWED by user (all drives, PowerShell/CMD, processes, unrestricted apps & Windows UI).");
        ShowToast("Full PC access allowed");
        SyncPolicyInBackgroundIfConnected();
        return true;
    }

    private void OnAgToggleFullPcClick(object sender, RoutedEventArgs e)
    {
        if (!_config.Policy.AllowFullPcAccess)
        {
            PromptAndEnableFullPcAccess();
            return;
        }

        _config.Policy.AllowFullPcAccess = false;
        _config.Policy.AllowUnrestrictedApps = false;
        _config.Policy.AllowShell = false;
        _config.Policy.AllowTier4 = false;
        _config.Policy.MaxTierUnattended = Math.Min(3, Math.Max(1, _config.Policy.MaxTierUnattended));

        LoadPolicyIntoUi();
        _configService.SaveConfig(_config);
        UpdateSecurityPostureSummary();
        RefreshLocalAgentsSummary();
        if (NamedPipeBridge.IsDesktopAgentAvailable())
        {
            _ = Task.Run(() => NamedPipeBridge.UpdateSessionPermissionsAsync(_config.Policy));
        }
        RecordPolicyAudit("User revoked Full PC Access");
        AppendLog("warn", "Full PC Access REVOKED by user (returned to confined workspace mode).");
        ShowToast("Full PC access revoked");
        SyncPolicyInBackgroundIfConnected();
    }

    private void OnAgToggleFullUiClick(object sender, RoutedEventArgs e)
    {
        bool enable = !_config.Policy.AllowDesktopControl;
        _config.Policy.AllowDesktopControl = enable;
        if (enable)
        {
            _config.Policy.AllowTier3 = true;
            _config.Policy.AllowClipboard = true;
            _config.Policy.AllowUnrestrictedApps = true;
            _config.Policy.MaxTierUnattended = Math.Max(3, _config.Policy.MaxTierUnattended);
        }
        else
        {
            _config.Policy.AllowUnrestrictedApps = false;
        }

        LoadPolicyIntoUi();
        _configService.SaveConfig(_config);
        UpdateSecurityPostureSummary();
        RefreshLocalAgentsSummary();
        if (NamedPipeBridge.IsDesktopAgentAvailable())
        {
            _ = Task.Run(() => NamedPipeBridge.UpdateSessionPermissionsAsync(_config.Policy));
        }

        AppendLog(enable ? "ok" : "warn", enable
            ? "Full Windows UI control ENABLED (mouse, keyboard, UI Automation, window manager & app launching)."
            : "Full Windows UI control DISABLED (read-only UI inspection only).");
        ShowToast(enable ? "Full Windows UI access enabled" : "Full Windows UI access disabled");
        SyncPolicyInBackgroundIfConnected();
    }

    private void SyncChatStreamVisibility()
    {
        bool hasMessages = _activeThread.Messages.Count > 0;
        AgWelcomePanel.Visibility = hasMessages ? Visibility.Collapsed : Visibility.Visible;
        AgMessagesList.Visibility = hasMessages ? Visibility.Visible : Visibility.Collapsed;
        if (hasMessages)
        {
            AgChatScroll.ScrollToEnd();
        }
    }

    private void OnAgNewChatClick(object sender, RoutedEventArgs e)
    {
        if (_activeThread.Messages.Count == 0)
        {
            AgPromptBox.Focus();
            return;
        }

        var next = new AgentChatThread
        {
            Title = "New Chat",
            Preview = string.IsNullOrWhiteSpace(_config.DeviceId)
                ? "Ready on this Windows PC"
                : $"Scoped to {_config.DeviceId}",
        };
        _chatThreads.Insert(0, next);
        _activeThread = next;
        AgThreadsList.SelectedItem = next;
        AgMessagesList.ItemsSource = _activeThread.Messages;
        SyncChatStreamVisibility();
        AgPromptBox.Focus();
    }

    private void OnAgToggleDrawerClick(object sender, RoutedEventArgs e)
    {
        AgSidePanel.Visibility = AgSidePanel.Visibility == Visibility.Visible
            ? Visibility.Collapsed
            : Visibility.Visible;
        if (AgSidePanel.Visibility == Visibility.Visible)
        {
            LoadDeviceAgentSettingsIntoUi();
        }
        ApplyResponsiveLayout(ActualWidth > 0 ? ActualWidth : Width);
    }

    private void OnAgOpenSettingsDrawerClick(object sender, RoutedEventArgs e)
    {
        LoadDeviceAgentSettingsIntoUi();
        SelectAgDrawerTab("ai");
        AgSidePanel.Visibility = Visibility.Visible;
        ApplyResponsiveLayout(ActualWidth > 0 ? ActualWidth : Width);
    }

    private void OnAgOpenRuntimeDrawerClick(object sender, RoutedEventArgs e)
    {
        SelectAgDrawerTab("runtime");
        AgSidePanel.Visibility = Visibility.Visible;
        ApplyResponsiveLayout(ActualWidth > 0 ? ActualWidth : Width);
    }

    private void OnAgShowTimelineClick(object sender, RoutedEventArgs e)
    {
        var items = AgentActionTimelineService.GetRecent(40);
        var body = items.Count == 0
            ? "No mutating agent actions recorded in this session yet."
            : string.Join("\n", items.Select(x => $"[{x.Timestamp}] ({x.Kind.ToUpperInvariant()}) {x.Summary}"));
        OpenDrawer("Agent Action Timeline", $"{items.Count} recorded step(s)", body);
    }

    private void OnAgShowMemoryClick(object sender, RoutedEventArgs e)
    {
        var mem = AgentMemoryService.Load();
        var body = mem.Count == 0
            ? "No persistent memories saved yet.\n\nAsk the agent: \"Remember that my favorite browser is Chrome\" or \"Remember my workspace path\"."
            : string.Join("\n", mem.Select(kv => $"• {kv.Key}: {kv.Value}"));
        OpenDrawer("Persistent Agent Memory", $"{mem.Count} saved fact(s) · ~/.ai-bridge/agent_memory.json", body);
    }

    private void OnAgUndoLastClick(object sender, RoutedEventArgs e)
    {
        var (ok, detail) = AgentActionTimelineService.UndoLastAction();
        AppendLog(ok ? "ok" : "warn", detail);
        ShowToast(detail);
    }

    private void OnAgSavePlaybookClick(object sender, RoutedEventArgs e)
    {
        var lastUser = _activeThread.Messages.LastOrDefault(m => string.Equals(m.Role, "user", StringComparison.OrdinalIgnoreCase))?.Content?.Trim();
        var prompt = !string.IsNullOrWhiteSpace(AgPromptBox.Text) ? AgPromptBox.Text.Trim() : (lastUser ?? "");
        if (string.IsNullOrWhiteSpace(prompt))
        {
            ShowToast("Type or send a prompt first to save as a Playbook");
            return;
        }
        var list = AgentPlaybookService.Load();
        var title = prompt.Length > 36 ? prompt[..36] + "..." : prompt;
        list.Insert(0, new AgentPlaybook
        {
            Name = title,
            Prompt = prompt,
            Description = $"Saved {DateTime.Now:MMM dd HH:mm}",
        });
        AgentPlaybookService.Save(list);
        BuildPaletteCommands();
        ShowToast($"Saved Playbook: {title}");
    }

    private void OnAgDeleteThreadClick(object sender, RoutedEventArgs e)
    {
        if (_chatThreads.Count <= 1)
        {
            _activeThread.Messages.Clear();
            _activeThread.Title = "New Chat";
            _activeThread.Preview = "Ready on this Windows PC";
        }
        else
        {
            _chatThreads.Remove(_activeThread);
            _activeThread = _chatThreads[0];
            AgThreadsList.SelectedItem = _activeThread;
            AgMessagesList.ItemsSource = _activeThread.Messages;
        }
        ChatHistoryService.Save(_chatThreads);
        AgThreadsList.Items.Refresh();
        SyncChatStreamVisibility();
        ShowToast("Deleted conversation");
    }

    private void OnAgExportThreadClick(object sender, RoutedEventArgs e)
    {
        if (_activeThread.Messages.Count == 0)
        {
            ShowToast("Nothing to export in this chat");
            return;
        }
        var dlg = new Microsoft.Win32.SaveFileDialog
        {
            FileName = $"ai-bridge-chat-{DateTimeOffset.Now:yyyyMMdd-HHmmss}.md",
            Filter = "Markdown (*.md)|*.md|Text (*.txt)|*.txt",
        };
        if (dlg.ShowDialog() == true)
        {
            File.WriteAllText(dlg.FileName, ChatHistoryService.ExportThreadToMarkdown(_activeThread));
            ShowToast("Exported chat to Markdown");
        }
    }

    private void OnAgThreadSearchChanged(object sender, TextChangedEventArgs e)
    {
        var q = AgThreadSearchBox?.Text?.Trim() ?? "";
        AgThreadsList.ItemsSource = string.IsNullOrWhiteSpace(q)
            ? _chatThreads
            : _chatThreads.Where(t =>
                t.Title.Contains(q, StringComparison.OrdinalIgnoreCase) ||
                t.Preview.Contains(q, StringComparison.OrdinalIgnoreCase)).ToList();
    }

    private void OnAgCopyMessageClick(object sender, RoutedEventArgs e)
    {
        if (sender is System.Windows.Controls.Button btn && btn.Tag is string txt && !string.IsNullOrWhiteSpace(txt))
        {
            System.Windows.Clipboard.SetText(txt);
            ShowToast("Copied message to clipboard");
        }
    }

    private void OnAgScreenshotClick(object sender, MouseButtonEventArgs e)
    {
        if (sender is FrameworkElement fe && fe.Tag is ImageSource img)
        {
            LightboxImage.Source = img;
            ScreenshotLightboxOverlay.Visibility = Visibility.Visible;
        }
    }

    private void OnCloseLightboxClick(object sender, MouseButtonEventArgs e) =>
        ScreenshotLightboxOverlay.Visibility = Visibility.Collapsed;

    private void OnCloseLightboxBtnClick(object sender, RoutedEventArgs e) =>
        ScreenshotLightboxOverlay.Visibility = Visibility.Collapsed;

    private void OnAgDrawerTabClick(object sender, RoutedEventArgs e)
    {
        if (sender is System.Windows.Controls.Button btn && btn.Tag is string tag)
        {
            SelectAgDrawerTab(tag);
        }
    }

    private void SelectAgDrawerTab(string tab)
    {
        if (AgDrawerAiPanel is null) return;
        AgDrawerAiPanel.Visibility = tab == "ai" ? Visibility.Visible : Visibility.Collapsed;
        AgDrawerFunctionsPanel.Visibility = tab == "functions" ? Visibility.Visible : Visibility.Collapsed;
        AgDrawerRuntimePanel.Visibility = tab == "runtime" ? Visibility.Visible : Visibility.Collapsed;

        var primary = (Style)FindResource("PrimaryBtnStyle");
        var normal = (Style)FindResource("BtnStyle");
        AgDrawerTabAiBtn.Style = tab == "ai" ? primary : normal;
        AgDrawerTabFnBtn.Style = tab == "functions" ? primary : normal;
        AgDrawerTabSysBtn.Style = tab == "runtime" ? primary : normal;
    }

    private void LoadDeviceAgentSettingsIntoUi()
    {
        _loadingAgentSettings = true;
        try
        {
            _config.DeviceAgent ??= new DeviceAgentSettings();
            _config.DeviceAgent.Functions ??= new AgentFunctionPermissions();
            var a = _config.DeviceAgent;
            var f = a.Functions;

            AgSetNameBox.Text = a.AgentName;
            AgSetInstructionNameBox.Text = a.InstructionName;
            AgSetSystemPromptBox.Text = a.SystemPrompt;
            AgSetTempBox.Text = a.Temperature.ToString("0.##", System.Globalization.CultureInfo.InvariantCulture);
            AgSetMaxStepsBox.Text = a.MaxToolSteps.ToString();
            AgSetPinnedWindowBox.Text = a.PinnedWindowTitle ?? "";
            AgSetPinnedMonitorBox.Text = a.PinnedMonitorIndex.ToString();
            AgSetModelBox.Text = a.Model;
            AgSetBaseUrlBox.Text = a.BaseUrl;
            AgSetApiKeyBox.Password = _agentApiKey;

            foreach (ComboBoxItem item in AgSetConnModeCombo.Items)
            {
                if (string.Equals(item.Tag?.ToString(), a.ConnectionMode, StringComparison.OrdinalIgnoreCase))
                {
                    AgSetConnModeCombo.SelectedItem = item;
                    break;
                }
            }
            foreach (ComboBoxItem item in AgSetProviderCombo.Items)
            {
                if (string.Equals(item.Tag?.ToString(), a.Provider, StringComparison.OrdinalIgnoreCase))
                {
                    AgSetProviderCombo.SelectedItem = item;
                    break;
                }
            }

            AgFnSysHealthChk.IsChecked = f.AllowSystemHealthTools;
            AgFnScreenCaptureChk.IsChecked = f.AllowScreenCapture;
            AgFnWindowMgmtChk.IsChecked = f.AllowWindowManagement;
            AgFnUiInputChk.IsChecked = f.AllowUiInputAutomation;
            AgFnClipboardChk.IsChecked = f.AllowClipboard;
            AgFnAppLaunchChk.IsChecked = f.AllowAppLaunching;
            AgFnWorkspaceFilesChk.IsChecked = f.AllowWorkspaceFiles;
            AgFnFullPcFilesChk.IsChecked = f.AllowFullPcFilesAndDrives;
            AgFnShellPsChk.IsChecked = f.AllowShellAndPowerShell;
            AgFnKillProcChk.IsChecked = f.AllowProcessTermination;
            AgFnVisionLoopChk.IsChecked = a.EnableVisionInLoop;
            AgFnAutoVerifyChk.IsChecked = a.AutoVerifyVisualActions;
            AgFnConfirmMutateChk.IsChecked = a.ConfirmMutatingFunctions;
            AgSetDisabledToolsBox.Text = string.Join(", ", f.DisabledTools ?? new List<string>());
        }
        finally
        {
            _loadingAgentSettings = false;
        }
    }

    private void SaveDeviceAgentSettingsFromUi()
    {
        _config.DeviceAgent ??= new DeviceAgentSettings();
        _config.DeviceAgent.Functions ??= new AgentFunctionPermissions();
        var a = _config.DeviceAgent;
        var f = a.Functions;

        a.AgentName = string.IsNullOrWhiteSpace(AgSetNameBox.Text) ? "Windows Copilot" : AgSetNameBox.Text.Trim();
        a.InstructionName = AgSetInstructionNameBox.Text.Trim();
        a.SystemPrompt = AgSetSystemPromptBox.Text.Trim();
        a.Temperature = double.TryParse(AgSetTempBox.Text.Trim(), System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out var tv)
            ? Math.Clamp(tv, 0.0, 2.0)
            : 0.2;
        a.MaxToolSteps = int.TryParse(AgSetMaxStepsBox.Text.Trim(), out var ms) ? Math.Clamp(ms, 1, 30) : 16;
        a.PinnedWindowTitle = AgSetPinnedWindowBox.Text.Trim();
        a.PinnedMonitorIndex = int.TryParse(AgSetPinnedMonitorBox.Text.Trim(), out var pm) ? Math.Clamp(pm, -1, 15) : -1;
        a.ConnectionMode = (AgSetConnModeCombo.SelectedItem as ComboBoxItem)?.Tag?.ToString() ?? "direct_or_local";
        a.Provider = (AgSetProviderCombo.SelectedItem as ComboBoxItem)?.Tag?.ToString() ?? "openai";
        a.BaseUrl = AgSetBaseUrlBox.Text.Trim();
        a.Model = AgSetModelBox.Text.Trim();
        a.ConfirmMutatingFunctions = AgFnConfirmMutateChk.IsChecked == true;
        a.EnableVisionInLoop = AgFnVisionLoopChk.IsChecked == true;
        a.AutoVerifyVisualActions = AgFnAutoVerifyChk.IsChecked == true;

        _agentApiKey = AgSetApiKeyBox.Password.Trim();
        _configService.SaveAgentApiKey(_agentApiKey);

        f.AllowSystemHealthTools = AgFnSysHealthChk.IsChecked == true;
        f.AllowScreenCapture = AgFnScreenCaptureChk.IsChecked == true;
        f.AllowWindowManagement = AgFnWindowMgmtChk.IsChecked == true;
        f.AllowUiInputAutomation = AgFnUiInputChk.IsChecked == true;
        f.AllowClipboard = AgFnClipboardChk.IsChecked == true;
        f.AllowAppLaunching = AgFnAppLaunchChk.IsChecked == true;
        f.AllowWorkspaceFiles = AgFnWorkspaceFilesChk.IsChecked == true;
        f.AllowFullPcFilesAndDrives = AgFnFullPcFilesChk.IsChecked == true;
        f.AllowShellAndPowerShell = AgFnShellPsChk.IsChecked == true;
        f.AllowProcessTermination = AgFnKillProcChk.IsChecked == true;
        f.DisabledTools = AgSetDisabledToolsBox.Text
            .Split(new[] { ',', '\n', '\r', ';' }, StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            .Distinct(StringComparer.OrdinalIgnoreCase)
            .ToList();

        _configService.SaveConfig(_config);
        RefreshLocalAgentsSummary();
    }

    private void OnAgSettingsProviderChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_loadingAgentSettings || AgSetProviderCombo is null || AgSetBaseUrlBox is null || AgSetModelBox is null) return;
        var provider = (AgSetProviderCombo.SelectedItem as ComboBoxItem)?.Tag?.ToString() ?? "openai";
        if (provider != "custom")
        {
            AgSetBaseUrlBox.Text = DeviceAgentRuntimeService.GetDefaultBaseUrl(provider);
            AgSetModelBox.Text = DeviceAgentRuntimeService.GetDefaultModel(provider);
        }
    }

    private async void OnAgTestApiClick(object sender, RoutedEventArgs e)
    {
        SaveDeviceAgentSettingsFromUi();
        AgApiTestStatusText.Text = "Testing AI API connection...";
        var (ok, detail) = await _agentRuntime.TestConnectionAsync(_config.DeviceAgent, _agentApiKey);
        AgApiTestStatusText.Text = detail;
        AgApiTestStatusText.Foreground = (System.Windows.Media.Brush)FindResource(ok ? "SuccessText" : "ErrorText");
        ShowToast(ok ? "AI API connection succeeded" : "AI API connection failed");
    }

    private void OnAgSaveSettingsClick(object sender, RoutedEventArgs e)
    {
        SaveDeviceAgentSettingsFromUi();
        AgApiTestStatusText.Text = $"Saved settings for '{_config.DeviceAgent.AgentName}' ({_config.DeviceAgent.Provider}/{_config.DeviceAgent.Model}).";
        AgApiTestStatusText.Foreground = (System.Windows.Media.Brush)FindResource("SuccessText");
        AppendLog("ok", $"Agent settings saved: {_config.DeviceAgent.AgentName} ({_config.DeviceAgent.InstructionName}).");
        ShowToast("Agent settings saved");
    }

    private void OnAgAllowAllFunctionsClick(object sender, RoutedEventArgs e)
    {
        AgFnSysHealthChk.IsChecked = true;
        AgFnScreenCaptureChk.IsChecked = true;
        AgFnWindowMgmtChk.IsChecked = true;
        AgFnUiInputChk.IsChecked = true;
        AgFnClipboardChk.IsChecked = true;
        AgFnAppLaunchChk.IsChecked = true;
        AgFnWorkspaceFilesChk.IsChecked = true;
        AgFnFullPcFilesChk.IsChecked = true;
        AgFnShellPsChk.IsChecked = true;
        AgFnKillProcChk.IsChecked = true;
        AgSetDisabledToolsBox.Text = "";
        SaveDeviceAgentSettingsFromUi();
        ShowToast("All device functions allowed in Agent Settings");
    }

    private void OnAgReadOnlyFunctionsClick(object sender, RoutedEventArgs e)
    {
        AgFnSysHealthChk.IsChecked = true;
        AgFnScreenCaptureChk.IsChecked = true;
        AgFnWindowMgmtChk.IsChecked = false;
        AgFnUiInputChk.IsChecked = false;
        AgFnClipboardChk.IsChecked = false;
        AgFnAppLaunchChk.IsChecked = false;
        AgFnWorkspaceFilesChk.IsChecked = false;
        AgFnFullPcFilesChk.IsChecked = false;
        AgFnShellPsChk.IsChecked = false;
        AgFnKillProcChk.IsChecked = false;
        SaveDeviceAgentSettingsFromUi();
        ShowToast("Agent functions set to Read-Only Inspection");
    }

    private void OnAgThreadSelected(object sender, SelectionChangedEventArgs e)
    {
        if (AgThreadsList.SelectedItem is AgentChatThread selected && !ReferenceEquals(selected, _activeThread))
        {
            _activeThread = selected;
            AgMessagesList.ItemsSource = _activeThread.Messages;
            SyncChatStreamVisibility();
        }
    }

    private async void OnAgStarterClick(object sender, RoutedEventArgs e)
    {
        if (sender is System.Windows.Controls.Button btn && btn.Tag is string prompt && !string.IsNullOrWhiteSpace(prompt))
        {
            await SendAgentPromptAsync(prompt);
        }
    }

    private async void OnAgPromptKeyDown(object sender, System.Windows.Input.KeyEventArgs e)
    {
        if (e.Key == Key.V && (Keyboard.Modifiers & ModifierKeys.Control) == ModifierKeys.Control &&
            System.Windows.Clipboard.ContainsImage())
        {
            try
            {
                var bmpSrc = System.Windows.Clipboard.GetImage();
                if (bmpSrc is not null)
                {
                    var enc = new PngBitmapEncoder();
                    enc.Frames.Add(BitmapFrame.Create(bmpSrc));
                    using var ms = new MemoryStream();
                    enc.Save(ms);
                    _pendingAttachmentImageBase64 = Convert.ToBase64String(ms.ToArray());
                    AgAttachmentLabelText.Text = $"📷 Pasted screenshot ({bmpSrc.PixelWidth}x{bmpSrc.PixelHeight})";
                    AgAttachmentBar.Visibility = Visibility.Visible;
                    e.Handled = true;
                    ShowToast("Attached pasted screenshot");
                    return;
                }
            }
            catch { }
        }
        if (e.Key == Key.Enter && (Keyboard.Modifiers & ModifierKeys.Shift) == 0)
        {
            e.Handled = true;
            var text = AgPromptBox.Text.Trim();
            if (!string.IsNullOrWhiteSpace(text) || _pendingAttachmentImageBase64 is not null || !string.IsNullOrWhiteSpace(_pendingAttachmentFileContext))
            {
                AgPromptBox.Clear();
                await SendAgentPromptAsync(string.IsNullOrWhiteSpace(text) ? "Inspect this attachment and help me with it." : text);
            }
        }
    }

    private void OnAgComposerDragOver(object sender, System.Windows.DragEventArgs e)
    {
        e.Effects = e.Data.GetDataPresent(System.Windows.DataFormats.FileDrop)
            ? System.Windows.DragDropEffects.Copy
            : System.Windows.DragDropEffects.None;
        e.Handled = true;
    }

    private void OnAgComposerDrop(object sender, System.Windows.DragEventArgs e)
    {
        if (e.Data.GetDataPresent(System.Windows.DataFormats.FileDrop) &&
            e.Data.GetData(System.Windows.DataFormats.FileDrop) is string[] files &&
            files.Length > 0)
        {
            AttachFileToComposer(files[0]);
        }
    }

    private void OnAgAttachFileClick(object sender, RoutedEventArgs e)
    {
        var dlg = new Microsoft.Win32.OpenFileDialog
        {
            Title = "Attach Image or File to Agent Prompt",
            Filter = "All Supported|*.png;*.jpg;*.jpeg;*.bmp;*.txt;*.md;*.json;*.log;*.cs;*.py;*.csv|All files (*.*)|*.*",
        };
        if (dlg.ShowDialog() == true)
        {
            AttachFileToComposer(dlg.FileName);
        }
    }

    private void AttachFileToComposer(string path)
    {
        if (string.IsNullOrWhiteSpace(path) || !File.Exists(path)) return;
        var ext = System.IO.Path.GetExtension(path).ToLowerInvariant();
        if (ext is ".png" or ".jpg" or ".jpeg" or ".bmp" or ".webp")
        {
            _pendingAttachmentImageBase64 = Convert.ToBase64String(File.ReadAllBytes(path));
            _pendingAttachmentFileContext = "";
            AgAttachmentLabelText.Text = $"📷 Image: {System.IO.Path.GetFileName(path)}";
            AgAttachmentBar.Visibility = Visibility.Visible;
            ShowToast($"Attached image {System.IO.Path.GetFileName(path)}");
        }
        else
        {
            _pendingAttachmentImageBase64 = null;
            _pendingAttachmentFileContext = DeviceAgentRuntimeService.FormatFileAttachmentContext(path);
            AgAttachmentLabelText.Text = $"📄 File: {System.IO.Path.GetFileName(path)}";
            AgAttachmentBar.Visibility = Visibility.Visible;
            ShowToast($"Attached file {System.IO.Path.GetFileName(path)}");
        }
    }

    private void OnAgClearAttachmentClick(object sender, RoutedEventArgs e)
    {
        _pendingAttachmentImageBase64 = null;
        _pendingAttachmentFileContext = "";
        AgAttachmentLabelText.Text = "";
        AgAttachmentBar.Visibility = Visibility.Collapsed;
    }

    private async void OnAgVoiceInputClick(object sender, RoutedEventArgs e)
    {
        if (AgMicBtn is null) return;
        AgMicBtn.IsEnabled = false;
        AgMicBtn.Content = "Listening...";
        ShowToast("Listening for voice command (speak now)...");
        try
        {
            var (ok, transcript, err) = await VoiceInputService.ListenOnceAsync(8);
            if (ok && !string.IsNullOrWhiteSpace(transcript))
            {
                AgPromptBox.Text = transcript;
                ShowToast($"Heard: {transcript}");
                await SendAgentPromptAsync(transcript);
            }
            else
            {
                ShowToast(string.IsNullOrWhiteSpace(err) ? "Voice input unavailable" : err);
            }
        }
        finally
        {
            AgMicBtn.Content = "🎤 Voice";
            AgMicBtn.IsEnabled = true;
        }
    }

    private async void OnAgSendChatClick(object sender, RoutedEventArgs e)
    {
        if (_isChatBusy)
        {
            _activeChatCts?.Cancel();
            AgentTakeoverService.TakeOver();
            ShowToast("Stopping AI Agent run...");
            return;
        }
        var text = AgPromptBox.Text.Trim();
        if (string.IsNullOrWhiteSpace(text)) return;
        AgPromptBox.Clear();
        await SendAgentPromptAsync(text);
    }

    private async Task SendAgentPromptAsync(string prompt)
    {
        if (_isChatBusy || string.IsNullOrWhiteSpace(prompt)) return;
        _isChatBusy = true;
        _activeChatCts = new System.Threading.CancellationTokenSource();
        var chatCt = _activeChatCts.Token;
        AgSendBtn.Content = "Stop ■";
        AgSendBtn.Style = (Style)FindResource("DangerBtnStyle");

        var attachedImg = _pendingAttachmentImageBase64;
        var attachedFileCtx = _pendingAttachmentFileContext;
        OnAgClearAttachmentClick(this, new RoutedEventArgs());
        if (!string.IsNullOrWhiteSpace(attachedFileCtx))
        {
            prompt = $"{prompt}\n\n{attachedFileCtx}";
        }

        AgentChatMessage? liveProgressMsg = null;
        try
        {
            if (_activeThread.Messages.Count == 0)
            {
                _activeThread.Title = prompt.Length > 34 ? prompt[..34] + "..." : prompt;
            }
            _activeThread.Preview = prompt.Length > 48 ? prompt[..48] + "..." : prompt;
            AgThreadsList.Items.Refresh();

            _activeThread.Messages.Add(new AgentChatMessage
            {
                Role = "user",
                Content = prompt,
                Timestamp = DateTime.Now.ToString("HH:mm:ss"),
            });
            SyncChatStreamVisibility();

            var modeTag = (AgChatModeCombo.SelectedItem as ComboBoxItem)?.Tag?.ToString() ?? "auto";
            var isSlashCommand = prompt.StartsWith('/');
            var agent = _config.DeviceAgent ?? new DeviceAgentSettings();
            var connMode = string.IsNullOrWhiteSpace(agent.ConnectionMode) ? "direct_or_local" : agent.ConnectionMode;

            // 1. Direct AI API Runtime on this device (OpenAI / Anthropic / Gemini / Ollama / Custom)
            bool wantsDirectApi = !isSlashCommand &&
                !string.Equals(modeTag, "local", StringComparison.OrdinalIgnoreCase) &&
                connMode is "direct_api" or "direct_or_local";
            bool hasDirectCreds = !string.IsNullOrWhiteSpace(_agentApiKey) ||
                agent.Provider.Equals("ollama", StringComparison.OrdinalIgnoreCase) ||
                agent.Provider.Equals("custom", StringComparison.OrdinalIgnoreCase);

            if (wantsDirectApi && hasDirectCreds)
            {
                var effConfig = _config;
                if (modeTag is "openai" or "anthropic" or "gemini" &&
                    !string.Equals(modeTag, agent.Provider, StringComparison.OrdinalIgnoreCase))
                {
                    agent.Provider = modeTag;
                }

                var historyPayload = _activeThread.Messages
                    .TakeLast(16)
                    .Select(m => (m.Role, m.Content))
                    .ToList();

                liveProgressMsg = new AgentChatMessage
                {
                    Role = "assistant",
                    Content = "Thinking and planning desktop actions...",
                    ToolsSummary = $"{agent.AgentName} · Live Execution",
                    Timestamp = DateTime.Now.ToString("HH:mm:ss"),
                };
                _activeThread.Messages.Add(liveProgressMsg);
                SyncChatStreamVisibility();

                var progressLines = new List<string>();
                void OnStepProgress(string line)
                {
                    Dispatcher.Invoke(() =>
                    {
                        progressLines.Add(line);
                        if (liveProgressMsg is not null)
                        {
                            liveProgressMsg.Content = string.Join("\n", progressLines.TakeLast(10));
                            AgMessagesList.Items.Refresh();
                            AgChatScroll.ScrollToEnd();
                        }
                    });
                }

                var (dOk, dReply, dSummary, dShot, dErr) = await _agentRuntime.RunChatAsync(
                    effConfig,
                    _agentApiKey,
                    historyPayload,
                    confirmMutatingToolAsync: async (tool, argsJson) => await RequestLocalApprovalAsync(tool, "agent-chat", argsJson),
                    ct: chatCt,
                    onProgress: OnStepProgress,
                    attachedImageBase64: attachedImg);

                if (liveProgressMsg is not null)
                {
                    _activeThread.Messages.Remove(liveProgressMsg);
                    liveProgressMsg = null;
                }

                if (dOk)
                {
                    _activeThread.Messages.Add(new AgentChatMessage
                    {
                        Role = "assistant",
                        Content = dReply,
                        ToolsSummary = dSummary,
                        ToolDetails = progressLines.Count > 0 ? string.Join("\n", progressLines) : "",
                        Timestamp = DateTime.Now.ToString("HH:mm:ss"),
                        ScreenshotBase64 = dShot,
                        ScreenshotImage = DecodeBase64Image(dShot),
                    });
                    _activeThread.Preview = dReply.Length > 48 ? dReply[..48] + "..." : dReply;
                    AgThreadsList.Items.Refresh();
                    SyncChatStreamVisibility();
                    return;
                }

                if (connMode == "direct_api")
                {
                    _activeThread.Messages.Add(new AgentChatMessage
                    {
                        Role = "assistant",
                        Content = $"Direct AI API error ({agent.Provider}/{agent.Model}): {dErr}\n\nCheck your URL, Model, or API Key inside **Agent Settings → AI & Prompt**.",
                        ToolsSummary = $"{agent.AgentName} · Error",
                        Timestamp = DateTime.Now.ToString("HH:mm:ss"),
                    });
                    AgThreadsList.Items.Refresh();
                    SyncChatStreamVisibility();
                    return;
                }
            }

            // 2. Optional Control Plane AI chat route (/api/ai/chat) scoped strictly to _config.DeviceId
            if (!isSlashCommand &&
                !string.Equals(modeTag, "local", StringComparison.OrdinalIgnoreCase) &&
                connMode != "local_only" &&
                !string.IsNullOrWhiteSpace(_operatorToken) &&
                !string.IsNullOrWhiteSpace(_config.DeviceId))
            {
                var provider = string.Equals(modeTag, "auto", StringComparison.OrdinalIgnoreCase) ? agent.Provider : modeTag;
                var apiKey = !string.IsNullOrWhiteSpace(AgApiKeyBox.Password) ? AgApiKeyBox.Password.Trim() : _agentApiKey;
                var historyPayload = new List<(string Role, string Content)>
                {
                    ("system", DeviceAgentRuntimeService.BuildEffectiveSystemPrompt(_config)),
                };
                historyPayload.AddRange(_activeThread.Messages.TakeLast(16).Select(m => (m.Role, m.Content)));
                var (ok, reply, toolsSummary, screenshotB64, err) = await _client.SendDeviceAgentChatAsync(
                    _config.ControlUrl,
                    _operatorToken,
                    _operatorIsAdminToken,
                    _config.DeviceId,
                    historyPayload,
                    provider,
                    string.IsNullOrWhiteSpace(agent.Model) ? null : agent.Model,
                    string.IsNullOrWhiteSpace(apiKey) ? null : apiKey,
                    _config.TenantId);

                if (ok)
                {
                    _activeThread.Messages.Add(new AgentChatMessage
                    {
                        Role = "assistant",
                        Content = reply,
                        ToolsSummary = !string.IsNullOrWhiteSpace(toolsSummary) ? toolsSummary : $"Device: {_config.DeviceId}",
                        Timestamp = DateTime.Now.ToString("HH:mm:ss"),
                        ScreenshotImage = DecodeBase64Image(screenshotB64),
                    });
                    _activeThread.Preview = reply.Length > 48 ? reply[..48] + "..." : reply;
                    AgThreadsList.Items.Refresh();
                    SyncChatStreamVisibility();
                    return;
                }

                // Fallback gracefully to local tools if the user asked for local inspection or cloud key isn't set
                var (localOk, localReply, localTool, localShot) = await TryExecuteLocalDevicePromptAsync(prompt);
                if (localOk)
                {
                    _activeThread.Messages.Add(new AgentChatMessage
                    {
                        Role = "assistant",
                        Content = $"{localReply}\n\n(Note: Cloud LLM returned \"{err}\" so local on-device execution ran instead.)",
                        ToolsSummary = $"Local Tool: {localTool}",
                        Timestamp = DateTime.Now.ToString("HH:mm:ss"),
                        ScreenshotImage = localShot,
                    });
                }
                else
                {
                    _activeThread.Messages.Add(new AgentChatMessage
                    {
                        Role = "assistant",
                        Content = $"Cloud agent request could not complete: {err}\n\nTip: Open 'Tools & Agents' to configure your Operator Auth / LLM API Key, or use slash commands like /sysinfo, /windows, /monitor, /screenshot, or /pipe.",
                        ToolsSummary = $"Device: {_config.DeviceId}",
                        Timestamp = DateTime.Now.ToString("HH:mm:ss"),
                    });
                }
                AgThreadsList.Items.Refresh();
                SyncChatStreamVisibility();
                return;
            }

            // Local-only mode, slash command, or unauthenticated offline execution strictly on this Windows PC
            var (handled, answer, toolLabel, shot) = await TryExecuteLocalDevicePromptAsync(prompt, allowDefaultSummary: true);
            var cloudHint = string.Equals(modeTag, "local", StringComparison.OrdinalIgnoreCase)
                ? ""
                : (string.IsNullOrWhiteSpace(_agentApiKey) && string.IsNullOrWhiteSpace(_operatorToken)
                    ? $"\n\n(Ran local Windows tools as '{agent.AgentName}'. Open **Agent Settings** in the top bar to connect an AI API key, edit System Prompt / Instruction Name, or allow/disallow functions.)"
                    : "");

            _activeThread.Messages.Add(new AgentChatMessage
            {
                Role = "assistant",
                Content = (handled ? answer : "Ready on this Windows PC.") + cloudHint,
                ToolsSummary = toolLabel,
                Timestamp = DateTime.Now.ToString("HH:mm:ss"),
                ScreenshotImage = shot,
            });
            _activeThread.Preview = answer.Split('\n')[0];
            if (_activeThread.Preview.Length > 48) _activeThread.Preview = _activeThread.Preview[..48] + "...";
            AgThreadsList.Items.Refresh();
            SyncChatStreamVisibility();
        }
        catch (OperationCanceledException)
        {
            if (liveProgressMsg is not null) _activeThread.Messages.Remove(liveProgressMsg);
            _activeThread.Messages.Add(new AgentChatMessage
            {
                Role = "assistant",
                Content = "Stopped by user.",
                ToolsSummary = "Cancelled",
                Timestamp = DateTime.Now.ToString("HH:mm:ss"),
            });
            AgThreadsList.Items.Refresh();
            SyncChatStreamVisibility();
        }
        finally
        {
            ChatHistoryService.Save(_chatThreads);
            _activeChatCts?.Dispose();
            _activeChatCts = null;
            _isChatBusy = false;
            AgSendBtn.IsEnabled = true;
            AgSendBtn.Content = "Send ↑";
            AgSendBtn.Style = (Style)FindResource("PrimaryBtnStyle");
            AgPromptBox.Focus();
        }
    }

    private async Task<(bool Handled, string Reply, string ToolLabel, ImageSource? Screenshot)> TryExecuteLocalDevicePromptAsync(
        string prompt,
        bool allowDefaultSummary = false)
    {
        var trimmed = prompt.Trim();

        // Multi-step chaining (e.g. "open notepad and type Hello" or "focus chrome then hotkey ctrl+l")
        if (!trimmed.StartsWith('/') &&
            (trimmed.Contains(" and then ", StringComparison.OrdinalIgnoreCase) ||
             trimmed.Contains(" then ", StringComparison.OrdinalIgnoreCase) ||
             (trimmed.Contains(" and ", StringComparison.OrdinalIgnoreCase) &&
              (trimmed.StartsWith("open ", StringComparison.OrdinalIgnoreCase) ||
               trimmed.StartsWith("focus ", StringComparison.OrdinalIgnoreCase) ||
               trimmed.StartsWith("click ", StringComparison.OrdinalIgnoreCase)))))
        {
            var parts = trimmed.Split(new[] { " and then ", " then ", " and " }, StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
            if (parts.Length > 1 && parts.Length <= 6)
            {
                var stepLogs = new List<string>();
                var stepTools = new List<string>();
                ImageSource? lastShot = null;
                foreach (var step in parts)
                {
                    var (sOk, sReply, sTool, sShot) = await ExecuteSingleLocalUiPromptAsync(step, allowDefaultSummary: false);
                    stepLogs.Add($"• {step}: {(sOk ? sReply : "Unrecognized action")}");
                    if (!string.IsNullOrWhiteSpace(sTool)) stepTools.Add(sTool);
                    if (sShot is not null) lastShot = sShot;
                    if (!sOk) break;
                    await Task.Delay(250);
                }
                if (stepTools.Count > 0)
                {
                    return (true, string.Join("\n\n", stepLogs), string.Join(" -> ", stepTools), lastShot);
                }
            }
        }

        return await ExecuteSingleLocalUiPromptAsync(trimmed, allowDefaultSummary);
    }

    private async Task<(bool Handled, string Reply, string ToolLabel, ImageSource? Screenshot)> ExecuteSingleLocalUiPromptAsync(
        string rawPrompt,
        bool allowDefaultSummary = false)
    {
        var trimmed = rawPrompt.Trim();
        var lower = trimmed.ToLowerInvariant();

        if (lower.StartsWith("/pipe") || lower.StartsWith("/cpp") || lower.StartsWith("/h264") || lower.StartsWith("/selftest") ||
            (lower.Contains("pipe") && !lower.Contains("screenshot")))
        {
            var (pingOk, pingResp) = await NamedPipeBridge.PingHeartbeatAsync();
            var (statusOk, statusResp) = await NamedPipeBridge.RequestDesktopStatusAsync();
            var (_, probeResp) = await NamedPipeBridge.RunCppDesktopProbeAsync(
                0,
                Math.Clamp(_config.CppAgent.H264TargetFps, 1, 60),
                Math.Clamp(_config.CppAgent.H264BitrateKbps, 250, 50_000));
            string selfTestSection = "";
            if (lower.StartsWith("/selftest") || lower.Contains("selftest") || lower.Contains("self-test"))
            {
                var (stOk, stOut) = await NamedPipeBridge.RunCppSelfTestsAsync();
                selfTestSection = $"\n• C++ Self-Tests ({(stOk ? "7/7 PASSED" : "FAILED")}):\n{stOut}\n";
            }
            var pipeName = string.IsNullOrWhiteSpace(_config.DesktopPipeName) ? NamedPipeBridge.PipeName : _config.DesktopPipeName;
            var summary = $"C++20 Native Agent & Desktop Pipe Bridge (\\\\.\\pipe\\{pipeName}):\n" +
                          $"• Heartbeat: {(pingOk ? "OK" : "Unavailable")}\n" +
                          $"• Ping Response: {pingResp}\n" +
                          $"• Pipe Status ({(statusOk ? "OK" : "Unavailable")}): {statusResp}\n" +
                          $"• DXGI + Media Foundation H.264 Probe:\n{probeResp}" +
                          selfTestSection;
            return (true, summary, "Local Tool: cpp_native_agent_bridge", null);
        }

        if (lower.StartsWith("/som") || lower.Contains("set of marks") || lower.Contains("set-of-marks"))
        {
            if (!DeviceAgentRuntimeService.IsToolAllowed("set_of_marks_screenshot", _config, out var deny))
                return (true, deny, "Disallowed: set_of_marks_screenshot", null);
            var winFilter = lower.StartsWith("/som") ? trimmed[4..].Trim() : "";
            var argsJson = JsonSerializer.Serialize(new { window = string.IsNullOrWhiteSpace(winFilter) ? null : winFilter, max_marks = 40 });
            var (ok, json) = await _executor.ExecuteAsync("set_of_marks_screenshot", argsJson, _config, default);
            if (ok)
            {
                try
                {
                    using var doc = JsonDocument.Parse(json);
                    var b64 = doc.RootElement.TryGetProperty("image_base64", out var bProp) ? bProp.GetString() : null;
                    var path = doc.RootElement.TryGetProperty("path", out var pProp) ? pProp.GetString() : "";
                    var marksSummary = doc.RootElement.TryGetProperty("marks", out var mProp)
                        ? string.Join("\n", mProp.EnumerateArray().Take(20).Select(m =>
                            $"[{m.GetProperty("mark_id").GetString()}] {m.GetProperty("control_type").GetString()} \"{m.GetProperty("name").GetString()}\" ({m.GetProperty("center_x").GetInt32()}, {m.GetProperty("center_y").GetInt32()})"))
                        : "";
                    return (true, $"Captured Set-of-Marks UI map ({path}). Use `/click #<mark_id>` to click any badge:\n{marksSummary}", "Local Tool: set_of_marks_screenshot", DecodeBase64Image(b64));
                }
                catch { }
            }
            return (true, $"Set-of-Marks failed:\n{FormatJsonForChat(json)}", "Local Tool: set_of_marks_screenshot", null);
        }

        if (lower.StartsWith("/ui") || lower.Contains("ui element") || lower.Contains("ui tree") || lower.Contains("clickable"))
        {
            if (!DeviceAgentRuntimeService.IsToolAllowed("list_ui_elements", _config, out var deny))
                return (true, deny, "Disallowed: list_ui_elements", null);
            var argPart = lower.StartsWith("/ui") ? trimmed[3..].Trim() : "";
            var argsJson = string.IsNullOrWhiteSpace(argPart)
                ? "{\"limit\":60}"
                : JsonSerializer.Serialize(new { query = argPart, limit = 60 });
            var (_, json) = await _executor.ExecuteAsync("list_ui_elements", argsJson, _config, default);
            return (true, $"Interactive Windows UI Elements on {Environment.MachineName}:\n{FormatJsonForChat(json, maxLen: 2600)}", "Local Tool: list_ui_elements", null);
        }

        if (lower.StartsWith("/active") || lower.Contains("active window"))
        {
            return await ExecuteUiToolWithPolicyHintAsync("get_active_window", new { }, "Active foreground window");
        }

        if (lower.StartsWith("/focus ") || lower.StartsWith("focus "))
        {
            var target = lower.StartsWith("/focus ") ? trimmed[7..].Trim() : trimmed[6..].Trim();
            return await ExecuteUiToolWithPolicyHintAsync("focus_window", new { title = target }, $"Focused window matching '{target}'");
        }

        if (lower.StartsWith("/max ") || lower.StartsWith("maximize "))
        {
            var target = lower.StartsWith("/max ") ? trimmed[5..].Trim() : trimmed[9..].Trim();
            return await ExecuteUiToolWithPolicyHintAsync("maximize_window", new { title = target }, $"Maximized window matching '{target}'");
        }

        if (lower.StartsWith("/min ") || lower.StartsWith("minimize "))
        {
            var target = lower.StartsWith("/min ") ? trimmed[5..].Trim() : trimmed[9..].Trim();
            return await ExecuteUiToolWithPolicyHintAsync("minimize_window", new { title = target }, $"Minimized window matching '{target}'");
        }

        if (lower.StartsWith("/restore ") || lower.StartsWith("restore "))
        {
            var target = lower.StartsWith("/restore ") ? trimmed[9..].Trim() : trimmed[8..].Trim();
            return await ExecuteUiToolWithPolicyHintAsync("restore_window", new { title = target }, $"Restored window matching '{target}'");
        }

        if (lower.StartsWith("/close ") || lower.StartsWith("close window "))
        {
            var target = lower.StartsWith("/close ") ? trimmed[7..].Trim() : trimmed[13..].Trim();
            return await ExecuteUiToolWithPolicyHintAsync("close_window", new { title = target }, $"Sent close signal to '{target}'");
        }

        if (lower.StartsWith("/open ") || lower.StartsWith("/launch ") || lower.StartsWith("open ") || lower.StartsWith("launch "))
        {
            var appTarget = lower.StartsWith("/open ") ? trimmed[6..].Trim()
                : lower.StartsWith("/launch ") ? trimmed[8..].Trim()
                : lower.StartsWith("open ") ? trimmed[5..].Trim()
                : trimmed[7..].Trim();
            return await ExecuteUiToolWithPolicyHintAsync("desktop_open_application", new { app = appTarget }, $"Launched '{appTarget}'");
        }

        if (lower.StartsWith("/click ") || lower.StartsWith("/dblclick ") || lower.StartsWith("/rclick ") ||
            lower.StartsWith("click ") || lower.StartsWith("double click ") || lower.StartsWith("right click "))
        {
            string button = (lower.StartsWith("/rclick ") || lower.StartsWith("right click ")) ? "right" : "left";
            bool dbl = lower.StartsWith("/dblclick ") || lower.StartsWith("double click ");
            int offset = lower.StartsWith("/dblclick ") ? 10
                : lower.StartsWith("/rclick ") ? 8
                : lower.StartsWith("/click ") ? 7
                : lower.StartsWith("double click ") ? 13
                : lower.StartsWith("right click ") ? 12 : 6;
            var target = trimmed[offset..].Trim();
            var coords = target.Split(',', StringSplitOptions.TrimEntries);
            if (coords.Length == 2 && int.TryParse(coords[0], out int cx) && int.TryParse(coords[1], out int cy))
            {
                return await ExecuteUiToolWithPolicyHintAsync("mouse_click", new { x = cx, y = cy, button, double_click = dbl }, $"Clicked ({cx}, {cy})");
            }
            if (target.StartsWith('#'))
            {
                var mark = target[1..].Trim();
                return await ExecuteUiToolWithPolicyHintAsync("click_element", new { mark_id = mark, button, double_click = dbl }, $"Clicked mark #{mark}");
            }
            return await ExecuteUiToolWithPolicyHintAsync("click_element", new { query = target, button, double_click = dbl }, $"Clicked UI element '{target}'");
        }

        if (lower.StartsWith("/invoke ") || lower.StartsWith("invoke "))
        {
            var target = lower.StartsWith("/invoke ") ? trimmed[8..].Trim() : trimmed[7..].Trim();
            return await ExecuteUiToolWithPolicyHintAsync("invoke_element", new { name = target }, $"Invoked UI element '{target}'");
        }

        if (lower.StartsWith("/set ") || lower.StartsWith("set "))
        {
            var body = lower.StartsWith("/set ") ? trimmed[5..].Trim() : trimmed[4..].Trim();
            var eqIdx = body.IndexOf('=');
            if (eqIdx > 0)
            {
                var elem = body[..eqIdx].Trim();
                var val = body[(eqIdx + 1)..].Trim();
                return await ExecuteUiToolWithPolicyHintAsync("set_element_value", new { name = elem, value = val }, $"Set '{elem}' value");
            }
        }

        if (lower.StartsWith("/type ") || lower.StartsWith("type "))
        {
            var text = lower.StartsWith("/type ") ? trimmed[6..] : trimmed[5..];
            return await ExecuteUiToolWithPolicyHintAsync("keyboard_type", new { text }, $"Typed {text.Length} characters");
        }

        if (lower.StartsWith("/hotkey ") || lower.StartsWith("hotkey ") || lower.StartsWith("press "))
        {
            var combo = lower.StartsWith("/hotkey ") ? trimmed[8..].Trim()
                : lower.StartsWith("hotkey ") ? trimmed[7..].Trim() : trimmed[6..].Trim();
            return await ExecuteUiToolWithPolicyHintAsync("press_hotkey", new { combo }, $"Pressed hotkey '{combo}'");
        }

        if (lower.StartsWith("/scroll"))
        {
            var arg = trimmed.Length > 7 ? trimmed[7..].Trim() : "-240";
            int delta = arg.Equals("up", StringComparison.OrdinalIgnoreCase) ? 240
                : arg.Equals("down", StringComparison.OrdinalIgnoreCase) ? -240
                : (int.TryParse(arg, out var d) ? d : -240);
            return await ExecuteUiToolWithPolicyHintAsync("mouse_scroll", new { delta }, $"Scrolled wheel ({delta})");
        }

        if (lower.StartsWith("/clipboard"))
        {
            var arg = trimmed.Length > 10 ? trimmed[10..].Trim() : "";
            if (string.IsNullOrWhiteSpace(arg))
            {
                var (_, json) = await _executor.ExecuteAsync("get_clipboard", "{}", _config, default);
                return (true, $"Windows clipboard:\n{FormatJsonForChat(json)}", "Local Tool: get_clipboard", null);
            }
            return await ExecuteUiToolWithPolicyHintAsync("set_clipboard", new { text = arg }, "Updated Windows clipboard");
        }

        if (lower.StartsWith("/drives") || lower.Contains("list drives") || lower.Contains("all drives"))
        {
            return await ExecuteFullPcToolWithPermissionPromptAsync("list_drives", new { }, "Local drives on this PC");
        }

        if (lower.StartsWith("/dir") || lower.StartsWith("/ls") || lower.StartsWith("browse "))
        {
            var path = lower.StartsWith("/dir") ? trimmed[4..].Trim()
                : lower.StartsWith("/ls") ? trimmed[3..].Trim()
                : trimmed[7..].Trim();
            return await ExecuteFullPcToolWithPermissionPromptAsync("list_directory", new { path }, $"Directory listing ({(string.IsNullOrWhiteSpace(path) ? "User Profile" : path)})");
        }

        if (lower.StartsWith("/read "))
        {
            var path = trimmed[6..].Trim();
            return await ExecuteFullPcToolWithPermissionPromptAsync("read_pc_file", new { path }, $"File contents ({path})");
        }

        if (lower.StartsWith("/ps ") || lower.StartsWith("powershell "))
        {
            var cmd = lower.StartsWith("/ps ") ? trimmed[4..].Trim() : trimmed[11..].Trim();
            return await ExecuteFullPcToolWithPermissionPromptAsync("run_powershell", new { command = cmd }, $"PowerShell output");
        }

        if (lower.StartsWith("/shell ") || lower.StartsWith("run command "))
        {
            var cmd = lower.StartsWith("/shell ") ? trimmed[7..].Trim() : trimmed[12..].Trim();
            return await ExecuteFullPcToolWithPermissionPromptAsync("run_shell", new { command = cmd }, $"Shell output");
        }

        if (lower.StartsWith("/kill ") || lower.StartsWith("kill process "))
        {
            var target = lower.StartsWith("/kill ") ? trimmed[6..].Trim() : trimmed[13..].Trim();
            object argsObj = int.TryParse(target, out int pid) ? new { pid } : new { name = target };
            return await ExecuteFullPcToolWithPermissionPromptAsync("kill_process", argsObj, $"Process terminated ({target})");
        }

        if (lower.StartsWith("/screenshot") || lower.Contains("screenshot") || lower.Contains("capture") || lower.Contains("screen"))
        {
            if (!DeviceAgentRuntimeService.IsToolAllowed("capture_desktop_screenshot", _config, out var deny))
                return (true, deny, "Disallowed: capture_desktop_screenshot", null);
            var (ok, json) = await _executor.ExecuteAsync("capture_desktop_screenshot", "{}", _config, default);
            if (ok)
            {
                try
                {
                    using var doc = JsonDocument.Parse(json);
                    var b64 = doc.RootElement.TryGetProperty("image_base64", out var bProp) ? bProp.GetString() : null;
                    var path = doc.RootElement.TryGetProperty("path", out var pProp) ? pProp.GetString() : "";
                    var shot = DecodeBase64Image(b64);
                    return (true, $"Captured live screenshot from primary display on {Environment.MachineName}.\nSaved path: {path}", "Local Tool: capture_desktop_screenshot", shot);
                }
                catch { }
            }
            return (true, $"Screenshot result:\n{FormatJsonForChat(json)}", "Local Tool: capture_desktop_screenshot", null);
        }

        if (lower.StartsWith("/windows") || lower.Contains("open window") || lower.Contains("windows") || lower.Contains("handles"))
        {
            return await ExecuteUiToolWithPolicyHintAsync("list_desktop_windows", new { }, $"Visible desktop windows on {Environment.MachineName}");
        }

        if (lower.StartsWith("/monitor") || lower.Contains("monitor") || lower.Contains("display"))
        {
            return await ExecuteUiToolWithPolicyHintAsync("get_monitors", new { }, $"Attached monitors on {Environment.MachineName}");
        }

        if (lower.StartsWith("/workspace") || lower.Contains("workspace") || lower.Contains("files"))
        {
            return await ExecuteUiToolWithPolicyHintAsync("list_workspace", new { }, $"Workspace contents ({_config.Workspace})");
        }

        if (lower.StartsWith("/sysinfo") || allowDefaultSummary ||
            lower.Contains("cpu") || lower.Contains("memory") || lower.Contains("health") ||
            lower.Contains("system") || lower.Contains("process") || lower.Contains("disk"))
        {
            if (!DeviceAgentRuntimeService.IsToolAllowed("get_system_info", _config, out var deny))
                return (true, deny, "Disallowed: get_system_info", null);
            var (_, sysJson) = await _executor.ExecuteAsync("get_system_info", "{}", _config, default);
            var (_, resJson) = await _executor.ExecuteAsync("get_resource_usage", "{}", _config, default);
            var (_, procJson) = await _executor.ExecuteAsync("list_processes", "{}", _config, default);
            var msg = $"Device Health Snapshot ({Environment.MachineName}):\n\n" +
                      $"System Info:\n{FormatJsonForChat(sysJson)}\n\n" +
                      $"Resource Usage:\n{FormatJsonForChat(resJson)}\n\n" +
                      $"Top Processes:\n{FormatJsonForChat(procJson, maxLen: 1400)}";
            return (true, msg, "Local Tools: get_system_info, get_resource_usage, list_processes", null);
        }

        return (false, "", "", null);
    }

    private async Task<(bool Handled, string Reply, string ToolLabel, ImageSource? Screenshot)> ExecuteUiToolWithPolicyHintAsync(
        string toolName,
        object argsObj,
        string successTitle)
    {
        if (!DeviceAgentRuntimeService.IsToolAllowed(toolName, _config, out var denyReason))
        {
            return (true, $"Blocked by Agent Settings:\n{denyReason}", $"Disallowed: {toolName}", null);
        }
        var argsJson = JsonSerializer.Serialize(argsObj);
        var (ok, resultJson) = await _executor.ExecuteAsync(toolName, argsJson, _config, default);
        if (!ok && resultJson.Contains("interactive desktop control is disabled", StringComparison.OrdinalIgnoreCase))
        {
            return (
                true,
                $"Interactive Windows UI control is currently OFF.\n\nClick the **Full UI: Off** button in the top-right header of the Agents bar above to enable full mouse, keyboard, UI Automation, window control, and app launching.",
                $"Policy Guard: {toolName}",
                null);
        }
        var prefix = ok ? $"{successTitle}:\n" : $"Tool '{toolName}' failed:\n";
        return (true, prefix + FormatJsonForChat(resultJson), $"Local Tool: {toolName}", null);
    }

    private async Task<(bool Handled, string Reply, string ToolLabel, ImageSource? Screenshot)> ExecuteFullPcToolWithPermissionPromptAsync(
        string toolName,
        object argsObj,
        string successTitle)
    {
        if (!DeviceAgentRuntimeService.IsToolAllowed(toolName, _config, out var denyReason))
        {
            return (true, $"Blocked by Agent Settings:\n{denyReason}", $"Disallowed: {toolName}", null);
        }
        if (!_config.Policy.AllowFullPcAccess ||
            ((toolName is "run_powershell" or "run_shell" or "kill_process") &&
             (!_config.Policy.AllowTier4 || _config.Policy.MaxTierUnattended < 4 || (toolName != "kill_process" && !_config.Policy.AllowShell))))
        {
            var granted = PromptAndEnableFullPcAccess($"{toolName} ({JsonSerializer.Serialize(argsObj)})");
            if (!granted)
            {
                return (
                    true,
                    $"Permission denied by user: '{toolName}' was not executed.\nClick **Full PC: Off** in the header anytime if you want to allow PC-wide access.",
                    $"Denied by User: {toolName}",
                    null);
            }
        }

        var argsJson = JsonSerializer.Serialize(argsObj);
        var (ok, resultJson) = await _executor.ExecuteAsync(toolName, argsJson, _config, default);
        var prefix = ok ? $"{successTitle}:\n" : $"Tool '{toolName}' failed:\n";
        return (true, prefix + FormatJsonForChat(resultJson, maxLen: 2800), $"Local Tool: {toolName}", null);
    }

    private static string FormatJsonForChat(string json, int maxLen = 2000)
    {
        if (string.IsNullOrWhiteSpace(json)) return "(empty)";
        try
        {
            using var doc = JsonDocument.Parse(json);
            var pretty = JsonSerializer.Serialize(doc, new JsonSerializerOptions { WriteIndented = true });
            return pretty.Length > maxLen ? pretty[..maxLen] + "\n... (truncated)" : pretty;
        }
        catch
        {
            return json.Length > maxLen ? json[..maxLen] + "..." : json;
        }
    }

    private static ImageSource? DecodeBase64Image(string? base64)
    {
        if (string.IsNullOrWhiteSpace(base64)) return null;
        try
        {
            var clean = base64.Contains(',') ? base64[(base64.IndexOf(',') + 1)..] : base64;
            var bytes = Convert.FromBase64String(clean);
            using var ms = new MemoryStream(bytes);
            var bmp = new BitmapImage();
            bmp.BeginInit();
            bmp.CacheOption = BitmapCacheOption.OnLoad;
            bmp.StreamSource = ms;
            bmp.EndInit();
            bmp.Freeze();
            return bmp;
        }
        catch
        {
            return null;
        }
    }

    private async void OnAgentPipePingClick(object sender, RoutedEventArgs e)
    {
        var (ok, resp) = await NamedPipeBridge.PingHeartbeatAsync();
        AgPipeConsoleBox.Text = $"[HEARTBEAT_PING -> {(ok ? "OK" : "UNAVAILABLE")}]\n{resp}";
        RefreshLocalAgentsSummary();
    }

    private async void OnAgentPipeStatusClick(object sender, RoutedEventArgs e)
    {
        var (ok, resp) = await NamedPipeBridge.RequestDesktopStatusAsync();
        var (_, probeResp) = await NamedPipeBridge.RunCppDesktopProbeAsync(0);
        AgPipeConsoleBox.Text =
            $"[DESKTOP_STATUS_REPORT -> {(ok ? "OK" : "UNAVAILABLE")}]\n{resp}\n\n" +
            $"[C++20 DXGI + MEDIA FOUNDATION H.264 PROBE]\n{probeResp}";
        RefreshLocalAgentsSummary();
    }

    private async void OnAgentPipePushPolicyClick(object sender, RoutedEventArgs e)
    {
        var (ok, resp) = await NamedPipeBridge.UpdateSessionPermissionsAsync(_config.Policy);
        AgPipeConsoleBox.Text = $"[UPDATE_SESSION_PERMISSIONS -> {(ok ? "OK" : "UNAVAILABLE")}]\n{resp}";
        ShowToast(ok ? "Session policy pushed to pipe" : "Desktop pipe not running");
    }

    private async void OnAgentPipeReleaseInputClick(object sender, RoutedEventArgs e)
    {
        var (ok, resp) = await NamedPipeBridge.ReleaseAllInputAsync();
        AgPipeConsoleBox.Text = $"[RELEASE_ALL_INPUT -> {(ok ? "OK" : "UNAVAILABLE")}]\n{resp}";
        RefreshVisionCppMetricsUi();
        ShowToast(ok ? "Released all held input" : "Desktop pipe not running");
    }

    private async void OnAgentCppSelfTestClick(object sender, RoutedEventArgs e)
    {
        AgPipeConsoleBox.Text = "Running C++20 7-stage verification suite (ai_bridge_tests.exe)...";
        var (ok, output) = await NamedPipeBridge.RunCppSelfTestsAsync();
        AgPipeConsoleBox.Text = $"[C++20 SELF-TESTS -> {(ok ? "ALL 7/7 PASSED" : "FAILED")}]\n{output}";
        if (SetCppConsoleBox is not null) SetCppConsoleBox.Text = AgPipeConsoleBox.Text;
        RefreshLocalAgentsSummary();
        ShowToast(ok ? "C++ 7/7 self-tests passed" : "C++ self-test reported an error");
    }

    private async void OnAgentRestartPipeClick(object sender, RoutedEventArgs e)
    {
        AgPipeConsoleBox.Text = "Restarting C++ supervisor named-pipe server (ai_bridge_service.exe --pipe-server)...";
        var pipeName = string.IsNullOrWhiteSpace(_config.DesktopPipeName) ? NamedPipeBridge.PipeName : _config.DesktopPipeName;
        bool ok = await Task.Run(() => NamedPipeBridge.RestartCppSupervisor(
            pipeName,
            NamedPipeBridge.ActiveHmacSecret,
            _config.ControlUrl,
            _deviceToken,
            _config.CppAgent));
        var (_, statusResp) = await NamedPipeBridge.RequestDesktopStatusAsync();
        AgPipeConsoleBox.Text = $"[RESTART C++ PIPE SERVER -> {(ok ? "ONLINE" : "OFFLINE")}]\n{statusResp}";
        if (SetCppConsoleBox is not null) SetCppConsoleBox.Text = AgPipeConsoleBox.Text;
        RefreshLocalAgentsSummary();
        RefreshCppEngineStatusUi();
        ShowToast(ok ? "C++ pipe server restarted" : "Failed to restart C++ pipe server");
    }

    private void RefreshCppEngineStatusUi()
    {
        if (SetCppStatusBadge is null) return;
        bool dllUp = CppNativeBridge.IsAvailable();
        var pipeName = string.IsNullOrWhiteSpace(_config.DesktopPipeName) ? NamedPipeBridge.PipeName : _config.DesktopPipeName;
        bool pipeUp = NamedPipeBridge.IsDesktopAgentAvailable(pipeName);
        SetCppStatusBadge.Text = (dllUp, pipeUp) switch
        {
            (true, true) => $"C++ DLL & Pipe Active ({pipeName})",
            (true, false) => "C++ DLL Active · Pipe Stopped",
            (false, true) => "Pipe Active · DLL Missing",
            _ => "C++ Engine Offline",
        };
        SetCppStatusBadge.Foreground = (System.Windows.Media.Brush)FindResource((dllUp && pipeUp) ? "SuccessText" : (dllUp || pipeUp ? "WarningText" : "ErrorText"));
        RefreshVisionCppMetricsUi();
    }

    private async void OnCppPullTurnClick(object sender, RoutedEventArgs e)
    {
        await HydrateTurnSettingsIfNeededAsync(force: true);
    }

    private async Task HydrateTurnSettingsIfNeededAsync(bool force)
    {
        _config.CppAgent ??= new CppAgentSettings();
        if (!force &&
            !string.IsNullOrWhiteSpace(_config.CppAgent.TurnUrl) &&
            !string.IsNullOrWhiteSpace(_config.CppAgent.TurnUsername) &&
            !string.IsNullOrWhiteSpace(_config.CppAgent.TurnCredential))
        {
            return;
        }

        if (string.IsNullOrWhiteSpace(_config.ControlUrl))
        {
            if (SetCppTurnHintText is not null)
            {
                SetCppTurnHintText.Text = "Set a Control Plane URL first.";
            }
            return;
        }

        if (SetCppTurnHintText is not null)
        {
            SetCppTurnHintText.Text = "Pulling TURN from control plane…";
        }

        var (ok, ice, detail) = await _client.FetchIceServersAsync(
            _config.ControlUrl,
            string.IsNullOrWhiteSpace(_deviceToken) ? null : _deviceToken,
            string.IsNullOrWhiteSpace(_operatorToken) ? null : _operatorToken,
            _operatorIsAdminToken);

        if (!ok || !ControlPlaneClient.TryExtractTurnSettings(ice, out var turnUrl, out var user, out var cred))
        {
            // Still seed the URL from the plane host so the fields aren't blank.
            if (string.IsNullOrWhiteSpace(_config.CppAgent.TurnUrl))
            {
                var derived = ControlPlaneClient.DeriveTurnUrlFromControlPlane(_config.ControlUrl);
                if (!string.IsNullOrWhiteSpace(derived))
                {
                    _config.CppAgent.TurnUrl = derived;
                    if (SetCppTurnUrlBox is not null) SetCppTurnUrlBox.Text = derived;
                }
            }
            if (SetCppTurnHintText is not null)
            {
                SetCppTurnHintText.Text = string.IsNullOrWhiteSpace(detail)
                    ? "Could not pull TURN credentials."
                    : detail;
            }
            if (force)
            {
                ShowToast("TURN pull failed — check operator login or plane deploy.");
            }
            return;
        }

        _config.CppAgent.TurnUrl = turnUrl;
        _config.CppAgent.TurnUsername = user;
        _config.CppAgent.TurnCredential = cred;
        _configService.SaveConfig(_config);
        if (SetCppTurnUrlBox is not null) SetCppTurnUrlBox.Text = turnUrl;
        if (SetCppTurnUserBox is not null) SetCppTurnUserBox.Text = user;
        if (SetCppTurnCredBox is not null) SetCppTurnCredBox.Password = cred;
        if (SetCppTurnHintText is not null)
        {
            SetCppTurnHintText.Text = detail;
        }

        // Push into C++ runtime config so the next WebRTC launch uses TURN.
        ApplyRuntimeSettings();
        if (force)
        {
            ShowToast($"TURN loaded: {turnUrl}");
            if (SetCppConsoleBox is not null)
            {
                SetCppConsoleBox.Text =
                    $"[TURN FROM CONTROL PLANE]\nurl={turnUrl}\nusername={user}\ncredential=(set, {cred.Length} chars)\n{detail}";
            }
        }
    }

    private void OnCppApplyBitrateClick(object sender, RoutedEventArgs e)
    {
        int kbps = int.TryParse(SetCppBitrateBox.Text, out var parsed) ? Math.Clamp(parsed, 250, 50_000) : 4500;
        int fps = int.TryParse(SetCppFpsBox.Text, out var parsedFps) ? Math.Clamp(parsedFps, 1, 60) : 30;
        _config.CppAgent ??= new CppAgentSettings();
        _config.CppAgent.H264BitrateKbps = kbps;
        _config.CppAgent.H264TargetFps = fps;
        _configService.SaveConfig(_config);

        bool appliedLive = CppNativeBridge.TrySetEncoderBitrate(kbps);
        CppNativeBridge.DefaultH264Fps = fps;
        var json = CppNativeBridge.ProbeCaptureAndH264Json(0, fps, kbps);
        SetCppConsoleBox.Text = $"[LIVE H.264 BITRATE UPDATE -> {kbps} kbps @ {fps} FPS (Encoder Active: {appliedLive})]\n{json}";
        RefreshVisionCppMetricsUi();
        ShowToast($"H.264 target set to {kbps} kbps @ {fps} FPS");
    }

    private async void OnCppProbeEngineClick(object sender, RoutedEventArgs e)
    {
        int kbps = int.TryParse(SetCppBitrateBox.Text, out var parsed) ? Math.Clamp(parsed, 250, 50_000) : _config.CppAgent.H264BitrateKbps;
        int fps = int.TryParse(SetCppFpsBox.Text, out var parsedFps) ? Math.Clamp(parsedFps, 1, 60) : _config.CppAgent.H264TargetFps;
        SetCppConsoleBox.Text = "Probing C++20 DXGI Desktop Duplication & Media Foundation H.264 encoder...";
        var (ok, resp) = await NamedPipeBridge.RunCppDesktopProbeAsync(0, fps, kbps);
        SetCppConsoleBox.Text = resp;
        RefreshCppEngineStatusUi();
        ShowToast(ok ? "C++ DXGI + H.264 probe succeeded" : "C++ probe completed with fallback");
    }

    private async void OnCppRunSelfTestsClick(object sender, RoutedEventArgs e)
    {
        SetCppConsoleBox.Text = "Executing C++20 7-stage verification suite (ai_bridge_tests.exe)...";
        var (ok, output) = await NamedPipeBridge.RunCppSelfTestsAsync();
        SetCppConsoleBox.Text = $"[ai_bridge_tests.exe -> {(ok ? "ALL 7/7 PASSED" : "FAILED")}]\n{output}";
        RefreshCppEngineStatusUi();
        ShowToast(ok ? "All 7/7 C++ self-tests passed" : "C++ self-test failed");
    }

    private void OnCppSystemHealthClick(object sender, RoutedEventArgs e)
    {
        var raw = CppNativeBridge.TryGetSystemHealthJson() ?? "{\"available\":false}";
        var monitors = CppNativeBridge.TryEnumerateMonitors();
        SetCppConsoleBox.Text =
            $"[C++20 NATIVE SYSTEM HEALTH (aibridge_get_system_health_json)]\n{PrettyJson(raw)}\n\n" +
            $"[C++20 DXGI ENUMERATED DISPLAYS ({monitors?.Count ?? 0})]\n" +
            JsonSerializer.Serialize(monitors, new JsonSerializerOptions { WriteIndented = true });
        RefreshCppEngineStatusUi();
        ShowToast("Queried C++ native system health");
    }

    private async void OnCppPingPipeClick(object sender, RoutedEventArgs e)
    {
        var (ok, resp) = await NamedPipeBridge.PingHeartbeatAsync();
        var (_, statusResp) = await NamedPipeBridge.RequestDesktopStatusAsync();
        SetCppConsoleBox.Text = $"[HEARTBEAT_PING -> {(ok ? "OK" : "OFFLINE")}]\n{resp}\n\n[DESKTOP_STATUS_REPORT]\n{statusResp}";
        RefreshCppEngineStatusUi();
        ShowToast(ok ? "C++ Named-Pipe HMAC ping OK" : "C++ Named-Pipe offline");
    }

    private async void OnCppRestartSupervisorClick(object sender, RoutedEventArgs e)
    {
        SetCppConsoleBox.Text = "Restarting C++ supervisor named-pipe server...";
        var pipeName = string.IsNullOrWhiteSpace(SetPipeNameBox.Text) ? _config.DesktopPipeName : SetPipeNameBox.Text.Trim();
        bool ok = await Task.Run(() => NamedPipeBridge.RestartCppSupervisor(
            pipeName,
            NamedPipeBridge.ActiveHmacSecret,
            _config.ControlUrl,
            _deviceToken,
            _config.CppAgent));
        var (_, statusResp) = await NamedPipeBridge.RequestDesktopStatusAsync();
        SetCppConsoleBox.Text = $"[RESTART C++ PIPE SERVER -> {(ok ? "ONLINE" : "OFFLINE")}]\n{statusResp}";
        RefreshCppEngineStatusUi();
        RefreshLocalAgentsSummary();
        ShowToast(ok ? "C++ pipe server restarted" : "Could not start C++ pipe server");
    }

    private void OnCppStopSupervisorClick(object sender, RoutedEventArgs e)
    {
        NamedPipeBridge.StopManagedCppSupervisor();
        try
        {
            foreach (var p in Process.GetProcessesByName("ai_bridge_service"))
            {
                try { p.Kill(); p.Dispose(); } catch { }
            }
        }
        catch { }
        SetCppConsoleBox.Text = "Stopped C++ supervisor named-pipe server (ai_bridge_service.exe).";
        RefreshCppEngineStatusUi();
        RefreshLocalAgentsSummary();
        ShowToast("Stopped C++ pipe server");
    }

    private async void OnCppReleaseKeysClick(object sender, RoutedEventArgs e)
    {
        int releasedLocal = CppNativeBridge.ReleaseAllKeys();
        var (ok, resp) = await NamedPipeBridge.ReleaseAllInputAsync();
        SetCppConsoleBox.Text = $"[RELEASE_ALL_INPUT (In-Process Released: {releasedLocal}, Pipe: {(ok ? "OK" : "OFFLINE")})]\n{resp}";
        RefreshCppEngineStatusUi();
        ShowToast("Released all held keys & mouse buttons");
    }

    private void OnAgentAuthModeChanged(object sender, SelectionChangedEventArgs e)
    {
        if (AgCredsPanel is null || AgTokenPanel is null) return;
        bool isToken = AgAuthModeCombo.SelectedIndex == 1;
        AgCredsPanel.Visibility = isToken ? Visibility.Collapsed : Visibility.Visible;
        AgTokenPanel.Visibility = isToken ? Visibility.Visible : Visibility.Collapsed;
    }

    private async void OnAgentsAuthenticateAndLoadClick(object sender, RoutedEventArgs e)
    {
        bool isTokenMode = AgAuthModeCombo.SelectedIndex == 1;
        var adminTok = isTokenMode ? AgAdminTokenBox.Password.Trim() : "";
        var user = AgUserBox.Text.Trim();
        var pass = AgPassBox.Password;

        if (isTokenMode && string.IsNullOrWhiteSpace(adminTok))
        {
            ShowToast("Enter X-Admin-Token first");
            return;
        }
        if (!isTokenMode && (string.IsNullOrWhiteSpace(user) || string.IsNullOrWhiteSpace(pass)))
        {
            ShowToast("Enter username and password");
            return;
        }

        AgAuthStatusText.Text = "Authenticating with Control Plane...";
        var (ok, tok, isAdmin, detail) = await _client.LoginOperatorAsync(_config.ControlUrl, user, pass, adminTok);
        if (!ok)
        {
            AgAuthStatusText.Text = detail;
            AppendLog("err", $"Agents auth failed: {detail}");
            return;
        }

        _operatorToken = tok;
        _operatorIsAdminToken = isAdmin;
        AgAuthStatusText.Text = detail;
        RefreshLocalAgentsSummary();
        await LoadFleetAgentsAndRolesAsync();
    }

    private async void OnAgentsRefreshClick(object sender, RoutedEventArgs e)
    {
        if (string.IsNullOrWhiteSpace(_operatorToken))
        {
            ShowToast("Authenticate above to load scheduled device agents");
            return;
        }
        await LoadFleetAgentsAndRolesAsync();
    }

    private async Task LoadFleetAgentsAndRolesAsync()
    {
        if (string.IsNullOrWhiteSpace(_deviceToken) || string.IsNullOrWhiteSpace(_config.DeviceId))
        {
            AgAuthStatusText.Text = "Device must be enrolled first — agents are restricted to this installed device.";
            _customAgents.Clear();
            return;
        }
        var (ok, custom, _, _, detail) = await _client.FetchAgentsAndRolesAsync(
            _config.ControlUrl,
            _operatorToken,
            _operatorIsAdminToken,
            _config.DeviceId);
        if (!ok)
        {
            AgAuthStatusText.Text = detail;
            AppendLog("warn", $"Load device agents: {detail}");
            return;
        }

        _customAgents.Clear();
        foreach (var item in custom) _customAgents.Add(item);

        AgAuthStatusText.Text = detail;
        AgPipeConsoleBox.Text = $"[Control Plane] {detail}";
    }

    private async void OnCreateCustomAgentClick(object sender, RoutedEventArgs e)
    {
        if (string.IsNullOrWhiteSpace(_deviceToken) || string.IsNullOrWhiteSpace(_config.DeviceId))
        {
            ShowToast("Enroll this Windows device first");
            return;
        }
        if (string.IsNullOrWhiteSpace(_operatorToken))
        {
            ShowToast("Authenticate as operator/admin first");
            return;
        }
        var provider = (AgNewProviderCombo.SelectedItem as ComboBoxItem)?.Content?.ToString() ?? "openai";
        int interval = int.TryParse(AgNewIntervalBox.Text.Trim(), out var iv) ? iv : 300;
        var (ok, detail) = await _client.CreateCustomAgentAsync(
            _config.ControlUrl,
            _operatorToken,
            _operatorIsAdminToken,
            AgNewNameBox.Text,
            AgNewInstructionBox.Text,
            provider,
            "",
            interval,
            _config.DeviceId);
        ShowToast(detail);
        if (ok) await LoadFleetAgentsAndRolesAsync();
    }

    private async void OnToggleCustomAgentClick(object sender, RoutedEventArgs e)
    {
        if (string.IsNullOrWhiteSpace(_operatorToken)) return;
        if (sender is System.Windows.Controls.Button btn && btn.Tag is CustomAgentItem item)
        {
            var (ok, detail) = await _client.SetCustomAgentEnabledAsync(
                _config.ControlUrl,
                _operatorToken,
                _operatorIsAdminToken,
                item.Id,
                !item.Enabled);
            ShowToast(detail);
            if (ok) await LoadFleetAgentsAndRolesAsync();
        }
    }

    private async void OnRunCustomAgentClick(object sender, RoutedEventArgs e)
    {
        if (string.IsNullOrWhiteSpace(_operatorToken)) return;
        if (sender is System.Windows.Controls.Button btn && btn.Tag is CustomAgentItem item)
        {
            var (_, detail) = await _client.RunCustomAgentNowAsync(
                _config.ControlUrl,
                _operatorToken,
                _operatorIsAdminToken,
                item.Id);
            ShowToast(detail);
        }
    }
}
