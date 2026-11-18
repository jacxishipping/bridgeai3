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
        _http = new HttpClient { Timeout = TimeSpan.FromSeconds(120) };
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

    public async Task<(bool Ok, string DeviceToken, string DeviceId, string Tenant, string Username, string Detail)> LoginAndEnrollAsync(
        string controlUrl,
        string username,
        string password,
        string deviceName,
        string tenant = "default",
        CancellationToken ct = default)
    {
        var baseUrl = NormalizeUrl(controlUrl);
        try
        {
            // 1. Authenticate user session via /api/auth/login
            var loginPayload = JsonSerializer.Serialize(new
            {
                username = username.Trim(),
                password,
            });
            using var loginReq = new HttpRequestMessage(HttpMethod.Post, $"{baseUrl}/api/auth/login");
            loginReq.Content = new StringContent(loginPayload, Encoding.UTF8, "application/json");
            using var loginResp = await _http.SendAsync(loginReq, ct);
            var loginBody = await loginResp.Content.ReadAsStringAsync(ct);
            if (!loginResp.IsSuccessStatusCode)
            {
                return (false, "", "", tenant, "", ExtractApiDetail(loginBody, $"Sign in failed (HTTP {(int)loginResp.StatusCode})"));
            }

            using var loginDoc = JsonDocument.Parse(loginBody);
            var lRoot = loginDoc.RootElement;
            var sessionToken = lRoot.TryGetProperty("session_token", out var sEl) ? sEl.GetString() ?? "" : "";
            var resolvedUser = lRoot.TryGetProperty("username", out var uEl) ? uEl.GetString() ?? username : username;
            var resolvedTenant = lRoot.TryGetProperty("tenant", out var tEl) ? tEl.GetString() ?? tenant : tenant;
            if (string.IsNullOrWhiteSpace(sessionToken))
            {
                return (false, "", "", resolvedTenant, resolvedUser, "Server did not return a session token.");
            }

            // 2. Enroll this Windows workstation using the authenticated session token
            var enrollPayload = JsonSerializer.Serialize(new
            {
                name = string.IsNullOrWhiteSpace(deviceName) ? Environment.MachineName : deviceName.Trim(),
                hostname = Environment.MachineName,
                os = "Windows",
                tenant = resolvedTenant,
            });
            using var enrollReq = new HttpRequestMessage(HttpMethod.Post, $"{baseUrl}/api/devices/enroll");
            enrollReq.Headers.Add("X-Session-Token", sessionToken);
            enrollReq.Content = new StringContent(enrollPayload, Encoding.UTF8, "application/json");
            using var enrollResp = await _http.SendAsync(enrollReq, ct);
            var enrollBody = await enrollResp.Content.ReadAsStringAsync(ct);
            if (!enrollResp.IsSuccessStatusCode)
            {
                return (false, "", "", resolvedTenant, resolvedUser, ExtractApiDetail(enrollBody, $"Device enrollment failed (HTTP {(int)enrollResp.StatusCode})"));
            }

            using var enrollDoc = JsonDocument.Parse(enrollBody);
            var eRoot = enrollDoc.RootElement;
            var tok = eRoot.TryGetProperty("device_token", out var dTokEl) ? dTokEl.GetString() ?? ""
                : eRoot.TryGetProperty("token", out var t2El) ? t2El.GetString() ?? "" : "";
            var devId = eRoot.TryGetProperty("device_id", out var dIdEl) ? dIdEl.GetString() ?? deviceName : deviceName;

            return (!string.IsNullOrEmpty(tok), tok, devId, resolvedTenant, resolvedUser,
                string.IsNullOrEmpty(tok) ? "No device_token returned by server." : "Signed in and device enrolled.");
        }
        catch (Exception ex)
        {
            return (false, "", "", tenant, "", ex.Message);
        }
    }

    public async Task<(bool Ok, string DeviceToken, string DeviceId, string Detail)> EnrollAsync(
        string controlUrl,
        string providedToken,
        string deviceId,
        CancellationToken ct = default)
    {
        var baseUrl = NormalizeUrl(controlUrl);
        var cleanTok = providedToken.Trim();
        try
        {
            // 1. Check whether the token is already a valid provisioned device token via /api/agent/whoami
            using (var whoReq = new HttpRequestMessage(HttpMethod.Get, $"{baseUrl}/api/agent/whoami"))
            {
                whoReq.Headers.Add("X-Device-Token", cleanTok);
                using var whoResp = await _http.SendAsync(whoReq, ct);
                if (whoResp.IsSuccessStatusCode)
                {
                    var whoBody = await whoResp.Content.ReadAsStringAsync(ct);
                    using var whoDoc = JsonDocument.Parse(whoBody);
                    var resolvedId = whoDoc.RootElement.TryGetProperty("device_id", out var idEl)
                        ? idEl.GetString() ?? deviceId
                        : deviceId;
                    return (true, cleanTok, resolvedId, "Device token verified.");
                }
            }

            // 2. Otherwise treat the provided token as an Admin Enrollment Token for POST /api/devices/enroll
            var payload = JsonSerializer.Serialize(new
            {
                name = string.IsNullOrWhiteSpace(deviceId) ? Environment.MachineName : deviceId.Trim(),
                hostname = Environment.MachineName,
                os = "Windows",
            });
            using var req = new HttpRequestMessage(HttpMethod.Post, $"{baseUrl}/api/devices/enroll");
            req.Headers.Add("X-Admin-Token", cleanTok);
            req.Content = new StringContent(payload, Encoding.UTF8, "application/json");
            using var resp = await _http.SendAsync(req, ct);
            var body = await resp.Content.ReadAsStringAsync(ct);

            if (!resp.IsSuccessStatusCode)
            {
                return (false, "", "", ExtractApiDetail(body, $"HTTP {(int)resp.StatusCode}"));
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

    private static string ExtractApiDetail(string body, string fallback)
    {
        try
        {
            using var doc = JsonDocument.Parse(body);
            if (doc.RootElement.TryGetProperty("detail", out var d) && d.GetString() is { Length: > 0 } msg)
            {
                return msg;
            }
        }
        catch
        {
            // Ignore non-JSON error payloads
        }
        return string.IsNullOrWhiteSpace(body) ? fallback : body;
    }

    public async Task<(bool Ok, string LatestVersion, string Detail)> CheckUpdateAsync(
        string controlUrl,
        string currentVersion,
        CancellationToken ct = default,
        string? customManifestUrl = null)
    {
        var baseUrl = NormalizeUrl(controlUrl);
        var manifestUrl = string.IsNullOrWhiteSpace(customManifestUrl)
            ? $"{baseUrl}/api/updates/manifest"
            : customManifestUrl.Trim();
        try
        {
            using var resp = await _http.GetAsync(manifestUrl, ct);
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
            // Match Control Plane DevicePolicyIn schema (desktop_enabled, allow_file_deletion, inherit_global, etc.)
            var serverPayload = new
            {
                inherit_global = false,
                allow_writes = policy.AllowWrites,
                allow_tier3 = policy.AllowTier3,
                desktop_enabled = policy.AllowDesktopControl,
                allow_shell = policy.AllowShell,
                allow_package_install = policy.AllowPackageInstall,
                allow_file_deletion = policy.AllowTier4,
                max_tier_unattended = Math.Clamp(policy.MaxTierUnattended, 1, 4),
                allowed_apps = policy.AllowedApps,
            };
            using var req = new HttpRequestMessage(HttpMethod.Put, $"{baseUrl}/api/agent/policy");
            req.Headers.Add("X-Device-Token", token);
            req.Content = new StringContent(JsonSerializer.Serialize(serverPayload), Encoding.UTF8, "application/json");
            using var resp = await _http.SendAsync(req, ct);
            var body = await resp.Content.ReadAsStringAsync(ct);
            return (resp.IsSuccessStatusCode, resp.IsSuccessStatusCode ? "Policy synced to Control Plane" : ExtractApiDetail(body, $"HTTP {(int)resp.StatusCode}"));
        }
        catch (Exception ex)
        {
            return (false, ex.Message);
        }
    }

    public async Task<(bool Ok, AgentPolicyConfig? Policy, string Detail)> FetchPolicyAsync(
        string controlUrl,
        string token,
        AgentPolicyConfig? basePolicy = null,
        CancellationToken ct = default)
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
                return (false, null, ExtractApiDetail(body, $"HTTP {(int)resp.StatusCode}"));
            }

            using var doc = JsonDocument.Parse(body);
            var root = doc.RootElement;
            // /api/agent/policy returns {"device_id", "custom_policy", "effective_policy", "has_custom_policy"}
            JsonElement polEl = root.TryGetProperty("effective_policy", out var effEl) && effEl.ValueKind == JsonValueKind.Object
                ? effEl
                : (root.TryGetProperty("custom_policy", out var custEl) && custEl.ValueKind == JsonValueKind.Object ? custEl : root);

            var parsed = new AgentPolicyConfig
            {
                AllowWrites = GetBoolOrDefault(polEl, "allow_writes", basePolicy?.AllowWrites ?? true),
                AllowTier3 = GetBoolOrDefault(polEl, "allow_tier3", basePolicy?.AllowTier3 ?? true),
                AllowTier4 = GetBoolOrDefault(polEl, "allow_file_deletion", GetBoolOrDefault(polEl, "allow_tier4", basePolicy?.AllowTier4 ?? false)),
                AllowShell = GetBoolOrDefault(polEl, "allow_shell", basePolicy?.AllowShell ?? false),
                AllowDesktopControl = GetBoolOrDefault(polEl, "desktop_enabled", GetBoolOrDefault(polEl, "allow_desktop_control", basePolicy?.AllowDesktopControl ?? false)),
                AllowPackageInstall = GetBoolOrDefault(polEl, "allow_package_install", basePolicy?.AllowPackageInstall ?? false),
                AllowClipboard = GetBoolOrDefault(polEl, "allow_clipboard", basePolicy?.AllowClipboard ?? true),
                RequireLocalApproval = GetBoolOrDefault(polEl, "require_local_approval", basePolicy?.RequireLocalApproval ?? false),
                MaxTierUnattended = polEl.TryGetProperty("max_tier_unattended", out var mtEl) && mtEl.TryGetInt32(out var mt)
                    ? Math.Clamp(mt, 1, 4)
                    : (basePolicy?.MaxTierUnattended ?? 3),
                AllowedApps = basePolicy?.AllowedApps is not null ? new(basePolicy.AllowedApps) : new(),
                AllowedCommands = basePolicy?.AllowedCommands is not null ? new(basePolicy.AllowedCommands) : new(),
            };

            if (polEl.TryGetProperty("allowed_apps", out var appsEl) && appsEl.ValueKind == JsonValueKind.Array)
            {
                var apps = new System.Collections.Generic.List<string>();
                foreach (var a in appsEl.EnumerateArray())
                {
                    if (a.GetString() is { Length: > 0 } s) apps.Add(s);
                }
                if (apps.Count > 0) parsed.AllowedApps = apps;
            }

            return (true, parsed, "Fetched policy from Control Plane");
        }
        catch (Exception ex)
        {
            return (false, null, ex.Message);
        }
    }

    private static bool GetBoolOrDefault(JsonElement el, string prop, bool fallback) =>
        el.TryGetProperty(prop, out var v) && (v.ValueKind == JsonValueKind.True || v.ValueKind == JsonValueKind.False)
            ? v.GetBoolean()
            : fallback;

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
                return (false, "", ExtractApiDetail(body, $"HTTP {(int)resp.StatusCode}"));
            }
            using var doc = JsonDocument.Parse(body);
            var root = doc.RootElement;
            var newTok = root.TryGetProperty("device_token", out var dtEl) ? dtEl.GetString() ?? ""
                : root.TryGetProperty("token", out var tEl) ? tEl.GetString() ?? "" : "";
            return (!string.IsNullOrEmpty(newTok), newTok, string.IsNullOrEmpty(newTok) ? "No device_token in rotate response" : "Token rotated");
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
        _wsTask = config.WebSocketEnabled
            ? Task.Run(() => WebSocketSideChannelLoopAsync(config, deviceToken, ct), ct)
            : Task.CompletedTask;
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
        int intervalSec = Math.Clamp(config.HeartbeatIntervalSeconds, 5, 120);
        int reqTimeoutSec = Math.Clamp(config.HttpTimeoutSeconds, 5, 120);
        while (!ct.IsCancellationRequested)
        {
            try
            {
                var t = _monitor.Sample();
                var payload = JsonSerializer.Serialize(new
                {
                    device_id = config.DeviceId,
                    tenant_id = config.TenantId,
                    os = "Windows",
                    hostname = Environment.MachineName,
                    agent_version = "0.3.0-csharp",
                    metrics = new
                    {
                        cpu_percent = t.CpuPercent,
                        memory_percent = t.MemoryPercent,
                        memory_total_gb = t.MemoryTotalGb,
                        disk_percent = t.DiskPercent,
                        disk_total_gb = t.DiskTotalGb,
                        uptime_hours = Math.Round(t.SystemUptime.TotalHours, 1),
                        process_count = t.ProcessCount,
                    },
                });
                using var req = new HttpRequestMessage(HttpMethod.Post, $"{baseUrl}/api/agent/heartbeat");
                req.Headers.Add("X-Device-Token", token);
                if (!string.IsNullOrWhiteSpace(config.TenantId))
                {
                    req.Headers.Add("X-Tenant-ID", config.TenantId);
                }
                req.Content = new StringContent(payload, Encoding.UTF8, "application/json");
                using var timeoutCts = CancellationTokenSource.CreateLinkedTokenSource(ct);
                timeoutCts.CancelAfter(TimeSpan.FromSeconds(reqTimeoutSec));
                using var resp = await _http.SendAsync(req, timeoutCts.Token);
                if (resp.StatusCode == HttpStatusCode.Unauthorized || resp.StatusCode == HttpStatusCode.Forbidden)
                {
                    StatusChanged?.Invoke("Auth Rejected", "err");
                    LogMessage?.Invoke("err", $"Heartbeat auth rejected (HTTP {(int)resp.StatusCode}). Stopping agent.");
                    _cts?.Cancel();
                    return;
                }
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { return; }
            catch (Exception ex)
            {
                LogMessage?.Invoke("warn", $"Heartbeat failed: {ex.Message}");
            }

            try { await Task.Delay(TimeSpan.FromSeconds(intervalSec), ct); } catch { return; }
        }
    }

    private async Task WebSocketSideChannelLoopAsync(AgentGuiConfig config, string token, CancellationToken ct)
    {
        var baseUrl = NormalizeUrl(config.ControlUrl);
        int reconnectSec = Math.Clamp(config.WebSocketReconnectSeconds, 3, 120);
        var wsUrl = baseUrl.StartsWith("https://", StringComparison.OrdinalIgnoreCase)
            ? "wss://" + baseUrl[8..] + "/api/agent/ws"
            : "ws://" + baseUrl[7..] + "/api/agent/ws";

        while (!ct.IsCancellationRequested)
        {
            try
            {
                using var ws = new ClientWebSocket();
                ws.Options.SetRequestHeader("X-Device-Token", token);
                if (!string.IsNullOrWhiteSpace(config.TenantId))
                {
                    ws.Options.SetRequestHeader("X-Tenant-ID", config.TenantId);
                }
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

            try { await Task.Delay(TimeSpan.FromSeconds(reconnectSec), ct); } catch { return; }
        }
    }

    private async Task PollLoopAsync(AgentGuiConfig config, string token, CancellationToken ct)
    {
        var baseUrl = NormalizeUrl(config.ControlUrl);
        var waitS = Math.Clamp(config.PollIntervalSeconds, 5, 60);
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
                    int? epoch = taskEl.TryGetProperty("epoch", out var epEl) && epEl.TryGetInt32(out var ep) ? ep : null;

                    await ExecuteClaimedTaskAsync(baseUrl, token, config, taskId, tool, argsJson, epoch, ct);
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
        int? epoch,
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
                    await PostTaskResultAsync(baseUrl, token, taskId, "failed", resultJson, epoch, outerCt);
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
        var serverStatus = status == "succeeded" ? "completed" : (status == "cancelled" ? "cancelled" : "failed");
        await PostTaskResultAsync(baseUrl, token, taskId, serverStatus, resultJson, epoch, outerCt);

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
                var stateStr = root.TryGetProperty("status", out var sEl) ? sEl.GetString() ?? "" : "";
                bool cancel = (root.TryGetProperty("cancelled", out var cEl) && cEl.ValueKind == JsonValueKind.True) ||
                              (root.TryGetProperty("emergency_stop", out var eEl) && eEl.ValueKind == JsonValueKind.True) ||
                              string.Equals(stateStr, "cancelled", StringComparison.OrdinalIgnoreCase) ||
                              string.Equals(stateStr, "timed_out", StringComparison.OrdinalIgnoreCase);
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

    private async Task PostTaskResultAsync(
        string baseUrl,
        string token,
        string taskId,
        string status,
        string resultJson,
        int? epoch,
        CancellationToken ct)
    {
        using var req = new HttpRequestMessage(HttpMethod.Post, $"{baseUrl}/api/agent/tasks/{taskId}/result");
        req.Headers.Add("X-Device-Token", token);
        var payload = epoch.HasValue
            ? JsonSerializer.Serialize(new { status, result = resultJson, epoch = epoch.Value })
            : JsonSerializer.Serialize(new { status, result = resultJson });
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

    public async Task<(bool Ok, string Token, bool IsAdminToken, string Detail)> LoginOperatorAsync(
        string controlUrl,
        string username,
        string password,
        string adminToken,
        CancellationToken ct = default)
    {
        var baseUrl = NormalizeUrl(controlUrl);
        try
        {
            if (!string.IsNullOrWhiteSpace(adminToken))
            {
                using var req = new HttpRequestMessage(HttpMethod.Get, $"{baseUrl}/api/auth/me");
                req.Headers.Add("X-Admin-Token", adminToken.Trim());
                using var resp = await _http.SendAsync(req, ct);
                var body = await resp.Content.ReadAsStringAsync(ct);
                if (!resp.IsSuccessStatusCode)
                {
                    return (false, "", true, ExtractApiDetail(body, $"HTTP {(int)resp.StatusCode}"));
                }
                return (true, adminToken.Trim(), true, "Authenticated via Admin Token.");
            }

            var payload = JsonSerializer.Serialize(new { username = username.Trim(), password });
            using var loginReq = new HttpRequestMessage(HttpMethod.Post, $"{baseUrl}/api/auth/login");
            loginReq.Content = new StringContent(payload, Encoding.UTF8, "application/json");
            using var loginResp = await _http.SendAsync(loginReq, ct);
            var loginBody = await loginResp.Content.ReadAsStringAsync(ct);
            if (!loginResp.IsSuccessStatusCode)
            {
                return (false, "", false, ExtractApiDetail(loginBody, $"HTTP {(int)loginResp.StatusCode}"));
            }
            using var doc = JsonDocument.Parse(loginBody);
            var tok = doc.RootElement.TryGetProperty("session_token", out var st) ? st.GetString() ?? "" : "";
            return (!string.IsNullOrEmpty(tok), tok, false, string.IsNullOrEmpty(tok) ? "No session token returned." : "Operator session active.");
        }
        catch (Exception ex)
        {
            return (false, "", false, ex.Message);
        }
    }

    private async Task AddOperatorHeadersAsync(
        HttpRequestMessage req,
        string baseUrl,
        string operatorToken,
        bool isAdminToken,
        bool includeCsrf,
        CancellationToken ct)
    {
        if (isAdminToken)
        {
            req.Headers.Add("X-Admin-Token", operatorToken);
            return;
        }
        req.Headers.Add("X-Session-Token", operatorToken);
        if (includeCsrf)
        {
            using var csrfReq = new HttpRequestMessage(HttpMethod.Get, $"{baseUrl}/api/auth/csrf-token");
            csrfReq.Headers.Add("X-Session-Token", operatorToken);
            using var csrfResp = await _http.SendAsync(csrfReq, ct);
            if (csrfResp.IsSuccessStatusCode)
            {
                var csrfBody = await csrfResp.Content.ReadAsStringAsync(ct);
                using var csrfDoc = JsonDocument.Parse(csrfBody);
                if (csrfDoc.RootElement.TryGetProperty("csrf_token", out var cEl) && cEl.GetString() is { Length: > 0 } csrfTok)
                {
                    req.Headers.Add("X-CSRF-Token", csrfTok);
                }
            }
        }
    }

    public async Task<(bool Ok, System.Collections.Generic.List<CustomAgentItem> CustomAgents, System.Collections.Generic.List<RoleAgentItem> Roles, System.Collections.Generic.List<string> Profiles, string Detail)> FetchAgentsAndRolesAsync(
        string controlUrl,
        string operatorToken,
        bool isAdminToken,
        string localDeviceId,
        CancellationToken ct = default)
    {
        var baseUrl = NormalizeUrl(controlUrl);
        var customList = new System.Collections.Generic.List<CustomAgentItem>();
        var roleList = new System.Collections.Generic.List<RoleAgentItem>();
        var profileList = new System.Collections.Generic.List<string>();
        var targetDevice = localDeviceId.Trim();
        if (string.IsNullOrWhiteSpace(targetDevice))
        {
            return (false, customList, roleList, profileList, "Device ID is not configured. Enroll this Windows device first.");
        }
        try
        {
            using (var aReq = new HttpRequestMessage(HttpMethod.Get, $"{baseUrl}/api/agents"))
            {
                await AddOperatorHeadersAsync(aReq, baseUrl, operatorToken, isAdminToken, false, ct);
                using var aResp = await _http.SendAsync(aReq, ct);
                var aBody = await aResp.Content.ReadAsStringAsync(ct);
                if (!aResp.IsSuccessStatusCode)
                {
                    return (false, customList, roleList, profileList, ExtractApiDetail(aBody, $"GET /api/agents HTTP {(int)aResp.StatusCode}"));
                }
                using var aDoc = JsonDocument.Parse(aBody);
                if (aDoc.RootElement.TryGetProperty("agents", out var arr) && arr.ValueKind == JsonValueKind.Array)
                {
                    foreach (var el in arr.EnumerateArray())
                    {
                        bool boundToThisDevice = false;
                        if (el.TryGetProperty("device_ids", out var devIdsEl) && devIdsEl.ValueKind == JsonValueKind.Array)
                        {
                            foreach (var did in devIdsEl.EnumerateArray())
                            {
                                if (string.Equals(did.GetString()?.Trim(), targetDevice, StringComparison.OrdinalIgnoreCase))
                                {
                                    boundToThisDevice = true;
                                    break;
                                }
                            }
                        }
                        if (!boundToThisDevice) continue;

                        customList.Add(new CustomAgentItem
                        {
                            Id = el.TryGetProperty("id", out var v) ? v.GetString() ?? "" : "",
                            Name = el.TryGetProperty("name", out var n) ? n.GetString() ?? "" : "",
                            Instruction = el.TryGetProperty("instruction", out var ins) ? ins.GetString() ?? "" : "",
                            Provider = el.TryGetProperty("provider", out var pr) ? pr.GetString() ?? "openai" : "openai",
                            Model = el.TryGetProperty("model", out var mo) ? mo.GetString() ?? "" : "",
                            IntervalSeconds = el.TryGetProperty("interval_seconds", out var iv) && iv.TryGetInt32(out var ivVal) ? ivVal : 300,
                            Enabled = el.TryGetProperty("enabled", out var en) && (en.ValueKind == JsonValueKind.True || (en.ValueKind == JsonValueKind.Number && en.GetInt32() != 0)),
                            Status = el.TryGetProperty("status", out var st) ? st.GetString() ?? "idle" : "idle",
                        });
                    }
                }
            }

            using (var rReq = new HttpRequestMessage(HttpMethod.Get, $"{baseUrl}/api/roles"))
            {
                await AddOperatorHeadersAsync(rReq, baseUrl, operatorToken, isAdminToken, false, ct);
                using var rResp = await _http.SendAsync(rReq, ct);
                if (rResp.IsSuccessStatusCode)
                {
                    var rBody = await rResp.Content.ReadAsStringAsync(ct);
                    using var rDoc = JsonDocument.Parse(rBody);
                    if (rDoc.RootElement.TryGetProperty("roles", out var rArr) && rArr.ValueKind == JsonValueKind.Array)
                    {
                        foreach (var el in rArr.EnumerateArray())
                        {
                            roleList.Add(new RoleAgentItem
                            {
                                Role = el.TryGetProperty("role", out var rv) ? rv.GetString() ?? "" : "",
                                Name = el.TryGetProperty("name", out var rn) ? rn.GetString() ?? "" : "",
                                Description = el.TryGetProperty("description", out var rd) ? rd.GetString() ?? "" : "",
                                Enabled = el.TryGetProperty("enabled", out var re) && (re.ValueKind == JsonValueKind.True || (re.ValueKind == JsonValueKind.Number && re.GetInt32() != 0)),
                            });
                        }
                    }
                    if (rDoc.RootElement.TryGetProperty("profiles", out var pArr) && pArr.ValueKind == JsonValueKind.Array)
                    {
                        foreach (var p in pArr.EnumerateArray())
                        {
                            if (p.GetString() is { Length: > 0 } ps) profileList.Add(ps);
                        }
                    }
                }
            }

            return (true, customList, roleList, profileList, $"Loaded {customList.Count} agent(s) bound to '{targetDevice}'.");
        }
        catch (Exception ex)
        {
            return (false, customList, roleList, profileList, ex.Message);
        }
    }

    public async Task<(bool Ok, string Detail)> CreateCustomAgentAsync(
        string controlUrl,
        string operatorToken,
        bool isAdminToken,
        string name,
        string instruction,
        string provider,
        string model,
        int intervalSeconds,
        string deviceId,
        CancellationToken ct = default)
    {
        var baseUrl = NormalizeUrl(controlUrl);
        if (string.IsNullOrWhiteSpace(deviceId))
        {
            return (false, "Cannot create agent: this Windows workstation has no enrolled Device ID.");
        }
        try
        {
            var payload = JsonSerializer.Serialize(new
            {
                name = name.Trim(),
                instruction = instruction.Trim(),
                provider = provider.Trim().ToLowerInvariant(),
                model = string.IsNullOrWhiteSpace(model) ? null : model.Trim(),
                device_ids = new[] { deviceId.Trim() },
                interval_seconds = Math.Clamp(intervalSeconds, 60, 86400),
                enabled = true,
            });
            using var req = new HttpRequestMessage(HttpMethod.Post, $"{baseUrl}/api/agents");
            await AddOperatorHeadersAsync(req, baseUrl, operatorToken, isAdminToken, true, ct);
            req.Content = new StringContent(payload, Encoding.UTF8, "application/json");
            using var resp = await _http.SendAsync(req, ct);
            var body = await resp.Content.ReadAsStringAsync(ct);
            return resp.IsSuccessStatusCode
                ? (true, $"Created custom agent '{name.Trim()}'.")
                : (false, ExtractApiDetail(body, $"HTTP {(int)resp.StatusCode}"));
        }
        catch (Exception ex)
        {
            return (false, ex.Message);
        }
    }

    public async Task<(bool Ok, string Detail)> SetCustomAgentEnabledAsync(
        string controlUrl,
        string operatorToken,
        bool isAdminToken,
        string agentId,
        bool enabled,
        CancellationToken ct = default)
    {
        var baseUrl = NormalizeUrl(controlUrl);
        try
        {
            using var req = new HttpRequestMessage(HttpMethod.Patch, $"{baseUrl}/api/agents/{Uri.EscapeDataString(agentId)}");
            await AddOperatorHeadersAsync(req, baseUrl, operatorToken, isAdminToken, true, ct);
            req.Content = new StringContent(JsonSerializer.Serialize(new { enabled }), Encoding.UTF8, "application/json");
            using var resp = await _http.SendAsync(req, ct);
            var body = await resp.Content.ReadAsStringAsync(ct);
            return resp.IsSuccessStatusCode
                ? (true, $"Agent {agentId} {(enabled ? "enabled" : "paused")}.")
                : (false, ExtractApiDetail(body, $"HTTP {(int)resp.StatusCode}"));
        }
        catch (Exception ex)
        {
            return (false, ex.Message);
        }
    }

    public async Task<(bool Ok, string Detail)> RunCustomAgentNowAsync(
        string controlUrl,
        string operatorToken,
        bool isAdminToken,
        string agentId,
        CancellationToken ct = default)
    {
        var baseUrl = NormalizeUrl(controlUrl);
        try
        {
            using var req = new HttpRequestMessage(HttpMethod.Post, $"{baseUrl}/api/agents/{Uri.EscapeDataString(agentId)}/run");
            await AddOperatorHeadersAsync(req, baseUrl, operatorToken, isAdminToken, true, ct);
            req.Content = new StringContent("{}", Encoding.UTF8, "application/json");
            using var resp = await _http.SendAsync(req, ct);
            var body = await resp.Content.ReadAsStringAsync(ct);
            return resp.IsSuccessStatusCode
                ? (true, $"Queued immediate run for {agentId}.")
                : (false, ExtractApiDetail(body, $"HTTP {(int)resp.StatusCode}"));
        }
        catch (Exception ex)
        {
            return (false, ex.Message);
        }
    }

    public async Task<(bool Ok, string Detail)> SetRoleEnabledAsync(
        string controlUrl,
        string operatorToken,
        bool isAdminToken,
        string roleId,
        bool enabled,
        CancellationToken ct = default)
    {
        var baseUrl = NormalizeUrl(controlUrl);
        try
        {
            using var req = new HttpRequestMessage(HttpMethod.Post, $"{baseUrl}/api/roles/{Uri.EscapeDataString(roleId)}/enable?enabled={(enabled ? "true" : "false")}");
            await AddOperatorHeadersAsync(req, baseUrl, operatorToken, isAdminToken, true, ct);
            req.Content = new StringContent("{}", Encoding.UTF8, "application/json");
            using var resp = await _http.SendAsync(req, ct);
            var body = await resp.Content.ReadAsStringAsync(ct);
            return resp.IsSuccessStatusCode
                ? (true, $"Role {roleId} {(enabled ? "enabled" : "disabled")}.")
                : (false, ExtractApiDetail(body, $"HTTP {(int)resp.StatusCode}"));
        }
        catch (Exception ex)
        {
            return (false, ex.Message);
        }
    }

    public async Task<(bool Ok, string Detail)> DispatchSupervisorHandoffAsync(
        string controlUrl,
        string operatorToken,
        bool isAdminToken,
        string deviceId,
        string goal,
        string profile,
        CancellationToken ct = default)
    {
        var baseUrl = NormalizeUrl(controlUrl);
        try
        {
            var payload = JsonSerializer.Serialize(new
            {
                device_id = deviceId.Trim(),
                goal = goal.Trim(),
                profile = string.IsNullOrWhiteSpace(profile) ? "balanced" : profile.Trim(),
            });
            using var req = new HttpRequestMessage(HttpMethod.Post, $"{baseUrl}/api/supervisor/handoff");
            await AddOperatorHeadersAsync(req, baseUrl, operatorToken, isAdminToken, true, ct);
            req.Content = new StringContent(payload, Encoding.UTF8, "application/json");
            using var resp = await _http.SendAsync(req, ct);
            var body = await resp.Content.ReadAsStringAsync(ct);
            return resp.IsSuccessStatusCode
                ? (true, "Supervisor handoff dispatched.")
                : (false, ExtractApiDetail(body, $"HTTP {(int)resp.StatusCode}"));
        }
        catch (Exception ex)
        {
            return (false, ex.Message);
        }
    }

    public async Task<(bool Ok, string ReplyText, string ToolsSummary, string? ScreenshotBase64, string Detail)> SendDeviceAgentChatAsync(
        string controlUrl,
        string operatorToken,
        bool isAdminToken,
        string localDeviceId,
        System.Collections.Generic.IEnumerable<(string Role, string Content)> messages,
        string? provider = null,
        string? model = null,
        string? apiKey = null,
        string tenant = "default",
        CancellationToken ct = default)
    {
        var baseUrl = NormalizeUrl(controlUrl);
        if (string.IsNullOrWhiteSpace(localDeviceId))
        {
            return (false, "", "", null, "Device ID is not configured.");
        }

        try
        {
            var msgArray = new System.Collections.Generic.List<object>();
            foreach (var (r, c) in messages)
            {
                msgArray.Add(new { role = r, content = c });
            }

            var payload = JsonSerializer.Serialize(new
            {
                messages = msgArray,
                provider = string.IsNullOrWhiteSpace(provider) ? "auto" : provider.Trim(),
                model = string.IsNullOrWhiteSpace(model) ? null : model.Trim(),
                api_key = string.IsNullOrWhiteSpace(apiKey) ? null : apiKey.Trim(),
                device = localDeviceId.Trim(),
                tenant = string.IsNullOrWhiteSpace(tenant) ? "default" : tenant.Trim(),
            });

            using var req = new HttpRequestMessage(HttpMethod.Post, $"{baseUrl}/api/ai/chat");
            await AddOperatorHeadersAsync(req, baseUrl, operatorToken, isAdminToken, true, ct);
            req.Content = new StringContent(payload, Encoding.UTF8, "application/json");
            using var resp = await _http.SendAsync(req, ct);
            var body = await resp.Content.ReadAsStringAsync(ct);

            if (!resp.IsSuccessStatusCode)
            {
                return (false, "", "", null, ExtractApiDetail(body, $"POST /api/ai/chat HTTP {(int)resp.StatusCode}"));
            }

            using var doc = JsonDocument.Parse(body);
            var root = doc.RootElement;
            string reply = "";
            if (root.TryGetProperty("message", out var msgEl) && msgEl.ValueKind == JsonValueKind.Object)
            {
                reply = msgEl.TryGetProperty("content", out var cEl) ? cEl.GetString() ?? "" : "";
            }
            if (string.IsNullOrWhiteSpace(reply) && root.TryGetProperty("response", out var rEl))
            {
                reply = rEl.GetString() ?? "";
            }

            var toolsUsed = new System.Collections.Generic.List<string>();
            if (root.TryGetProperty("steps", out var stepsEl) && stepsEl.ValueKind == JsonValueKind.Array)
            {
                foreach (var st in stepsEl.EnumerateArray())
                {
                    if (st.TryGetProperty("tool", out var tEl) && tEl.GetString() is { Length: > 0 } tName)
                    {
                        toolsUsed.Add(tName);
                    }
                }
            }

            string? screenshot = null;
            if (root.TryGetProperty("screenshot", out var sEl) && sEl.ValueKind == JsonValueKind.String)
            {
                screenshot = sEl.GetString();
            }

            var summary = toolsUsed.Count > 0 ? $"Tools ({localDeviceId}): {string.Join(", ", toolsUsed)}" : "";
            return (true, string.IsNullOrWhiteSpace(reply) ? "Completed." : reply, summary, screenshot, "OK");
        }
        catch (Exception ex)
        {
            return (false, "", "", null, ex.Message);
        }
    }

    public void Dispose()
    {
        _cts?.Cancel();
        _cts?.Dispose();
        _http.Dispose();
        _wakeSignal.Dispose();
    }
}
