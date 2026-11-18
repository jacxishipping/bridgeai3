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
                "get_system_info" or "system_info" or "windows_system_info" => GetSystemInfo(),
                "get_resource_usage" => _monitor.Sample(),
                "get_memory_usage" => GetMemoryUsage(),
                "get_disk_usage" => GetDiskUsage(),
                "get_network_info" => GetNetworkInfo(),
                "get_service_status" or "windows_service_status" => await GetServiceStatusAsync(args, ct),
                "get_recent_logs" or "windows_event_logs" => await GetEventLogsAsync(args, ct),
                "list_processes" => ListProcesses(args),
                "list_workspace" => ListWorkspace(config, args),
                "read_workspace_file" or "read_file" => ReadWorkspaceFile(config, args),
                "search_workspace" => SearchWorkspace(config, args),
                "create_workspace_file" or "write_file" => CreateWorkspaceFile(config, args),
                "edit_workspace_file" => EditWorkspaceFile(config, args),
                "move_workspace_file" => MoveWorkspaceFile(config, args),
                "delete_workspace_file" => DeleteWorkspaceFile(config, args),
                "get_monitors" or "desktop_get_monitors" => new { monitors = ScreenCaptureService.GetMonitors() },
                "get_screen_size" => GetScreenSize(),
                "get_desktop_status" or "desktop_get_status" => new
                {
                    pipe_available = NamedPipeBridge.IsDesktopAgentAvailable(config.DesktopPipeName),
                    pipe_name = string.IsNullOrWhiteSpace(config.DesktopPipeName) ? NamedPipeBridge.PipeName : config.DesktopPipeName,
                    desktop_control_enabled = config.Policy.AllowDesktopControl,
                    clipboard_enabled = config.Policy.AllowClipboard,
                    max_tier_unattended = config.Policy.MaxTierUnattended,
                    allowed_apps = config.Policy.AllowedApps,
                },
                "list_desktop_windows" => ListDesktopWindows(args),
                "focus_window" => FocusDesktopWindow(config, args),
                "launch_approved_application" => LaunchApprovedApplication(config, args),
                "get_clipboard" or "desktop_clipboard_get" => GetClipboard(config),
                "set_clipboard" or "desktop_clipboard_set" => SetClipboard(config, args),
                "get_ui_tree" or "find_elements" or "list_ui_elements" => FindUiElements(args),
                "click_element" or "click_text" => ClickUiElement(config, args),
                "start_webrtc_stream" => await SendPipeStreamCommandAsync(config, "START_WEBRTC_SESSION", args, ct),
                "stop_webrtc_stream" => await SendPipeStreamCommandAsync(config, "STOP_WEBRTC_SESSION", args, ct),
                "capture_desktop_screenshot" or "take_screenshot" or "capture_desktop_frame" or "desktop_get_screenshot" => CaptureScreenshot(config, args),
                "desktop_get_window_screenshot" => CaptureWindowScreenshot(config, args),
                "mouse_move" or "desktop_mouse_move" => MouseMove(config, args),
                "mouse_click" or "desktop_click" => MouseClick(config, args),
                "mouse_scroll" or "desktop_scroll" => MouseScroll(config, args),
                "mouse_drag" => MouseDrag(config, args),
                "keyboard_type" or "desktop_keyboard" => KeyboardType(config, args),
                "key_press" or "keyboard_press" => KeyPress(config, args),
                "press_hotkey" => PressHotkey(config, args),
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

    private static object GetSystemInfo()
    {
        var (product, displayVer, build) = DiagnosticsService.GetWindowsEditionAndBuild();
        return new
        {
            hostname = Environment.MachineName,
            os = "Windows",
            windows_edition = product,
            windows_display_version = displayVer,
            windows_build = build,
            os_version = Environment.OSVersion.VersionString,
            architecture = RuntimeInformation.OSArchitecture.ToString(),
            cpu_count = Environment.ProcessorCount,
            dotnet_version = RuntimeInformation.FrameworkDescription,
            user = Environment.UserName,
            desktop_pipe_available = NamedPipeBridge.IsDesktopAgentAvailable(),
        };
    }

    private static Task<object> GetServiceStatusAsync(JsonElement args, CancellationToken ct)
    {
        var svc = args.TryGetProperty("service", out var sEl) ? sEl.GetString() ?? ""
            : args.TryGetProperty("name", out var nEl) ? nEl.GetString() ?? "" : "";
        return DiagnosticsService.GetServiceStatusAsync(svc, ct);
    }

    private static Task<object> GetEventLogsAsync(JsonElement args, CancellationToken ct)
    {
        var logName = args.TryGetProperty("log_name", out var lEl) ? lEl.GetString() ?? "System" : "System";
        int maxEvents = args.TryGetProperty("max_events", out var cEl) && cEl.TryGetInt32(out var c) ? c : 30;
        return DiagnosticsService.GetEventLogsAsync(logName, maxEvents, ct);
    }

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

    private static object ListWorkspace(AgentGuiConfig config, JsonElement args)
    {
        var rel = args.TryGetProperty("path", out var p) ? p.GetString() ?? "" : "";
        var target = ConfineWorkspacePath(config.Workspace, rel);
        if (!Directory.Exists(target))
        {
            throw new DirectoryNotFoundException($"Directory not found: {rel}");
        }

        int maxList = Math.Clamp(config.MaxListEntries, 10, 1000);
        var entries = Directory.EnumerateFileSystemEntries(target)
            .Take(maxList)
            .Select(e => new
            {
                name = Path.GetFileName(e),
                is_dir = Directory.Exists(e),
                size = File.Exists(e) ? new FileInfo(e).Length : 0L,
            })
            .ToList();

        return new { path = rel, entries };
    }

    private static object ReadWorkspaceFile(AgentGuiConfig config, JsonElement args)
    {
        var rel = args.TryGetProperty("path", out var p) ? p.GetString() ?? "" : "";
        var target = ConfineWorkspacePath(config.Workspace, rel);
        var fileName = Path.GetFileName(target);
        if (fileName.StartsWith(".env", StringComparison.OrdinalIgnoreCase) ||
            fileName.EndsWith(".pem", StringComparison.OrdinalIgnoreCase) ||
            fileName.EndsWith(".key", StringComparison.OrdinalIgnoreCase))
        {
            throw new UnauthorizedAccessException($"Reading secret file '{fileName}' is blocked by policy.");
        }
        var content = File.ReadAllText(target);
        int maxRead = Math.Clamp(config.MaxReadBytes, 1024, 2_000_000);
        if (content.Length > maxRead)
        {
            content = content[..maxRead] + "\n...[truncated]";
        }
        return new { path = rel, content };
    }

    private static object SearchWorkspace(AgentGuiConfig config, JsonElement args)
    {
        var query = args.TryGetProperty("query", out var q) ? q.GetString() ?? "" : "";
        if (string.IsNullOrWhiteSpace(query))
        {
            throw new ArgumentException("Search query is required.");
        }

        var root = ConfineWorkspacePath(config.Workspace, "");
        int maxMatches = Math.Clamp(config.MaxListEntries, 10, 1000);
        var matches = new List<object>();
        foreach (var file in Directory.EnumerateFiles(root, "*", SearchOption.AllDirectories).Take(500))
        {
            try
            {
                var text = File.ReadAllText(file);
                if (text.Contains(query, StringComparison.OrdinalIgnoreCase))
                {
                    matches.Add(new { path = Path.GetRelativePath(root, file) });
                    if (matches.Count >= maxMatches) break;
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
        if (!config.Policy.AllowWrites || config.Policy.MaxTierUnattended < 2)
        {
            throw new UnauthorizedAccessException("Policy denied: Tier 2 workspace writes are disabled.");
        }
        var rel = args.TryGetProperty("path", out var p) ? p.GetString() ?? "" : "";
        var content = args.TryGetProperty("content", out var c) ? c.GetString() ?? "" : "";
        var target = ConfineWorkspacePath(config.Workspace, rel);
        EnsureNotSecretFile(target);
        var dir = Path.GetDirectoryName(target);
        if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);
        File.WriteAllText(target, content);
        return new { path = rel, bytes_written = content.Length, status = "created" };
    }

    private static object EditWorkspaceFile(AgentGuiConfig config, JsonElement args)
    {
        if (!config.Policy.AllowTier3 || config.Policy.MaxTierUnattended < 3)
        {
            throw new UnauthorizedAccessException("Policy denied: Tier 3 sensitive operations are disabled.");
        }
        var rel = args.TryGetProperty("path", out var p) ? p.GetString() ?? "" : "";
        var target = ConfineWorkspacePath(config.Workspace, rel);
        EnsureNotSecretFile(target);
        if (!File.Exists(target)) throw new FileNotFoundException($"File not found: {rel}");

        var oldText = args.TryGetProperty("old_text", out var oEl) ? oEl.GetString() : null;
        var newText = args.TryGetProperty("new_text", out var nEl) ? nEl.GetString()
            : args.TryGetProperty("content", out var cEl) ? cEl.GetString() : null;

        if (oldText is not null && newText is not null)
        {
            var existing = File.ReadAllText(target);
            if (!existing.Contains(oldText, StringComparison.Ordinal))
            {
                throw new InvalidOperationException("Target old_text was not found in file.");
            }
            var updated = existing.Replace(oldText, newText, StringComparison.Ordinal);
            File.WriteAllText(target, updated);
            return new { path = rel, bytes_written = updated.Length, status = "edited" };
        }
        if (newText is not null)
        {
            File.WriteAllText(target, newText);
            return new { path = rel, bytes_written = newText.Length, status = "replaced" };
        }
        throw new ArgumentException("Either (old_text, new_text) or content is required.");
    }

    private static object MoveWorkspaceFile(AgentGuiConfig config, JsonElement args)
    {
        if (!config.Policy.AllowTier3 || config.Policy.MaxTierUnattended < 3)
        {
            throw new UnauthorizedAccessException("Policy denied: Tier 3 sensitive operations are disabled.");
        }
        var srcRel = args.TryGetProperty("source", out var sEl) ? sEl.GetString() ?? ""
            : args.TryGetProperty("src", out var s2) ? s2.GetString() ?? "" : "";
        var dstRel = args.TryGetProperty("destination", out var dEl) ? dEl.GetString() ?? ""
            : args.TryGetProperty("dst", out var d2) ? d2.GetString() ?? "" : "";

        var srcFull = ConfineWorkspacePath(config.Workspace, srcRel);
        var dstFull = ConfineWorkspacePath(config.Workspace, dstRel);
        EnsureNotSecretFile(srcFull);
        EnsureNotSecretFile(dstFull);

        var dir = Path.GetDirectoryName(dstFull);
        if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);
        File.Move(srcFull, dstFull, overwrite: false);
        return new { source = srcRel, destination = dstRel, status = "moved" };
    }

    private static object DeleteWorkspaceFile(AgentGuiConfig config, JsonElement args)
    {
        if (!config.Policy.AllowTier4 || config.Policy.MaxTierUnattended < 4)
        {
            throw new UnauthorizedAccessException("Policy denied: Tier 4 high-risk operations are disabled.");
        }
        var rel = args.TryGetProperty("path", out var p) ? p.GetString() ?? "" : "";
        var target = ConfineWorkspacePath(config.Workspace, rel);
        EnsureNotSecretFile(target);
        if (!File.Exists(target)) throw new FileNotFoundException($"File not found: {rel}");
        File.Delete(target);
        return new { path = rel, status = "deleted" };
    }

    private static void EnsureNotSecretFile(string fullPath)
    {
        var fileName = Path.GetFileName(fullPath);
        if (fileName.StartsWith(".env", StringComparison.OrdinalIgnoreCase) ||
            fileName.EndsWith(".pem", StringComparison.OrdinalIgnoreCase) ||
            fileName.EndsWith(".key", StringComparison.OrdinalIgnoreCase))
        {
            throw new UnauthorizedAccessException($"Access to secret file '{fileName}' is blocked by policy.");
        }
    }

    private string ResolveScreenshotDir(AgentGuiConfig config) =>
        string.IsNullOrWhiteSpace(config.ScreenshotDirectory) ? _screenshotDir : config.ScreenshotDirectory;

    private object CaptureScreenshot(AgentGuiConfig config, JsonElement args)
    {
        if (!config.Policy.AllowTier3 || config.Policy.MaxTierUnattended < 3)
        {
            throw new UnauthorizedAccessException("Policy denied: Tier 3 sensitive operations are disabled.");
        }
        var dir = ResolveScreenshotDir(config);
        bool fullDesktop = args.TryGetProperty("full_desktop", out var fdEl) && fdEl.ValueKind == JsonValueKind.True;
        int monitorIdx = args.TryGetProperty("monitor_index", out var mEl) && mEl.TryGetInt32(out var m) ? m : 0;
        var shot = fullDesktop
            ? ScreenCaptureService.CaptureVirtualDesktop(dir)
            : ScreenCaptureService.CapturePrimaryScreen(dir, monitorIdx);
        return new
        {
            path = shot.FilePath,
            monitor_index = shot.MonitorIndex,
            width = shot.Width,
            height = shot.Height,
            size_bytes = shot.SizeBytes,
            base64_data = shot.Base64Png,
            image_base64 = shot.Base64Png,
        };
    }

    private object CaptureWindowScreenshot(AgentGuiConfig config, JsonElement args)
    {
        if (!config.Policy.AllowTier3 || config.Policy.MaxTierUnattended < 3)
        {
            throw new UnauthorizedAccessException("Policy denied: Tier 3 sensitive operations are disabled.");
        }
        var title = args.TryGetProperty("title", out var tEl) ? tEl.GetString()
            : args.TryGetProperty("query", out var qEl) ? qEl.GetString() : null;
        long? hwnd = args.TryGetProperty("handle", out var hEl) && hEl.TryGetInt64(out var h) ? h : null;
        var shot = ScreenCaptureService.CaptureWindow(ResolveScreenshotDir(config), title, hwnd);
        return new
        {
            path = shot.FilePath,
            width = shot.Width,
            height = shot.Height,
            size_bytes = shot.SizeBytes,
            base64_data = shot.Base64Png,
            image_base64 = shot.Base64Png,
        };
    }

    private static object GetScreenSize()
    {
        var (vx, vy, vw, vh, pw, ph) = DesktopInputService.GetVirtualScreenSize();
        return new
        {
            primary_width = pw,
            primary_height = ph,
            virtual_left = vx,
            virtual_top = vy,
            virtual_width = vw,
            virtual_height = vh,
            monitors = ScreenCaptureService.GetMonitors(),
        };
    }

    private static object ListDesktopWindows(JsonElement args)
    {
        var query = args.TryGetProperty("query", out var qEl) ? qEl.GetString() : null;
        int limit = args.TryGetProperty("limit", out var lEl) && lEl.TryGetInt32(out var l) ? l : 60;
        var windows = WindowManagementService.ListVisibleWindows(query, limit);
        return new { count = windows.Count, windows };
    }

    private static object FocusDesktopWindow(AgentGuiConfig config, JsonElement args)
    {
        RequireDesktopControl(config);
        var title = args.TryGetProperty("title", out var tEl) ? tEl.GetString()
            : args.TryGetProperty("query", out var qEl) ? qEl.GetString() : null;
        long? hwnd = args.TryGetProperty("handle", out var hEl) && hEl.TryGetInt64(out var h) ? h : null;
        var win = WindowManagementService.FocusWindow(title, hwnd);
        return new { status = "focused", window = win };
    }

    private static object LaunchApprovedApplication(AgentGuiConfig config, JsonElement args)
    {
        RequireDesktopControl(config);
        var requested = args.TryGetProperty("app", out var aEl) ? aEl.GetString() ?? ""
            : args.TryGetProperty("name", out var nEl) ? nEl.GetString() ?? ""
            : args.TryGetProperty("application", out var apEl) ? apEl.GetString() ?? "" : "";
        var clean = Path.GetFileName(requested.Trim());
        if (string.IsNullOrWhiteSpace(clean) ||
            requested.Contains('/') || requested.Contains('\\') ||
            !config.Policy.AllowedApps.Contains(clean, StringComparer.OrdinalIgnoreCase))
        {
            throw new UnauthorizedAccessException($"Policy denied: application '{requested}' is not in the AllowedApps matrix.");
        }

        var psi = new ProcessStartInfo
        {
            FileName = clean,
            UseShellExecute = true,
        };
        if (args.TryGetProperty("arguments", out var argEl) && argEl.GetString() is { Length: > 0 } appArgs)
        {
            if (appArgs.Contains('\0')) throw new ArgumentException("Invalid arguments.");
            psi.Arguments = appArgs;
        }
        using var proc = Process.Start(psi) ?? throw new InvalidOperationException($"Failed to launch '{clean}'.");
        return new { status = "launched", app = clean, pid = proc.Id };
    }

    private static object GetClipboard(AgentGuiConfig config)
    {
        if (!config.Policy.AllowClipboard)
        {
            throw new UnauthorizedAccessException("Policy denied: clipboard synchronization is disabled.");
        }
        return new { text = WindowManagementService.GetClipboardText() };
    }

    private static object SetClipboard(AgentGuiConfig config, JsonElement args)
    {
        RequireDesktopControl(config);
        if (!config.Policy.AllowClipboard)
        {
            throw new UnauthorizedAccessException("Policy denied: clipboard synchronization is disabled.");
        }
        var text = args.TryGetProperty("text", out var tEl) ? tEl.GetString() ?? "" : "";
        WindowManagementService.SetClipboardText(text);
        return new { status = "ok", length = text.Length };
    }

    private static void RequireDesktopControl(AgentGuiConfig config)
    {
        if (!config.Policy.AllowTier3 || config.Policy.MaxTierUnattended < 3 || !config.Policy.AllowDesktopControl)
        {
            throw new UnauthorizedAccessException("Policy denied: interactive desktop control is disabled.");
        }
    }

    private static object MouseMove(AgentGuiConfig config, JsonElement args)
    {
        RequireDesktopControl(config);
        int x = args.TryGetProperty("x", out var xEl) ? xEl.GetInt32() : 0;
        int y = args.TryGetProperty("y", out var yEl) ? yEl.GetInt32() : 0;
        DesktopInputService.MoveMouse(x, y);
        return new { status = "ok", x, y };
    }

    private static object MouseClick(AgentGuiConfig config, JsonElement args)
    {
        RequireDesktopControl(config);
        int x = args.TryGetProperty("x", out var xEl) ? xEl.GetInt32() : 0;
        int y = args.TryGetProperty("y", out var yEl) ? yEl.GetInt32() : 0;
        string button = args.TryGetProperty("button", out var bEl) ? bEl.GetString() ?? "left" : "left";
        bool dbl = args.TryGetProperty("double_click", out var dEl) && dEl.GetBoolean();
        DesktopInputService.ClickMouse(x, y, button, dbl);
        return new { status = "ok", x, y, button, double_click = dbl };
    }

    private static object MouseScroll(AgentGuiConfig config, JsonElement args)
    {
        RequireDesktopControl(config);
        int delta = args.TryGetProperty("delta", out var dEl) ? dEl.GetInt32() : -120;
        DesktopInputService.ScrollMouse(delta);
        return new { status = "ok", delta };
    }

    private static object KeyboardType(AgentGuiConfig config, JsonElement args)
    {
        RequireDesktopControl(config);
        string text = args.TryGetProperty("text", out var tEl) ? tEl.GetString() ?? "" : "";
        DesktopInputService.TypeText(text);
        return new { status = "ok", length = text.Length };
    }

    private static object KeyPress(AgentGuiConfig config, JsonElement args)
    {
        RequireDesktopControl(config);
        if (args.TryGetProperty("key", out var kEl) && kEl.GetString() is { Length: > 0 } keyName)
        {
            ushort vkNamed = DesktopInputService.ResolveVirtualKey(keyName);
            DesktopInputService.PressKey(vkNamed);
            return new { status = "ok", key = keyName, vk = vkNamed };
        }
        ushort vk = args.TryGetProperty("vk", out var vEl) && vEl.TryGetUInt16(out var code) ? code : (ushort)0x0D;
        DesktopInputService.PressKey(vk);
        return new { status = "ok", vk };
    }

    private static object MouseDrag(AgentGuiConfig config, JsonElement args)
    {
        RequireDesktopControl(config);
        int x1 = args.TryGetProperty("start_x", out var sx) ? sx.GetInt32()
            : args.TryGetProperty("x1", out var x1El) ? x1El.GetInt32() : 0;
        int y1 = args.TryGetProperty("start_y", out var sy) ? sy.GetInt32()
            : args.TryGetProperty("y1", out var y1El) ? y1El.GetInt32() : 0;
        int x2 = args.TryGetProperty("end_x", out var ex) ? ex.GetInt32()
            : args.TryGetProperty("x2", out var x2El) ? x2El.GetInt32() : 0;
        int y2 = args.TryGetProperty("end_y", out var ey) ? ey.GetInt32()
            : args.TryGetProperty("y2", out var y2El) ? y2El.GetInt32() : 0;
        string button = args.TryGetProperty("button", out var bEl) ? bEl.GetString() ?? "left" : "left";
        DesktopInputService.DragMouse(x1, y1, x2, y2, button);
        return new { status = "ok", x1, y1, x2, y2, button };
    }

    private static object PressHotkey(AgentGuiConfig config, JsonElement args)
    {
        RequireDesktopControl(config);
        var combo = args.TryGetProperty("combo", out var cEl) ? cEl.GetString() ?? ""
            : args.TryGetProperty("hotkey", out var hEl) ? hEl.GetString() ?? "" : "";
        DesktopInputService.PressHotkey(combo);
        return new { status = "ok", combo };
    }

    private static object FindUiElements(JsonElement args)
    {
        string? query = args.TryGetProperty("query", out var qEl) ? qEl.GetString() : null;
        int limit = args.TryGetProperty("limit", out var lEl) && lEl.TryGetInt32(out var l) ? Math.Clamp(l, 1, 150) : 60;
        var elements = UiAutomationService.FindVisibleElements(query, limit);
        return new { count = elements.Count, elements };
    }

    private static object ClickUiElement(AgentGuiConfig config, JsonElement args)
    {
        RequireDesktopControl(config);
        string query = args.TryGetProperty("query", out var qEl) ? qEl.GetString() ?? ""
            : args.TryGetProperty("text", out var tEl) ? tEl.GetString() ?? ""
            : args.TryGetProperty("name", out var nEl) ? nEl.GetString() ?? "" : "";
        string button = args.TryGetProperty("button", out var bEl) ? bEl.GetString() ?? "left" : "left";
        var clicked = UiAutomationService.ClickElement(query, button);
        return new { status = "clicked", element = clicked };
    }

    private static async Task<object> SendPipeStreamCommandAsync(
        AgentGuiConfig config,
        string msgType,
        JsonElement args,
        CancellationToken ct)
    {
        if (!config.Policy.AllowTier3)
        {
            throw new UnauthorizedAccessException("Policy denied: Tier 3 sensitive operations are disabled.");
        }
        var (ok, reply) = await NamedPipeBridge.SendCommandAsync(msgType, args, ct: ct);
        if (!ok)
        {
            throw new InvalidOperationException(reply);
        }
        return new { status = "ok", pipe_response = reply };
    }

    private static async Task<object> RunShellAsync(AgentGuiConfig config, JsonElement args, CancellationToken ct)
    {
        if (!config.Policy.AllowTier4 || config.Policy.MaxTierUnattended < 4 || !config.Policy.AllowShell)
        {
            throw new UnauthorizedAccessException("Policy denied: Tier 4 shell execution is disabled.");
        }

        var command = args.TryGetProperty("command", out var cmdEl) ? cmdEl.GetString() ?? "" : "";
        if (string.IsNullOrWhiteSpace(command))
        {
            throw new ArgumentException("Shell command is required.");
        }

        if (config.Policy.AllowedCommands.Count > 0)
        {
            var firstToken = Path.GetFileName(command.Trim().Split(new[] { ' ', '\t' }, StringSplitOptions.RemoveEmptyEntries)[0]);
            if (!config.Policy.AllowedCommands.Any(c =>
                string.Equals(Path.GetFileName(c.Trim()), firstToken, StringComparison.OrdinalIgnoreCase)))
            {
                throw new UnauthorizedAccessException($"Policy denied: command '{firstToken}' is not in AllowedCommands.");
            }
        }

        int defaultSec = Math.Clamp(config.DefaultTimeoutSeconds, 1, 3600);
        int maxSec = Math.Clamp(config.MaxTimeoutSeconds, defaultSec, 3600);
        var timeoutSec = args.TryGetProperty("timeout", out var tEl) && tEl.TryGetInt32(out var t)
            ? Math.Clamp(t, 1, maxSec)
            : defaultSec;

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
            var stdout = await stdoutTask;
            var stderr = await stderrTask;
            int maxOut = Math.Clamp(config.MaxOutputBytes, 1024, 2_000_000);
            if (stdout.Length > maxOut) stdout = stdout[..maxOut] + "\n...[truncated]";
            if (stderr.Length > maxOut) stderr = stderr[..maxOut] + "\n...[truncated]";
            return new
            {
                returncode = proc.ExitCode,
                stdout,
                stderr,
            };
        }
        catch (OperationCanceledException)
        {
            try { proc.Kill(entireProcessTree: true); } catch { }
            throw;
        }
    }
}
