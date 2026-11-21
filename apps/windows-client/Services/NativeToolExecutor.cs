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
                "kill_process" or "stop_process" => KillProcess(config, args),
                "list_drives" or "get_drives" => ListDrives(config),
                "list_directory" or "browse_pc" => ListDirectory(config, args),
                "read_pc_file" => ReadPcFile(config, args),
                "write_pc_file" => WritePcFile(config, args),
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
                    cpp_native_dll_loaded = CppNativeBridge.IsAvailable(),
                    cpp_capture_metrics = new
                    {
                        valid = CppNativeBridge.LastMetrics.Valid != 0,
                        from_dxgi = CppNativeBridge.LastMetrics.FromDxgi != 0,
                        width = CppNativeBridge.LastMetrics.Width,
                        height = CppNativeBridge.LastMetrics.Height,
                        capture_time_ms = Math.Round(CppNativeBridge.LastMetrics.CaptureTimeMs, 2),
                        encode_time_ms = Math.Round(CppNativeBridge.LastMetrics.EncodeTimeMs, 2),
                        encoder_name = CppNativeBridge.LastMetrics.EncoderName ?? "",
                        held_keys = CppNativeBridge.GetHeldKeyCount(),
                    },
                    desktop_control_enabled = config.Policy.AllowDesktopControl,
                    unrestricted_apps_enabled = config.Policy.AllowUnrestrictedApps,
                    clipboard_enabled = config.Policy.AllowClipboard,
                    max_tier_unattended = config.Policy.MaxTierUnattended,
                    allowed_apps = config.Policy.AllowedApps,
                    active_window = WindowManagementService.GetActiveWindowInfo(),
                },
                "get_native_encode_status" or "probe_cpp_agent" or "cpp_agent_status" => new
                {
                    dll_available = CppNativeBridge.IsAvailable(),
                    pipe_available = NamedPipeBridge.IsDesktopAgentAvailable(config.DesktopPipeName),
                    probe_output = (await NamedPipeBridge.RunCppDesktopProbeAsync(
                        0,
                        Math.Clamp(config.CppAgent?.H264TargetFps ?? 30, 1, 60),
                        Math.Clamp(config.CppAgent?.H264BitrateKbps ?? 4500, 250, 50_000),
                        ct)).Response,
                },
                "run_cpp_self_tests" => new
                {
                    passed = (await NamedPipeBridge.RunCppSelfTestsAsync(ct)).Ok,
                    output = (await NamedPipeBridge.RunCppSelfTestsAsync(ct)).Output,
                },
                "set_h264_bitrate" => SetH264Bitrate(config, args),
                "list_desktop_windows" or "desktop_get_windows" => ListDesktopWindows(args),
                "get_active_window" => new { window = WindowManagementService.GetActiveWindowInfo() },
                "focus_window" => FocusDesktopWindow(config, args),
                "maximize_window" => ManageDesktopWindow(config, args, "maximize"),
                "minimize_window" => ManageDesktopWindow(config, args, "minimize"),
                "restore_window" => ManageDesktopWindow(config, args, "restore"),
                "close_window" => ManageDesktopWindow(config, args, "close"),
                "move_resize_window" => MoveResizeDesktopWindow(config, args),
                "launch_approved_application" or "desktop_open_application" or "open_application" => LaunchApprovedApplication(config, args),
                "get_clipboard" or "desktop_clipboard_get" => GetClipboard(config),
                "set_clipboard" or "desktop_clipboard_set" => SetClipboard(config, args),
                "get_ui_tree" or "find_elements" or "list_ui_elements" or "desktop_get_ui_elements" => FindUiElements(args),
                "click_element" or "click_text" => ClickUiElement(config, args),
                "invoke_element" => InvokeUiElement(config, args),
                "set_element_value" or "type_into_element" => SetUiElementValue(config, args),
                "focus_element" => FocusUiElement(config, args),
                "perform_approved_ui_action" => PerformUiAction(config, args),
                "desktop_wait" => await DesktopWaitAsync(args, ct),
                "desktop_verify_action" or "verify_screen_contains" => VerifyUiCondition(args),
                "start_webrtc_stream" => await SendPipeStreamCommandAsync(config, "START_WEBRTC_SESSION", args, ct),
                "stop_webrtc_stream" => await SendPipeStreamCommandAsync(config, "STOP_WEBRTC_SESSION", args, ct),
                "capture_desktop_screenshot" or "take_screenshot" or "capture_desktop_frame" or "desktop_get_screenshot" => CaptureScreenshot(config, args),
                "set_of_marks_screenshot" => CaptureSetOfMarksScreenshot(config, args),
                "desktop_get_window_screenshot" => CaptureWindowScreenshot(config, args),
                "mouse_move" or "desktop_mouse_move" => MouseMove(config, args),
                "mouse_click" or "desktop_click" => MouseClick(config, args),
                "mouse_scroll" or "desktop_scroll" => MouseScroll(config, args),
                "mouse_drag" => MouseDrag(config, args),
                "mouse_draw_path" or "draw_path" => MouseDrawPath(config, args),
                "keyboard_type" or "desktop_keyboard" => KeyboardType(config, args),
                "key_press" or "keyboard_press" => KeyPress(config, args),
                "press_hotkey" => PressHotkey(config, args),
                "run_shell" or "desktop_execute_command" => await RunShellAsync(config, args, ct, usePowerShell: false),
                "run_powershell" or "powershell" => await RunShellAsync(config, args, ct, usePowerShell: true),
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

    private static object SetH264Bitrate(AgentGuiConfig config, JsonElement args)
    {
        int kbps = args.TryGetProperty("bitrate_kbps", out var brEl) && brEl.TryGetInt32(out var brVal)
            ? Math.Clamp(brVal, 250, 50_000)
            : Math.Clamp(config.CppAgent?.H264BitrateKbps ?? 4500, 250, 50_000);
        if (config.CppAgent is not null)
        {
            config.CppAgent.H264BitrateKbps = kbps;
        }
        bool applied = CppNativeBridge.TrySetEncoderBitrate(kbps);
        return new
        {
            bitrate_kbps = kbps,
            applied_live = applied,
        };
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

        // Lexical path checks do not contain a path that crosses a junction or
        // symlink. Reject reparse points from the volume root through the target.
        EnsureNoReparsePoints(rootFull, candidate);

        return candidate;
    }

    private static void EnsureNoReparsePoints(string workspaceRoot, string candidate)
    {
        var volumeRoot = Path.GetPathRoot(workspaceRoot)
            ?? throw new UnauthorizedAccessException("Workspace path has no volume root.");
        var current = volumeRoot;
        var relativeRoot = Path.GetRelativePath(volumeRoot, workspaceRoot);
        var relativeCandidate = Path.GetRelativePath(workspaceRoot, candidate);
        var parts = relativeRoot.Split(Path.DirectorySeparatorChar, StringSplitOptions.RemoveEmptyEntries)
            .Concat(relativeCandidate == "." ? Array.Empty<string>() : relativeCandidate.Split(Path.DirectorySeparatorChar, StringSplitOptions.RemoveEmptyEntries));
        foreach (var part in parts)
        {
            current = Path.Combine(current, part);
            if (!File.Exists(current) && !Directory.Exists(current)) continue;
            if ((File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0)
            {
                throw new UnauthorizedAccessException("Workspace paths cannot traverse symbolic links, junctions, or other reparse points.");
            }
        }
    }

    private static string ResolveAccessiblePath(AgentGuiConfig config, string requestedPath, bool requireFullPcFlag = false)
    {
        if (requestedPath.Contains('\0'))
        {
            throw new UnauthorizedAccessException("Invalid path: NUL byte not allowed.");
        }

        if (!config.Policy.AllowFullPcAccess)
        {
            if (requireFullPcFlag)
            {
                throw new UnauthorizedAccessException("Permission required: Full PC Access is disabled. Allow 'Full PC Access' to browse or modify files outside the workspace.");
            }
            return ConfineWorkspacePath(config.Workspace, requestedPath);
        }

        var expanded = Environment.ExpandEnvironmentVariables(requestedPath.Trim());
        if (string.IsNullOrWhiteSpace(expanded))
        {
            return !string.IsNullOrWhiteSpace(config.Workspace)
                ? Path.GetFullPath(config.Workspace)
                : Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        }

        if (Path.IsPathRooted(expanded))
        {
            return Path.GetFullPath(expanded);
        }

        var baseDir = !string.IsNullOrWhiteSpace(config.Workspace)
            ? Path.GetFullPath(config.Workspace)
            : Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        return Path.GetFullPath(Path.Combine(baseDir, expanded));
    }

    private static void RequireFullPcAccess(AgentGuiConfig config, string operation)
    {
        if (!config.Policy.AllowFullPcAccess)
        {
            throw new UnauthorizedAccessException($"Permission required: '{operation}' requires Full PC Access to be explicitly allowed by the user.");
        }
    }

    private static object ListDrives(AgentGuiConfig config)
    {
        RequireFullPcAccess(config, "list_drives");
        var drives = DriveInfo.GetDrives()
            .Select(d =>
            {
                long totalGb = 0;
                long freeGb = 0;
                string label = "";
                try
                {
                    if (d.IsReady)
                    {
                        totalGb = d.TotalSize / (1024 * 1024 * 1024);
                        freeGb = d.AvailableFreeSpace / (1024 * 1024 * 1024);
                        label = d.VolumeLabel;
                    }
                }
                catch { }
                return new
                {
                    name = d.Name,
                    drive_type = d.DriveType.ToString(),
                    is_ready = d.IsReady,
                    label,
                    free_gb = freeGb,
                    total_gb = totalGb,
                };
            })
            .ToList();
        return new { count = drives.Count, drives };
    }

    private static object ListDirectory(AgentGuiConfig config, JsonElement args)
    {
        RequireFullPcAccess(config, "list_directory");
        if (!config.Policy.AllowTier3 || config.Policy.MaxTierUnattended < 3)
        {
            throw new UnauthorizedAccessException("Policy denied: Tier 3 directory listing outside workspace is disabled.");
        }
        var requested = args.TryGetProperty("path", out var p) ? p.GetString() ?? "" : "";
        var target = string.IsNullOrWhiteSpace(requested)
            ? Environment.GetFolderPath(Environment.SpecialFolder.UserProfile)
            : ResolveAccessiblePath(config, requested, requireFullPcFlag: true);

        if (!Directory.Exists(target))
        {
            throw new DirectoryNotFoundException($"Directory not found: {target}");
        }

        int maxList = Math.Clamp(config.MaxListEntries, 10, 1000);
        var entries = Directory.EnumerateFileSystemEntries(target)
            .Take(maxList)
            .Select(e =>
            {
                long size = 0;
                try { if (File.Exists(e)) size = new FileInfo(e).Length; } catch { }
                return new
                {
                    name = Path.GetFileName(e),
                    full_path = e,
                    is_dir = Directory.Exists(e),
                    size,
                };
            })
            .ToList();

        return new { path = target, count = entries.Count, entries };
    }

    private static object ReadPcFile(AgentGuiConfig config, JsonElement args)
    {
        RequireFullPcAccess(config, "read_pc_file");
        if (!config.Policy.AllowTier3 || config.Policy.MaxTierUnattended < 3)
        {
            throw new UnauthorizedAccessException("Policy denied: Tier 3 file reads outside workspace are disabled.");
        }
        var requested = args.TryGetProperty("path", out var p) ? p.GetString() ?? "" : "";
        var target = ResolveAccessiblePath(config, requested, requireFullPcFlag: true);
        if (!File.Exists(target)) throw new FileNotFoundException($"File not found: {target}");
        var content = File.ReadAllText(target);
        int maxRead = Math.Clamp(config.MaxReadBytes, 1024, 2_000_000);
        if (content.Length > maxRead)
        {
            content = content[..maxRead] + "\n...[truncated]";
        }
        return new { path = target, content };
    }

    private static object WritePcFile(AgentGuiConfig config, JsonElement args)
    {
        RequireFullPcAccess(config, "write_pc_file");
        if (!config.Policy.AllowWrites || !config.Policy.AllowTier4 || config.Policy.MaxTierUnattended < 4)
        {
            throw new UnauthorizedAccessException("Policy denied: Tier 4 full-PC file writes are disabled.");
        }
        var requested = args.TryGetProperty("path", out var p) ? p.GetString() ?? "" : "";
        var content = args.TryGetProperty("content", out var c) ? c.GetString() ?? "" : "";
        var target = ResolveAccessiblePath(config, requested, requireFullPcFlag: true);
        var dir = Path.GetDirectoryName(target);
        if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);
        File.WriteAllText(target, content);
        return new { path = target, bytes_written = content.Length, status = "written" };
    }

    private static object KillProcess(AgentGuiConfig config, JsonElement args)
    {
        RequireFullPcAccess(config, "kill_process");
        if (!config.Policy.AllowTier4 || config.Policy.MaxTierUnattended < 4)
        {
            throw new UnauthorizedAccessException("Policy denied: Tier 4 process termination is disabled.");
        }

        int? pid = args.TryGetProperty("pid", out var pidEl) && pidEl.TryGetInt32(out var pv) ? pv : null;
        string? name = args.TryGetProperty("name", out var nEl) ? nEl.GetString() : null;

        if (pid.HasValue)
        {
            if (pid.Value == Environment.ProcessId)
            {
                throw new UnauthorizedAccessException("Refusing to terminate the active AI Bridge client process.");
            }
            using var proc = Process.GetProcessById(pid.Value);
            var procName = proc.ProcessName;
            proc.Kill(entireProcessTree: true);
            return new { status = "killed", pid = pid.Value, name = procName };
        }

        if (!string.IsNullOrWhiteSpace(name))
        {
            var clean = Path.GetFileNameWithoutExtension(name.Trim());
            var killedPids = new List<int>();
            foreach (var proc in Process.GetProcessesByName(clean))
            {
                using (proc)
                {
                    if (proc.Id == Environment.ProcessId) continue;
                    proc.Kill(entireProcessTree: true);
                    killedPids.Add(proc.Id);
                }
            }
            if (killedPids.Count == 0) throw new InvalidOperationException($"No running process matched '{name}'.");
            return new { status = "killed", name = clean, pids = killedPids };
        }

        throw new ArgumentException("Either 'pid' or 'name' is required.");
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

        var pathArg = args.TryGetProperty("path", out var pEl) ? pEl.GetString() ?? "" : "";
        var root = ConfineWorkspacePath(config.Workspace, pathArg);
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
        var target = ResolveAccessiblePath(config, rel);
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

        var srcFull = ResolveAccessiblePath(config, srcRel);
        var dstFull = ResolveAccessiblePath(config, dstRel);
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
        var target = ResolveAccessiblePath(config, rel);
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
        var title = GetTitleArg(args);
        long? hwnd = args.TryGetProperty("handle", out var hEl) && hEl.TryGetInt64(out var h) ? h : null;
        var win = WindowManagementService.FocusWindow(title, hwnd);
        return new { status = "focused", window = win };
    }

    private static string? GetTitleArg(JsonElement args) =>
        args.TryGetProperty("title", out var tEl) ? tEl.GetString()
            : args.TryGetProperty("window", out var wEl) ? wEl.GetString()
            : args.TryGetProperty("query", out var qEl) ? qEl.GetString() : null;

    private static object ManageDesktopWindow(AgentGuiConfig config, JsonElement args, string action)
    {
        RequireDesktopControl(config);
        var title = GetTitleArg(args);
        long? hwnd = args.TryGetProperty("handle", out var hEl) && hEl.TryGetInt64(out var h) ? h : null;
        var win = action switch
        {
            "maximize" => WindowManagementService.MaximizeWindow(title, hwnd),
            "minimize" => WindowManagementService.MinimizeWindow(title, hwnd),
            "restore" => WindowManagementService.RestoreWindow(title, hwnd),
            "close" => WindowManagementService.CloseWindow(title, hwnd),
            _ => throw new ArgumentException($"Unsupported window action: {action}"),
        };
        return new { status = action, window = win };
    }

    private static object MoveResizeDesktopWindow(AgentGuiConfig config, JsonElement args)
    {
        RequireDesktopControl(config);
        var title = GetTitleArg(args);
        long? hwnd = args.TryGetProperty("handle", out var hEl) && hEl.TryGetInt64(out var h) ? h : null;
        int? x = args.TryGetProperty("x", out var xEl) && xEl.TryGetInt32(out var xv) ? xv : null;
        int? y = args.TryGetProperty("y", out var yEl) && yEl.TryGetInt32(out var yv) ? yv : null;
        int? w = args.TryGetProperty("width", out var wEl) && wEl.TryGetInt32(out var wv) ? wv : null;
        int? height = args.TryGetProperty("height", out var htEl) && htEl.TryGetInt32(out var hv) ? hv : null;
        var win = WindowManagementService.MoveResizeWindow(title, hwnd, x, y, w, height);
        return new { status = "moved_resized", window = win };
    }

    private object CaptureSetOfMarksScreenshot(AgentGuiConfig config, JsonElement args)
    {
        if (!config.Policy.AllowTier3 || config.Policy.MaxTierUnattended < 3)
        {
            throw new UnauthorizedAccessException("Policy denied: Tier 3 sensitive operations are disabled.");
        }
        int maxMarks = args.TryGetProperty("max_marks", out var mEl) && mEl.TryGetInt32(out var m) ? m : 40;
        var window = GetTitleArg(args);
        var (shot, marks) = UiAutomationService.CaptureSetOfMarks(ResolveScreenshotDir(config), maxMarks, window);
        return new
        {
            path = shot.FilePath,
            width = shot.Width,
            height = shot.Height,
            size_bytes = shot.SizeBytes,
            base64_data = shot.Base64Png,
            image_base64 = shot.Base64Png,
            marks = marks.Select(x => new
            {
                mark_id = x.Mark.ToString(),
                name = x.Element.Name,
                control_type = x.Element.ControlType,
                center_x = x.Element.CenterX,
                center_y = x.Element.CenterY,
            }).ToList(),
        };
    }

    private static object LaunchApprovedApplication(AgentGuiConfig config, JsonElement args)
    {
        RequireDesktopControl(config);
        var requested = args.TryGetProperty("app", out var aEl) ? aEl.GetString() ?? ""
            : args.TryGetProperty("app_name", out var anEl) ? anEl.GetString() ?? ""
            : args.TryGetProperty("name", out var nEl) ? nEl.GetString() ?? ""
            : args.TryGetProperty("application", out var apEl) ? apEl.GetString() ?? "" : "";
        requested = requested.Trim();
        if (string.IsNullOrWhiteSpace(requested) || requested.Contains('\0'))
        {
            throw new ArgumentException("Application name or path is required.");
        }

        string launchTarget = requested;
        if (!config.Policy.AllowUnrestrictedApps)
        {
            var clean = Path.GetFileName(requested);
            if (string.IsNullOrWhiteSpace(clean) ||
                requested.Contains('/') || requested.Contains('\\') ||
                !config.Policy.AllowedApps.Contains(clean, StringComparer.OrdinalIgnoreCase))
            {
                throw new UnauthorizedAccessException($"Policy denied: application '{requested}' is not in the AllowedApps matrix.");
            }
            launchTarget = clean;
        }

        var psi = new ProcessStartInfo
        {
            FileName = launchTarget,
            UseShellExecute = true,
        };
        var argStr = args.TryGetProperty("arguments", out var argEl) ? argEl.GetString()
            : args.TryGetProperty("url", out var urlEl) ? urlEl.GetString() : null;
        if (!string.IsNullOrWhiteSpace(argStr))
        {
            if (argStr.Contains('\0')) throw new ArgumentException("Invalid arguments.");
            psi.Arguments = argStr;
        }
        using var proc = Process.Start(psi);
        return new { status = "launched", app = launchTarget, pid = proc?.Id ?? 0 };
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
        if (AgentTakeoverService.IsUserInControl)
        {
            throw new UnauthorizedAccessException("Human takeover is active: click 'Return Control to Agent' to resume agent desktop input.");
        }
        AgentTakeoverService.NotifyAgentActivity();
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
        bool dbl = (args.TryGetProperty("double_click", out var dEl) && dEl.ValueKind == JsonValueKind.True) ||
                   (args.TryGetProperty("clicks", out var cEl) && cEl.TryGetInt32(out var clicks) && clicks >= 2);
        DesktopInputService.ClickMouse(x, y, button, dbl);
        return new { status = "ok", x, y, button, double_click = dbl };
    }

    private static object MouseScroll(AgentGuiConfig config, JsonElement args)
    {
        RequireDesktopControl(config);
        int delta = args.TryGetProperty("delta", out var dEl) ? dEl.GetInt32() : -120;
        int? x = args.TryGetProperty("x", out var xEl) && xEl.TryGetInt32(out var xv) ? xv : null;
        int? y = args.TryGetProperty("y", out var yEl) && yEl.TryGetInt32(out var yv) ? yv : null;
        bool horizontal = args.TryGetProperty("horizontal", out var hEl) && hEl.ValueKind == JsonValueKind.True;
        DesktopInputService.ScrollMouse(delta, x, y, horizontal);
        return new { status = "ok", delta, x, y, horizontal };
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

    private static object MouseDrawPath(AgentGuiConfig config, JsonElement args)
    {
        RequireDesktopControl(config);
        string button = args.TryGetProperty("button", out var bEl) ? bEl.GetString() ?? "left" : "left";
        int stepDelay = args.TryGetProperty("step_delay_ms", out var dEl) && dEl.TryGetInt32(out var dVal) ? dVal : 4;
        var strokes = new List<IReadOnlyList<(int X, int Y)>>();

        if (args.TryGetProperty("shapes", out var shapesEl) && shapesEl.ValueKind == JsonValueKind.Array)
        {
            foreach (var shapeEl in shapesEl.EnumerateArray())
            {
                if (shapeEl.ValueKind != JsonValueKind.Object) continue;
                var type = (shapeEl.TryGetProperty("type", out var tEl) ? tEl.GetString() ?? "" : "").Trim().ToLowerInvariant();
                if (type is "circle" or "ellipse" or "arc")
                {
                    double cx = shapeEl.TryGetProperty("cx", out var cxEl) ? cxEl.GetDouble() : 0;
                    double cy = shapeEl.TryGetProperty("cy", out var cyEl) ? cyEl.GetDouble() : 0;
                    double rx = shapeEl.TryGetProperty("rx", out var rxEl) ? rxEl.GetDouble()
                        : (shapeEl.TryGetProperty("r", out var rEl) ? rEl.GetDouble() : 40);
                    double ry = shapeEl.TryGetProperty("ry", out var ryEl) ? ryEl.GetDouble() : rx;
                    double startDeg = shapeEl.TryGetProperty("start_deg", out var sEl) ? sEl.GetDouble() : 0;
                    double endDeg = shapeEl.TryGetProperty("end_deg", out var eEl) ? eEl.GetDouble() : 360;
                    int segs = Math.Clamp((int)Math.Ceiling(Math.Abs(endDeg - startDeg) / 9.0), 12, 64);
                    var pts = new List<(int X, int Y)>(segs + 1);
                    for (int i = 0; i <= segs; i++)
                    {
                        double rad = (startDeg + (endDeg - startDeg) * i / segs) * Math.PI / 180.0;
                        pts.Add(((int)Math.Round(cx + rx * Math.Cos(rad)), (int)Math.Round(cy + ry * Math.Sin(rad))));
                    }
                    if (pts.Count >= 2) strokes.Add(pts);
                }
                else if (type == "rect")
                {
                    int x = shapeEl.TryGetProperty("x", out var xEl) ? xEl.GetInt32() : 0;
                    int y = shapeEl.TryGetProperty("y", out var yEl) ? yEl.GetInt32() : 0;
                    int w = shapeEl.TryGetProperty("w", out var wEl) ? wEl.GetInt32() : (shapeEl.TryGetProperty("width", out var w2El) ? w2El.GetInt32() : 100);
                    int h = shapeEl.TryGetProperty("h", out var hEl) ? hEl.GetInt32() : (shapeEl.TryGetProperty("height", out var h2El) ? h2El.GetInt32() : 60);
                    strokes.Add(new List<(int X, int Y)> { (x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y) });
                }
                else if (type == "line")
                {
                    int x1 = shapeEl.TryGetProperty("x1", out var x1El) ? x1El.GetInt32() : 0;
                    int y1 = shapeEl.TryGetProperty("y1", out var y1El) ? y1El.GetInt32() : 0;
                    int x2 = shapeEl.TryGetProperty("x2", out var x2El) ? x2El.GetInt32() : 0;
                    int y2 = shapeEl.TryGetProperty("y2", out var y2El) ? y2El.GetInt32() : 0;
                    strokes.Add(new List<(int X, int Y)> { (x1, y1), (x2, y2) });
                }
            }
        }

        if (args.TryGetProperty("strokes", out var strokesEl) && strokesEl.ValueKind == JsonValueKind.Array)
        {
            foreach (var strokeEl in strokesEl.EnumerateArray())
            {
                if (strokeEl.ValueKind != JsonValueKind.Array) continue;
                var pts = new List<(int X, int Y)>();
                foreach (var ptEl in strokeEl.EnumerateArray())
                {
                    if (ptEl.ValueKind == JsonValueKind.Array && ptEl.GetArrayLength() >= 2)
                    {
                        pts.Add((ptEl[0].GetInt32(), ptEl[1].GetInt32()));
                    }
                    else if (ptEl.ValueKind == JsonValueKind.Object &&
                             ptEl.TryGetProperty("x", out var xEl) && ptEl.TryGetProperty("y", out var yEl))
                    {
                        pts.Add((xEl.GetInt32(), yEl.GetInt32()));
                    }
                }
                if (pts.Count >= 2) strokes.Add(pts);
            }
        }
        else if (args.TryGetProperty("points", out var ptsEl) && ptsEl.ValueKind == JsonValueKind.Array)
        {
            var pts = new List<(int X, int Y)>();
            foreach (var ptEl in ptsEl.EnumerateArray())
            {
                if (ptEl.ValueKind == JsonValueKind.Array && ptEl.GetArrayLength() >= 2)
                {
                    pts.Add((ptEl[0].GetInt32(), ptEl[1].GetInt32()));
                }
                else if (ptEl.ValueKind == JsonValueKind.Object &&
                         ptEl.TryGetProperty("x", out var xEl) && ptEl.TryGetProperty("y", out var yEl))
                {
                    pts.Add((xEl.GetInt32(), yEl.GetInt32()));
                }
            }
            if (pts.Count >= 2) strokes.Add(pts);
        }

        if (strokes.Count == 0)
        {
            throw new ArgumentException("Provide 'strokes' (array of [[x,y], ...]), 'shapes' (circle/ellipse/arc/rect/line), or 'points' with at least 2 points.");
        }

        int segments = DesktopInputService.DrawPath(strokes, button, stepDelay);
        return new { status = "ok", strokes_drawn = strokes.Count, segments_drawn = segments };
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
        string? window = args.TryGetProperty("window", out var wEl) ? wEl.GetString()
            : args.TryGetProperty("window_title", out var wtEl) ? wtEl.GetString() : null;
        string? controlType = args.TryGetProperty("control_type", out var ctEl) ? ctEl.GetString() : null;
        int maxDepth = args.TryGetProperty("max_depth", out var dEl) && dEl.TryGetInt32(out var d) ? Math.Clamp(d, 2, 10) : 6;
        int limit = args.TryGetProperty("limit", out var lEl) && lEl.TryGetInt32(out var l) ? Math.Clamp(l, 1, 200) : 80;
        var elements = UiAutomationService.FindVisibleElements(query, limit, window, controlType, maxDepth);
        return new { count = elements.Count, window = window ?? "desktop", elements };
    }

    private static object ClickUiElement(AgentGuiConfig config, JsonElement args)
    {
        RequireDesktopControl(config);
        string query = args.TryGetProperty("query", out var qEl) ? qEl.GetString() ?? ""
            : args.TryGetProperty("text", out var tEl) ? tEl.GetString() ?? ""
            : args.TryGetProperty("name", out var nEl) ? nEl.GetString() ?? "" : "";
        string? markId = args.TryGetProperty("mark_id", out var mEl) ? mEl.GetString() : null;
        string? window = args.TryGetProperty("window", out var wEl) ? wEl.GetString() : null;
        string button = args.TryGetProperty("button", out var bEl) ? bEl.GetString() ?? "left" : "left";
        bool dbl = (args.TryGetProperty("double_click", out var dEl) && dEl.ValueKind == JsonValueKind.True) ||
                   (args.TryGetProperty("clicks", out var cEl) && cEl.TryGetInt32(out var clicks) && clicks >= 2);
        var clicked = UiAutomationService.ClickElement(query, button, dbl, window, markId);
        return new { status = "clicked", element = clicked };
    }

    private static object InvokeUiElement(AgentGuiConfig config, JsonElement args)
    {
        RequireDesktopControl(config);
        string query = args.TryGetProperty("query", out var qEl) ? qEl.GetString() ?? ""
            : args.TryGetProperty("name", out var nEl) ? nEl.GetString() ?? "" : "";
        string? window = args.TryGetProperty("window", out var wEl) ? wEl.GetString() : null;
        var invoked = UiAutomationService.InvokeElement(query, window);
        return new { status = "invoked", element = invoked };
    }

    private static object SetUiElementValue(AgentGuiConfig config, JsonElement args)
    {
        RequireDesktopControl(config);
        string query = args.TryGetProperty("query", out var qEl) ? qEl.GetString() ?? ""
            : args.TryGetProperty("name", out var nEl) ? nEl.GetString() ?? "" : "";
        string value = args.TryGetProperty("value", out var vEl) ? vEl.GetString() ?? ""
            : args.TryGetProperty("text", out var tEl) ? tEl.GetString() ?? "" : "";
        bool submit = args.TryGetProperty("submit", out var sEl) && sEl.ValueKind == JsonValueKind.True;
        string? window = args.TryGetProperty("window", out var wEl) ? wEl.GetString() : null;
        var updated = UiAutomationService.SetElementValue(query, value, submit, window);
        return new { status = "updated", element = updated, length = value.Length };
    }

    private static object FocusUiElement(AgentGuiConfig config, JsonElement args)
    {
        RequireDesktopControl(config);
        string query = args.TryGetProperty("query", out var qEl) ? qEl.GetString() ?? ""
            : args.TryGetProperty("name", out var nEl) ? nEl.GetString() ?? "" : "";
        string? window = args.TryGetProperty("window", out var wEl) ? wEl.GetString() : null;
        var focused = UiAutomationService.FocusElement(query, window);
        return new { status = "focused", element = focused };
    }

    private static object PerformUiAction(AgentGuiConfig config, JsonElement args)
    {
        var action = (args.TryGetProperty("action", out var aEl) ? aEl.GetString() ?? "invoke" : "invoke").Trim().ToLowerInvariant();
        return action switch
        {
            "click" or "right_click" or "double_click" => ClickUiElement(config, args),
            "set_value" or "type" => SetUiElementValue(config, args),
            "focus" => FocusUiElement(config, args),
            _ => InvokeUiElement(config, args),
        };
    }

    private static async Task<object> DesktopWaitAsync(JsonElement args, CancellationToken ct)
    {
        int ms = args.TryGetProperty("ms", out var msEl) && msEl.TryGetInt32(out var mv) ? mv
            : args.TryGetProperty("milliseconds", out var m2) && m2.TryGetInt32(out var m2v) ? m2v : 500;
        ms = Math.Clamp(ms, 50, 10000);
        await Task.Delay(ms, ct);
        return new { status = "waited", ms };
    }

    private static object VerifyUiCondition(JsonElement args)
    {
        string text = args.TryGetProperty("text", out var tEl) ? tEl.GetString() ?? ""
            : args.TryGetProperty("expected", out var eEl) ? eEl.GetString() ?? "" : "";
        string? window = args.TryGetProperty("window", out var wEl) ? wEl.GetString() : null;
        var matches = UiAutomationService.FindVisibleElements(text, 10, window);
        var winMatches = WindowManagementService.ListVisibleWindows(text, 10);
        bool visible = matches.Count > 0 || winMatches.Count > 0;
        return new { verified = visible, ui_matches = matches.Count, window_matches = winMatches.Count };
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
        if (string.Equals(msgType, "START_WEBRTC_SESSION", StringComparison.OrdinalIgnoreCase))
        {
            AgentTakeoverService.NotifyAgentActivity();
        }
        var (ok, reply) = await NamedPipeBridge.SendCommandAsync(msgType, args, ct: ct);
        if (!ok)
        {
            throw new InvalidOperationException(reply);
        }
        return new { status = "ok", pipe_response = reply };
    }

    private static async Task<object> RunShellAsync(
        AgentGuiConfig config,
        JsonElement args,
        CancellationToken ct,
        bool usePowerShell = false)
    {
        if (usePowerShell)
        {
            RequireFullPcAccess(config, "run_powershell");
        }
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

        var requestedCwd = args.TryGetProperty("working_directory", out var wdEl) ? wdEl.GetString()
            : args.TryGetProperty("cwd", out var cwdEl) ? cwdEl.GetString() : null;
        var cwd = !string.IsNullOrWhiteSpace(requestedCwd)
            ? ResolveAccessiblePath(config, requestedCwd, requireFullPcFlag: true)
            : (config.Policy.AllowFullPcAccess && string.IsNullOrWhiteSpace(config.Workspace)
                ? Environment.GetFolderPath(Environment.SpecialFolder.UserProfile)
                : ConfineWorkspacePath(config.Workspace, ""));

        var psi = new ProcessStartInfo
        {
            FileName = usePowerShell ? "powershell.exe" : "cmd.exe",
            Arguments = usePowerShell
                ? $"-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command \"{command.Replace("\"", "\\\"")}\""
                : $"/c {command}",
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
