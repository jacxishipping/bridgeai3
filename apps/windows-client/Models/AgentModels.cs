using System;
using System.Collections.Generic;
using System.Text.Json.Serialization;

namespace AIBridgeClient.Models;

public sealed class AgentPolicyConfig
{
    [JsonPropertyName("allow_writes")]
    public bool AllowWrites { get; set; } = true;

    [JsonPropertyName("allow_tier3")]
    public bool AllowTier3 { get; set; } = false;

    [JsonPropertyName("allow_tier4")]
    public bool AllowTier4 { get; set; } = false;

    [JsonPropertyName("allow_shell")]
    public bool AllowShell { get; set; } = false;

    [JsonPropertyName("allow_desktop_control")]
    public bool AllowDesktopControl { get; set; } = false;

    [JsonPropertyName("require_local_approval")]
    public bool RequireLocalApproval { get; set; } = false;

    [JsonPropertyName("max_tier_unattended")]
    public int MaxTierUnattended { get; set; } = 2;

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
    public string ControlUrl { get; set; } = "https://ec2-77-114-76-12.us-east-2.compute.amazonaws.com";

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

    [JsonPropertyName("device_agent")]
    public DeviceAgentSettings DeviceAgent { get; set; } = new();

    [JsonPropertyName("cpp_agent")]
    public CppAgentSettings CppAgent { get; set; } = new();
}

public sealed class CppAgentSettings
{
    [JsonPropertyName("auto_start_supervisor")]
    public bool AutoStartSupervisor { get; set; } = true;

    [JsonPropertyName("prefer_dxgi_capture")]
    public bool PreferDxgiCapture { get; set; } = true;

    [JsonPropertyName("h264_target_fps")]
    public int H264TargetFps { get; set; } = 30;

    [JsonPropertyName("h264_bitrate_kbps")]
    public int H264BitrateKbps { get; set; } = 4500;

    [JsonPropertyName("h264_keyframe_interval_sec")]
    public int H264KeyframeIntervalSec { get; set; } = 2;

    [JsonPropertyName("h264_prefer_hardware")]
    public bool H264PreferHardware { get; set; } = true;

    [JsonPropertyName("h264_adaptive_bitrate")]
    public bool H264AdaptiveBitrate { get; set; } = true;

    [JsonPropertyName("turn_url")]
    public string TurnUrl { get; set; } = "";

    [JsonPropertyName("turn_username")]
    public string TurnUsername { get; set; } = "";

    [JsonPropertyName("turn_credential")]
    public string TurnCredential { get; set; } = "";
}

public sealed class AgentFunctionPermissions
{
    [JsonPropertyName("allow_system_health_tools")]
    public bool AllowSystemHealthTools { get; set; } = true;

    [JsonPropertyName("allow_screen_capture")]
    public bool AllowScreenCapture { get; set; } = true;

    [JsonPropertyName("allow_window_management")]
    public bool AllowWindowManagement { get; set; } = true;

    [JsonPropertyName("allow_ui_input_automation")]
    public bool AllowUiInputAutomation { get; set; } = true;

    [JsonPropertyName("allow_clipboard")]
    public bool AllowClipboard { get; set; } = true;

    [JsonPropertyName("allow_app_launching")]
    public bool AllowAppLaunching { get; set; } = true;

    [JsonPropertyName("allow_workspace_files")]
    public bool AllowWorkspaceFiles { get; set; } = true;

    [JsonPropertyName("allow_full_pc_files_and_drives")]
    public bool AllowFullPcFilesAndDrives { get; set; } = false;

    [JsonPropertyName("allow_shell_and_powershell")]
    public bool AllowShellAndPowerShell { get; set; } = false;

    [JsonPropertyName("allow_process_termination")]
    public bool AllowProcessTermination { get; set; } = false;

    [JsonPropertyName("disabled_tools")]
    public List<string> DisabledTools { get; set; } = new();
}

public sealed class DeviceAgentSettings
{
    [JsonPropertyName("agent_name")]
    public string AgentName { get; set; } = "Windows Copilot";

    [JsonPropertyName("instruction_name")]
    public string InstructionName { get; set; } = "Default Windows Assistant";

    [JsonPropertyName("system_prompt")]
    public string SystemPrompt { get; set; } =
        "You are a dedicated Windows desktop AI assistant scoped strictly to this PC. " +
        "Inspect active state before mutating windows, files, or running commands, keep answers concise, " +
        "and strictly respect all device function permissions.";

    [JsonPropertyName("connection_mode")]
    public string ConnectionMode { get; set; } = "direct_or_local"; // direct_or_local | direct_api | control_plane | local_only

    [JsonPropertyName("provider")]
    public string Provider { get; set; } = "openai"; // openai | anthropic | gemini | ollama | custom

    [JsonPropertyName("base_url")]
    public string BaseUrl { get; set; } = "https://api.openai.com/v1";

    [JsonPropertyName("model")]
    public string Model { get; set; } = "gpt-4o";

    [JsonPropertyName("temperature")]
    public double Temperature { get; set; } = 0.2;

    [JsonPropertyName("max_tool_steps")]
    public int MaxToolSteps { get; set; } = 16;

    [JsonPropertyName("confirm_mutating_functions")]
    public bool ConfirmMutatingFunctions { get; set; } = false;

    [JsonPropertyName("functions")]
    public AgentFunctionPermissions Functions { get; set; } = new();
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
