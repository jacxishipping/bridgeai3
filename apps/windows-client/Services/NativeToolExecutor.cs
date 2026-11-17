using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net.NetworkInformation;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using AIBridgeClient.Models;

namespace AIBridgeClient.Services;

public sealed class NativeToolExecutor
{
    private readonly SystemMonitorService _monitor;
    private readonly string _screenshotDir;

    public NativeToolExecutor(SystemMonitorService monitor, string screenshotDir)
    {
        _monitor = monitor;
        _screenshotDir = screenshotDir;
    }

    public async Task<(bool Ok, string ResultJson)> ExecuteAsync(
        string toolName,
        string argsJson,
        AgentGuiConfig config,
        CancellationToken ct)
    {
        var tool = NormalizeToolName(toolName);
        using var doc = JsonDocument.Parse(string.IsNullOrWhiteSpace(argsJson) ? "{}" : argsJson);
        var args = doc.RootElement;

        try
        {
            object result = tool switch
            {
                "get_system_info" or "system_info" => GetSystemInfo(),
                "get_resource_usage" => _monitor.Sample(),
                "get_memory_usage" => GetMemoryUsage(),
                "get_disk_usage" => GetDiskUsage(),
                "get_network_info" => GetNetworkInfo(),
                "list_processes" => ListProcesses(args),
                "list_workspace" => ListWorkspace(config.Workspace, args),
                "read_workspace_file" or "read_file" => ReadWorkspaceFile(config.Workspace, args),
                "search_workspace" => SearchWorkspace(config.Workspace, args),
                "create_workspace_file" => CreateWorkspaceFile(config, args),
                "capture_desktop_screenshot" or "take_screenshot" or "capture_desktop_frame" => CaptureScreenshot(config),
                "run_shell" => await RunShellAsync(config, args, ct),
                _ => throw new InvalidOperationException($"Tool '{tool}' is not supported by native C# executor."),
            };

            return (true, JsonSerializer.Serialize(result, new JsonSerializerOptions { WriteIndented = true }));
        }
        catch (Exception ex)
        {
            var errPayload = JsonSerializer.Serialize(new { error = ex.Message, tool });
            return (false, errPayload);
        }
    }

    private static string NormalizeToolName(string name)
    {
        var trimmed = name.Trim();
        var dot = trimmed.LastIndexOf('.');
        return dot >= 0 ? trimmed[(dot + 1)..] : trimmed;
    }

    private static object GetSystemInfo() => new
    {
        hostname = Environment.MachineName,
        os = "Windows",
        os_version = Environment.OSVersion.VersionString,
        architecture = RuntimeInformation.OSArchitecture.ToString(),
        cpu_count = Environment.ProcessorCount,
        dotnet_version = RuntimeInformation.FrameworkDescription,
        user = Environment.UserName,
    };

    private object GetMemoryUsage()
    {
        var t = _monitor.Sample();
        return new
        {
            memory_percent = t.MemoryPercent,
            used_gb = t.MemoryUsedGb,
            total_gb = t.MemoryTotalGb,
        };
    }

    private object GetDiskUsage()
    {
        var t = _monitor.Sample();
        return new
        {
            disk_percent = t.DiskPercent,
            used_gb = t.DiskUsedGb,
            total_gb = t.DiskTotalGb,
        };
    }

    private static object GetNetworkInfo()
    {
        var adapters = NetworkInterface.GetAllNetworkInterfaces()
            .Where(n => n.OperationalStatus == OperationalStatus.Up)
            .Select(n => new
            {
                name = n.Name,
                type = n.NetworkInterfaceType.ToString(),
                speed_mbps = n.Speed > 0 ? n.Speed / 1_000_000 : 0,
            })
            .Take(20)
            .ToList();
        return new { adapters };
    }

    private static object ListProcesses(JsonElement args)
    {
        var limit = args.TryGetProperty("limit", out var limEl) && limEl.TryGetInt32(out var l)
            ? Math.Clamp(l, 1, 200)
            : 50;

        var procs = Process.GetProcesses()
            .OrderByDescending(p =>
            {
                try { return p.WorkingSet64; } catch { return 0L; }
            })
            .Take(limit)
            .Select(p =>
            {
                long memMb = 0;
                try { memMb = p.WorkingSet64 / (1024 * 1024); } catch { }
                return new { pid = p.Id, name = p.ProcessName, memory_mb = memMb };
            })
            .ToList();

        return new { count = procs.Count, processes = procs };
    }

    private static string ConfineWorkspacePath(string workspaceRoot, string relativePath)
    {
        if (string.IsNullOrWhiteSpace(workspaceRoot))
        {
            throw new InvalidOperationException("Workspace directory is not configured.");
        }
        if (relativePath.Contains('\0'))
        {
            throw new UnauthorizedAccessException("Invalid path: NUL byte not allowed.");
        }
        if (Path.IsPathRooted(relativePath))
        {
            throw new UnauthorizedAccessException("Absolute paths are blocked outside workspace confinement.");
        }

        var rootFull = Path.GetFullPath(workspaceRoot);
        Directory.CreateDirectory(rootFull);
        var candidate = Path.GetFullPath(Path.Combine(rootFull, relativePath));
        var rootPrefix = rootFull.EndsWith(Path.DirectorySeparatorChar)
            ? rootFull
            : rootFull + Path.DirectorySeparatorChar;

        if (!string.Equals(candidate, rootFull, StringComparison.OrdinalIgnoreCase) &&
            !candidate.StartsWith(rootPrefix, StringComparison.OrdinalIgnoreCase))
        {
            throw new UnauthorizedAccessException("Path traversal outside workspace is denied.");
        }

        return candidate;
    }

    private static object ListWorkspace(string workspaceRoot, JsonElement args)
    {
        var rel = args.TryGetProperty("path", out var p) ? p.GetString() ?? "" : "";
        var target = ConfineWorkspacePath(workspaceRoot, rel);
        if (!Directory.Exists(target))
        {
            throw new DirectoryNotFoundException($"Directory not found: {rel}");
        }

        var entries = Directory.EnumerateFileSystemEntries(target)
            .Take(200)
            .Select(e => new
            {
                name = Path.GetFileName(e),
                is_dir = Directory.Exists(e),
                size = File.Exists(e) ? new FileInfo(e).Length : 0L,
            })
            .ToList();

        return new { path = rel, entries };
    }

    private static object ReadWorkspaceFile(string workspaceRoot, JsonElement args)
    {
        var rel = args.TryGetProperty("path", out var p) ? p.GetString() ?? "" : "";
        var target = ConfineWorkspacePath(workspaceRoot, rel);
        var fileName = Path.GetFileName(target);
        if (fileName.StartsWith(".env", StringComparison.OrdinalIgnoreCase) ||
            fileName.EndsWith(".pem", StringComparison.OrdinalIgnoreCase) ||
            fileName.EndsWith(".key", StringComparison.OrdinalIgnoreCase))
        {
            throw new UnauthorizedAccessException($"Reading secret file '{fileName}' is blocked by policy.");
        }
        var content = File.ReadAllText(target);
        if (content.Length > 100_000)
        {
            content = content[..100_000] + "\n...[truncated]";
        }
        return new { path = rel, content };
    }

    private static object SearchWorkspace(string workspaceRoot, JsonElement args)
    {
        var query = args.TryGetProperty("query", out var q) ? q.GetString() ?? "" : "";
        if (string.IsNullOrWhiteSpace(query))
        {
            throw new ArgumentException("Search query is required.");
        }

        var root = ConfineWorkspacePath(workspaceRoot, "");
        var matches = new List<object>();
        foreach (var file in Directory.EnumerateFiles(root, "*", SearchOption.AllDirectories).Take(500))
        {
            try
            {
                var text = File.ReadAllText(file);
                if (text.Contains(query, StringComparison.OrdinalIgnoreCase))
                {
                    matches.Add(new { path = Path.GetRelativePath(root, file) });
                    if (matches.Count >= 50) break;
                }
            }
            catch
            {
                // Skip unreadable files
            }
        }
        return new { query, matches };
    }

    private static object CreateWorkspaceFile(AgentGuiConfig config, JsonElement args)
    {
        if (!config.Policy.AllowWrites)
        {
            throw new UnauthorizedAccessException("Policy denied: Tier 2 workspace writes are disabled.");
        }
        var rel = args.TryGetProperty("path", out var p) ? p.GetString() ?? "" : "";
        var content = args.TryGetProperty("content", out var c) ? c.GetString() ?? "" : "";
        var target = ConfineWorkspacePath(config.Workspace, rel);
        var dir = Path.GetDirectoryName(target);
        if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);
        File.WriteAllText(target, content);
        return new { path = rel, bytes_written = content.Length, status = "created" };
    }

    private object CaptureScreenshot(AgentGuiConfig config)
    {
        if (!config.Policy.AllowTier3)
        {
            throw new UnauthorizedAccessException("Policy denied: Tier 3 sensitive operations are disabled.");
        }
        var shot = ScreenCaptureService.CapturePrimaryScreen(_screenshotDir);
        return new
        {
            path = shot.FilePath,
            width = shot.Width,
            height = shot.Height,
            size_bytes = shot.SizeBytes,
            image_base64 = shot.Base64Png,
        };
    }

    private static async Task<object> RunShellAsync(AgentGuiConfig config, JsonElement args, CancellationToken ct)
    {
        if (!config.Policy.AllowTier4 || !config.Policy.AllowShell)
        {
            throw new UnauthorizedAccessException("Policy denied: Tier 4 shell execution is disabled.");
        }

        var command = args.TryGetProperty("command", out var cmdEl) ? cmdEl.GetString() ?? "" : "";
        if (string.IsNullOrWhiteSpace(command))
        {
            throw new ArgumentException("Shell command is required.");
        }

        var timeoutSec = args.TryGetProperty("timeout", out var tEl) && tEl.TryGetInt32(out var t)
            ? Math.Clamp(t, 1, 300)
            : 60;

        var cwd = ConfineWorkspacePath(config.Workspace, "");
        var psi = new ProcessStartInfo
        {
            FileName = "cmd.exe",
            Arguments = $"/c {command}",
            WorkingDirectory = cwd,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
        };

        // Scrub sensitive AI_BRIDGE_* environment variables from child process
        foreach (var key in psi.Environment.Keys.Where(k => k.StartsWith("AI_BRIDGE_", StringComparison.OrdinalIgnoreCase)).ToList())
        {
            psi.Environment.Remove(key);
        }

        using var proc = Process.Start(psi) ?? throw new InvalidOperationException("Failed to start shell process.");
        using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        cts.CancelAfter(TimeSpan.FromSeconds(timeoutSec));

        try
        {
            var stdoutTask = proc.StandardOutput.ReadToEndAsync(cts.Token);
            var stderrTask = proc.StandardError.ReadToEndAsync(cts.Token);
            await proc.WaitForExitAsync(cts.Token);
            return new
            {
                returncode = proc.ExitCode,
                stdout = await stdoutTask,
                stderr = await stderrTask,
            };
        }
        catch (OperationCanceledException)
        {
            try { proc.Kill(entireProcessTree: true); } catch { }
            throw;
        }
    }
}
