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
    private Task? _pollTask;
    private Task? _heartbeatTask;
    private Task? _wsTask;
    private readonly SemaphoreSlim _wakeSignal = new(0, 1);

    public bool IsRunning => _cts is not null && !_cts.IsCancellationRequested;

    public event Action<string, string>? LogMessage;
    public event Action<string, string>? StatusChanged;
    public event Action<TaskHistoryRecord>? TaskRecorded;

    public ControlPlaneClient(SystemMonitorService monitor, NativeToolExecutor executor)
    {
        _monitor = monitor;
        _executor = executor;
        _http = new HttpClient { Timeout = TimeSpan.FromSeconds(45) };
        _http.DefaultRequestHeaders.UserAgent.ParseAdd("AI-Bridge-Windows-CSharp-Client/0.3.0");
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
                    if (msg.Contains("task_available", StringComparison.OrdinalIgnoreCase) && _wakeSignal.CurrentCount == 0)
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
                StatusChanged?.Invoke("Connected", "ok");

                var body = await resp.Content.ReadAsStringAsync(ct);
                using var doc = JsonDocument.Parse(body);
                var root = doc.RootElement;

                if (root.TryGetProperty("task", out var taskEl) && taskEl.ValueKind == JsonValueKind.Object)
                {
                    var taskId = taskEl.GetProperty("id").GetString() ?? "";
                    var tool = taskEl.GetProperty("tool").GetString() ?? "";
                    var argsJson = taskEl.TryGetProperty("args_json", out var aEl) ? aEl.GetString() ?? "{}" : "{}";

                    LogMessage?.Invoke("info", $"Executing task {taskId}: {tool}");
                    var sw = Stopwatch.StartNew();
                    var (ok, resultJson) = await _executor.ExecuteAsync(tool, argsJson, config, ct);
                    sw.Stop();

                    var status = ok ? "succeeded" : "failed";
                    await PostTaskResultAsync(baseUrl, token, taskId, status, resultJson, ct);

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
                    LogMessage?.Invoke(ok ? "ok" : "err", $"Task {taskId} {status} ({record.DurationMs} ms)");
                }
                else
                {
                    // Optional short wait on WS wake signal if server returned immediately
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

    private async Task PostTaskResultAsync(string baseUrl, string token, string taskId, string status, string resultJson, CancellationToken ct)
    {
        using var req = new HttpRequestMessage(HttpMethod.Post, $"{baseUrl}/api/agent/tasks/{taskId}/result");
        req.Headers.Add("X-Device-Token", token);
        var payload = JsonSerializer.Serialize(new { status, result = resultJson });
        req.Content = new StringContent(payload, Encoding.UTF8, "application/json");
        using var resp = await _http.SendAsync(req, ct);
        resp.EnsureSuccessStatusCode();
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
