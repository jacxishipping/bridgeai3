using System;
using System.Diagnostics;
using System.Net;
using System.Net.Http;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using AIBridgeClient.Models;

namespace AIBridgeClient.Services;

public sealed class ControlPlaneClient : IDisposable
{
    private readonly HttpClient _http;
    private readonly SystemMonitorService _monitor;
    private readonly NativeToolExecutor _executor;

    private CancellationTokenSource? _cts;
    private CancellationTokenSource? _activeTaskCts;
    private string? _activeTaskId;
    private Task? _pollTask;
    private Task? _heartbeatTask;
    private Task? _wsTask;
    private readonly SemaphoreSlim _wakeSignal = new(0, 1);

    public bool IsRunning => _cts is not null && !_cts.IsCancellationRequested;
    public bool IsPaused { get; private set; }
    public bool IsEmergencyStopActive { get; private set; }

    public event Action<string, string>? LogMessage;
    public event Action<string, string>? StatusChanged;
    public event Action<TaskHistoryRecord>? TaskRecorded;
    public event Action<bool>? EmergencyStopChanged;
    public Func<string, string, string, Task<bool>>? LocalApprovalRequested;

    public ControlPlaneClient(SystemMonitorService monitor, NativeToolExecutor executor)
    {
        _monitor = monitor;
        _executor = executor;
        _http = new HttpClient { Timeout = TimeSpan.FromSeconds(45) };
        _http.DefaultRequestHeaders.UserAgent.ParseAdd("AI-Bridge-Windows-CSharp-Client/0.3.0");
    }

    public void SetPaused(bool paused)
    {
        IsPaused = paused;
        if (IsRunning)
        {
            StatusChanged?.Invoke(paused ? "Paused" : "Connected", paused ? "warn" : "ok");
        }
        LogMessage?.Invoke(paused ? "warn" : "info", paused ? "Task execution paused locally." : "Task execution resumed.");
    }

    public async Task<(bool Ok, double LatencyMs, string Detail)> PingAsync(string controlUrl, CancellationToken ct = default)
    {
        var baseUrl = NormalizeUrl(controlUrl);
        var sw = Stopwatch.StartNew();
        try
        {
            using var resp = await _http.GetAsync($"{baseUrl}/health", ct);
            sw.Stop();
            var body = await resp.Content.ReadAsStringAsync(ct);
            return (resp.IsSuccessStatusCode, Math.Round(sw.Elapsed.TotalMilliseconds, 1), $"HTTP {(int)resp.StatusCode} {body}");
        }
        catch (Exception ex)
        {
            sw.Stop();
            return (false, Math.Round(sw.Elapsed.TotalMilliseconds, 1), ex.Message);
        }
    }

    public async Task<(bool Ok, string DeviceToken, string DeviceId, string Detail)> EnrollAsync(
        string controlUrl,
        string enrollmentToken,
        string deviceId,
        CancellationToken ct = default)
    {
        var baseUrl = NormalizeUrl(controlUrl);
        try
        {
            var payload = JsonSerializer.Serialize(new
            {
                enrollment_token = enrollmentToken.Trim(),
                device_id = deviceId.Trim(),
                hostname = Environment.MachineName,
                os = "Windows",
                platform = "windows",
                agent_version = "0.3.0-csharp",
            });
            using var req = new HttpRequestMessage(HttpMethod.Post, $"{baseUrl}/api/agent/enroll");
            req.Content = new StringContent(payload, Encoding.UTF8, "application/json");
            using var resp = await _http.SendAsync(req, ct);
            var body = await resp.Content.ReadAsStringAsync(ct);

            if (!resp.IsSuccessStatusCode)
            {
                return (false, "", "", $"HTTP {(int)resp.StatusCode}: {body}");
            }

            using var doc = JsonDocument.Parse(body);
            var root = doc.RootElement;
            var tok = root.TryGetProperty("device_token", out var tEl) ? tEl.GetString() ?? ""
                : root.TryGetProperty("token", out var t2) ? t2.GetString() ?? "" : "";
            var id = root.TryGetProperty("device_id", out var dEl) ? dEl.GetString() ?? deviceId : deviceId;
            return (!string.IsNullOrEmpty(tok), tok, id, string.IsNullOrEmpty(tok) ? "No device_token in response" : "Device enrolled successfully");
        }
        catch (Exception ex)
        {
            return (false, "", "", ex.Message);
        }
    }

    public async Task<(bool Ok, string LatestVersion, string Detail)> CheckUpdateAsync(
        string controlUrl,
        string currentVersion,
        CancellationToken ct = default)
    {
        var baseUrl = NormalizeUrl(controlUrl);
        try
        {
            using var resp = await _http.GetAsync($"{baseUrl}/api/updates/manifest", ct);
            var body = await resp.Content.ReadAsStringAsync(ct);
            if (!resp.IsSuccessStatusCode)
            {
                return (false, currentVersion, $"Manifest endpoint returned HTTP {(int)resp.StatusCode}");
            }

            using var doc = JsonDocument.Parse(body);
            var root = doc.RootElement;
            var ver = root.TryGetProperty("version", out var vEl) ? vEl.GetString() ?? currentVersion : currentVersion;
            var hasSig = root.TryGetProperty("signature", out var sEl) && !string.IsNullOrWhiteSpace(sEl.GetString());
            var status = ver == currentVersion
                ? $"Up to date (v{currentVersion})"
                : $"Update available: v{ver} (signed: {(hasSig ? "Ed25519" : "unsigned")})";
            return (true, ver, status);
        }
        catch (Exception ex)
        {
            return (false, currentVersion, ex.Message);
        }
    }

    public async Task<(bool Ok, string Detail)> SyncPolicyAsync(string controlUrl, string token, AgentPolicyConfig policy, CancellationToken ct = default)
    {
        var baseUrl = NormalizeUrl(controlUrl);
        try
        {
            using var req = new HttpRequestMessage(HttpMethod.Put, $"{baseUrl}/api/agent/policy");
            req.Headers.Add("X-Device-Token", token);
            req.Content = new StringContent(JsonSerializer.Serialize(policy), Encoding.UTF8, "application/json");
            using var resp = await _http.SendAsync(req, ct);
            var body = await resp.Content.ReadAsStringAsync(ct);
            return (resp.IsSuccessStatusCode, resp.IsSuccessStatusCode ? "Policy synced to Control Plane" : $"HTTP {(int)resp.StatusCode}: {body}");
        }
        catch (Exception ex)
        {
            return (false, ex.Message);
        }
    }

    public async Task<(bool Ok, AgentPolicyConfig? Policy, string Detail)> FetchPolicyAsync(string controlUrl, string token, CancellationToken ct = default)
    {
        var baseUrl = NormalizeUrl(controlUrl);
        try
        {
            using var req = new HttpRequestMessage(HttpMethod.Get, $"{baseUrl}/api/agent/policy");
            req.Headers.Add("X-Device-Token", token);
            using var resp = await _http.SendAsync(req, ct);
            var body = await resp.Content.ReadAsStringAsync(ct);
            if (!resp.IsSuccessStatusCode)
            {
                return (false, null, $"HTTP {(int)resp.StatusCode}: {body}");
            }
            var parsed = JsonSerializer.Deserialize<AgentPolicyConfig>(body, new JsonSerializerOptions { PropertyNameCaseInsensitive = true });
            return (true, parsed, "Fetched policy from Control Plane");
        }
        catch (Exception ex)
        {
            return (false, null, ex.Message);
        }
    }

    public async Task<(bool Ok, string NewToken, string Detail)> RotateTokenAsync(string controlUrl, string token, CancellationToken ct = default)
    {
        var baseUrl = NormalizeUrl(controlUrl);
        try
        {
            using var req = new HttpRequestMessage(HttpMethod.Post, $"{baseUrl}/api/agent/rotate");
            req.Headers.Add("X-Device-Token", token);
            req.Content = new StringContent("{}", Encoding.UTF8, "application/json");
            using var resp = await _http.SendAsync(req, ct);
            var body = await resp.Content.ReadAsStringAsync(ct);
            if (!resp.IsSuccessStatusCode)
            {
                return (false, "", $"HTTP {(int)resp.StatusCode}: {body}");
            }
            using var doc = JsonDocument.Parse(body);
            var newTok = doc.RootElement.TryGetProperty("token", out var tEl) ? tEl.GetString() ?? "" : "";
            return (!string.IsNullOrEmpty(newTok), newTok, "Token rotated");
        }
        catch (Exception ex)
        {
            return (false, "", ex.Message);
        }
    }

    public void Start(AgentGuiConfig config, string deviceToken)
    {
        if (IsRunning) return;
        _cts = new CancellationTokenSource();
        var ct = _cts.Token;

        StatusChanged?.Invoke("Connecting", "warn");
        LogMessage?.Invoke("info", $"Starting agent poller -> {NormalizeUrl(config.ControlUrl)}");

        _heartbeatTask = Task.Run(() => HeartbeatLoopAsync(config, deviceToken, ct), ct);
        _wsTask = Task.Run(() => WebSocketSideChannelLoopAsync(config, deviceToken, ct), ct);
        _pollTask = Task.Run(() => PollLoopAsync(config, deviceToken, ct), ct);
    }

    public async Task StopAsync()
    {
        if (_cts is null) return;
        _cts.Cancel();
        try
        {
            await Task.WhenAll(
                _pollTask ?? Task.CompletedTask,
                _heartbeatTask ?? Task.CompletedTask,
                _wsTask ?? Task.CompletedTask);
        }
        catch
        {
            // Expected on cancellation
        }
        _cts.Dispose();
        _cts = null;
        StatusChanged?.Invoke("Disconnected", "muted");
        LogMessage?.Invoke("info", "Agent stopped.");
    }

    private void UpdateEmergencyStop(bool active)
    {
        if (IsEmergencyStopActive == active) return;
        IsEmergencyStopActive = active;
        EmergencyStopChanged?.Invoke(active);
        if (active)
        {
            _activeTaskCts?.Cancel();
            if (NamedPipeBridge.IsDesktopAgentAvailable())
            {
                _ = Task.Run(() => NamedPipeBridge.ReleaseAllInputAsync(CancellationToken.None));
            }
            LogMessage?.Invoke("err", "EMERGENCY ALL-STOP ACTIVE: task execution halted.");
        }
        else
        {
            LogMessage?.Invoke("ok", "Emergency All-Stop cleared.");
        }
    }

    private async Task HeartbeatLoopAsync(AgentGuiConfig config, string token, CancellationToken ct)
    {
        var baseUrl = NormalizeUrl(config.ControlUrl);
        while (!ct.IsCancellationRequested)
        {
            try
            {
                var t = _monitor.Sample();
                var payload = JsonSerializer.Serialize(new
                {
                    device_id = config.DeviceId,
                    os = "Windows",
                    hostname = Environment.MachineName,
                    agent_version = "0.3.0-csharp",
                    metrics = new
                    {
                        cpu_percent = t.CpuPercent,
                        memory_percent = t.MemoryPercent,
                        disk_percent = t.DiskPercent,
                        process_count = t.ProcessCount,
                    },
                });
                using var req = new HttpRequestMessage(HttpMethod.Post, $"{baseUrl}/api/agent/heartbeat");
                req.Headers.Add("X-Device-Token", token);
                req.Content = new StringContent(payload, Encoding.UTF8, "application/json");
                using var resp = await _http.SendAsync(req, ct);
                if (resp.StatusCode == HttpStatusCode.Unauthorized || resp.StatusCode == HttpStatusCode.Forbidden)
                {
                    StatusChanged?.Invoke("Auth Rejected", "err");
                    LogMessage?.Invoke("err", $"Heartbeat auth rejected (HTTP {(int)resp.StatusCode}). Stopping agent.");
                    _cts?.Cancel();
                    return;
                }
            }
            catch (OperationCanceledException) { return; }
            catch (Exception ex)
            {
                LogMessage?.Invoke("warn", $"Heartbeat failed: {ex.Message}");
            }

            try { await Task.Delay(TimeSpan.FromSeconds(15), ct); } catch { return; }
        }
    }

    private async Task WebSocketSideChannelLoopAsync(AgentGuiConfig config, string token, CancellationToken ct)
    {
        var baseUrl = NormalizeUrl(config.ControlUrl);
        var wsUrl = baseUrl.StartsWith("https://", StringComparison.OrdinalIgnoreCase)
            ? "wss://" + baseUrl[8..] + "/api/agent/ws"
            : "ws://" + baseUrl[7..] + "/api/agent/ws";

        while (!ct.IsCancellationRequested)
        {
            try
            {
                using var ws = new ClientWebSocket();
                ws.Options.SetRequestHeader("X-Device-Token", token);
                await ws.ConnectAsync(new Uri(wsUrl), ct);
                LogMessage?.Invoke("ok", "WebSocket side-channel connected.");

                var buf = new byte[4096];
                while (ws.State == WebSocketState.Open && !ct.IsCancellationRequested)
                {
                    var res = await ws.ReceiveAsync(buf, ct);
                    if (res.MessageType == WebSocketMessageType.Close) break;
                    var msg = Encoding.UTF8.GetString(buf, 0, res.Count);

                    if (msg.Contains("emergency_stop", StringComparison.OrdinalIgnoreCase))
                    {
                        UpdateEmergencyStop(true);
                    }
                    else if (msg.Contains("cancel", StringComparison.OrdinalIgnoreCase) &&
                             (_activeTaskId is null || msg.Contains(_activeTaskId, StringComparison.OrdinalIgnoreCase)))
                    {
                        _activeTaskCts?.Cancel();
                    }
                    else if (msg.Contains("task_available", StringComparison.OrdinalIgnoreCase) && _wakeSignal.CurrentCount == 0)
                    {
                        _wakeSignal.Release();
                    }
                }
            }
            catch (OperationCanceledException) { return; }
            catch
            {
                // Side-channel is optional; HTTP long-poll remains primary
            }

            try { await Task.Delay(TimeSpan.FromSeconds(10), ct); } catch { return; }
        }
    }

    private async Task PollLoopAsync(AgentGuiConfig config, string token, CancellationToken ct)
    {
        var baseUrl = NormalizeUrl(config.ControlUrl);
        var waitS = Math.Clamp(config.PollIntervalSeconds, 5, 30);
        int backoffAttempt = 0;

        while (!ct.IsCancellationRequested)
        {
            if (IsPaused)
            {
                try { await Task.Delay(TimeSpan.FromSeconds(1), ct); } catch { return; }
                continue;
            }

            try
            {
                using var req = new HttpRequestMessage(HttpMethod.Get, $"{baseUrl}/api/agent/tasks/next?wait={waitS}");
                req.Headers.Add("X-Device-Token", token);
                using var resp = await _http.SendAsync(req, ct);

                if (resp.StatusCode == HttpStatusCode.Unauthorized || resp.StatusCode == HttpStatusCode.Forbidden)
                {
                    StatusChanged?.Invoke("Auth Rejected", "err");
                    LogMessage?.Invoke("err", $"Task poll rejected (HTTP {(int)resp.StatusCode}). Check device token.");
                    _cts?.Cancel();
                    return;
                }

                if (!resp.IsSuccessStatusCode)
                {
                    backoffAttempt++;
                    StatusChanged?.Invoke($"Error {(int)resp.StatusCode}", "warn");
                    await Task.Delay(TimeSpan.FromSeconds(Math.Min(30, 2 * backoffAttempt)), ct);
                    continue;
                }

                backoffAttempt = 0;
                StatusChanged?.Invoke(IsPaused ? "Paused" : "Connected", IsPaused ? "warn" : "ok");

                var body = await resp.Content.ReadAsStringAsync(ct);
                using var doc = JsonDocument.Parse(body);
                var root = doc.RootElement;

                bool eStop = root.TryGetProperty("emergency_stop", out var esEl) && esEl.ValueKind == JsonValueKind.True;
                UpdateEmergencyStop(eStop);

                if (!eStop && root.TryGetProperty("task", out var taskEl) && taskEl.ValueKind == JsonValueKind.Object)
                {
                    var taskId = taskEl.GetProperty("id").GetString() ?? "";
                    var tool = taskEl.GetProperty("tool").GetString() ?? "";
                    var argsJson = taskEl.TryGetProperty("args_json", out var aEl) ? aEl.GetString() ?? "{}" : "{}";

                    await ExecuteClaimedTaskAsync(baseUrl, token, config, taskId, tool, argsJson, ct);
                }
                else
                {
                    await _wakeSignal.WaitAsync(TimeSpan.FromMilliseconds(250), ct);
                }
            }
            catch (OperationCanceledException) { return; }
            catch (Exception ex)
            {
                backoffAttempt++;
                StatusChanged?.Invoke("Reconnecting", "warn");
                LogMessage?.Invoke("warn", $"Poll error: {ex.Message}");
                try { await Task.Delay(TimeSpan.FromSeconds(Math.Min(30, 2 * backoffAttempt)), ct); } catch { return; }
            }
        }
    }

    private async Task ExecuteClaimedTaskAsync(
        string baseUrl,
        string token,
        AgentGuiConfig config,
        string taskId,
        string tool,
        string argsJson,
        CancellationToken outerCt)
    {
        LogMessage?.Invoke("info", $"Executing task {taskId}: {tool}");
        var sw = Stopwatch.StartNew();

        using var linkedCts = CancellationTokenSource.CreateLinkedTokenSource(outerCt);
        _activeTaskCts = linkedCts;
        _activeTaskId = taskId;

        var cancelMonitorTask = MonitorTaskCancellationAsync(baseUrl, token, taskId, linkedCts);

        string status;
        string resultJson;
        try
        {
            if (config.Policy.RequireLocalApproval && IsSensitiveTool(tool) && LocalApprovalRequested is not null)
            {
                bool approved = await LocalApprovalRequested.Invoke(taskId, tool, argsJson);
                if (!approved)
                {
                    status = "denied";
                    resultJson = JsonSerializer.Serialize(new { error = "Denied by local workstation operator." });
                    sw.Stop();
                    await PostTaskResultAsync(baseUrl, token, taskId, status, resultJson, outerCt);
                    var deniedRec = new TaskHistoryRecord
                    {
                        Id = taskId,
                        Tool = tool,
                        Status = status,
                        Timestamp = DateTimeOffset.Now.ToString("yyyy-MM-dd HH:mm:ss"),
                        DurationMs = Math.Round(sw.Elapsed.TotalMilliseconds, 1),
                        ArgsJson = argsJson,
                        ResultPreview = resultJson,
                    };
                    TaskRecorded?.Invoke(deniedRec);
                    LogMessage?.Invoke("warn", $"Task {taskId} denied by local operator.");
                    return;
                }
            }

            var (ok, res) = await _executor.ExecuteAsync(tool, argsJson, config, linkedCts.Token);
            status = ok ? "succeeded" : "failed";
            resultJson = res;
        }
        catch (OperationCanceledException) when (!outerCt.IsCancellationRequested)
        {
            status = "cancelled";
            resultJson = JsonSerializer.Serialize(new { error = "Task cancelled by Control Plane or Emergency Stop." });
        }
        finally
        {
            linkedCts.Cancel();
            try { await cancelMonitorTask; } catch { }
            _activeTaskCts = null;
            _activeTaskId = null;
        }

        sw.Stop();
        await PostTaskResultAsync(baseUrl, token, taskId, status, resultJson, outerCt);

        var record = new TaskHistoryRecord
        {
            Id = taskId,
            Tool = tool,
            Status = status,
            Timestamp = DateTimeOffset.Now.ToString("yyyy-MM-dd HH:mm:ss"),
            DurationMs = Math.Round(sw.Elapsed.TotalMilliseconds, 1),
            ArgsJson = argsJson,
            ResultPreview = resultJson.Length > 4000 ? resultJson[..4000] : resultJson,
        };
        TaskRecorded?.Invoke(record);
        LogMessage?.Invoke(status == "succeeded" ? "ok" : (status == "cancelled" ? "warn" : "err"),
            $"Task {taskId} {status} ({record.DurationMs} ms)");
    }

    private async Task MonitorTaskCancellationAsync(string baseUrl, string token, string taskId, CancellationTokenSource taskCts)
    {
        while (!taskCts.IsCancellationRequested)
        {
            try
            {
                await Task.Delay(TimeSpan.FromMilliseconds(1500), taskCts.Token);
                using var req = new HttpRequestMessage(HttpMethod.Get, $"{baseUrl}/api/agent/tasks/{taskId}/execution-state");
                req.Headers.Add("X-Device-Token", token);
                using var resp = await _http.SendAsync(req, taskCts.Token);
                if (!resp.IsSuccessStatusCode) continue;

                var json = await resp.Content.ReadAsStringAsync(taskCts.Token);
                using var doc = JsonDocument.Parse(json);
                var root = doc.RootElement;
                bool cancel = (root.TryGetProperty("cancelled", out var cEl) && cEl.ValueKind == JsonValueKind.True) ||
                              (root.TryGetProperty("emergency_stop", out var eEl) && eEl.ValueKind == JsonValueKind.True);
                if (cancel)
                {
                    taskCts.Cancel();
                    return;
                }
            }
            catch (OperationCanceledException) { return; }
            catch { /* Ignore transient check errors */ }
        }
    }

    private async Task PostTaskResultAsync(string baseUrl, string token, string taskId, string status, string resultJson, CancellationToken ct)
    {
        using var req = new HttpRequestMessage(HttpMethod.Post, $"{baseUrl}/api/agent/tasks/{taskId}/result");
        req.Headers.Add("X-Device-Token", token);
        var payload = JsonSerializer.Serialize(new { status, result = resultJson });
        req.Content = new StringContent(payload, Encoding.UTF8, "application/json");
        using var resp = await _http.SendAsync(req, ct);
        resp.EnsureSuccessStatusCode();
    }

    private static bool IsSensitiveTool(string toolName)
    {
        var t = toolName.Trim();
        var dot = t.LastIndexOf('.');
        if (dot >= 0) t = t[(dot + 1)..];
        return t is "run_shell" or "delete_workspace_file" or "edit_workspace_file" or "move_workspace_file"
            or "mouse_move" or "mouse_click" or "mouse_scroll" or "mouse_drag"
            or "desktop_mouse_move" or "desktop_click" or "desktop_scroll" or "desktop_keyboard"
            or "keyboard_type" or "key_press" or "keyboard_press" or "press_hotkey"
            or "click_element" or "click_text" or "focus_window" or "launch_approved_application"
            or "set_clipboard" or "desktop_clipboard_set"
            or "capture_desktop_screenshot" or "take_screenshot" or "capture_desktop_frame"
            or "desktop_get_screenshot" or "desktop_get_window_screenshot"
            or "start_webrtc_stream" or "stop_webrtc_stream";
    }

    public static string NormalizeUrl(string url) => url.Trim().TrimEnd('/');

    public void Dispose()
    {
        _cts?.Cancel();
        _cts?.Dispose();
        _http.Dispose();
        _wakeSignal.Dispose();
    }
}
