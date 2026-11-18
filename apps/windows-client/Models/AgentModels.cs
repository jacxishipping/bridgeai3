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

    [JsonPropertyName("allowed_apps")]
    public List<string> AllowedApps { get; set; } = new()
    {
        "notepad.exe",
        "calc.exe",
        "code.exe",
        "explorer.exe",
        "mspaint.exe",
    };
}

public sealed class AgentGuiConfig
{
    [JsonPropertyName("control_url")]
    public string ControlUrl { get; set; } = "http://127.0.0.1:8000";

    [JsonPropertyName("device_id")]
    public string DeviceId { get; set; } = Environment.MachineName.ToLowerInvariant();

    [JsonPropertyName("workspace")]
    public string Workspace { get; set; } = "";

    [JsonPropertyName("poll_interval_s")]
    public int PollIntervalSeconds { get; set; } = 25;

    [JsonPropertyName("theme")]
    public string Theme { get; set; } = "dark";

    [JsonPropertyName("auto_connect")]
    public bool AutoConnect { get; set; } = false;

    [JsonPropertyName("minimize_to_tray")]
    public bool MinimizeToTray { get; set; } = true;

    [JsonPropertyName("notifications_enabled")]
    public bool NotificationsEnabled { get; set; } = true;

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
