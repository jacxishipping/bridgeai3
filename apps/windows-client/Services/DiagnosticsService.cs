using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using AIBridgeClient.Models;
using Microsoft.Win32;

namespace AIBridgeClient.Services;

/// <summary>
/// Windows OS diagnostics: Edition/build metadata, safe sc.exe service queries,
/// wevtutil event log queries, and sanitized ZIP diagnostics bundles.
/// </summary>
public static partial class DiagnosticsService
{
    [GeneratedRegex(@"^[A-Za-z0-9_.\- ]{1,128}$")]
    private static partial Regex SafeServiceRegex();

    public static (string ProductName, string DisplayVersion, string BuildNumber) GetWindowsEditionAndBuild()
    {
        try
        {
            using var key = Registry.LocalMachine.OpenSubKey(@"SOFTWARE\Microsoft\Windows NT\CurrentVersion", false);
            var product = key?.GetValue("ProductName")?.ToString() ?? "Windows";
            var display = key?.GetValue("DisplayVersion")?.ToString() ?? "";
            var build = key?.GetValue("CurrentBuildNumber")?.ToString() ?? Environment.OSVersion.Version.Build.ToString();
            var ubr = key?.GetValue("UBR")?.ToString();
            if (!string.IsNullOrWhiteSpace(ubr)) build = $"{build}.{ubr}";
            return (product, display, build);
        }
        catch
        {
            return ("Windows", "", Environment.OSVersion.Version.Build.ToString());
        }
    }

    public static async Task<object> GetServiceStatusAsync(string serviceName, CancellationToken ct = default)
    {
        var name = (serviceName ?? "").Trim();
        if (!SafeServiceRegex().IsMatch(name))
        {
            throw new ArgumentException("Invalid Windows service name.");
        }

        var (code, stdout, stderr) = await RunToolAsync("sc.exe", new[] { "query", name }, 10, ct);
        string state = "unknown";
        foreach (var line in stdout.Split('\n'))
        {
            if (line.Contains("STATE", StringComparison.OrdinalIgnoreCase))
            {
                state = line.Trim();
                break;
            }
        }
        return new { service = name, ok = code == 0, state, raw = stdout.Trim(), error = stderr.Trim() };
    }

    public static async Task<object> GetEventLogsAsync(string logName = "System", int maxEvents = 30, CancellationToken ct = default)
    {
        var log = (logName ?? "System").Trim();
        if (log is not ("System" or "Application" or "Security"))
        {
            throw new ArgumentException("Log name must be System, Application, or Security.");
        }

        int count = Math.Clamp(maxEvents, 1, 100);
        var (code, stdout, stderr) = await RunToolAsync(
            "wevtutil.exe",
            new[] { "qe", log, $"/c:{count}", "/rd:true", "/f:text" },
            15,
            ct);

        if (stdout.Length > 40_000)
        {
            stdout = stdout[..40_000] + "\n...[truncated]";
        }

        return new { log_name = log, max_events = count, ok = code == 0, output = stdout, error = stderr.Trim() };
    }

    public static string BuildDiagnosticsJson(
        AgentGuiConfig cfg,
        SystemMonitorService monitor,
        bool hasToken,
        int taskCount,
        string connectionState)
    {
        var hw = monitor.GetHardwareInfo();
        var telemetry = monitor.Sample();
        var (product, displayVer, build) = GetWindowsEditionAndBuild();

        var report = new
        {
            generated_at = DateTimeOffset.Now.ToString("O"),
            client = "AI Bridge Native C# v0.3.0",
            connection_state = connectionState,
            control_url = cfg.ControlUrl,
            device_id = cfg.DeviceId,
            workspace = cfg.Workspace,
            token_configured = hasToken,
            token_vault = "DPAPI CryptProtect (CurrentUser)",
            windows = new
            {
                product,
                display_version = displayVer,
                build,
                machine = Environment.MachineName,
                user = Environment.UserName,
                arch = System.Runtime.InteropServices.RuntimeInformation.OSArchitecture.ToString(),
                dotnet = System.Runtime.InteropServices.RuntimeInformation.FrameworkDescription,
            },
            hardware = hw,
            telemetry,
            monitors = ScreenCaptureService.GetMonitors(),
            desktop_pipe = new
            {
                pipe_name = NamedPipeBridge.PipeName,
                available = NamedPipeBridge.IsDesktopAgentAvailable(),
            },
            task_history_count = taskCount,
            policy = cfg.Policy,
        };

        return JsonSerializer.Serialize(report, new JsonSerializerOptions { WriteIndented = true });
    }

    public static void ExportDiagnosticsBundle(
        string destZipPath,
        ConfigService cfgService,
        AgentGuiConfig cfg,
        SystemMonitorService monitor,
        string recentLogsText,
        bool hasToken,
        int taskCount,
        string connectionState)
    {
        if (File.Exists(destZipPath)) File.Delete(destZipPath);

        using var zip = ZipFile.Open(destZipPath, ZipArchiveMode.Create);

        WriteZipText(zip, "report.json", BuildDiagnosticsJson(cfg, monitor, hasToken, taskCount, connectionState));

        var sanitizedCfg = new
        {
            cfg.ControlUrl,
            cfg.DeviceId,
            cfg.Workspace,
            cfg.PollIntervalSeconds,
            cfg.Theme,
            cfg.AutoConnect,
            cfg.MinimizeToTray,
            cfg.NotificationsEnabled,
            cfg.Policy,
        };
        WriteZipText(zip, "config.sanitized.json", JsonSerializer.Serialize(sanitizedCfg, new JsonSerializerOptions { WriteIndented = true }));
        WriteZipText(zip, "session-log.txt", recentLogsText ?? "");

        if (File.Exists(cfgService.TaskHistoryFile))
        {
            zip.CreateEntryFromFile(cfgService.TaskHistoryFile, "task_history.json");
        }
        if (File.Exists(cfgService.ClientLogFile))
        {
            zip.CreateEntryFromFile(cfgService.ClientLogFile, "ai-bridge-client.log");
        }
        if (File.Exists(cfgService.PolicyAuditFile))
        {
            zip.CreateEntryFromFile(cfgService.PolicyAuditFile, "policy_audit.json");
        }
    }

    private static void WriteZipText(ZipArchive zip, string entryName, string content)
    {
        var entry = zip.CreateEntry(entryName);
        using var writer = new StreamWriter(entry.Open());
        writer.Write(content);
    }

    private static async Task<(int Code, string Stdout, string Stderr)> RunToolAsync(
        string fileName,
        IEnumerable<string> args,
        int timeoutSeconds,
        CancellationToken ct)
    {
        var psi = new ProcessStartInfo
        {
            FileName = fileName,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        foreach (var a in args) psi.ArgumentList.Add(a);

        using var proc = Process.Start(psi) ?? throw new InvalidOperationException($"Could not start {fileName}");
        using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        cts.CancelAfter(TimeSpan.FromSeconds(timeoutSeconds));
        var stdoutTask = proc.StandardOutput.ReadToEndAsync(cts.Token);
        var stderrTask = proc.StandardError.ReadToEndAsync(cts.Token);
        await proc.WaitForExitAsync(cts.Token);
        return (proc.ExitCode, await stdoutTask, await stderrTask);
    }
}
