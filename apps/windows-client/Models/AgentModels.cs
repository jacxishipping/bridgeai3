using System;
using System.Collections.Generic;
using System.Text.Json.Serialization;

namespace AIBridgeClient.Models;

public sealed class AgentPolicyConfig
{
    [JsonPropertyName("allow_writes")]
    public bool AllowWrites { get; set; } = true;

    [JsonPropertyName("allow_tier3")]
    public bool AllowTier3 { get; set; } = true;

    [JsonPropertyName("allow_tier4")]
    public bool AllowTier4 { get; set; } = false;

    [JsonPropertyName("allow_shell")]
    public bool AllowShell { get; set; } = false;

    [JsonPropertyName("allow_desktop_control")]
    public bool AllowDesktopControl { get; set; } = false;

    [JsonPropertyName("require_local_approval")]
    public bool RequireLocalApproval { get; set; } = false;

    [JsonPropertyName("max_tier_unattended")]
    public int MaxTierUnattended { get; set; } = 3;

    [JsonPropertyName("allow_package_install")]
    public bool AllowPackageInstall { get; set; } = false;

    [JsonPropertyName("allow_clipboard")]
    public bool AllowClipboard { get; set; } = true;

    [JsonPropertyName("allow_unrestricted_apps")]
    public bool AllowUnrestrictedApps { get; set; } = false;

    [JsonPropertyName("allow_full_pc_access")]
    public bool AllowFullPcAccess { get; set; } = false;

    [JsonPropertyName("allowed_apps")]
    public List<string> AllowedApps { get; set; } = new()
    {
        "notepad.exe",
        "calc.exe",
        "code.exe",
        "explorer.exe",
        "mspaint.exe",
    };

    [JsonPropertyName("allowed_commands")]
    public List<string> AllowedCommands { get; set; } = new();
}

public sealed class AgentGuiConfig
{
    // Control Plane settings
    [JsonPropertyName("control_url")]
    public string ControlUrl { get; set; } = "http://127.0.0.1:8000";

    [JsonPropertyName("device_id")]
    public string DeviceId { get; set; } = Environment.MachineName.ToLowerInvariant();

    [JsonPropertyName("tenant_id")]
    public string TenantId { get; set; } = "default";

    [JsonPropertyName("signed_in_user")]
    public string SignedInUser { get; set; } = "";

    [JsonPropertyName("tags")]
    public List<string> Tags { get; set; } = new() { "windows", "workstation", "dpapi-sealed" };

    [JsonPropertyName("poll_interval_s")]
    public int PollIntervalSeconds { get; set; } = 25;

    [JsonPropertyName("heartbeat_interval_s")]
    public int HeartbeatIntervalSeconds { get; set; } = 15;

    [JsonPropertyName("http_timeout_s")]
    public int HttpTimeoutSeconds { get; set; } = 45;

    [JsonPropertyName("ws_enabled")]
    public bool WebSocketEnabled { get; set; } = true;

    [JsonPropertyName("ws_reconnect_s")]
    public int WebSocketReconnectSeconds { get; set; } = 10;

    [JsonPropertyName("auto_connect")]
    public bool AutoConnect { get; set; } = false;

    // General & Window settings
    [JsonPropertyName("workspace")]
    public string Workspace { get; set; } = "";

    [JsonPropertyName("screenshot_dir")]
    public string ScreenshotDirectory { get; set; } = "";

    [JsonPropertyName("minimize_to_tray")]
    public bool MinimizeToTray { get; set; } = true;

    [JsonPropertyName("notifications_enabled")]
    public bool NotificationsEnabled { get; set; } = true;

    [JsonPropertyName("confirm_destructive_actions")]
    public bool ConfirmDestructiveActions { get; set; } = true;

    // Agent Execution & Telemetry Limits
    [JsonPropertyName("default_timeout_s")]
    public int DefaultTimeoutSeconds { get; set; } = 30;

    [JsonPropertyName("max_timeout_s")]
    public int MaxTimeoutSeconds { get; set; } = 300;

    [JsonPropertyName("max_read_bytes")]
    public int MaxReadBytes { get; set; } = 128_000;

    [JsonPropertyName("max_output_bytes")]
    public int MaxOutputBytes { get; set; } = 64_000;

    [JsonPropertyName("max_list_entries")]
    public int MaxListEntries { get; set; } = 200;

    [JsonPropertyName("task_history_limit")]
    public int TaskHistoryLimit { get; set; } = 500;

    [JsonPropertyName("auto_refresh_monitor")]
    public bool AutoRefreshMonitor { get; set; } = true;

    [JsonPropertyName("monitor_interval_ms")]
    public int MonitorIntervalMs { get; set; } = 1000;

    // Logging settings
    [JsonPropertyName("log_level")]
    public string LogLevel { get; set; } = "INFO";

    [JsonPropertyName("file_logging_enabled")]
    public bool FileLoggingEnabled { get; set; } = true;

    [JsonPropertyName("max_log_entries")]
    public int MaxLogEntries { get; set; } = 1000;

    [JsonPropertyName("max_log_file_mb")]
    public int MaxLogFileMb { get; set; } = 2;

    [JsonPropertyName("auto_scroll_logs")]
    public bool AutoScrollLogs { get; set; } = true;

    // Appearance settings
    [JsonPropertyName("theme")]
    public string Theme { get; set; } = "dark";

    [JsonPropertyName("compact_sidebar")]
    public bool CompactSidebar { get; set; } = false;

    // Advanced & Pipe IPC settings
    [JsonPropertyName("desktop_pipe_name")]
    public string DesktopPipeName { get; set; } = "AIBridgeDesktopPipe";

    [JsonPropertyName("auto_check_updates")]
    public bool AutoCheckUpdates { get; set; } = false;

    [JsonPropertyName("update_manifest_url")]
    public string UpdateManifestUrl { get; set; } = "";

    [JsonPropertyName("window_width")]
    public double WindowWidth { get; set; } = 1300;

    [JsonPropertyName("window_height")]
    public double WindowHeight { get; set; } = 840;

    [JsonPropertyName("window_maximized")]
    public bool WindowMaximized { get; set; } = false;

    [JsonPropertyName("last_tab")]
    public int LastTab { get; set; } = 0;

    [JsonPropertyName("policy")]
    public AgentPolicyConfig Policy { get; set; } = new();
}

public sealed class TaskHistoryRecord
{
    [JsonPropertyName("id")]
    public string Id { get; set; } = "";

    [JsonPropertyName("tool")]
    public string Tool { get; set; } = "";

    [JsonPropertyName("status")]
    public string Status { get; set; } = "pending";

    [JsonPropertyName("timestamp")]
    public string Timestamp { get; set; } = "";

    [JsonPropertyName("duration_ms")]
    public double DurationMs { get; set; }

    [JsonPropertyName("args_json")]
    public string ArgsJson { get; set; } = "{}";

    [JsonPropertyName("result_preview")]
    public string ResultPreview { get; set; } = "";
}

public sealed class HardwareTelemetry
{
    public double CpuPercent { get; init; }
    public double MemoryPercent { get; init; }
    public double MemoryUsedGb { get; init; }
    public double MemoryTotalGb { get; init; }
    public double DiskPercent { get; init; }
    public double DiskUsedGb { get; init; }
    public double DiskTotalGb { get; init; }
    public double NetworkSentKbps { get; init; }
    public double NetworkRecvKbps { get; init; }
    public int ProcessCount { get; init; }
    public TimeSpan SystemUptime { get; init; }
}

public sealed class RemoteTaskEnvelope
{
    [JsonPropertyName("id")]
    public string Id { get; set; } = "";

    [JsonPropertyName("tool")]
    public string Tool { get; set; } = "";

    [JsonPropertyName("args_json")]
    public string ArgsJson { get; set; } = "{}";

    [JsonPropertyName("emergency_stop")]
    public bool EmergencyStop { get; set; }
}

public sealed class LogEntry
{
    public string Timestamp { get; init; } = "";
    public string Level { get; init; } = "INFO";
    public string Message { get; init; } = "";
    public string LevelColor { get; init; } = "#34D399";
    public string FormattedLine => $"[{Timestamp}] [{Level}] {Message}";
}

public sealed record CaptureHistoryItem(
    string FilePath,
    int MonitorIndex,
    string Resolution,
    string SizeLabel,
    string Timestamp);

public sealed record HardwareInfo(
    string CpuName,
    int LogicalCores,
    string GpuName,
    string OsVersion,
    string MachineName);

public sealed class CustomAgentItem
{
    public string Id { get; set; } = "";
    public string Name { get; set; } = "";
    public string Instruction { get; set; } = "";
    public string Provider { get; set; } = "openai";
    public string Model { get; set; } = "";
    public int IntervalSeconds { get; set; } = 300;
    public bool Enabled { get; set; }
    public string Status { get; set; } = "idle";
    public string ToggleLabel => Enabled ? "Pause" : "Enable";
}

public sealed class RoleAgentItem
{
    public string Role { get; set; } = "";
    public string Name { get; set; } = "";
    public string Description { get; set; } = "";
    public bool Enabled { get; set; }
    public string StateLabel => Enabled ? "Enabled" : "Disabled";
    public string ToggleLabel => Enabled ? "Disable" : "Enable";
}

public sealed class AgentChatMessage
{
    public string Role { get; set; } = "assistant";
    public string Content { get; set; } = "";
    public string ToolsSummary { get; set; } = "";
    public string Timestamp { get; set; } = "";
    public System.Windows.Media.ImageSource? ScreenshotImage { get; set; }

    public System.Windows.Visibility UserBubbleVisibility =>
        string.Equals(Role, "user", StringComparison.OrdinalIgnoreCase)
            ? System.Windows.Visibility.Visible
            : System.Windows.Visibility.Collapsed;

    public System.Windows.Visibility AssistantBubbleVisibility =>
        !string.Equals(Role, "user", StringComparison.OrdinalIgnoreCase)
            ? System.Windows.Visibility.Visible
            : System.Windows.Visibility.Collapsed;

    public System.Windows.Visibility ToolsVisibility =>
        string.IsNullOrWhiteSpace(ToolsSummary)
            ? System.Windows.Visibility.Collapsed
            : System.Windows.Visibility.Visible;

    public System.Windows.Visibility ScreenshotVisibility =>
        ScreenshotImage is not null
            ? System.Windows.Visibility.Visible
            : System.Windows.Visibility.Collapsed;
}

public sealed class AgentChatThread
{
    public string Id { get; set; } = Guid.NewGuid().ToString("N")[..8];
    public string Title { get; set; } = "New Chat";
    public string Preview { get; set; } = "Scoped to this Windows PC";
    public System.Collections.ObjectModel.ObservableCollection<AgentChatMessage> Messages { get; } = new();
}
