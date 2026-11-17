using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Forms;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Shapes;
using System.Windows.Threading;
using AIBridgeClient.Models;
using AIBridgeClient.Services;

namespace AIBridgeClient;

public partial class MainWindow : Window
{
    private readonly ConfigService _configService = new();
    private readonly SystemMonitorService _monitor = new();
    private readonly NativeToolExecutor _executor;
    private readonly ControlPlaneClient _client;

    private AgentGuiConfig _config;
    private string _deviceToken;
    private List<TaskHistoryRecord> _taskHistory;
    private string _taskFilter = "all";
    private string _lastScreenshotPath = "";
    private bool _tokenVisible;
    private bool _allowRealClose;

    private readonly Queue<double> _cpuHistory = new();
    private readonly Queue<double> _memHistory = new();
    private const int SparklineCapacity = 60;

    private readonly DispatcherTimer _monitorTimer;
    private NotifyIcon? _trayIcon;
    private readonly System.Windows.Controls.Button[] _navButtons;

    [DllImport("dwmapi.dll", PreserveSig = true)]
    private static extern int DwmSetWindowAttribute(IntPtr hwnd, int attr, ref int attrValue, int attrSize);

    private static readonly (string Title, string Subtitle)[] TabMeta =
    {
        ("Overview", "Agent status, connection, and live activity"),
        ("Task Explorer", "Every task this device received, with full payloads"),
        ("System Monitor", "Live hardware telemetry & rolling 60s sparklines"),
        ("Desktop Vision", "Native Windows GDI+ desktop screenshot capture"),
        ("Security & Policy", "4-tier guardrails and allowed application matrix"),
        ("Settings", "Control Plane endpoint, DPAPI token, and startup options"),
    };

    public MainWindow()
    {
        InitializeComponent();

        _navButtons = new[]
        {
            NavOverviewBtn,
            NavTasksBtn,
            NavMonitorBtn,
            NavVisionBtn,
            NavSecurityBtn,
            NavSettingsBtn,
        };

        _config = _configService.LoadConfig();
        _deviceToken = _configService.LoadToken();
        _taskHistory = _configService.LoadTaskHistory();

        var screenshotDir = System.IO.Path.Combine(_configService.ConfigDir, "screenshots");
        _executor = new NativeToolExecutor(_monitor, screenshotDir);
        _client = new ControlPlaneClient(_monitor, _executor);

        _client.LogMessage += (level, msg) => Dispatcher.Invoke(() => AppendLog(level, msg));
        _client.StatusChanged += (status, tone) => Dispatcher.Invoke(() => UpdateConnectionStatus(status, tone));
        _client.TaskRecorded += rec => Dispatcher.Invoke(() => RecordTask(rec));

        _monitorTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
        _monitorTimer.Tick += (_, _) => RefreshSystemMonitor();
        _monitorTimer.Start();

        SetupSystemTray();
        LoadConfigIntoUi();
        RefreshTaskGrid();
        RefreshSystemMonitor();
        AppendLog("info", "AI Bridge Native C# Client initialized.");

        if (_config.AutoConnect && !string.IsNullOrWhiteSpace(_deviceToken))
        {
            _client.Start(_config, _deviceToken);
            HeaderToggleAgentBtn.Content = "Stop Agent";
        }
    }

    protected override void OnSourceInitialized(EventArgs e)
    {
        base.OnSourceInitialized(e);
        var hwnd = new WindowInteropHelper(this).Handle;
        int dark = 1;
        DwmSetWindowAttribute(hwnd, 20, ref dark, sizeof(int)); // DWMWA_USE_IMMERSIVE_DARK_MODE
        int round = 2;
        DwmSetWindowAttribute(hwnd, 33, ref round, sizeof(int)); // DWMWA_WINDOW_CORNER_PREFERENCE = DWMWCP_ROUND
    }

    private void OnMinimizeWindowClick(object sender, RoutedEventArgs e) => WindowState = WindowState.Minimized;

    private void OnMaximizeRestoreClick(object sender, RoutedEventArgs e) =>
        WindowState = WindowState == WindowState.Maximized ? WindowState.Normal : WindowState.Maximized;

    private void OnCloseToTrayClick(object sender, RoutedEventArgs e) => Close();

    private void SetupSystemTray()
    {
        var menu = new ContextMenuStrip();
        menu.Items.Add("Show Window", null, (_, _) => ShowFromTray());
        menu.Items.Add("Start / Stop Agent", null, (_, _) => OnToggleAgentClick(this, new RoutedEventArgs()));
        menu.Items.Add("Ping Control Plane", null, (_, _) => OnPingClick(this, new RoutedEventArgs()));
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("Exit", null, (_, _) => ExitApplication());

        _trayIcon = new NotifyIcon
        {
            Text = "AI Bridge Windows Client",
            Icon = SystemIcons.Application,
            Visible = true,
            ContextMenuStrip = menu,
        };
        _trayIcon.DoubleClick += (_, _) => ShowFromTray();
    }

    private void ShowFromTray()
    {
        Show();
        WindowState = WindowState.Normal;
        Activate();
    }

    protected override void OnClosing(System.ComponentModel.CancelEventArgs e)
    {
        if (!_allowRealClose)
        {
            e.Cancel = true;
            Hide();
            _trayIcon?.ShowBalloonTip(2000, "AI Bridge", "Client minimized to system tray.", ToolTipIcon.Info);
            return;
        }
        base.OnClosing(e);
    }

    private async void ExitApplication()
    {
        _allowRealClose = true;
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
        }

        var activeStyle = (Style)FindResource("NavButtonActiveStyle");
        var normalStyle = (Style)FindResource("NavButtonStyle");
        for (int i = 0; i < _navButtons.Length; i++)
        {
            _navButtons[i].Style = i == idx ? activeStyle : normalStyle;
        }
    }

    private void LoadConfigIntoUi()
    {
        TitleBarHostText.Text = $"{Environment.MachineName.ToUpperInvariant()} · {_config.DeviceId}";
        OverviewEndpointText.Text = $"Control Plane: {_config.ControlUrl}";
        FooterWorkspaceText.Text = $"Workspace: {_config.Workspace}";
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

        AllowedAppsList.Items.Clear();
        foreach (var app in _config.Policy.AllowedApps)
        {
            AllowedAppsList.Items.Add(app);
        }
    }

    private void LoadSettingsIntoUi()
    {
        SetControlUrlBox.Text = _config.ControlUrl;
        SetDeviceIdBox.Text = _config.DeviceId;
        SetWorkspaceBox.Text = _config.Workspace;
        SetPollIntervalBox.Text = _config.PollIntervalSeconds.ToString();
        SetAutoStartChk.IsChecked = _configService.IsAutoStartEnabled();
        SetAutoConnectChk.IsChecked = _config.AutoConnect;
        SetTokenPasswordBox.Password = _deviceToken;
        SetTokenPlainBox.Text = _deviceToken;
    }

    private void UpdateConnectionStatus(string status, string tone)
    {
        SidebarStatusText.Text = status;
        KpiStatusText.Text = status;
        var brush = tone switch
        {
            "ok" => new SolidColorBrush(System.Windows.Media.Color.FromRgb(16, 185, 129)),
            "warn" => new SolidColorBrush(System.Windows.Media.Color.FromRgb(245, 158, 11)),
            "err" => new SolidColorBrush(System.Windows.Media.Color.FromRgb(239, 68, 68)),
            _ => new SolidColorBrush(System.Windows.Media.Color.FromRgb(100, 116, 139)),
        };
        SidebarStatusDot.Fill = brush;
        KpiStatusText.Foreground = brush;
        HeaderToggleAgentBtn.Content = _client.IsRunning ? "Stop Agent" : "Start Agent";
        FooterStatusText.Text = $"Status: {status}";
    }

    private void AppendLog(string level, string message)
    {
        var line = $"[{DateTimeOffset.Now:HH:mm:ss}] [{level.ToUpperInvariant()}] {message}{Environment.NewLine}";
        ActivityLogBox.AppendText(line);
        ActivityLogBox.ScrollToEnd();
        FooterStatusText.Text = message;
    }

    private void RecordTask(TaskHistoryRecord rec)
    {
        _taskHistory.Insert(0, rec);
        if (_taskHistory.Count > 500)
        {
            _taskHistory.RemoveRange(500, _taskHistory.Count - 500);
        }
        _configService.SaveTaskHistory(_taskHistory);
        KpiTasksText.Text = _taskHistory.Count.ToString();
        RefreshTaskGrid();
    }

    private void RefreshTaskGrid()
    {
        var q = TaskSearchBox?.Text?.Trim() ?? "";
        var filtered = _taskHistory.Where(t =>
            (_taskFilter == "all" || string.Equals(t.Status, _taskFilter, StringComparison.OrdinalIgnoreCase)) &&
            (string.IsNullOrEmpty(q) ||
             t.Id.Contains(q, StringComparison.OrdinalIgnoreCase) ||
             t.Tool.Contains(q, StringComparison.OrdinalIgnoreCase)))
            .ToList();
        TasksGrid.ItemsSource = filtered;
    }

    private void RefreshSystemMonitor()
    {
        var t = _monitor.Sample();

        _cpuHistory.Enqueue(t.CpuPercent);
        if (_cpuHistory.Count > SparklineCapacity) _cpuHistory.Dequeue();
        _memHistory.Enqueue(t.MemoryPercent);
        if (_memHistory.Count > SparklineCapacity) _memHistory.Dequeue();

        OverviewCpuLiveText.Text = $"{t.CpuPercent:F1} %";
        MonCpuText.Text = $"{t.CpuPercent:F1} %";
        MonCpuBar.Value = t.CpuPercent;

        MonMemText.Text = $"{t.MemoryUsedGb:F1} / {t.MemoryTotalGb:F1} GB ({t.MemoryPercent:F0}%)";
        MonMemBar.Value = t.MemoryPercent;

        MonDiskText.Text = $"{t.DiskUsedGb:F1} / {t.DiskTotalGb:F1} GB ({t.DiskPercent:F1}%)";
        MonDiskBar.Value = t.DiskPercent;

        MonNetText.Text = $"Down {t.NetworkRecvKbps:F1} KB/s | Up {t.NetworkSentKbps:F1} KB/s";
        MonProcText.Text = $"Processes: {t.ProcessCount} | Uptime: {(int)t.SystemUptime.TotalHours}h {t.SystemUptime.Minutes}m";

        RenderSparkline(OverviewCpuSparkCanvas, OverviewCpuSparkLine, _cpuHistory);
        RenderSparkline(MonCpuSparkCanvas, MonCpuSparkLine, _cpuHistory);
        RenderSparkline(MonMemSparkCanvas, MonMemSparkLine, _memHistory);
    }

    private static void RenderSparkline(Canvas canvas, Polyline line, IReadOnlyCollection<double> values)
    {
        var w = canvas.ActualWidth;
        var h = canvas.ActualHeight;
        if (w <= 4 || h <= 4 || values.Count < 2) return;

        var pts = new PointCollection(values.Count);
        int i = 0;
        double stepX = w / (SparklineCapacity - 1);
        double startX = w - (values.Count - 1) * stepX;

        foreach (var v in values)
        {
            double x = startX + i * stepX;
            double y = h - 3 - (Math.Clamp(v, 0, 100) / 100.0) * (h - 6);
            pts.Add(new System.Windows.Point(x, y));
            i++;
        }
        line.Points = pts;
    }

    // --- Event Handlers ---

    private async void OnToggleAgentClick(object sender, RoutedEventArgs e)
    {
        if (_client.IsRunning)
        {
            await _client.StopAsync();
            HeaderToggleAgentBtn.Content = "Start Agent";
            return;
        }

        if (string.IsNullOrWhiteSpace(_deviceToken))
        {
            System.Windows.MessageBox.Show(
                "Please configure a Device Token in Settings before starting the agent.",
                "Missing Device Token",
                MessageBoxButton.OK,
                MessageBoxImage.Warning);
            SelectTab(5);
            return;
        }

        _client.Start(_config, _deviceToken);
        HeaderToggleAgentBtn.Content = "Stop Agent";
    }

    private async void OnPingClick(object sender, RoutedEventArgs e)
    {
        var (ok, latencyMs, detail) = await _client.PingAsync(_config.ControlUrl);
        KpiLatencyText.Text = ok ? $"{latencyMs:F1} ms" : "ERR";
        AppendLog(ok ? "ok" : "err", $"Ping {_config.ControlUrl}: {detail} ({latencyMs:F1} ms)");
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
            return;
        }
        var (ok, newTok, detail) = await _client.RotateTokenAsync(_config.ControlUrl, _deviceToken);
        if (ok)
        {
            _deviceToken = newTok;
            _configService.SaveToken(newTok);
            LoadSettingsIntoUi();
            AppendLog("ok", "Device token rotated and sealed with DPAPI.");
        }
        else
        {
            AppendLog("err", $"Token rotation failed: {detail}");
        }
    }

    private void OnClearLogClick(object sender, RoutedEventArgs e) => ActivityLogBox.Clear();

    private void OnTaskFilterClick(object sender, RoutedEventArgs e)
    {
        if (sender is System.Windows.Controls.Button btn && btn.Tag is string f)
        {
            _taskFilter = f;
            RefreshTaskGrid();
        }
    }

    private void OnTaskSearchChanged(object sender, TextChangedEventArgs e) => RefreshTaskGrid();

    private void OnTaskSelected(object sender, SelectionChangedEventArgs e)
    {
        if (TasksGrid.SelectedItem is TaskHistoryRecord rec)
        {
            TaskInspectorBox.Text = $"ID: {rec.Id}\nTool: {rec.Tool}\nStatus: {rec.Status}\nArgs:\n{rec.ArgsJson}\n\nResult:\n{rec.ResultPreview}";
        }
    }

    private void OnCopyTaskPayloadClick(object sender, RoutedEventArgs e)
    {
        if (!string.IsNullOrEmpty(TaskInspectorBox.Text))
        {
            System.Windows.Clipboard.SetText(TaskInspectorBox.Text);
            FooterStatusText.Text = "Copied task payload to clipboard.";
        }
    }

    private void OnClearTasksClick(object sender, RoutedEventArgs e)
    {
        _taskHistory.Clear();
        _configService.SaveTaskHistory(_taskHistory);
        KpiTasksText.Text = "0";
        TaskInspectorBox.Clear();
        RefreshTaskGrid();
    }

    private void OnCaptureScreenshotClick(object sender, RoutedEventArgs e)
    {
        try
        {
            var dir = System.IO.Path.Combine(_configService.ConfigDir, "screenshots");
            var shot = ScreenCaptureService.CapturePrimaryScreen(dir);
            _lastScreenshotPath = shot.FilePath;
            VisionMetaText.Text = $"{shot.Width}x{shot.Height} | {shot.SizeBytes / 1024} KB | {System.IO.Path.GetFileName(shot.FilePath)}";

            var bmp = new BitmapImage();
            bmp.BeginInit();
            bmp.CacheOption = BitmapCacheOption.OnLoad;
            bmp.UriSource = new Uri(shot.FilePath);
            bmp.EndInit();
            VisionPreviewImage.Source = bmp;

            AppendLog("ok", $"Captured desktop screenshot: {shot.FilePath}");
        }
        catch (Exception ex)
        {
            AppendLog("err", $"Screenshot capture failed: {ex.Message}");
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
            FooterStatusText.Text = "Copied screenshot path to clipboard.";
        }
    }

    private void OnPresetFullClick(object sender, RoutedEventArgs e)
    {
        PolAllowWritesChk.IsChecked = true;
        PolAllowTier3Chk.IsChecked = true;
        PolAllowTier4Chk.IsChecked = false;
        PolAllowShellChk.IsChecked = false;
        PolAllowDesktopChk.IsChecked = true;
    }

    private void OnPresetWorkstationClick(object sender, RoutedEventArgs e)
    {
        PolAllowWritesChk.IsChecked = true;
        PolAllowTier3Chk.IsChecked = true;
        PolAllowTier4Chk.IsChecked = false;
        PolAllowShellChk.IsChecked = false;
        PolAllowDesktopChk.IsChecked = false;
    }

    private void OnPresetLockdownClick(object sender, RoutedEventArgs e)
    {
        PolAllowWritesChk.IsChecked = false;
        PolAllowTier3Chk.IsChecked = false;
        PolAllowTier4Chk.IsChecked = false;
        PolAllowShellChk.IsChecked = false;
        PolAllowDesktopChk.IsChecked = false;
    }

    private void OnAddAllowedAppClick(object sender, RoutedEventArgs e)
    {
        var app = NewAppBox.Text.Trim();
        if (!string.IsNullOrEmpty(app) && !_config.Policy.AllowedApps.Contains(app, StringComparer.OrdinalIgnoreCase))
        {
            _config.Policy.AllowedApps.Add(app);
            AllowedAppsList.Items.Add(app);
            NewAppBox.Clear();
        }
    }

    private void OnRemoveAllowedAppClick(object sender, RoutedEventArgs e)
    {
        if (AllowedAppsList.SelectedItem is string app)
        {
            _config.Policy.AllowedApps.Remove(app);
            AllowedAppsList.Items.Remove(app);
        }
    }

    private void SavePolicyFromUi()
    {
        _config.Policy.AllowWrites = PolAllowWritesChk.IsChecked == true;
        _config.Policy.AllowTier3 = PolAllowTier3Chk.IsChecked == true;
        _config.Policy.AllowTier4 = PolAllowTier4Chk.IsChecked == true;
        _config.Policy.AllowShell = PolAllowShellChk.IsChecked == true;
        _config.Policy.AllowDesktopControl = PolAllowDesktopChk.IsChecked == true;
        _configService.SaveConfig(_config);
    }

    private void OnSavePolicyClick(object sender, RoutedEventArgs e)
    {
        SavePolicyFromUi();
        AppendLog("ok", "Local security policy saved.");
    }

    private async void OnSyncPolicyClick(object sender, RoutedEventArgs e)
    {
        SavePolicyFromUi();
        if (string.IsNullOrWhiteSpace(_deviceToken))
        {
            AppendLog("warn", "Policy saved locally; set a Device Token to sync with Control Plane.");
            return;
        }
        var (ok, detail) = await _client.SyncPolicyAsync(_config.ControlUrl, _deviceToken, _config.Policy);
        AppendLog(ok ? "ok" : "err", detail);
    }

    private async void OnFetchPolicyClick(object sender, RoutedEventArgs e)
    {
        if (string.IsNullOrWhiteSpace(_deviceToken))
        {
            AppendLog("warn", "Device Token required to fetch policy from Control Plane.");
            return;
        }
        var (ok, policy, detail) = await _client.FetchPolicyAsync(_config.ControlUrl, _deviceToken);
        if (ok && policy is not null)
        {
            _config.Policy = policy;
            _configService.SaveConfig(_config);
            LoadPolicyIntoUi();
            AppendLog("ok", detail);
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
            FooterStatusText.Text = "Copied device token to clipboard.";
        }
    }

    private void OnReloadTokenClick(object sender, RoutedEventArgs e)
    {
        _deviceToken = _configService.LoadToken();
        LoadSettingsIntoUi();
        AppendLog("info", "Reloaded DPAPI-sealed token from disk.");
    }

    private void OnSaveSettingsClick(object sender, RoutedEventArgs e)
    {
        _config.ControlUrl = ControlPlaneClient.NormalizeUrl(SetControlUrlBox.Text);
        _config.DeviceId = SetDeviceIdBox.Text.Trim();
        _config.Workspace = SetWorkspaceBox.Text.Trim();
        if (int.TryParse(SetPollIntervalBox.Text, out var poll))
        {
            _config.PollIntervalSeconds = Math.Clamp(poll, 5, 30);
        }
        _config.AutoConnect = SetAutoConnectChk.IsChecked == true;

        var tok = (_tokenVisible ? SetTokenPlainBox.Text : SetTokenPasswordBox.Password).Trim();
        _deviceToken = tok;
        if (!string.IsNullOrEmpty(tok))
        {
            _configService.SaveToken(tok);
        }

        _configService.SaveConfig(_config);
        _configService.SetAutoStart(SetAutoStartChk.IsChecked == true);
        LoadConfigIntoUi();
        AppendLog("ok", "Configuration and DPAPI token saved.");
    }

    private void OnResetSettingsClick(object sender, RoutedEventArgs e)
    {
        _config = new AgentGuiConfig
        {
            Workspace = System.IO.Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                "ai-bridge-workspace"),
        };
        _configService.SaveConfig(_config);
        LoadConfigIntoUi();
        AppendLog("warn", "Configuration reset to defaults.");
    }
}
