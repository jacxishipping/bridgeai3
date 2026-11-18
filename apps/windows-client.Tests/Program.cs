using System;
using System.Collections.Generic;
using System.IO;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using AIBridgeClient.Models;
using AIBridgeClient.Services;

int passed = 0;
int failed = 0;

void Assert(bool condition, string message)
{
    if (!condition)
    {
        throw new InvalidOperationException($"Assertion failed: {message}");
    }
}

async Task RunTest(string name, Func<Task> testFn)
{
    try
    {
        await testFn();
        Console.WriteLine($"[PASS] {name}");
        passed++;
    }
    catch (Exception ex)
    {
        Console.Error.WriteLine($"[FAIL] {name}: {ex.Message}");
        failed++;
    }
}

// 1. DPAPI token sealing & legacy plaintext compatibility
await RunTest("DpapiTokenStore: Seal -> Unseal round-trip", () =>
{
    const string secret = "tok_test_secret_123456";
    var sealedTok = DpapiTokenStore.SealToken(secret);
    Assert(sealedTok.StartsWith(DpapiTokenStore.DpapiMagic, StringComparison.Ordinal), "Expected DPAPI magic prefix");
    Assert(DpapiTokenStore.UnsealToken(sealedTok) == secret, "Unsealed token mismatch");
    Assert(DpapiTokenStore.UnsealToken("legacy_plain_tok") == "legacy_plain_tok", "Legacy plaintext passthrough failed");
    return Task.CompletedTask;
});

// 2. NamedPipeBridge HMAC signing & 64KB limit
await RunTest("NamedPipeBridge: SignEnvelope produces valid JSON + HMAC", () =>
{
    var env = NamedPipeBridge.SignEnvelope("HEARTBEAT_PING", new { pid = 42 }, "secret-key");
    using var doc = JsonDocument.Parse(env);
    Assert(doc.RootElement.TryGetProperty("hmac", out var h) && !string.IsNullOrEmpty(h.GetString()), "Expected non-empty HMAC");
    Assert(doc.RootElement.TryGetProperty("body", out var b) && b.GetString()!.Contains("HEARTBEAT_PING"), "Expected message type in body");
    return Task.CompletedTask;
});

// 3. SystemMonitorService telemetry & process list
await RunTest("SystemMonitorService: Sample & GetTopProcesses", () =>
{
    var mon = new SystemMonitorService();
    var t = mon.Sample();
    Assert(t.MemoryTotalGb > 0, "Expected total memory > 0");
    Assert(t.ProcessCount > 0, "Expected process count > 0");
    var procs = mon.GetTopProcesses(limit: 10);
    Assert(procs.Count > 0 && procs.Count <= 10, "Expected non-empty process list");
    Assert(!mon.TryKillProcess(Environment.ProcessId, out _), "Must refuse killing self process");
    var hw = mon.GetHardwareInfo();
    Assert(hw.LogicalCores > 0 && !string.IsNullOrWhiteSpace(hw.CpuName) && !string.IsNullOrWhiteSpace(hw.GpuName), "Expected valid CPU and GPU hardware info");
    return Task.CompletedTask;
});

// 4. ScreenCaptureService monitor enumeration
await RunTest("ScreenCaptureService: GetMonitors returns >= 1 monitor", () =>
{
    var monitors = ScreenCaptureService.GetMonitors();
    Assert(monitors.Count >= 1, "Expected at least 1 monitor");
    return Task.CompletedTask;
});

// 5. NativeToolExecutor workspace path confinement & secret blocking
await RunTest("NativeToolExecutor: Workspace confinement blocks traversal, absolute, NUL, and .env", async () =>
{
    var tempWs = Path.Combine(Path.GetTempPath(), $"aib-test-ws-{Guid.NewGuid():N}");
    Directory.CreateDirectory(tempWs);
    try
    {
        File.WriteAllText(Path.Combine(tempWs, "hello.txt"), "hello world");
        File.WriteAllText(Path.Combine(tempWs, ".env"), "SECRET=123");

        var cfg = new AgentGuiConfig { Workspace = tempWs };
        var exec = new NativeToolExecutor(new SystemMonitorService(), Path.Combine(tempWs, "shots"));

        var (okRead, resRead) = await exec.ExecuteAsync("read_workspace_file", "{\"path\":\"hello.txt\"}", cfg, CancellationToken.None);
        Assert(okRead && resRead.Contains("hello world"), "Expected valid workspace read to succeed");

        var (okTrav, _) = await exec.ExecuteAsync("read_workspace_file", "{\"path\":\"../secret.txt\"}", cfg, CancellationToken.None);
        Assert(!okTrav, "Expected path traversal to be blocked");

        var (okAbs, _) = await exec.ExecuteAsync("read_workspace_file", "{\"path\":\"C:\\\\Windows\\\\win.ini\"}", cfg, CancellationToken.None);
        Assert(!okAbs, "Expected absolute path to be blocked");

        var (okEnv, _) = await exec.ExecuteAsync("read_workspace_file", "{\"path\":\".env\"}", cfg, CancellationToken.None);
        Assert(!okEnv, "Expected .env secret read to be blocked");
    }
    finally
    {
        Directory.Delete(tempWs, recursive: true);
    }
});

// 6. NativeToolExecutor policy tier enforcement
await RunTest("NativeToolExecutor: Policy tiers enforce AllowWrites, AllowTier3, AllowShell, AllowDesktopControl", async () =>
{
    var tempWs = Path.Combine(Path.GetTempPath(), $"aib-test-pol-{Guid.NewGuid():N}");
    Directory.CreateDirectory(tempWs);
    try
    {
        var cfg = new AgentGuiConfig
        {
            Workspace = tempWs,
            Policy = new AgentPolicyConfig
            {
                AllowWrites = false,
                AllowTier3 = false,
                AllowTier4 = false,
                AllowShell = false,
                AllowDesktopControl = false,
            },
        };
        var exec = new NativeToolExecutor(new SystemMonitorService(), Path.Combine(tempWs, "shots"));

        var (okW, _) = await exec.ExecuteAsync("create_workspace_file", "{\"path\":\"a.txt\",\"content\":\"x\"}", cfg, CancellationToken.None);
        Assert(!okW, "Tier 2 write must fail when AllowWrites=false");

        var (okT3, _) = await exec.ExecuteAsync("capture_desktop_screenshot", "{}", cfg, CancellationToken.None);
        Assert(!okT3, "Tier 3 screenshot must fail when AllowTier3=false");

        var (okShell, _) = await exec.ExecuteAsync("run_shell", "{\"command\":\"echo hi\"}", cfg, CancellationToken.None);
        Assert(!okShell, "Tier 4 shell must fail when AllowShell=false");

        var (okMouse, _) = await exec.ExecuteAsync("mouse_click", "{\"x\":10,\"y\":10}", cfg, CancellationToken.None);
        Assert(!okMouse, "Desktop input must fail when AllowDesktopControl=false");
    }
    finally
    {
        Directory.Delete(tempWs, recursive: true);
    }
});

// 7. Workspace edit, move, and delete tier enforcement + secret blocking
await RunTest("NativeToolExecutor: edit/move/delete obey tiers and confinement", async () =>
{
    var tempWs = Path.Combine(Path.GetTempPath(), $"aib-test-ops-{Guid.NewGuid():N}");
    Directory.CreateDirectory(tempWs);
    try
    {
        var cfg = new AgentGuiConfig
        {
            Workspace = tempWs,
            Policy = new AgentPolicyConfig { AllowWrites = true, AllowTier3 = true, AllowTier4 = false },
        };
        var exec = new NativeToolExecutor(new SystemMonitorService(), Path.Combine(tempWs, "shots"));

        var (okC, _) = await exec.ExecuteAsync("create_workspace_file", "{\"path\":\"note.txt\",\"content\":\"hello beta\"}", cfg, CancellationToken.None);
        Assert(okC, "Expected create_workspace_file to succeed");

        var (okE, _) = await exec.ExecuteAsync("edit_workspace_file", "{\"path\":\"note.txt\",\"old_text\":\"beta\",\"new_text\":\"gamma\"}", cfg, CancellationToken.None);
        Assert(okE && File.ReadAllText(Path.Combine(tempWs, "note.txt")) == "hello gamma", "Expected edit_workspace_file to update text");

        var (okM, _) = await exec.ExecuteAsync("move_workspace_file", "{\"source\":\"note.txt\",\"destination\":\"sub/note2.txt\"}", cfg, CancellationToken.None);
        Assert(okM && File.Exists(Path.Combine(tempWs, "sub", "note2.txt")), "Expected move_workspace_file to move file");

        var (okDelBlocked, _) = await exec.ExecuteAsync("delete_workspace_file", "{\"path\":\"sub/note2.txt\"}", cfg, CancellationToken.None);
        Assert(!okDelBlocked, "Expected delete_workspace_file to fail when AllowTier4=false");

        cfg.Policy.AllowTier4 = true;
        var (okDelCapped, _) = await exec.ExecuteAsync("delete_workspace_file", "{\"path\":\"sub/note2.txt\"}", cfg, CancellationToken.None);
        Assert(!okDelCapped, "Expected delete_workspace_file to fail when MaxTierUnattended=3");

        cfg.Policy.MaxTierUnattended = 4;
        var (okDel, _) = await exec.ExecuteAsync("delete_workspace_file", "{\"path\":\"sub/note2.txt\"}", cfg, CancellationToken.None);
        Assert(okDel && !File.Exists(Path.Combine(tempWs, "sub", "note2.txt")), "Expected delete_workspace_file to succeed when AllowTier4=true and MaxTierUnattended=4");
    }
    finally
    {
        Directory.Delete(tempWs, recursive: true);
    }
});

// 8. Windows OS diagnostics, screen bounds, window enumeration & service query validation
await RunTest("Windows native tools: system_info, screen_size, windows, service_status, hotkey parsing", async () =>
{
    var cfg = new AgentGuiConfig { Workspace = Path.GetTempPath() };
    var exec = new NativeToolExecutor(new SystemMonitorService(), Path.GetTempPath());

    var (okSys, sysJson) = await exec.ExecuteAsync("windows_system_info", "{}", cfg, CancellationToken.None);
    Assert(okSys && sysJson.Contains("windows_edition") && sysJson.Contains("windows_build"), "Expected Windows edition & build in windows_system_info");

    var (okScr, scrJson) = await exec.ExecuteAsync("get_screen_size", "{}", cfg, CancellationToken.None);
    Assert(okScr && scrJson.Contains("primary_width") && scrJson.Contains("virtual_width"), "Expected screen metrics");

    var (okWin, winJson) = await exec.ExecuteAsync("list_desktop_windows", "{\"limit\":15}", cfg, CancellationToken.None);
    Assert(okWin && winJson.Contains("windows"), "Expected list_desktop_windows to succeed");

    var (okSvcBad, _) = await exec.ExecuteAsync("windows_service_status", "{\"service\":\"bad;whoami\"}", cfg, CancellationToken.None);
    Assert(!okSvcBad, "Expected invalid service name with shell characters to be rejected");

    var (okSvc, svcJson) = await exec.ExecuteAsync("windows_service_status", "{\"service\":\"EventLog\"}", cfg, CancellationToken.None);
    Assert(okSvc && svcJson.Contains("EventLog"), "Expected EventLog service status query to succeed");

    Assert(DesktopInputService.ResolveVirtualKey("ctrl") == 0x11, "VK ctrl mismatch");
    Assert(DesktopInputService.ResolveVirtualKey("f5") == 0x74, "VK F5 mismatch");
    Assert(DesktopInputService.ResolveVirtualKey("a") == (ushort)'A', "VK A mismatch");
});

// 9. AllowedApps enforcement & Clipboard gating
await RunTest("NativeToolExecutor: AllowedApps blocks unapproved executables & clipboard gating", async () =>
{
    var cfg = new AgentGuiConfig
    {
        Workspace = Path.GetTempPath(),
        Policy = new AgentPolicyConfig { AllowTier3 = true, AllowDesktopControl = true },
    };
    var exec = new NativeToolExecutor(new SystemMonitorService(), Path.GetTempPath());

    var (okBadApp, _) = await exec.ExecuteAsync("launch_approved_application", "{\"app\":\"powershell.exe\"}", cfg, CancellationToken.None);
    Assert(!okBadApp, "Unapproved app must be blocked by AllowedApps matrix");

    var (okPathApp, _) = await exec.ExecuteAsync("launch_approved_application", "{\"app\":\"C:\\\\Windows\\\\notepad.exe\"}", cfg, CancellationToken.None);
    Assert(!okPathApp, "Path-qualified app name must be rejected");

    cfg.Policy.AllowDesktopControl = false;
    var (okClipBlocked, _) = await exec.ExecuteAsync("set_clipboard", "{\"text\":\"test\"}", cfg, CancellationToken.None);
    Assert(!okClipBlocked, "set_clipboard must fail when AllowDesktopControl=false");

    var (okClipRead, _) = await exec.ExecuteAsync("get_clipboard", "{}", cfg, CancellationToken.None);
    Assert(okClipRead, "get_clipboard read-only tool should succeed");
});

// 10. Diagnostics JSON report generation
await RunTest("DiagnosticsService: BuildDiagnosticsJson produces redacted report", () =>
{
    var mon = new SystemMonitorService();
    var cfg = new AgentGuiConfig { ControlUrl = "https://example.test", DeviceId = "win-dev", Workspace = Path.GetTempPath() };
    var json = DiagnosticsService.BuildDiagnosticsJson(cfg, mon, hasToken: true, taskCount: 3, connectionState: "Connected");
    using var doc = JsonDocument.Parse(json);
    Assert(doc.RootElement.TryGetProperty("windows", out _), "Expected windows section in diagnostics");
    Assert(doc.RootElement.TryGetProperty("desktop_pipe", out _), "Expected desktop_pipe section in diagnostics");
    return Task.CompletedTask;
});

// 11. Extended Settings & Policy enforcement (MaxTierUnattended, AllowClipboard, AllowedCommands, MaxReadBytes)
await RunTest("Extended settings: MaxTierUnattended, AllowClipboard, AllowedCommands & MaxReadBytes", async () =>
{
    var tempWs = Path.Combine(Path.GetTempPath(), $"aib-test-ext-{Guid.NewGuid():N}");
    Directory.CreateDirectory(tempWs);
    try
    {
        File.WriteAllText(Path.Combine(tempWs, "big.txt"), new string('A', 2000));
        var cfg = new AgentGuiConfig
        {
            Workspace = tempWs,
            MaxReadBytes = 1024,
            Policy = new AgentPolicyConfig
            {
                AllowWrites = true,
                AllowTier3 = true,
                AllowTier4 = true,
                AllowShell = true,
                AllowClipboard = false,
                MaxTierUnattended = 2,
                AllowedCommands = new List<string> { "echo" },
            },
        };
        var exec = new NativeToolExecutor(new SystemMonitorService(), Path.Combine(tempWs, "shots"));

        var (okBig, bigJson) = await exec.ExecuteAsync("read_workspace_file", "{\"path\":\"big.txt\"}", cfg, CancellationToken.None);
        Assert(okBig && bigJson.Contains("...[truncated]"), "MaxReadBytes must truncate large workspace reads");

        var (okClip, _) = await exec.ExecuteAsync("get_clipboard", "{}", cfg, CancellationToken.None);
        Assert(!okClip, "get_clipboard must fail when AllowClipboard=false");

        var (okTierCap, _) = await exec.ExecuteAsync("capture_desktop_screenshot", "{}", cfg, CancellationToken.None);
        Assert(!okTierCap, "Tier 3 tool must fail when MaxTierUnattended=2");

        cfg.Policy.MaxTierUnattended = 4;
        var (okBadCmd, _) = await exec.ExecuteAsync("run_shell", "{\"command\":\"whoami\"}", cfg, CancellationToken.None);
        Assert(!okBadCmd, "run_shell must block commands outside AllowedCommands");
    }
    finally
    {
        Directory.Delete(tempWs, recursive: true);
    }
});

// 12. ControlPlaneClient protocol alignment (login->enroll, policy wrapper, token rotate, screenshot base64_data)
await RunTest("ControlPlaneClient: Auth login->enroll, policy schema & token rotation", async () =>
{
    var listenerTcp = new System.Net.Sockets.TcpListener(System.Net.IPAddress.Loopback, 0);
    listenerTcp.Start();
    int port = ((System.Net.IPEndPoint)listenerTcp.LocalEndpoint).Port;
    listenerTcp.Stop();

    var baseUrl = $"http://127.0.0.1:{port}";
    using var listener = new System.Net.HttpListener();
    listener.Prefixes.Add(baseUrl + "/");
    listener.Start();

    string? capturedPutBody = null;
    using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(10));
    var serverTask = Task.Run(async () =>
    {
        while (!cts.IsCancellationRequested)
        {
            System.Net.HttpListenerContext ctx;
            try { ctx = await listener.GetContextAsync(); } catch { break; }
            string path = ctx.Request.Url?.AbsolutePath ?? "";
            string reqBody = "";
            using (var sr = new StreamReader(ctx.Request.InputStream)) reqBody = await sr.ReadToEndAsync();

            string respJson = "{}";
            if (path == "/api/auth/login")
            {
                respJson = "{\"session_token\":\"sess_123\",\"username\":\"alice\",\"role\":\"admin\",\"tenant\":\"acme\"}";
            }
            else if (path == "/api/devices/enroll")
            {
                respJson = "{\"device_id\":\"dev_abc\",\"device_token\":\"dtok_111\"}";
            }
            else if (path == "/api/agent/rotate")
            {
                respJson = "{\"device_id\":\"dev_abc\",\"device_token\":\"dtok_222\"}";
            }
            else if (path == "/api/agent/policy" && ctx.Request.HttpMethod == "PUT")
            {
                capturedPutBody = reqBody;
                respJson = "{\"saved\":true}";
            }
            else if (path == "/api/agent/policy" && ctx.Request.HttpMethod == "GET")
            {
                respJson = "{\"device_id\":\"dev_abc\",\"effective_policy\":{\"allow_writes\":true,\"allow_tier3\":true,\"desktop_enabled\":true,\"allow_shell\":false,\"max_tier_unattended\":3,\"allowed_apps\":[\"notepad.exe\"]}}";
            }
            var bytes = System.Text.Encoding.UTF8.GetBytes(respJson);
            ctx.Response.ContentType = "application/json";
            ctx.Response.ContentLength64 = bytes.Length;
            await ctx.Response.OutputStream.WriteAsync(bytes);
            ctx.Response.Close();
        }
    });

    using var client = new ControlPlaneClient(new SystemMonitorService(), new NativeToolExecutor(new SystemMonitorService(), Path.GetTempPath()));
    var (okLogin, dtok, did, ten, user, _) = await client.LoginAndEnrollAsync(baseUrl, "alice", "pass123", "my-win");
    Assert(okLogin && dtok == "dtok_111" && did == "dev_abc" && ten == "acme" && user == "alice", "LoginAndEnrollAsync failed");

    var (okRot, newTok, _) = await client.RotateTokenAsync(baseUrl, dtok);
    Assert(okRot && newTok == "dtok_222", "RotateTokenAsync must read device_token");

    var (okSync, _) = await client.SyncPolicyAsync(baseUrl, newTok, new AgentPolicyConfig { AllowDesktopControl = true, AllowTier4 = true });
    Assert(okSync && capturedPutBody is not null && capturedPutBody.Contains("\"desktop_enabled\":true") && capturedPutBody.Contains("\"allow_file_deletion\":true"), "SyncPolicyAsync must serialize desktop_enabled and allow_file_deletion");

    var (okFetch, fetched, _) = await client.FetchPolicyAsync(baseUrl, newTok);
    Assert(okFetch && fetched is not null && fetched.AllowDesktopControl && !fetched.AllowShell && fetched.AllowedApps.Contains("notepad.exe"), "FetchPolicyAsync must unwrap effective_policy");

    listener.Stop();
    await cts.CancelAsync();
    try { await serverTask; } catch { }
});

// 13. Full Windows UI tool parity, aliases, virtual keys, and policy gating
await RunTest("Full Windows UI tools: window/UI aliases, virtual keys & desktop gating", async () =>
{
    var exec = new NativeToolExecutor(new SystemMonitorService(), Path.GetTempPath());
    var roCfg = new AgentGuiConfig
    {
        Workspace = Path.GetTempPath(),
        Policy = new AgentPolicyConfig { AllowDesktopControl = false },
    };

    var (okWinAlias, winAliasJson) = await exec.ExecuteAsync("desktop_get_windows", "{\"limit\":10}", roCfg, CancellationToken.None);
    Assert(okWinAlias && winAliasJson.Contains("windows"), "Expected desktop_get_windows alias to succeed");

    var (okActive, _) = await exec.ExecuteAsync("get_active_window", "{}", roCfg, CancellationToken.None);
    Assert(okActive, "Expected get_active_window to succeed");

    var (okUi, uiJson) = await exec.ExecuteAsync("desktop_get_ui_elements", "{\"limit\":15}", roCfg, CancellationToken.None);
    Assert(okUi && uiJson.Contains("elements"), "Expected desktop_get_ui_elements to succeed");

    var (okWait, waitJson) = await exec.ExecuteAsync("desktop_wait", "{\"ms\":50}", roCfg, CancellationToken.None);
    Assert(okWait && waitJson.Contains("waited"), "Expected desktop_wait to succeed");

    var (okVerify, verifyJson) = await exec.ExecuteAsync("desktop_verify_action", "{\"text\":\"Program Manager\"}", roCfg, CancellationToken.None);
    Assert(okVerify && verifyJson.Contains("verified"), "Expected desktop_verify_action to succeed");

    var (okMaxBlocked, _) = await exec.ExecuteAsync("maximize_window", "{\"title\":\"NonExistent\"}", roCfg, CancellationToken.None);
    Assert(!okMaxBlocked, "maximize_window must block when AllowDesktopControl=false");

    var (okSetBlocked, _) = await exec.ExecuteAsync("set_element_value", "{\"name\":\"X\",\"value\":\"Y\"}", roCfg, CancellationToken.None);
    Assert(!okSetBlocked, "set_element_value must block when AllowDesktopControl=false");

    Assert(DesktopInputService.ResolveVirtualKey("apps") == 0x5D, "VK apps mismatch");
    Assert(DesktopInputService.ResolveVirtualKey("volumeup") == 0xAF, "VK volumeup mismatch");
    Assert(DesktopInputService.ResolveVirtualKey("rctrl") == 0xA3, "VK rctrl mismatch");
});

// 14. Full PC Access opt-in gating: blocked when false, allowed across PC when true
await RunTest("Full PC Access gating: drives, directories, PowerShell & kill_process require explicit opt-in", async () =>
{
    var tempRoot = Path.Combine(Path.GetTempPath(), $"aib-test-fullpc-{Guid.NewGuid():N}");
    var tempWs = Path.Combine(tempRoot, "ws");
    var outsideFile = Path.Combine(tempRoot, "outside.txt");
    Directory.CreateDirectory(tempWs);
    File.WriteAllText(outsideFile, "outside pc data");
    try
    {
        var exec = new NativeToolExecutor(new SystemMonitorService(), tempWs);
        var restrictedCfg = new AgentGuiConfig
        {
            Workspace = tempWs,
            Policy = new AgentPolicyConfig
            {
                AllowFullPcAccess = false,
                AllowWrites = true,
                AllowTier3 = true,
                AllowTier4 = true,
                AllowShell = true,
                MaxTierUnattended = 4,
            },
        };

        var (okDrvDenied, _) = await exec.ExecuteAsync("list_drives", "{}", restrictedCfg, CancellationToken.None);
        Assert(!okDrvDenied, "list_drives must fail unless AllowFullPcAccess=true");

        var (okDirDenied, _) = await exec.ExecuteAsync("list_directory", JsonSerializer.Serialize(new { path = tempRoot }), restrictedCfg, CancellationToken.None);
        Assert(!okDirDenied, "list_directory must fail unless AllowFullPcAccess=true");

        var (okReadDenied, _) = await exec.ExecuteAsync("read_pc_file", JsonSerializer.Serialize(new { path = outsideFile }), restrictedCfg, CancellationToken.None);
        Assert(!okReadDenied, "read_pc_file must fail unless AllowFullPcAccess=true");

        var (okPsDenied, _) = await exec.ExecuteAsync("run_powershell", "{\"command\":\"Write-Output hi\"}", restrictedCfg, CancellationToken.None);
        Assert(!okPsDenied, "run_powershell must fail unless AllowFullPcAccess=true");

        restrictedCfg.Policy.AllowFullPcAccess = true;

        var (okDrv, drvJson) = await exec.ExecuteAsync("list_drives", "{}", restrictedCfg, CancellationToken.None);
        Assert(okDrv && drvJson.Contains("drives"), "list_drives must succeed when AllowFullPcAccess=true");

        var (okDir, dirJson) = await exec.ExecuteAsync("list_directory", JsonSerializer.Serialize(new { path = tempRoot }), restrictedCfg, CancellationToken.None);
        Assert(okDir && dirJson.Contains("outside.txt"), "list_directory must list outside folder when AllowFullPcAccess=true");

        var (okRead, readJson) = await exec.ExecuteAsync("read_pc_file", JsonSerializer.Serialize(new { path = outsideFile }), restrictedCfg, CancellationToken.None);
        Assert(okRead && readJson.Contains("outside pc data"), "read_pc_file must read outside file when AllowFullPcAccess=true");

        var (okKillSelf, _) = await exec.ExecuteAsync("kill_process", JsonSerializer.Serialize(new { pid = Environment.ProcessId }), restrictedCfg, CancellationToken.None);
        Assert(!okKillSelf, "kill_process must refuse terminating active client self PID even when AllowFullPcAccess=true");
    }
    finally
    {
        Directory.Delete(tempRoot, recursive: true);
    }
});

// 15. Per-agent settings: AI API connection config, system prompt, instruction name & function allow/disallow
await RunTest("DeviceAgentRuntimeService: system prompt, instruction name & function allow/disallow", () =>
{
    var cfg = new AgentGuiConfig
    {
        DeviceId = "win-dev-local",
        DeviceAgent = new DeviceAgentSettings
        {
            AgentName = "SysAdmin Bot",
            InstructionName = "Safe Read-Only Profile",
            SystemPrompt = "Never restart services unless asked twice.",
            Provider = "anthropic",
            Model = "claude-3-5-sonnet-latest",
            Functions = new AgentFunctionPermissions
            {
                AllowSystemHealthTools = true,
                AllowWindowManagement = true,
                AllowShellAndPowerShell = false,
                AllowProcessTermination = false,
                DisabledTools = new List<string> { "close_window" },
            },
        },
    };

    Assert(DeviceAgentRuntimeService.IsToolAllowed("get_system_info", cfg, out _), "Expected get_system_info to be allowed");
    Assert(!DeviceAgentRuntimeService.IsToolAllowed("run_shell", cfg, out _), "Expected run_shell to be disallowed when AllowShellAndPowerShell=false");
    Assert(!DeviceAgentRuntimeService.IsToolAllowed("kill_process", cfg, out _), "Expected kill_process to be disallowed when AllowProcessTermination=false");
    Assert(!DeviceAgentRuntimeService.IsToolAllowed("close_window", cfg, out _), "Expected close_window to be disallowed via DisabledTools");

    var enabledNames = DeviceAgentRuntimeService.GetEnabledTools(cfg).Select(t => t.Name).ToHashSet();
    Assert(enabledNames.Contains("get_system_info"), "Expected enabled tools to contain get_system_info");
    Assert(!enabledNames.Contains("close_window") && !enabledNames.Contains("run_powershell"), "Expected disabled tools excluded from schema list");

    var prompt = DeviceAgentRuntimeService.BuildEffectiveSystemPrompt(cfg);
    Assert(prompt.Contains("SysAdmin Bot") && prompt.Contains("Safe Read-Only Profile") && prompt.Contains("Never restart services unless asked twice."), "Effective system prompt must include name, instruction name & custom system prompt");
    Assert(DeviceAgentRuntimeService.GetDefaultBaseUrl("gemini").Contains("generativelanguage.googleapis.com"), "Default Gemini base URL mismatch");
    return Task.CompletedTask;
});

Console.WriteLine($"\nSummary: {passed} passed, {failed} failed ({passed + failed} total)");
return failed > 0 ? 1 : 0;
