using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Automation;
using AIBridgeClient.Models;

namespace AIBridgeClient.Services;

public sealed record HybridA11yRefEntry(
    string Ref,
    string SnapshotId,
    string Role,
    string Name,
    string AutomationId,
    string WindowTitle,
    int X,
    int Y,
    int Width,
    int Height,
    int CenterX,
    int CenterY,
    bool Enabled,
    bool IsPassword,
    string Patterns,
    string Value,
    bool? Checked,
    bool? Expanded,
    AutomationElement? LiveElement);

/// <summary>
/// Native Windows Hybrid Desktop Accessibility &amp; Visual Fallback Engine.
/// Provides deterministic snapshot-scoped <c>@s&lt;id&gt;:e&lt;n&gt;</c> refs with <c>STALE_REF</c>
/// validation, password redaction, headless UIA pattern execution (idempotent check/uncheck,
/// expand/collapse, select, clear, scroll_to, set_value, invoke), atomic batch execution,
/// element-condition waits, optional <c>agent-desktop</c> CLI detection, and automatic
/// physical/OCR fallback when a control is not accessible via UIA.
/// </summary>
public static class HybridA11yService
{
    private const int MaxBatchSteps = 25;
    private static readonly TimeSpan SnapshotTtl = TimeSpan.FromMinutes(5);
    private static readonly Regex RefPattern = new(@"^@(?:[A-Za-z0-9_-]{2,40}:)?[A-Za-z0-9_.-]{1,40}$", RegexOptions.Compiled);
    private static readonly Regex SecretHintPattern = new(
        @"(password|passwd|passcode|\bpin\b|secret|api[_ -]?key|token|ssn|cvv|credit.?card)",
        RegexOptions.IgnoreCase | RegexOptions.Compiled);

    public static readonly HashSet<string> SupportedActions = new(StringComparer.OrdinalIgnoreCase)
    {
        "click", "double_click", "right_click", "hover",
        "type", "fill", "set_value", "clear",
        "check", "uncheck", "toggle",
        "select", "expand", "collapse",
        "scroll_to", "focus", "show_menu",
    };

    private static readonly object StoreLock = new();
    private static string _activeSnapshotId = "";
    private static DateTimeOffset _snapshotCreatedAt = DateTimeOffset.MinValue;
    private static Dictionary<string, HybridA11yRefEntry> _refs = new(StringComparer.Ordinal);

    public static string? FindAgentDesktopCli()
    {
        var explicitBin = Environment.GetEnvironmentVariable("AI_BRIDGE_AGENT_DESKTOP_BIN")?.Trim();
        if (!string.IsNullOrWhiteSpace(explicitBin) && File.Exists(explicitBin))
        {
            return explicitBin;
        }
        var pathEnv = Environment.GetEnvironmentVariable("PATH") ?? "";
        foreach (var dir in pathEnv.Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries))
        {
            foreach (var name in new[] { "agent-desktop.exe", "agent-desktop.cmd", "agent-desktop" })
            {
                var candidate = Path.Combine(dir.Trim(), name);
                if (File.Exists(candidate))
                {
                    return candidate;
                }
            }
        }
        return null;
    }

    public static object DescribeStatus()
    {
        var cli = FindAgentDesktopCli();
        lock (StoreLock)
        {
            bool expired = _snapshotCreatedAt == DateTimeOffset.MinValue || (DateTimeOffset.UtcNow - _snapshotCreatedAt) > SnapshotTtl;
            return new
            {
                ok = true,
                active_backend = string.IsNullOrEmpty(cli) ? "native_windows_uia" : "hybrid_uia_and_cli",
                platform_adapter = "windows_uia",
                cli_available = !string.IsNullOrEmpty(cli),
                cli_path = cli ?? "",
                native_uia_available = true,
                visual_ocr_fallback_available = true,
                supported_actions = SupportedActions.OrderBy(s => s).ToArray(),
                active_snapshot_id = string.IsNullOrEmpty(_activeSnapshotId) ? null : _activeSnapshotId,
                cached_ref_count = expired ? 0 : _refs.Count,
                permissions = CheckPermissions(),
            };
        }
    }

    public static object CheckPermissions()
    {
        bool interactive = Environment.UserInteractive;
        bool secureDesktop = CppNativeBridge.LastMetrics.SecureDesktop != 0;
        return new
        {
            ok = interactive && !secureDesktop,
            platform = "windows",
            accessibility_granted = interactive && !secureDesktop,
            screen_recording_granted = interactive && !secureDesktop,
            interactive_session = interactive,
            secure_desktop = secureDesktop,
            guide = (interactive && !secureDesktop)
                ? "Ready: Windows UI Automation active in interactive user session (UIPI still isolates elevated windows from unelevated agents)."
                : "Run AIBridgeClient in an unlocked interactive user session (not Session 0 or UAC secure desktop).",
        };
    }

    public static bool IsSensitiveNode(string name, string automationId, string role, bool isPassword)
    {
        if (isPassword) return true;
        if (role.Contains("Password", StringComparison.OrdinalIgnoreCase)) return true;
        return SecretHintPattern.IsMatch($"{name} {automationId}");
    }

    public static bool ValidateRefSyntax(string? refId) =>
        !string.IsNullOrWhiteSpace(refId) && RefPattern.IsMatch(refId.Trim());

    public static void ClearSnapshotStore()
    {
        lock (StoreLock)
        {
            _activeSnapshotId = "";
            _snapshotCreatedAt = DateTimeOffset.MinValue;
            _refs.Clear();
        }
    }

    /// <summary>
    /// Seeds a deterministic synthetic snapshot in-memory (useful for unit tests and offline verification).
    /// </summary>
    public static string SeedSnapshotForTesting(IEnumerable<HybridA11yRefEntry> entries, string? snapshotId = null)
    {
        var sid = string.IsNullOrWhiteSpace(snapshotId) ? $"s{Guid.NewGuid():N}"[..9] : snapshotId;
        lock (StoreLock)
        {
            _activeSnapshotId = sid;
            _snapshotCreatedAt = DateTimeOffset.UtcNow;
            _refs = entries.ToDictionary(e => e.Ref, e => e with { SnapshotId = sid }, StringComparer.Ordinal);
        }
        return sid;
    }

    private static readonly HashSet<string> InteractiveRoles = new(StringComparer.OrdinalIgnoreCase)
    {
        "Button", "Edit", "CheckBox", "RadioButton", "ComboBox", "ListItem",
        "MenuItem", "TabItem", "Hyperlink", "TreeItem", "Slider", "SplitButton",
        "Spinner", "Document", "DataItem",
    };

    public static object CaptureSnapshot(
        string? windowTitle = null,
        string? rootRef = null,
        bool skeleton = false,
        bool interactiveOnly = false,
        bool compact = true,
        int maxDepth = 10,
        int maxElements = 80)
    {
        AutomationElement? startNode = null;
        string resolvedWindow = windowTitle?.Trim() ?? "";

        if (!string.IsNullOrWhiteSpace(rootRef))
        {
            if (!ValidateRefSyntax(rootRef))
            {
                throw new ArgumentException($"Invalid root_ref format: '{rootRef}'.");
            }
            if (!TryResolveRef(rootRef.Trim(), out var rootEntry) || rootEntry?.LiveElement is null)
            {
                return new
                {
                    ok = false,
                    error_code = "STALE_REF",
                    @ref = rootRef.Trim(),
                    error = $"Root ref '{rootRef}' is stale or unavailable.",
                    retry_hint = "Call desktop_a11y_snapshot without root_ref to refresh.",
                };
            }
            startNode = rootEntry.LiveElement;
            resolvedWindow = rootEntry.WindowTitle;
        }
        else if (!string.IsNullOrWhiteSpace(windowTitle))
        {
            var win = WindowManagementService.ResolveWindow(windowTitle);
            startNode = AutomationElement.FromHandle(new IntPtr(win.Handle));
            resolvedWindow = win.Title;
        }
        else
        {
            startNode = AutomationElement.RootElement;
        }

        if (startNode is null)
        {
            return new { ok = false, error_code = "NO_ROOT", error = "Unable to resolve UI Automation root." };
        }

        int depthCap = Math.Clamp(maxDepth, 1, 16);
        int elemCap = Math.Clamp(maxElements, 1, 250);
        string snapshotId = $"s{Guid.NewGuid():N}"[..9];

        var walker = TreeWalker.ControlViewWalker;
        var queue = new Queue<(AutomationElement El, int Depth, string WinTitle)>();
        if (!string.IsNullOrWhiteSpace(resolvedWindow) || !string.IsNullOrWhiteSpace(rootRef))
        {
            queue.Enqueue((startNode, 0, resolvedWindow));
        }
        else
        {
            var child = walker.GetFirstChild(startNode);
            while (child is not null)
            {
                string childWin = "";
                try { childWin = child.Current.Name ?? ""; } catch { }
                queue.Enqueue((child, 0, childWin));
                child = walker.GetNextSibling(child);
            }
        }

        var entries = new List<HybridA11yRefEntry>();
        var publicList = new List<object>();

        while (queue.Count > 0 && entries.Count < elemCap)
        {
            var (el, depth, curWin) = queue.Dequeue();
            try
            {
                var cur = el.Current;
                if (!cur.IsOffscreen && !cur.BoundingRectangle.IsEmpty)
                {
                    var name = cur.Name ?? "";
                    var autoId = cur.AutomationId ?? "";
                    var role = cur.ControlType.ProgrammaticName.Replace("ControlType.", "");
                    bool includeNode = (!string.IsNullOrWhiteSpace(name) || !string.IsNullOrWhiteSpace(autoId)) &&
                        (!skeleton || role is "Window" or "Pane" or "Menu" or "MenuBar" or "Tab" or "ToolBar" or "Group" or "Dialog") &&
                        (!interactiveOnly || InteractiveRoles.Contains(role));

                    if (includeNode)
                    {
                        var r = cur.BoundingRectangle;
                        int rx = (int)r.X;
                        int ry = (int)r.Y;
                        int rw = (int)r.Width;
                        int rh = (int)r.Height;
                        bool sensitive = IsSensitiveNode(name, autoId, role, cur.IsPassword);
                        var (patterns, val, isChecked, isExpanded) = InspectElementState(el, sensitive);
                        string refId = $"@{snapshotId}:e{entries.Count + 1}";

                        var entry = new HybridA11yRefEntry(
                            refId,
                            snapshotId,
                            role,
                            name,
                            autoId,
                            string.IsNullOrWhiteSpace(curWin) ? name : curWin,
                            rx,
                            ry,
                            rw,
                            rh,
                            rx + Math.Max(1, rw / 2),
                            ry + Math.Max(1, rh / 2),
                            cur.IsEnabled,
                            sensitive,
                            patterns,
                            sensitive ? (string.IsNullOrEmpty(val) ? "" : "[REDACTED]") : val,
                            isChecked,
                            isExpanded,
                            el);
                        entries.Add(entry);
                        publicList.Add(FormatPublicEntry(entry, compact));
                    }
                }

                if (depth < depthCap && !skeleton)
                {
                    var sub = walker.GetFirstChild(el);
                    int siblings = 0;
                    int maxSiblings = string.IsNullOrWhiteSpace(resolvedWindow) ? 40 : 100;
                    while (sub is not null && siblings < maxSiblings)
                    {
                        queue.Enqueue((sub, depth + 1, curWin));
                        sub = walker.GetNextSibling(sub);
                        siblings++;
                    }
                }
            }
            catch
            {
                // Skip transient or inaccessible UIA nodes
            }
        }

        lock (StoreLock)
        {
            _activeSnapshotId = snapshotId;
            _snapshotCreatedAt = DateTimeOffset.UtcNow;
            _refs = entries.ToDictionary(e => e.Ref, e => e, StringComparer.Ordinal);
        }

        return new
        {
            ok = true,
            backend = "native_windows_uia",
            snapshot_id = snapshotId,
            skeleton,
            compact,
            ref_count = entries.Count,
            elements = publicList,
        };
    }

    public static object FindElements(string? query = null, string? role = null, string? windowTitle = null, int maxResults = 25)
    {
        CaptureSnapshot(windowTitle: windowTitle, skeleton: false, compact: false, maxDepth: 10, maxElements: Math.Max(80, maxResults * 4));
        string q = (query ?? "").Trim();
        string r = (role ?? "").Trim();
        int cap = Math.Clamp(maxResults, 1, 100);

        List<object> matches;
        string snapId;
        lock (StoreLock)
        {
            snapId = _activeSnapshotId;
            matches = _refs.Values
                .Where(e =>
                    (string.IsNullOrEmpty(q) ||
                     e.Name.Contains(q, StringComparison.OrdinalIgnoreCase) ||
                     e.AutomationId.Contains(q, StringComparison.OrdinalIgnoreCase)) &&
                    (string.IsNullOrEmpty(r) || e.Role.Contains(r, StringComparison.OrdinalIgnoreCase)))
                .Take(cap)
                .Select(e => FormatPublicEntry(e, compact: false))
                .ToList();
        }

        return new
        {
            ok = true,
            backend = "native_windows_uia",
            snapshot_id = snapId,
            query = q,
            role = r,
            count = matches.Count,
            matches,
        };
    }

    public static object GetElementValue(string refId)
    {
        if (!ValidateRefSyntax(refId))
        {
            throw new ArgumentException($"Invalid ref format: '{refId}'.");
        }
        if (!TryResolveRef(refId.Trim(), out var entry) || entry is null)
        {
            return StaleRefResponse(refId.Trim());
        }

        string liveValue = entry.Value;
        bool? liveChecked = entry.Checked;
        bool? liveExpanded = entry.Expanded;
        bool liveEnabled = entry.Enabled;

        if (entry.LiveElement is not null)
        {
            try
            {
                var cur = entry.LiveElement.Current;
                liveEnabled = cur.IsEnabled;
                var (_, v, chk, exp) = InspectElementState(entry.LiveElement, entry.IsPassword);
                liveValue = entry.IsPassword ? (string.IsNullOrEmpty(v) ? "" : "[REDACTED]") : v;
                liveChecked = chk;
                liveExpanded = exp;
            }
            catch
            {
                return StaleRefResponse(refId.Trim());
            }
        }

        return new
        {
            ok = true,
            @ref = entry.Ref,
            snapshot_id = entry.SnapshotId,
            role = entry.Role,
            name = entry.Name,
            value = entry.IsPassword ? (string.IsNullOrEmpty(liveValue) ? "" : "[REDACTED]") : liveValue,
            redacted = entry.IsPassword,
            enabled = liveEnabled,
            @checked = liveChecked,
            expanded = liveExpanded,
            bounds = new { x = entry.X, y = entry.Y, width = entry.Width, height = entry.Height },
        };
    }

    public static object ListSurfaces(string? appFilter = null)
    {
        var surfaces = new List<object>();
        var filter = (appFilter ?? "").Trim();
        var walker = TreeWalker.ControlViewWalker;
        var child = walker.GetFirstChild(AutomationElement.RootElement);
        int count = 0;
        while (child is not null && count < 100)
        {
            try
            {
                var cur = child.Current;
                if (!cur.IsOffscreen && !cur.BoundingRectangle.IsEmpty)
                {
                    var role = cur.ControlType.ProgrammaticName.Replace("ControlType.", "");
                    var title = cur.Name ?? "";
                    if (string.IsNullOrEmpty(filter) || title.Contains(filter, StringComparison.OrdinalIgnoreCase))
                    {
                        var r = cur.BoundingRectangle;
                        string surfaceType = role is "Menu" or "MenuBar" ? "menu"
                            : role is "ToolTip" ? "popover"
                            : role is "Window" ? "window"
                            : "surface";
                        surfaces.Add(new
                        {
                            surface_type = surfaceType,
                            role,
                            title,
                            pid = cur.ProcessId,
                            bounds = new { x = (int)r.X, y = (int)r.Y, width = (int)r.Width, height = (int)r.Height },
                        });
                        count++;
                    }
                }
            }
            catch { }
            child = walker.GetNextSibling(child);
        }

        return new { ok = true, backend = "native_windows_uia", count = surfaces.Count, surfaces };
    }

    public static object ListApps()
    {
        var byProc = new Dictionary<string, (int Pid, int Windows, List<string> Titles)>(StringComparer.OrdinalIgnoreCase);
        foreach (var w in WindowManagementService.ListVisibleWindows(limit: 80))
        {
            var key = string.IsNullOrWhiteSpace(w.ProcessName) ? w.Title : w.ProcessName;
            if (byProc.TryGetValue(key, out var cur))
            {
                cur.Titles.Add(w.Title);
                byProc[key] = (cur.Pid, cur.Windows + 1, cur.Titles);
            }
            else
            {
                byProc[key] = (w.Pid, 1, new List<string> { w.Title });
            }
        }
        var apps = byProc.Select(kv => new
        {
            name = kv.Key,
            pid = kv.Value.Pid,
            window_count = kv.Value.Windows,
            titles = kv.Value.Titles.Take(5).ToArray(),
        }).ToList();
        return new { ok = true, backend = "native_windows_uia", count = apps.Count, apps };
    }

    public static async Task<object> ActOnRefAsync(
        string refId,
        string action,
        string? text,
        string? option,
        bool submit,
        bool headed,
        bool fallbackToVisual,
        string? fallbackText,
        AgentGuiConfig config,
        CancellationToken ct)
    {
        if (AgentTakeoverService.IsUserInControl)
        {
            throw new UnauthorizedAccessException("Human takeover is active: AI desktop input is paused until control is returned.");
        }
        if (!config.Policy.AllowTier3 || !config.Policy.AllowDesktopControl || config.Policy.MaxTierUnattended < 3)
        {
            throw new UnauthorizedAccessException("Policy denied: Tier 3 desktop control is disabled.");
        }

        var normAction = (action ?? "click").Trim().ToLowerInvariant().Replace("-", "_");
        if (!SupportedActions.Contains(normAction))
        {
            throw new ArgumentException($"Unsupported accessibility action '{action}'.");
        }
        if (!ValidateRefSyntax(refId))
        {
            throw new ArgumentException($"Invalid accessibility ref '{refId}'.");
        }

        if (!TryResolveRef(refId.Trim(), out var entry) || entry is null)
        {
            return StaleRefResponse(refId.Trim(), normAction);
        }

        // Enforce pinned window scope when active
        var pinnedTitle = config.DeviceAgent?.PinnedWindowTitle?.Trim();
        if (!string.IsNullOrWhiteSpace(pinnedTitle) &&
            !entry.WindowTitle.Contains(pinnedTitle, StringComparison.OrdinalIgnoreCase))
        {
            throw new UnauthorizedAccessException(
                $"Scoped focus denied: ref '{entry.Ref}' belongs to '{entry.WindowTitle}', not pinned window '{pinnedTitle}'.");
        }

        // Layer 1: Headless UIA pattern execution (no physical mouse movement unless headed=true)
        if (!headed && entry.LiveElement is not null)
        {
            var headlessOutcome = TryExecuteHeadlessUia(entry, normAction, text ?? "", option ?? "", submit);
            if (headlessOutcome is not null)
            {
                return headlessOutcome;
            }
        }
        else if (!headed && entry.LiveElement is null)
        {
            // Synthetic / non-live entry idempotent checks
            if (normAction == "check" && entry.Checked == true)
                return new { ok = true, backend = "native_windows_uia", headless = true, noop = true, @ref = entry.Ref, action = normAction, state = "checked" };
            if (normAction == "uncheck" && entry.Checked == false)
                return new { ok = true, backend = "native_windows_uia", headless = true, noop = true, @ref = entry.Ref, action = normAction, state = "unchecked" };
            if (normAction == "expand" && entry.Expanded == true)
                return new { ok = true, backend = "native_windows_uia", headless = true, noop = true, @ref = entry.Ref, action = normAction, state = "expanded" };
            if (normAction == "collapse" && entry.Expanded == false)
                return new { ok = true, backend = "native_windows_uia", headless = true, noop = true, @ref = entry.Ref, action = normAction, state = "collapsed" };
        }

        if (!fallbackToVisual && !headed)
        {
            return new
            {
                ok = false,
                error_code = "HEADLESS_UNSUPPORTED",
                @ref = entry.Ref,
                action = normAction,
                error = $"Headless UIA pattern for '{normAction}' is not supported on '{entry.Role}' and fallback_to_visual=false.",
            };
        }

        // Layer 2: Physical / OCR fallback
        AgentTakeoverService.NotifyAgentActivity();
        if (entry.Width <= 0 || entry.Height <= 0)
        {
            if (!string.IsNullOrWhiteSpace(fallbackText))
            {
                var shotDir = string.IsNullOrWhiteSpace(config.ScreenshotDirectory) ? Path.GetTempPath() : config.ScreenshotDirectory;
                var ocr = await OcrService.RecognizeScreenAsync(shotDir, 0, fallbackText, ct);
                var hit = ocr.Words.FirstOrDefault();
                if (hit is not null)
                {
                    DesktopInputService.ClickMouse(hit.CenterX, hit.CenterY, "left", false);
                    return new
                    {
                        ok = true,
                        backend = "visual_ocr_fallback",
                        fallback_used = true,
                        headless = false,
                        @ref = entry.Ref,
                        action = normAction,
                        coords = new { x = hit.CenterX, y = hit.CenterY },
                    };
                }
            }
            return new { ok = false, error_code = "NO_BOUNDS", @ref = entry.Ref, action = normAction, error = "Element has no bounds and OCR fallback did not match." };
        }

        switch (normAction)
        {
            case "hover":
                DesktopInputService.MoveMouse(entry.CenterX, entry.CenterY);
                break;
            case "double_click":
                DesktopInputService.ClickMouse(entry.CenterX, entry.CenterY, "left", true);
                break;
            case "right_click":
            case "show_menu":
                DesktopInputService.ClickMouse(entry.CenterX, entry.CenterY, "right", false);
                break;
            case "clear":
                DesktopInputService.ClickMouse(entry.CenterX, entry.CenterY, "left", false);
                DesktopInputService.PressHotkey("ctrl+a");
                DesktopInputService.PressKey(0x08);
                break;
            case "fill":
            case "set_value":
                DesktopInputService.ClickMouse(entry.CenterX, entry.CenterY, "left", false);
                DesktopInputService.PressHotkey("ctrl+a");
                DesktopInputService.TypeText(text ?? "");
                if (submit) DesktopInputService.PressKey(0x0D);
                break;
            case "type":
                DesktopInputService.ClickMouse(entry.CenterX, entry.CenterY, "left", false);
                DesktopInputService.TypeText(text ?? "");
                if (submit) DesktopInputService.PressKey(0x0D);
                break;
            default:
                DesktopInputService.ClickMouse(entry.CenterX, entry.CenterY, "left", false);
                break;
        }

        return new
        {
            ok = true,
            backend = "visual_physical_fallback",
            fallback_used = true,
            headless = false,
            @ref = entry.Ref,
            snapshot_id = entry.SnapshotId,
            action = normAction,
            coords = new { x = entry.CenterX, y = entry.CenterY },
        };
    }

    public static async Task<object> ExecuteBatchAsync(
        IReadOnlyList<System.Text.Json.JsonElement> steps,
        bool stopOnError,
        AgentGuiConfig config,
        CancellationToken ct)
    {
        if (steps.Count == 0)
            throw new ArgumentException("Batch requires at least one step.");
        if (steps.Count > MaxBatchSteps)
            throw new ArgumentException($"Batch exceeds maximum of {MaxBatchSteps} steps.");

        // v2.6 contract: pre-validate every step before executing step 0.
        for (int i = 0; i < steps.Count; i++)
        {
            var s = steps[i];
            var action = (s.TryGetProperty("action", out var aEl) ? aEl.GetString()
                : s.TryGetProperty("command", out var cEl) ? cEl.GetString() : "click") ?? "click";
            var norm = action.Trim().ToLowerInvariant().Replace("-", "_");
            if (!SupportedActions.Contains(norm))
                throw new ArgumentException($"Batch step {i} has unsupported action '{action}'.");
            var refId = (s.TryGetProperty("ref", out var rEl) ? rEl.GetString()
                : s.TryGetProperty("target", out var tEl) ? tEl.GetString() : "") ?? "";
            if (!ValidateRefSyntax(refId))
                throw new ArgumentException($"Batch step {i} has invalid ref '{refId}'.");
        }

        var results = new List<object>(steps.Count);
        bool allOk = true;
        for (int i = 0; i < steps.Count; i++)
        {
            ct.ThrowIfCancellationRequested();
            var s = steps[i];
            var action = s.TryGetProperty("action", out var aEl) ? aEl.GetString() ?? "click"
                : s.TryGetProperty("command", out var cEl) ? cEl.GetString() ?? "click" : "click";
            var refId = s.TryGetProperty("ref", out var rEl) ? rEl.GetString() ?? ""
                : s.TryGetProperty("target", out var tgtEl) ? tgtEl.GetString() ?? "" : "";
            var text = s.TryGetProperty("text", out var tEl) ? tEl.GetString() : null;
            var option = s.TryGetProperty("option", out var oEl) ? oEl.GetString() : null;
            bool submit = s.TryGetProperty("submit", out var subEl) && subEl.ValueKind == System.Text.Json.JsonValueKind.True;
            bool headed = s.TryGetProperty("headed", out var hEl) && hEl.ValueKind == System.Text.Json.JsonValueKind.True;
            bool fbVis = !s.TryGetProperty("fallback_to_visual", out var fvEl) || fvEl.ValueKind != System.Text.Json.JsonValueKind.False;
            var fbText = s.TryGetProperty("fallback_text", out var ftEl) ? ftEl.GetString() : null;

            object stepResult = await ActOnRefAsync(refId, action, text, option, submit, headed, fbVis, fbText, config, ct);
            bool stepOk = !System.Text.Json.JsonSerializer.Serialize(stepResult).Contains("\"ok\":false", StringComparison.OrdinalIgnoreCase);
            results.Add(new { step = i, result = stepResult });
            if (!stepOk)
            {
                allOk = false;
                if (stopOnError) break;
            }
        }

        return new
        {
            ok = allOk,
            steps_requested = steps.Count,
            steps_executed = results.Count,
            results,
        };
    }

    public static async Task<object> WaitForConditionAsync(
        string condition,
        string? query,
        string? role,
        string? windowTitle,
        double timeoutSeconds,
        CancellationToken ct)
    {
        var cond = (condition ?? "exists").Trim().ToLowerInvariant();
        if (cond is not ("exists" or "gone" or "enabled" or "window_open" or "window_closed"))
            throw new ArgumentException($"Unsupported wait condition '{condition}'.");

        var timeout = TimeSpan.FromSeconds(Math.Clamp(timeoutSeconds, 0.1, 30.0));
        var sw = Stopwatch.StartNew();

        while (true)
        {
            ct.ThrowIfCancellationRequested();
            if (cond is "window_open" or "window_closed")
            {
                var target = (windowTitle ?? query ?? "").Trim();
                var wins = WindowManagementService.ListVisibleWindows(limit: 80)
                    .Where(w => string.IsNullOrEmpty(target) || w.Title.Contains(target, StringComparison.OrdinalIgnoreCase))
                    .ToList();
                bool satisfied = cond == "window_open" ? wins.Count > 0 : wins.Count == 0;
                if (satisfied)
                {
                    return new { ok = true, condition = cond, elapsed_seconds = Math.Round(sw.Elapsed.TotalSeconds, 3), match_count = wins.Count };
                }
            }
            else
            {
                FindElements(query, role, windowTitle, maxResults: 10);
                List<HybridA11yRefEntry> matches;
                lock (StoreLock)
                {
                    string q = (query ?? "").Trim();
                    string r = (role ?? "").Trim();
                    matches = _refs.Values
                        .Where(e =>
                            (string.IsNullOrEmpty(q) || e.Name.Contains(q, StringComparison.OrdinalIgnoreCase) || e.AutomationId.Contains(q, StringComparison.OrdinalIgnoreCase)) &&
                            (string.IsNullOrEmpty(r) || e.Role.Contains(r, StringComparison.OrdinalIgnoreCase)))
                        .ToList();
                }

                if (cond == "exists" && matches.Count > 0)
                    return new { ok = true, condition = cond, elapsed_seconds = Math.Round(sw.Elapsed.TotalSeconds, 3), @ref = matches[0].Ref, name = matches[0].Name };
                if (cond == "gone" && matches.Count == 0)
                    return new { ok = true, condition = cond, elapsed_seconds = Math.Round(sw.Elapsed.TotalSeconds, 3) };
                if (cond == "enabled" && matches.Any(m => m.Enabled))
                {
                    var m = matches.First(x => x.Enabled);
                    return new { ok = true, condition = cond, elapsed_seconds = Math.Round(sw.Elapsed.TotalSeconds, 3), @ref = m.Ref, name = m.Name };
                }
            }

            if (sw.Elapsed >= timeout)
            {
                return new
                {
                    ok = false,
                    error_code = "WAIT_TIMEOUT",
                    condition = cond,
                    elapsed_seconds = Math.Round(sw.Elapsed.TotalSeconds, 3),
                    error = $"Condition '{cond}' not met within {timeout.TotalSeconds:0.#}s.",
                };
            }
            await Task.Delay(250, ct);
        }
    }

    private static object? TryExecuteHeadlessUia(HybridA11yRefEntry entry, string action, string text, string option, bool submit)
    {
        var el = entry.LiveElement!;
        try
        {
            switch (action)
            {
                case "click":
                    if (el.TryGetCurrentPattern(InvokePattern.Pattern, out var invObj) && invObj is InvokePattern inv)
                    {
                        inv.Invoke();
                        return new { ok = true, backend = "native_windows_uia", headless = true, fallback_used = false, @ref = entry.Ref, action };
                    }
                    if (el.TryGetCurrentPattern(SelectionItemPattern.Pattern, out var selObj) && selObj is SelectionItemPattern sel)
                    {
                        sel.Select();
                        return new { ok = true, backend = "native_windows_uia", headless = true, fallback_used = false, @ref = entry.Ref, action };
                    }
                    return null;

                case "check":
                case "uncheck":
                case "toggle":
                    if (el.TryGetCurrentPattern(TogglePattern.Pattern, out var togObj) && togObj is TogglePattern tog)
                    {
                        var cur = tog.Current.ToggleState;
                        if (action == "check" && cur == ToggleState.On)
                            return new { ok = true, backend = "native_windows_uia", headless = true, noop = true, @ref = entry.Ref, action, state = "checked" };
                        if (action == "uncheck" && cur == ToggleState.Off)
                            return new { ok = true, backend = "native_windows_uia", headless = true, noop = true, @ref = entry.Ref, action, state = "unchecked" };
                        tog.Toggle();
                        var after = tog.Current.ToggleState;
                        return new { ok = true, backend = "native_windows_uia", headless = true, fallback_used = false, @ref = entry.Ref, action, state = after.ToString().ToLowerInvariant() };
                    }
                    return null;

                case "expand":
                case "collapse":
                    if (el.TryGetCurrentPattern(ExpandCollapsePattern.Pattern, out var expObj) && expObj is ExpandCollapsePattern exp)
                    {
                        var state = exp.Current.ExpandCollapseState;
                        if (action == "expand")
                        {
                            if (state == ExpandCollapseState.Expanded)
                                return new { ok = true, backend = "native_windows_uia", headless = true, noop = true, @ref = entry.Ref, action, state = "expanded" };
                            exp.Expand();
                            return new { ok = true, backend = "native_windows_uia", headless = true, fallback_used = false, @ref = entry.Ref, action, state = "expanded" };
                        }
                        if (state == ExpandCollapseState.Collapsed)
                            return new { ok = true, backend = "native_windows_uia", headless = true, noop = true, @ref = entry.Ref, action, state = "collapsed" };
                        exp.Collapse();
                        return new { ok = true, backend = "native_windows_uia", headless = true, fallback_used = false, @ref = entry.Ref, action, state = "collapsed" };
                    }
                    return null;

                case "select":
                    if (!string.IsNullOrWhiteSpace(option))
                    {
                        if (el.TryGetCurrentPattern(ExpandCollapsePattern.Pattern, out var ecObj) && ecObj is ExpandCollapsePattern ec)
                        {
                            try { ec.Expand(); } catch { }
                        }
                        var child = el.FindFirst(TreeScope.Subtree, new PropertyCondition(AutomationElement.NameProperty, option, PropertyConditionFlags.IgnoreCase));
                        if (child is not null && child.TryGetCurrentPattern(SelectionItemPattern.Pattern, out var cSelObj) && cSelObj is SelectionItemPattern cSel)
                        {
                            cSel.Select();
                            return new { ok = true, backend = "native_windows_uia", headless = true, fallback_used = false, @ref = entry.Ref, action, option };
                        }
                    }
                    if (el.TryGetCurrentPattern(SelectionItemPattern.Pattern, out var sObj) && sObj is SelectionItemPattern sPat)
                    {
                        sPat.Select();
                        return new { ok = true, backend = "native_windows_uia", headless = true, fallback_used = false, @ref = entry.Ref, action };
                    }
                    return null;

                case "clear":
                case "fill":
                case "set_value":
                    if (el.TryGetCurrentPattern(ValuePattern.Pattern, out var vpObj) && vpObj is ValuePattern vp && !vp.Current.IsReadOnly)
                    {
                        vp.SetValue(action == "clear" ? "" : text);
                        if (submit && action is "fill" or "set_value")
                        {
                            try { el.SetFocus(); } catch { }
                            DesktopInputService.PressKey(0x0D);
                        }
                        return new { ok = true, backend = "native_windows_uia", headless = true, fallback_used = false, @ref = entry.Ref, action };
                    }
                    return null;

                case "scroll_to":
                    if (el.TryGetCurrentPattern(ScrollItemPattern.Pattern, out var scObj) && scObj is ScrollItemPattern sc)
                    {
                        sc.ScrollIntoView();
                        return new { ok = true, backend = "native_windows_uia", headless = true, fallback_used = false, @ref = entry.Ref, action };
                    }
                    return null;

                case "focus":
                    el.SetFocus();
                    return new { ok = true, backend = "native_windows_uia", headless = true, fallback_used = false, @ref = entry.Ref, action };

                default:
                    return null;
            }
        }
        catch
        {
            return null;
        }
    }

    private static bool TryResolveRef(string refId, out HybridA11yRefEntry? entry)
    {
        lock (StoreLock)
        {
            entry = null;
            if (string.IsNullOrEmpty(_activeSnapshotId)) return false;
            if ((DateTimeOffset.UtcNow - _snapshotCreatedAt) > SnapshotTtl) return false;
            if (_refs.TryGetValue(refId, out var found) && found.SnapshotId == _activeSnapshotId)
            {
                entry = found;
                return true;
            }
            // v2.6 short-ref alias: @e1 -> @{activeSnapshotId}:e1
            if (refId.StartsWith("@", StringComparison.Ordinal) && !refId.Contains(':'))
            {
                var expanded = $"@{_activeSnapshotId}:{refId[1..]}";
                if (_refs.TryGetValue(expanded, out var shortFound) && shortFound.SnapshotId == _activeSnapshotId)
                {
                    entry = shortFound;
                    return true;
                }
            }
            return false;
        }
    }

    private static object StaleRefResponse(string refId, string? action = null) => new
    {
        ok = false,
        error_code = "STALE_REF",
        @ref = refId,
        action,
        error = $"Element ref '{refId}' is stale or not in the active snapshot.",
        retry_hint = "Call desktop_a11y_snapshot or desktop_a11y_find to obtain fresh refs.",
        error_detail = new
        {
            code = "STALE_REF",
            message = $"Element ref '{refId}' is stale or not in the active snapshot.",
            suggestion = "Call desktop_a11y_snapshot or desktop_a11y_find to obtain fresh refs.",
            recovery = new { strategy = "refresh_snapshot_then_retry_original", retryable = true },
            disposition = new { delivery = "not_delivered", retry = "safe" },
        },
    };

    private static object FormatPublicEntry(HybridA11yRefEntry e, bool compact)
    {
        if (compact)
        {
            return new
            {
                @ref = e.Ref,
                role = e.Role,
                name = e.Name,
                value = e.IsPassword ? (string.IsNullOrEmpty(e.Value) ? "" : "[REDACTED]") : e.Value,
                redacted = e.IsPassword,
                enabled = e.Enabled,
                @checked = e.Checked,
                expanded = e.Expanded,
                bounds = new { x = e.X, y = e.Y, width = e.Width, height = e.Height },
            };
        }
        return new
        {
            @ref = e.Ref,
            snapshot_id = e.SnapshotId,
            role = e.Role,
            name = e.Name,
            automation_id = e.AutomationId,
            window_title = e.WindowTitle,
            patterns = e.Patterns,
            value = e.IsPassword ? (string.IsNullOrEmpty(e.Value) ? "" : "[REDACTED]") : e.Value,
            redacted = e.IsPassword,
            enabled = e.Enabled,
            @checked = e.Checked,
            expanded = e.Expanded,
            bounds = new { x = e.X, y = e.Y, width = e.Width, height = e.Height },
        };
    }

    private static (string Patterns, string Value, bool? Checked, bool? Expanded) InspectElementState(
        AutomationElement el,
        bool isPassword)
    {
        var tags = new List<string>(6);
        string val = "";
        bool? isChecked = null;
        bool? isExpanded = null;
        try
        {
            if (el.TryGetCurrentPattern(InvokePattern.Pattern, out _)) tags.Add("Invoke");
            if (el.TryGetCurrentPattern(TogglePattern.Pattern, out var tObj) && tObj is TogglePattern tp)
            {
                tags.Add("Toggle");
                isChecked = tp.Current.ToggleState == ToggleState.On;
            }
            if (el.TryGetCurrentPattern(SelectionItemPattern.Pattern, out _)) tags.Add("Select");
            if (el.TryGetCurrentPattern(ExpandCollapsePattern.Pattern, out var eObj) && eObj is ExpandCollapsePattern ep)
            {
                tags.Add("Expand");
                isExpanded = ep.Current.ExpandCollapseState == ExpandCollapseState.Expanded;
            }
            if (el.TryGetCurrentPattern(ScrollItemPattern.Pattern, out _)) tags.Add("Scroll");
            if (el.TryGetCurrentPattern(ValuePattern.Pattern, out var vpObj) && vpObj is ValuePattern vp)
            {
                tags.Add("Value");
                var raw = vp.Current.Value ?? "";
                val = isPassword
                    ? (string.IsNullOrEmpty(raw) ? "" : "[REDACTED]")
                    : (raw.Length > 120 ? raw[..120] + "..." : raw);
            }
        }
        catch { }
        return (string.Join(",", tags), val, isChecked, isExpanded);
    }
}
