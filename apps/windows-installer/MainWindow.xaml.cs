using System;
using System.Diagnostics;
using System.IO;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using AIBridgeInstaller.Models;
using AIBridgeInstaller.Services;

namespace AIBridgeInstaller;

public partial class MainWindow : Window
{
    private readonly InstallerOptions _options;
    private readonly InstallEngine _engine = new();
    private CancellationTokenSource? _cts;
    private InstallResult? _lastResult;
    private int _step; // 0=Welcome, 1=Location, 2=Connect, 3=Progress, 4=Complete, 5=UninstallConfirm
    private bool _isBusy;
    private bool _isLightTheme;

    private readonly Border[] _railSteps;
    private readonly Border[] _railDots;
    private readonly TextBlock[] _railNums;
    private readonly TextBlock[] _railLabels;
    private readonly TextBlock[] _chkIcons;
    private readonly TextBlock[] _chkLabels;

    public MainWindow(InstallerOptions options)
    {
        InitializeComponent();
        _options = options;
        _isLightTheme = string.Equals(options.Theme, "light", StringComparison.OrdinalIgnoreCase);

        _railSteps = new[] { RailStep0, RailStep1, RailStep2, RailStep3, RailStep4 };
        _railDots = new[] { RailDot0, RailDot1, RailDot2, RailDot3, RailDot4 };
        _railNums = new[] { RailNum0, RailNum1, RailNum2, RailNum3, RailNum4 };
        _railLabels = new[] { RailLbl0, RailLbl1, RailLbl2, RailLbl3, RailLbl4 };
        _chkIcons = new[] { ChkStepIcon0, ChkStepIcon1, ChkStepIcon2, ChkStepIcon3, ChkStepIcon4 };
        _chkLabels = new[] { ChkStepLbl0, ChkStepLbl1, ChkStepLbl2, ChkStepLbl3, ChkStepLbl4 };

        InitializeFieldsFromOptions();
        DetectExistingInstallation();

        if (_options.UninstallMode)
        {
            ShowStep(5);
        }
        else
        {
            ShowStep(0);
        }
    }

    private void InitializeFieldsFromOptions()
    {
        InstallDirBox.Text = _options.InstallDirectory;
        WorkspaceDirBox.Text = _options.WorkspaceDirectory;
        ChkDesktopShortcut.IsChecked = _options.CreateDesktopShortcut;
        ChkStartMenuShortcut.IsChecked = _options.CreateStartMenuShortcut;
        ChkAutoStart.IsChecked = _options.StartWithWindows;
        ControlUrlBox.Text = _options.ControlPlaneUrl;
        DeviceIdBox.Text = _options.DeviceId;
        if (!string.IsNullOrEmpty(_options.DeviceToken))
        {
            DeviceTokenBox.Password = _options.DeviceToken;
        }
        ChkAllowWrites.IsChecked = _options.AllowWrites;
        ChkAllowTier3.IsChecked = _options.AllowTier3;
        ChkAllowShell.IsChecked = _options.AllowShell;
        ChkAllowDesktop.IsChecked = _options.AllowDesktopControl;
        ChkKeepConfig.IsChecked = _options.KeepUserConfigOnUninstall;
        ChkLaunchNow.IsChecked = _options.LaunchAfterInstall;

        PayloadStatusNote.Text = InstallEngine.HasEmbeddedPayload()
            ? $"Bundle: Self-contained single-file payload embedded (v{InstallerOptions.ProductVersion})"
            : "Bundle: Developer / directory mode (installs from adjacent publish folder or --payload)";
        UpdateDiskSpaceDisplay();
    }

    private void DetectExistingInstallation()
    {
        var existingDir = ShortcutAndRegistryService.DetectExistingInstallDirectory();
        var existingVer = ShortcutAndRegistryService.DetectInstalledVersion();
        if (!string.IsNullOrWhiteSpace(existingDir))
        {
            _options.InstallDirectory = existingDir;
            InstallDirBox.Text = existingDir;
            ExistingInstallBanner.Visibility = Visibility.Visible;
            ExistingInstallTitle.Text = $"Existing installation found ({existingVer ?? "installed"})";
            ExistingInstallSub.Text = $"Installed at {existingDir}. Continue to upgrade/repair in place, or uninstall.";
            ModeBadgeText.Text = $"v{InstallerOptions.ProductVersion} · Upgrade / Repair";
            UninstallTargetText.Text = $"Installed at: {existingDir}. Stopping running client, removing shortcuts, startup registration, and binaries.";
        }
        else
        {
            ModeBadgeText.Text = $"v{InstallerOptions.ProductVersion} · Clean Install";
        }
    }

    private void UpdateDiskSpaceDisplay()
    {
        var free = InstallEngine.GetFreeDiskBytes(InstallDirBox.Text);
        if (free > 0)
        {
            var gb = free / (1024.0 * 1024.0 * 1024.0);
            DiskSpaceText.Text = $"{gb:F1} GB free on drive";
        }
        else
        {
            DiskSpaceText.Text = "Drive space unavailable";
        }
    }

    private void ShowStep(int step)
    {
        _step = step;

        PageWelcome.Visibility = step == 0 ? Visibility.Visible : Visibility.Collapsed;
        PageLocation.Visibility = step == 1 ? Visibility.Visible : Visibility.Collapsed;
        PageConnect.Visibility = step == 2 ? Visibility.Visible : Visibility.Collapsed;
        PageProgress.Visibility = step == 3 ? Visibility.Visible : Visibility.Collapsed;
        PageComplete.Visibility = step == 4 ? Visibility.Visible : Visibility.Collapsed;
        PageUninstall.Visibility = step == 5 ? Visibility.Visible : Visibility.Collapsed;

        HeaderTitleText.Text = step switch
        {
            0 => "Step 1 of 5 · Welcome",
            1 => "Step 2 of 5 · Location & Windows Integration",
            2 => "Step 3 of 5 · Control Plane & Guardrails",
            3 => _options.UninstallMode ? "Uninstalling AI Bridge Client" : "Step 4 of 5 · Installing",
            4 => _options.UninstallMode ? "Uninstall Complete" : "Step 5 of 5 · Setup Complete",
            5 => "Uninstall Confirmation",
            _ => "AI Bridge Setup",
        };

        BackBtn.Visibility = (step is 1 or 2) || (step == 5 && !_options.UninstallMode) ? Visibility.Visible : Visibility.Collapsed;
        CancelBtn.Visibility = step is 0 or 1 or 2 or 5 ? Visibility.Visible : Visibility.Collapsed;
        UninstallActionBtn.Visibility = step == 5 ? Visibility.Visible : Visibility.Collapsed;
        NextBtn.Visibility = step is 0 or 1 or 2 or 4 ? Visibility.Visible : Visibility.Collapsed;

        NextBtn.Content = step switch
        {
            0 => "Get Started →",
            1 => "Continue →",
            2 => "Install Now →",
            4 => "Finish",
            _ => "Continue →",
        };

        UpdateStepperRail(step == 5 ? 0 : step);
    }

    private void UpdateStepperRail(int activeStep)
    {
        var accent = (System.Windows.Media.Brush)FindResource("AccentPrimary");
        var accentSoftBg = (System.Windows.Media.Brush)FindResource("AccentSoftBg");
        var success = (System.Windows.Media.Brush)FindResource("SuccessColor");
        var elevated = (System.Windows.Media.Brush)FindResource("BgElevated");
        var textPrimary = (System.Windows.Media.Brush)FindResource("TextPrimary");
        var textMuted = (System.Windows.Media.Brush)FindResource("TextMuted");

        for (int i = 0; i < _railSteps.Length; i++)
        {
            if (i < activeStep)
            {
                _railSteps[i].Background = System.Windows.Media.Brushes.Transparent;
                _railDots[i].Background = success;
                _railNums[i].Text = "✓";
                _railNums[i].Foreground = System.Windows.Media.Brushes.White;
                _railLabels[i].Foreground = textPrimary;
            }
            else if (i == activeStep)
            {
                _railSteps[i].Background = accentSoftBg;
                _railDots[i].Background = accent;
                _railNums[i].Text = (i + 1).ToString();
                _railNums[i].Foreground = System.Windows.Media.Brushes.White;
                _railLabels[i].Foreground = textPrimary;
            }
            else
            {
                _railSteps[i].Background = System.Windows.Media.Brushes.Transparent;
                _railDots[i].Background = elevated;
                _railNums[i].Text = (i + 1).ToString();
                _railNums[i].Foreground = textMuted;
                _railLabels[i].Foreground = textMuted;
            }
        }
    }

    private void SyncOptionsFromUi()
    {
        _options.InstallDirectory = InstallDirBox.Text.Trim();
        _options.WorkspaceDirectory = WorkspaceDirBox.Text.Trim();
        _options.CreateDesktopShortcut = ChkDesktopShortcut.IsChecked == true;
        _options.CreateStartMenuShortcut = ChkStartMenuShortcut.IsChecked == true;
        _options.StartWithWindows = ChkAutoStart.IsChecked == true;
        _options.ControlPlaneUrl = ControlUrlBox.Text.Trim();
        _options.DeviceId = DeviceIdBox.Text.Trim();
        _options.DeviceToken = DeviceTokenBox.Password;
        _options.AllowWrites = ChkAllowWrites.IsChecked == true;
        _options.AllowTier3 = ChkAllowTier3.IsChecked == true;
        _options.AllowShell = ChkAllowShell.IsChecked == true;
        _options.AllowDesktopControl = ChkAllowDesktop.IsChecked == true;
        _options.KeepUserConfigOnUninstall = ChkKeepConfig.IsChecked == true;
        _options.Theme = _isLightTheme ? "light" : "dark";
    }

    private async void OnNextClick(object sender, RoutedEventArgs e)
    {
        if (_isBusy) return;

        if (_step == 0)
        {
            ShowStep(1);
            return;
        }
        if (_step == 1)
        {
            if (string.IsNullOrWhiteSpace(InstallDirBox.Text))
            {
                System.Windows.MessageBox.Show("Please choose a valid installation directory.", "AI Bridge Setup", MessageBoxButton.OK, MessageBoxImage.Warning);
                return;
            }
            ShowStep(2);
            return;
        }
        if (_step == 2)
        {
            SyncOptionsFromUi();
            await RunInstallAsync();
            return;
        }
        if (_step == 4)
        {
            if (!_options.UninstallMode && _lastResult?.Success == true && ChkLaunchNow.IsChecked == true &&
                !string.IsNullOrWhiteSpace(_lastResult.ClientExePath) && File.Exists(_lastResult.ClientExePath))
            {
                try
                {
                    Process.Start(new ProcessStartInfo
                    {
                        FileName = _lastResult.ClientExePath,
                        WorkingDirectory = _lastResult.InstallDirectory,
                        UseShellExecute = true,
                    });
                }
                catch { }
            }
            Close();
        }
    }

    private void OnBackClick(object sender, RoutedEventArgs e)
    {
        if (_isBusy) return;
        if (_step == 5)
        {
            _options.UninstallMode = false;
            ShowStep(0);
            return;
        }
        if (_step > 0 && _step <= 2)
        {
            ShowStep(_step - 1);
        }
    }

    private void OnGoToUninstallClick(object sender, RoutedEventArgs e)
    {
        if (_isBusy) return;
        ShowStep(5);
    }

    private async void OnConfirmUninstallClick(object sender, RoutedEventArgs e)
    {
        if (_isBusy) return;
        _options.UninstallMode = true;
        SyncOptionsFromUi();
        await RunUninstallAsync();
    }

    private void ResetProgressChecklist(bool uninstall)
    {
        MainProgressBar.Value = 0;
        ProgressPercentText.Text = "0%";
        InstallLogBox.Clear();
        ProgressHeadingText.Text = uninstall ? "Uninstalling AI Bridge Client..." : "Installing AI Bridge Windows Client...";
        ProgressSubText.Text = uninstall ? "Preparing clean removal..." : "Starting installation...";

        string[] labels = uninstall
            ? new[]
            {
                "Stop running AI Bridge Client processes",
                "Remove shortcuts, Startup key & Installed Apps entry",
                "Delete installed binaries",
                "Apply user configuration retention preference",
                "Finalize removal",
            }
            : new[]
            {
                "Preflight checks & process cleanup",
                "Deploy binaries & native GUI uninstaller",
                "Write configuration & seal DPAPI token",
                "Create shortcuts & Windows Installed Apps entry",
                "Verify binary SHA-256 checksum",
            };

        var muted = (System.Windows.Media.Brush)FindResource("TextMuted");
        var secondary = (System.Windows.Media.Brush)FindResource("TextSecondary");
        for (int i = 0; i < _chkIcons.Length; i++)
        {
            _chkIcons[i].Text = "○";
            _chkIcons[i].Foreground = muted;
            _chkLabels[i].Text = labels[i];
            _chkLabels[i].Foreground = secondary;
        }
    }

    private void ApplyProgressEvent(InstallProgressEvent ev)
    {
        MainProgressBar.Value = ev.Percent;
        ProgressPercentText.Text = $"{ev.Percent}%";
        ProgressSubText.Text = ev.StepTitle;
        InstallLogBox.AppendText(ev.LogMessage + Environment.NewLine);
        InstallLogBox.ScrollToEnd();

        var success = (System.Windows.Media.Brush)FindResource("SuccessFg");
        var accent = (System.Windows.Media.Brush)FindResource("AccentSoftFg");
        var err = (System.Windows.Media.Brush)FindResource("DangerFg");
        var primary = (System.Windows.Media.Brush)FindResource("TextPrimary");

        for (int i = 0; i < _chkIcons.Length; i++)
        {
            if (i < ev.StepIndex || (ev.Percent >= 100 && !ev.IsError))
            {
                _chkIcons[i].Text = "✓";
                _chkIcons[i].Foreground = success;
                _chkLabels[i].Foreground = primary;
            }
            else if (i == ev.StepIndex)
            {
                _chkIcons[i].Text = ev.IsError ? "✕" : "●";
                _chkIcons[i].Foreground = ev.IsError ? err : accent;
                _chkLabels[i].Foreground = primary;
            }
        }
    }

    private async Task RunInstallAsync()
    {
        _isBusy = true;
        CloseChromeBtn.IsEnabled = false;
        ResetProgressChecklist(uninstall: false);
        ShowStep(3);

        _cts = new CancellationTokenSource();
        var progress = new Progress<InstallProgressEvent>(ApplyProgressEvent);
        _lastResult = await _engine.InstallAsync(_options, progress, _cts.Token);

        _isBusy = false;
        CloseChromeBtn.IsEnabled = true;
        PopulateResultPage(_lastResult, isUninstall: false);
        ShowStep(4);
    }

    private async Task RunUninstallAsync()
    {
        _isBusy = true;
        CloseChromeBtn.IsEnabled = false;
        ResetProgressChecklist(uninstall: true);
        ShowStep(3);

        _cts = new CancellationTokenSource();
        var progress = new Progress<InstallProgressEvent>(ApplyProgressEvent);
        _lastResult = await _engine.UninstallAsync(_options, progress, _cts.Token);

        _isBusy = false;
        CloseChromeBtn.IsEnabled = true;
        PopulateResultPage(_lastResult, isUninstall: true);
        ShowStep(4);
    }

    private void PopulateResultPage(InstallResult res, bool isUninstall)
    {
        if (res.Success)
        {
            ResultBanner.Background = (System.Windows.Media.Brush)FindResource("SuccessBg");
            ResultBanner.BorderBrush = (System.Windows.Media.Brush)FindResource("SuccessColor");
            ResultIconBg.Background = (System.Windows.Media.Brush)FindResource("SuccessColor");
            ResultIconGlyph.Text = "\uE73E";
            ResultHeadingText.Text = isUninstall ? "AI Bridge Client Removed" : "AI Bridge Client Installed!";
            ResultMessageText.Text = res.Message;
        }
        else
        {
            ResultBanner.Background = (System.Windows.Media.Brush)FindResource("DangerBg");
            ResultBanner.BorderBrush = (System.Windows.Media.Brush)FindResource("DangerBorder");
            ResultIconBg.Background = (System.Windows.Media.Brush)FindResource("DangerFg");
            ResultIconGlyph.Text = "\uE711";
            ResultHeadingText.Text = isUninstall ? "Uninstall Encountered an Error" : "Installation Failed";
            ResultMessageText.Text = res.Message;
        }

        SummaryCard.Visibility = (!isUninstall && res.Success) ? Visibility.Visible : Visibility.Collapsed;
        ChkLaunchNow.Visibility = (!isUninstall && res.Success) ? Visibility.Visible : Visibility.Collapsed;

        SummaryPathText.Text = res.InstallDirectory;
        SummaryControlText.Text = string.IsNullOrWhiteSpace(_options.ControlPlaneUrl) ? "Configure in app" : _options.ControlPlaneUrl;
        SummaryShaText.Text = string.IsNullOrWhiteSpace(res.ClientSha256) ? "—" : res.ClientSha256[..Math.Min(24, res.ClientSha256.Length)] + "...";
        SummaryStartupText.Text = _options.StartWithWindows ? "Enabled (minimized to tray)" : "Manual launch";
    }

    private async void OnTestConnectionClick(object sender, RoutedEventArgs e)
    {
        TestConnBtn.IsEnabled = false;
        ConnTestStatusText.Text = "Probing /health endpoint...";
        ConnTestStatusText.Foreground = (System.Windows.Media.Brush)FindResource("TextMuted");

        var res = await _engine.TestControlPlaneAsync(ControlUrlBox.Text);
        ConnTestStatusText.Text = res.Message;
        ConnTestStatusText.Foreground = (System.Windows.Media.Brush)FindResource(res.Reachable ? "SuccessFg" : "DangerFg");
        TestConnBtn.IsEnabled = true;
    }

    private void OnBrowseInstallDirClick(object sender, RoutedEventArgs e)
    {
        using var dlg = new System.Windows.Forms.FolderBrowserDialog
        {
            Description = "Select installation folder for AI Bridge Windows Client",
            SelectedPath = Directory.Exists(InstallDirBox.Text) ? InstallDirBox.Text : Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            UseDescriptionForTitle = true,
        };
        if (dlg.ShowDialog() == System.Windows.Forms.DialogResult.OK && !string.IsNullOrWhiteSpace(dlg.SelectedPath))
        {
            InstallDirBox.Text = dlg.SelectedPath;
            UpdateDiskSpaceDisplay();
        }
    }

    private void OnBrowseWorkspaceDirClick(object sender, RoutedEventArgs e)
    {
        using var dlg = new System.Windows.Forms.FolderBrowserDialog
        {
            Description = "Select default workspace folder for AI Bridge tools",
            SelectedPath = Directory.Exists(WorkspaceDirBox.Text) ? WorkspaceDirBox.Text : Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
            UseDescriptionForTitle = true,
        };
        if (dlg.ShowDialog() == System.Windows.Forms.DialogResult.OK && !string.IsNullOrWhiteSpace(dlg.SelectedPath))
        {
            WorkspaceDirBox.Text = dlg.SelectedPath;
        }
    }

    private void OnInstallDirTextChanged(object sender, TextChangedEventArgs e)
        => UpdateDiskSpaceDisplay();

    private void OnToggleThemeClick(object sender, RoutedEventArgs e)
    {
        _isLightTheme = !_isLightTheme;
        App.ApplyTheme(_isLightTheme ? "light" : "dark");
        ThemeIconText.Text = _isLightTheme ? "\uE708" : "\uE706";
        UpdateStepperRail(_step == 5 ? 0 : _step);
    }

    private void OnMinimizeClick(object sender, RoutedEventArgs e)
        => WindowState = WindowState.Minimized;

    private void OnCloseClick(object sender, RoutedEventArgs e)
    {
        if (_isBusy) return;
        Close();
    }
}
