using System;
using System.Collections.Generic;
using System.Linq;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using AIBridgeClient.Models;

namespace AIBridgeClient.Services;

/// <summary>
/// Standalone on-device AI Agent runtime for OpenAI, Anthropic, Gemini, Ollama, and Custom
/// OpenAI-compatible APIs with per-agent system prompt, instruction profile, and device function
/// allow/disallow enforcement.
/// </summary>
public sealed class DeviceAgentRuntimeService : IDisposable
{
    private readonly NativeToolExecutor _executor;
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(60) };

    public sealed record ToolDescriptor(
        string Name,
        string Category,
        bool IsMutating,
        string Description,
        object Parameters);

    private static readonly List<ToolDescriptor> Catalog = new()
    {
        new("get_system_info", "sys", false, "Get Windows OS edition, build, hostname, CPU count, and desktop pipe availability.", new { type = "object", properties = new { } }),
        new("get_resource_usage", "sys", false, "Get live CPU, memory, disk, and network utilization metrics.", new { type = "object", properties = new { } }),
        new("list_processes", "sys", false, "List top running processes sorted by memory usage.", new { type = "object", properties = new { limit = new { type = "integer" } } }),
        new("get_service_status", "sys", false, "Query the state of a Windows service by name.", new { type = "object", properties = new { service = new { type = "string" } }, required = new[] { "service" } }),
        new("get_monitors", "sys", false, "List connected monitors and pixel bounds.", new { type = "object", properties = new { } }),
        new("get_screen_size", "sys", false, "Get virtual desktop bounds and primary monitor dimensions.", new { type = "object", properties = new { } }),

        new("capture_desktop_screenshot", "capture", false, "Capture a PNG screenshot of the primary display or virtual desktop.", new { type = "object", properties = new { full_desktop = new { type = "boolean" }, monitor_index = new { type = "integer" } } }),
        new("desktop_get_window_screenshot", "capture", false, "Capture a screenshot of a specific window by title substring.", new { type = "object", properties = new { title = new { type = "string" } } }),
        new("set_of_marks_screenshot", "capture", false, "Capture screenshot overlaid with numbered [1]..[N] interactive UI element badges.", new { type = "object", properties = new { window = new { type = "string" }, max_marks = new { type = "integer" } } }),

        new("list_desktop_windows", "window", false, "List all visible top-level windows with titles, process names, PIDs, and coordinates.", new { type = "object", properties = new { query = new { type = "string" }, limit = new { type = "integer" } } }),
        new("get_active_window", "window", false, "Get details of the currently focused foreground window.", new { type = "object", properties = new { } }),
        new("focus_window", "window", true, "Bring a window to the foreground by title substring.", new { type = "object", properties = new { title = new { type = "string" } }, required = new[] { "title" } }),
        new("maximize_window", "window", true, "Maximize a window by title substring.", new { type = "object", properties = new { title = new { type = "string" } }, required = new[] { "title" } }),
        new("minimize_window", "window", true, "Minimize a window by title substring.", new { type = "object", properties = new { title = new { type = "string" } }, required = new[] { "title" } }),
        new("restore_window", "window", true, "Restore a minimized or maximized window.", new { type = "object", properties = new { title = new { type = "string" } }, required = new[] { "title" } }),
        new("close_window", "window", true, "Close a window cleanly by sending WM_CLOSE.", new { type = "object", properties = new { title = new { type = "string" } }, required = new[] { "title" } }),
        new("move_resize_window", "window", true, "Move and/or resize a window by title substring.", new { type = "object", properties = new { title = new { type = "string" }, x = new { type = "integer" }, y = new { type = "integer" }, width = new { type = "integer" }, height = new { type = "integer" } }, required = new[] { "title" } }),

        new("list_ui_elements", "ui", false, "Inspect interactive UI Automation elements (buttons, inputs, menus) optionally scoped to a window.", new { type = "object", properties = new { query = new { type = "string" }, window = new { type = "string" }, control_type = new { type = "string" }, limit = new { type = "integer" } } }),
        new("click_element", "ui", true, "Click a UI element by name, AutomationId, or Set-of-Marks mark_id.", new { type = "object", properties = new { name = new { type = "string" }, mark_id = new { type = "string" }, window = new { type = "string" }, button = new { type = "string" }, double_click = new { type = "boolean" } } }),
        new("invoke_element", "ui", true, "Invoke, toggle, select, or expand a native UI control by name.", new { type = "object", properties = new { name = new { type = "string" }, window = new { type = "string" } }, required = new[] { "name" } }),
        new("set_element_value", "ui", true, "Set text inside an input field by name or AutomationId, with optional Enter submit.", new { type = "object", properties = new { name = new { type = "string" }, value = new { type = "string" }, submit = new { type = "boolean" }, window = new { type = "string" } }, required = new[] { "name", "value" } }),
        new("mouse_click", "ui", true, "Click the mouse at exact screen coordinates (x, y).", new { type = "object", properties = new { x = new { type = "integer" }, y = new { type = "integer" }, button = new { type = "string" }, clicks = new { type = "integer" } }, required = new[] { "x", "y" } }),
        new("mouse_scroll", "ui", true, "Scroll mouse wheel by delta (positive up, negative down), optionally at (x, y).", new { type = "object", properties = new { delta = new { type = "integer" }, x = new { type = "integer" }, y = new { type = "integer" }, horizontal = new { type = "boolean" } }, required = new[] { "delta" } }),
        new("keyboard_type", "ui", true, "Type text at the current keyboard focus.", new { type = "object", properties = new { text = new { type = "string" } }, required = new[] { "text" } }),
        new("press_hotkey", "ui", true, "Press a keyboard shortcut combo such as ctrl+c, alt+tab, or win+d.", new { type = "object", properties = new { combo = new { type = "string" } }, required = new[] { "combo" } }),

        new("get_clipboard", "clipboard", false, "Read current text from the Windows clipboard.", new { type = "object", properties = new { } }),
        new("set_clipboard", "clipboard", true, "Write text to the Windows clipboard.", new { type = "object", properties = new { text = new { type = "string" } }, required = new[] { "text" } }),

        new("desktop_open_application", "app", true, "Launch a Windows application by name, executable, or path.", new { type = "object", properties = new { app = new { type = "string" }, arguments = new { type = "string" } }, required = new[] { "app" } }),

        new("list_workspace", "workspace", false, "List files and folders inside the confined AI Bridge workspace.", new { type = "object", properties = new { path = new { type = "string" } } }),
        new("read_workspace_file", "workspace", false, "Read a file from the confined workspace.", new { type = "object", properties = new { path = new { type = "string" } }, required = new[] { "path" } }),
        new("create_workspace_file", "workspace", true, "Create a file inside the confined workspace.", new { type = "object", properties = new { path = new { type = "string" }, content = new { type = "string" } }, required = new[] { "path", "content" } }),
        new("edit_workspace_file", "workspace", true, "Edit a file inside the confined workspace.", new { type = "object", properties = new { path = new { type = "string" }, old_text = new { type = "string" }, new_text = new { type = "string" } }, required = new[] { "path" } }),

        new("list_drives", "fullpc", false, "List all mounted drives and free storage on this Windows PC (requires Full PC Access).", new { type = "object", properties = new { } }),
        new("list_directory", "fullpc", false, "Browse any directory path on the PC (requires Full PC Access).", new { type = "object", properties = new { path = new { type = "string" } } }),
        new("read_pc_file", "fullpc", false, "Read any accessible file on the PC (requires Full PC Access).", new { type = "object", properties = new { path = new { type = "string" } }, required = new[] { "path" } }),
        new("write_pc_file", "fullpc", true, "Write or update any file on the PC (requires Full PC Access).", new { type = "object", properties = new { path = new { type = "string" }, content = new { type = "string" } }, required = new[] { "path", "content" } }),

        new("run_shell", "shell", true, "Run a cmd.exe command (requires Shell permission).", new { type = "object", properties = new { command = new { type = "string" }, working_directory = new { type = "string" } }, required = new[] { "command" } }),
        new("run_powershell", "shell", true, "Run a PowerShell command anywhere on the PC (requires Full PC Access & Shell permission).", new { type = "object", properties = new { command = new { type = "string" }, working_directory = new { type = "string" } }, required = new[] { "command" } }),

        new("kill_process", "kill", true, "Terminate a running process by PID or name (requires Process Termination & Full PC Access).", new { type = "object", properties = new { pid = new { type = "integer" }, name = new { type = "string" } } }),
    };

    public DeviceAgentRuntimeService(NativeToolExecutor executor)
    {
        _executor = executor;
    }

    public static string GetDefaultBaseUrl(string provider) =>
        provider.Trim().ToLowerInvariant() switch
        {
            "anthropic" => "https://api.anthropic.com/v1",
            "gemini" => "https://generativelanguage.googleapis.com/v1beta/openai",
            "ollama" => "http://localhost:11434/v1",
            _ => "https://api.openai.com/v1",
        };

    public static string GetDefaultModel(string provider) =>
        provider.Trim().ToLowerInvariant() switch
        {
            "anthropic" => "claude-3-5-sonnet-latest",
            "gemini" => "gemini-2.5-flash",
            "ollama" => "llama3.2",
            _ => "gpt-4o",
        };

    public static bool IsToolMutating(string toolName)
    {
        var canonical = toolName.Trim().ToLowerInvariant();
        var item = Catalog.FirstOrDefault(t => string.Equals(t.Name, canonical, StringComparison.OrdinalIgnoreCase));
        if (item is not null) return item.IsMutating;
        return canonical is "delete_workspace_file" or "move_workspace_file" or "mouse_move" or "mouse_drag"
            or "key_press" or "keyboard_press" or "focus_element" or "launch_approved_application" or "open_application";
    }

    public static bool IsToolAllowed(string toolName, AgentGuiConfig config, out string denyReason)
    {
        var fn = config.DeviceAgent?.Functions ?? new AgentFunctionPermissions();
        var normalized = toolName.Trim().ToLowerInvariant();
        var dot = normalized.LastIndexOf('.');
        if (dot >= 0) normalized = normalized[(dot + 1)..];

        if (fn.DisabledTools.Any(d => string.Equals(d.Trim(), normalized, StringComparison.OrdinalIgnoreCase)))
        {
            denyReason = $"Function '{normalized}' is explicitly listed in this agent's Disallowed Tools list.";
            return false;
        }

        string category = normalized switch
        {
            "get_system_info" or "system_info" or "windows_system_info" or "get_resource_usage" or "get_memory_usage"
                or "get_disk_usage" or "get_network_info" or "get_service_status" or "windows_service_status"
                or "get_recent_logs" or "windows_event_logs" or "list_processes" or "get_monitors"
                or "desktop_get_monitors" or "get_screen_size" or "get_desktop_status" or "desktop_get_status" => "sys",

            "capture_desktop_screenshot" or "take_screenshot" or "capture_desktop_frame" or "desktop_get_screenshot"
                or "desktop_get_window_screenshot" or "set_of_marks_screenshot" => "capture",

            "list_desktop_windows" or "desktop_get_windows" or "get_active_window" or "focus_window"
                or "maximize_window" or "minimize_window" or "restore_window" or "close_window"
                or "move_resize_window" => "window",

            "get_ui_tree" or "find_elements" or "list_ui_elements" or "desktop_get_ui_elements"
                or "click_element" or "click_text" or "invoke_element" or "set_element_value"
                or "type_into_element" or "focus_element" or "perform_approved_ui_action"
                or "mouse_move" or "desktop_mouse_move" or "mouse_click" or "desktop_click"
                or "mouse_scroll" or "desktop_scroll" or "mouse_drag" or "keyboard_type"
                or "desktop_keyboard" or "key_press" or "keyboard_press" or "press_hotkey" => "ui",

            "get_clipboard" or "desktop_clipboard_get" or "set_clipboard" or "desktop_clipboard_set" => "clipboard",

            "launch_approved_application" or "desktop_open_application" or "open_application" => "app",

            "list_workspace" or "read_workspace_file" or "read_file" or "search_workspace"
                or "create_workspace_file" or "write_file" or "edit_workspace_file"
                or "move_workspace_file" or "delete_workspace_file" => "workspace",

            "list_drives" or "get_drives" or "list_directory" or "browse_pc" or "read_pc_file" or "write_pc_file" => "fullpc",

            "run_shell" or "desktop_execute_command" or "run_powershell" or "powershell" => "shell",

            "kill_process" or "stop_process" => "kill",

            _ => "other",
        };

        bool allowed = category switch
        {
            "sys" => fn.AllowSystemHealthTools,
            "capture" => fn.AllowScreenCapture,
            "window" => fn.AllowWindowManagement,
            "ui" => fn.AllowUiInputAutomation,
            "clipboard" => fn.AllowClipboard,
            "app" => fn.AllowAppLaunching,
            "workspace" => fn.AllowWorkspaceFiles,
            "fullpc" => fn.AllowFullPcFilesAndDrives,
            "shell" => fn.AllowShellAndPowerShell,
            "kill" => fn.AllowProcessTermination,
            _ => true,
        };

        if (!allowed)
        {
            denyReason = $"Function '{normalized}' (category '{category}') is disallowed in Agent Settings → Device Functions.";
            return false;
        }

        denyReason = "";
        return true;
    }

    public static List<ToolDescriptor> GetEnabledTools(AgentGuiConfig config) =>
        Catalog.Where(t => IsToolAllowed(t.Name, config, out _)).ToList();

    public static string BuildEffectiveSystemPrompt(AgentGuiConfig config)
    {
        var agent = config.DeviceAgent ?? new DeviceAgentSettings();
        var enabled = GetEnabledTools(config).Select(t => t.Name).ToList();
        var disallowed = Catalog.Select(t => t.Name).Except(enabled, StringComparer.OrdinalIgnoreCase).ToList();

        var sb = new StringBuilder();
        sb.AppendLine($"Agent Name: {agent.AgentName}");
        if (!string.IsNullOrWhiteSpace(agent.InstructionName))
        {
            sb.AppendLine($"Instruction Profile: {agent.InstructionName}");
        }
        sb.AppendLine($"Target Device: {config.DeviceId} (Windows host: {Environment.MachineName}, user: {Environment.UserName})");
        sb.AppendLine($"Policy State: Interactive UI={(config.Policy.AllowDesktopControl ? "ON" : "OFF")}, Full PC Access={(config.Policy.AllowFullPcAccess ? "ALLOWED" : "OFF")}.");
        sb.AppendLine();
        if (!string.IsNullOrWhiteSpace(agent.SystemPrompt))
        {
            sb.AppendLine("System Instructions:");
            sb.AppendLine(agent.SystemPrompt.Trim());
            sb.AppendLine();
        }
        sb.AppendLine("Allowed Device Functions: " + (enabled.Count > 0 ? string.Join(", ", enabled) : "NONE"));
        if (disallowed.Count > 0)
        {
            sb.AppendLine("Disallowed Device Functions (DO NOT invoke): " + string.Join(", ", disallowed));
        }
        return sb.ToString();
    }

    public async Task<(bool Ok, string Detail)> TestConnectionAsync(
        DeviceAgentSettings settings,
        string apiKey,
        CancellationToken ct = default)
    {
        var provider = (settings.Provider ?? "openai").Trim().ToLowerInvariant();
        var baseUrl = string.IsNullOrWhiteSpace(settings.BaseUrl)
            ? GetDefaultBaseUrl(provider)
            : settings.BaseUrl.Trim().TrimEnd('/');

        if (provider != "ollama" && provider != "custom" && string.IsNullOrWhiteSpace(apiKey))
        {
            return (false, $"API Key is required for provider '{provider}'.");
        }

        try
        {
            if (provider == "anthropic")
            {
                using var req = new HttpRequestMessage(HttpMethod.Get, $"{baseUrl}/models");
                req.Headers.Add("x-api-key", apiKey.Trim());
                req.Headers.Add("anthropic-version", "2023-06-01");
                using var resp = await _http.SendAsync(req, ct);
                var body = await resp.Content.ReadAsStringAsync(ct);
                return resp.IsSuccessStatusCode
                    ? (true, $"Connected to Anthropic ({baseUrl})")
                    : (false, $"Anthropic HTTP {(int)resp.StatusCode}: {Truncate(body, 180)}");
            }

            using var oaiReq = new HttpRequestMessage(HttpMethod.Get, $"{baseUrl}/models");
            if (!string.IsNullOrWhiteSpace(apiKey))
            {
                oaiReq.Headers.TryAddWithoutValidation("Authorization", $"Bearer {apiKey.Trim()}");
            }
            using var oaiResp = await _http.SendAsync(oaiReq, ct);
            var oaiBody = await oaiResp.Content.ReadAsStringAsync(ct);
            return oaiResp.IsSuccessStatusCode
                ? (true, $"Connected to {provider} ({baseUrl})")
                : (false, $"{provider} HTTP {(int)oaiResp.StatusCode}: {Truncate(oaiBody, 180)}");
        }
        catch (Exception ex)
        {
            return (false, ex.Message);
        }
    }

    public async Task<(bool Ok, string ReplyText, string ToolsSummary, string? ScreenshotBase64, string Error)> RunChatAsync(
        AgentGuiConfig config,
        string apiKey,
        IEnumerable<(string Role, string Content)> history,
        Func<string, string, Task<bool>>? confirmMutatingToolAsync = null,
        CancellationToken ct = default)
    {
        var agent = config.DeviceAgent ?? new DeviceAgentSettings();
        var provider = (agent.Provider ?? "openai").Trim().ToLowerInvariant();
        var baseUrl = string.IsNullOrWhiteSpace(agent.BaseUrl)
            ? GetDefaultBaseUrl(provider)
            : agent.BaseUrl.Trim().TrimEnd('/');
        var model = string.IsNullOrWhiteSpace(agent.Model)
            ? GetDefaultModel(provider)
            : agent.Model.Trim();

        if (provider != "ollama" && provider != "custom" && string.IsNullOrWhiteSpace(apiKey))
        {
            return (false, "", "", null, $"Direct AI API key is not configured for provider '{provider}'.");
        }

        return provider == "anthropic"
            ? await RunAnthropicLoopAsync(config, agent, baseUrl, model, apiKey, history, confirmMutatingToolAsync, ct)
            : await RunOpenAiCompatLoopAsync(config, agent, baseUrl, model, apiKey, history, confirmMutatingToolAsync, ct);
    }

    private async Task<(bool Ok, string ReplyText, string ToolsSummary, string? ScreenshotBase64, string Error)> RunOpenAiCompatLoopAsync(
        AgentGuiConfig config,
        DeviceAgentSettings agent,
        string baseUrl,
        string model,
        string apiKey,
        IEnumerable<(string Role, string Content)> history,
        Func<string, string, Task<bool>>? confirmMutatingToolAsync,
        CancellationToken ct)
    {
        var messages = new List<object>
        {
            new { role = "system", content = BuildEffectiveSystemPrompt(config) },
        };
        foreach (var (r, c) in history.TakeLast(16))
        {
            messages.Add(new { role = r.Equals("user", StringComparison.OrdinalIgnoreCase) ? "user" : "assistant", content = c });
        }

        var enabledTools = GetEnabledTools(config);
        var toolsPayload = enabledTools.Select(t => new
        {
            type = "function",
            function = new
            {
                name = t.Name,
                description = t.Description,
                parameters = t.Parameters,
            },
        }).ToList();

        var executedTools = new List<string>();
        string? latestScreenshot = null;
        int maxSteps = Math.Clamp(agent.MaxToolSteps, 1, 20);

        for (int step = 0; step < maxSteps; step++)
        {
            var reqObj = new Dictionary<string, object?>
            {
                ["model"] = model,
                ["messages"] = messages,
                ["temperature"] = Math.Clamp(agent.Temperature, 0.0, 2.0),
            };
            if (toolsPayload.Count > 0)
            {
                reqObj["tools"] = toolsPayload;
                reqObj["tool_choice"] = "auto";
            }

            using var req = new HttpRequestMessage(HttpMethod.Post, $"{baseUrl}/chat/completions");
            if (!string.IsNullOrWhiteSpace(apiKey))
            {
                req.Headers.TryAddWithoutValidation("Authorization", $"Bearer {apiKey.Trim()}");
            }
            req.Content = new StringContent(JsonSerializer.Serialize(reqObj), Encoding.UTF8, "application/json");

            using var resp = await _http.SendAsync(req, ct);
            var body = await resp.Content.ReadAsStringAsync(ct);
            if (!resp.IsSuccessStatusCode)
            {
                return (false, "", "", latestScreenshot, $"HTTP {(int)resp.StatusCode}: {Truncate(body, 280)}");
            }

            using var doc = JsonDocument.Parse(body);
            if (!doc.RootElement.TryGetProperty("choices", out var choices) || choices.GetArrayLength() == 0)
            {
                return (false, "", "", latestScreenshot, "AI response contained no choices.");
            }

            var msgEl = choices[0].GetProperty("message");
            string content = msgEl.TryGetProperty("content", out var cEl) && cEl.ValueKind == JsonValueKind.String
                ? cEl.GetString() ?? ""
                : "";

            if (!msgEl.TryGetProperty("tool_calls", out var callsEl) ||
                callsEl.ValueKind != JsonValueKind.Array ||
                callsEl.GetArrayLength() == 0)
            {
                var summary = executedTools.Count > 0
                    ? $"{agent.AgentName} ({model}) · Tools: {string.Join(", ", executedTools)}"
                    : $"{agent.AgentName} ({model})";
                return (true, string.IsNullOrWhiteSpace(content) ? "Completed." : content, summary, latestScreenshot, "");
            }

            // Preserve assistant tool_calls turn
            var rawAssistantCalls = new List<object>();
            var callItems = new List<(string Id, string Name, string ArgsJson)>();
            foreach (var tc in callsEl.EnumerateArray())
            {
                var id = tc.TryGetProperty("id", out var idEl) ? idEl.GetString() ?? Guid.NewGuid().ToString("N")[..8] : Guid.NewGuid().ToString("N")[..8];
                var fnEl = tc.GetProperty("function");
                var name = fnEl.GetProperty("name").GetString() ?? "";
                var argsJson = fnEl.TryGetProperty("arguments", out var aEl) ? aEl.GetString() ?? "{}" : "{}";
                callItems.Add((id, name, argsJson));
                rawAssistantCalls.Add(new
                {
                    id,
                    type = "function",
                    function = new { name, arguments = argsJson },
                });
            }

            messages.Add(new { role = "assistant", content = string.IsNullOrEmpty(content) ? (string?)null : content, tool_calls = rawAssistantCalls });

            foreach (var (id, name, argsJson) in callItems)
            {
                var toolResult = await ExecuteGuardedToolAsync(name, argsJson, config, agent, confirmMutatingToolAsync, ct);
                executedTools.Add(name);
                if (toolResult.ScreenshotBase64 is not null)
                {
                    latestScreenshot = toolResult.ScreenshotBase64;
                }
                messages.Add(new
                {
                    role = "tool",
                    tool_call_id = id,
                    content = StripBase64ForLlm(toolResult.ResultJson),
                });
            }
        }

        return (true, "Reached configured max tool steps.", $"{agent.AgentName} ({model}) · Tools: {string.Join(", ", executedTools)}", latestScreenshot, "");
    }

    private async Task<(bool Ok, string ReplyText, string ToolsSummary, string? ScreenshotBase64, string Error)> RunAnthropicLoopAsync(
        AgentGuiConfig config,
        DeviceAgentSettings agent,
        string baseUrl,
        string model,
        string apiKey,
        IEnumerable<(string Role, string Content)> history,
        Func<string, string, Task<bool>>? confirmMutatingToolAsync,
        CancellationToken ct)
    {
        var messages = new List<object>();
        foreach (var (r, c) in history.TakeLast(16))
        {
            messages.Add(new { role = r.Equals("user", StringComparison.OrdinalIgnoreCase) ? "user" : "assistant", content = c });
        }

        var enabledTools = GetEnabledTools(config);
        var toolsPayload = enabledTools.Select(t => new
        {
            name = t.Name,
            description = t.Description,
            input_schema = t.Parameters,
        }).ToList();

        var executedTools = new List<string>();
        string? latestScreenshot = null;
        int maxSteps = Math.Clamp(agent.MaxToolSteps, 1, 20);
        var sysPrompt = BuildEffectiveSystemPrompt(config);

        for (int step = 0; step < maxSteps; step++)
        {
            var reqObj = new Dictionary<string, object?>
            {
                ["model"] = model,
                ["system"] = sysPrompt,
                ["max_tokens"] = 2048,
                ["temperature"] = Math.Clamp(agent.Temperature, 0.0, 1.0),
                ["messages"] = messages,
            };
            if (toolsPayload.Count > 0)
            {
                reqObj["tools"] = toolsPayload;
            }

            using var req = new HttpRequestMessage(HttpMethod.Post, $"{baseUrl}/messages");
            req.Headers.Add("x-api-key", apiKey.Trim());
            req.Headers.Add("anthropic-version", "2023-06-01");
            req.Content = new StringContent(JsonSerializer.Serialize(reqObj), Encoding.UTF8, "application/json");

            using var resp = await _http.SendAsync(req, ct);
            var body = await resp.Content.ReadAsStringAsync(ct);
            if (!resp.IsSuccessStatusCode)
            {
                return (false, "", "", latestScreenshot, $"Anthropic HTTP {(int)resp.StatusCode}: {Truncate(body, 280)}");
            }

            using var doc = JsonDocument.Parse(body);
            var contentArr = doc.RootElement.GetProperty("content");
            var assistantBlocks = new List<object>();
            var textParts = new List<string>();
            var toolUses = new List<(string Id, string Name, string ArgsJson)>();

            foreach (var block in contentArr.EnumerateArray())
            {
                var type = block.GetProperty("type").GetString();
                if (type == "text")
                {
                    var txt = block.GetProperty("text").GetString() ?? "";
                    textParts.Add(txt);
                    assistantBlocks.Add(new { type = "text", text = txt });
                }
                else if (type == "tool_use")
                {
                    var id = block.GetProperty("id").GetString() ?? "";
                    var name = block.GetProperty("name").GetString() ?? "";
                    var inputEl = block.GetProperty("input");
                    var inputJson = inputEl.GetRawText();
                    toolUses.Add((id, name, inputJson));
                    assistantBlocks.Add(new { type = "tool_use", id, name, input = JsonSerializer.Deserialize<object>(inputJson) });
                }
            }

            if (toolUses.Count == 0)
            {
                var finalTxt = string.Join("\n", textParts);
                var summary = executedTools.Count > 0
                    ? $"{agent.AgentName} ({model}) · Tools: {string.Join(", ", executedTools)}"
                    : $"{agent.AgentName} ({model})";
                return (true, string.IsNullOrWhiteSpace(finalTxt) ? "Completed." : finalTxt, summary, latestScreenshot, "");
            }

            messages.Add(new { role = "assistant", content = assistantBlocks });
            var toolResults = new List<object>();
            foreach (var (id, name, argsJson) in toolUses)
            {
                var toolResult = await ExecuteGuardedToolAsync(name, argsJson, config, agent, confirmMutatingToolAsync, ct);
                executedTools.Add(name);
                if (toolResult.ScreenshotBase64 is not null)
                {
                    latestScreenshot = toolResult.ScreenshotBase64;
                }
                toolResults.Add(new
                {
                    type = "tool_result",
                    tool_use_id = id,
                    content = StripBase64ForLlm(toolResult.ResultJson),
                });
            }
            messages.Add(new { role = "user", content = toolResults });
        }

        return (true, "Reached configured max tool steps.", $"{agent.AgentName} ({model}) · Tools: {string.Join(", ", executedTools)}", latestScreenshot, "");
    }

    private async Task<(string ResultJson, string? ScreenshotBase64)> ExecuteGuardedToolAsync(
        string toolName,
        string argsJson,
        AgentGuiConfig config,
        DeviceAgentSettings agent,
        Func<string, string, Task<bool>>? confirmMutatingToolAsync,
        CancellationToken ct)
    {
        if (!IsToolAllowed(toolName, config, out var denyReason))
        {
            return (JsonSerializer.Serialize(new { error = denyReason, blocked_by = "agent_settings" }), null);
        }

        if (agent.ConfirmMutatingFunctions && IsToolMutating(toolName) && confirmMutatingToolAsync is not null)
        {
            var approved = await confirmMutatingToolAsync(toolName, argsJson);
            if (!approved)
            {
                return (JsonSerializer.Serialize(new { error = $"User declined execution of '{toolName}'.", blocked_by = "user_confirmation" }), null);
            }
        }

        var (_, resultJson) = await _executor.ExecuteAsync(toolName, argsJson, config, ct);
        string? shot = null;
        try
        {
            using var doc = JsonDocument.Parse(resultJson);
            if (doc.RootElement.TryGetProperty("image_base64", out var bProp)) shot = bProp.GetString();
            else if (doc.RootElement.TryGetProperty("base64_data", out var b2Prop)) shot = b2Prop.GetString();
        }
        catch { }

        return (resultJson, shot);
    }

    private static string StripBase64ForLlm(string json)
    {
        try
        {
            using var doc = JsonDocument.Parse(json);
            if (doc.RootElement.ValueKind != JsonValueKind.Object) return Truncate(json, 6000);
            var dict = new Dictionary<string, JsonElement>();
            foreach (var prop in doc.RootElement.EnumerateObject())
            {
                if (prop.Name is "base64_data" or "image_base64") continue;
                dict[prop.Name] = prop.Value.Clone();
            }
            return Truncate(JsonSerializer.Serialize(dict), 6000);
        }
        catch
        {
            return Truncate(json, 6000);
        }
    }

    private static string Truncate(string s, int max) =>
        s.Length <= max ? s : s[..max] + "...";

    public void Dispose() => _http.Dispose();
}
