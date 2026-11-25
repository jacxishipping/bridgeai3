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
            Policy = new AgentPolicyConfig { AllowWrites = true, AllowTier3 = true, AllowTier4 = false, MaxTierUnattended = 3 },
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
        Policy = new AgentPolicyConfig { AllowTier3 = true, MaxTierUnattended = 3, AllowDesktopControl = true },
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

        // Safe defaults & tier checks (Release 1 security review):
        var defaultPol = new AgentPolicyConfig();
        Assert(!defaultPol.AllowTier3 && defaultPol.MaxTierUnattended == 2, "Default AgentPolicyConfig must keep Tier 3 off and MaxTierUnattended=2");
        var defaultFns = new AgentFunctionPermissions();
        Assert(!defaultFns.AllowFullPcFilesAndDrives && !defaultFns.AllowShellAndPowerShell, "Dangerous function groups must default off");

        restrictedCfg.Policy.MaxTierUnattended = 2;
        var (okDirTierBlocked, _) = await exec.ExecuteAsync("list_directory", JsonSerializer.Serialize(new { path = tempRoot }), restrictedCfg, CancellationToken.None);
        Assert(!okDirTierBlocked, "list_directory must require Tier 3");

        restrictedCfg.Policy.MaxTierUnattended = 3;
        var outsideWrite = Path.Combine(tempRoot, "written.txt");
        var (okWriteTierBlocked, _) = await exec.ExecuteAsync("write_pc_file", JsonSerializer.Serialize(new { path = outsideWrite, content = "hi" }), restrictedCfg, CancellationToken.None);
        Assert(!okWriteTierBlocked, "write_pc_file must require Tier 4");

        restrictedCfg.Policy.MaxTierUnattended = 4;
        var (okWriteOk, _) = await exec.ExecuteAsync("write_pc_file", JsonSerializer.Serialize(new { path = outsideWrite, content = "hi" }), restrictedCfg, CancellationToken.None);
        Assert(okWriteOk && File.ReadAllText(outsideWrite) == "hi", "write_pc_file must succeed when Full PC Access + Tier 4 enabled");

        // Workspace tools must remain strictly confined to workspace even when AllowFullPcAccess=true.
        var (okWsEscapeRead, _) = await exec.ExecuteAsync("read_workspace_file", JsonSerializer.Serialize(new { path = outsideFile }), restrictedCfg, CancellationToken.None);
        Assert(!okWsEscapeRead, "read_workspace_file must stay confined to workspace even with AllowFullPcAccess=true");
        var (okWsEscapeWrite, _) = await exec.ExecuteAsync("create_workspace_file", JsonSerializer.Serialize(new { path = outsideWrite, content = "bypass" }), restrictedCfg, CancellationToken.None);
        Assert(!okWsEscapeWrite, "create_workspace_file must stay confined to workspace even with AllowFullPcAccess=true");

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

// 16. C++20 Native Agent Wire Protocol, CppAgentSettings & NativeToolExecutor C++ tools
await RunTest("C++20 Native Agent: 5-line HMAC wire round-trip, CppAgentSettings & tool integration", async () =>
{
    var wire = NamedPipeBridge.BuildCppWireMessage("HEARTBEAT_PING", new { client = "test" }, "test-hmac-secret");
    Assert(NamedPipeBridge.TryParseCppWireMessage(wire, "test-hmac-secret", out var msgType, out var payloadJson), "Expected C++ 5-line wire message to verify");
    Assert(msgType == "HEARTBEAT_PING" && payloadJson.Contains("test"), "Parsed wire message mismatch");
    Assert(!NamedPipeBridge.TryParseCppWireMessage(wire, "wrong-secret", out _, out _), "Expected tampered secret to fail verification");

    var cfg = new AgentGuiConfig();
    Assert(cfg.CppAgent.AutoStartSupervisor && cfg.CppAgent.PreferDxgiCapture && cfg.CppAgent.H264TargetFps == 30 && cfg.CppAgent.H264BitrateKbps == 4500, "Expected default CppAgentSettings values");

    var exec = new NativeToolExecutor(new SystemMonitorService(), Path.GetTempPath());
    var (okStatus, statusJson) = await exec.ExecuteAsync("get_desktop_status", "{}", cfg, CancellationToken.None);
    Assert(okStatus && statusJson.Contains("cpp_native_dll_loaded") && statusJson.Contains("cpp_capture_metrics"), "Expected get_desktop_status to include C++ metrics");

    var (okBr, brJson) = await exec.ExecuteAsync("set_h264_bitrate", "{\"bitrate_kbps\":6000}", cfg, CancellationToken.None);
    Assert(okBr && brJson.Contains("6000") && cfg.CppAgent.H264BitrateKbps == 6000, "Expected set_h264_bitrate to update target bitrate");
});

// 17. Agent Screen Takeover input lock, border glow overlay & Take Over state machine
await RunTest("AgentTakeoverService: AgentControlling -> TakeOver blocks desktop tools -> ReturnControl resumes", async () =>
{
    var tcs = new TaskCompletionSource<bool>();
    var sta = new Thread(() =>
    {
        try
        {
            var app = System.Windows.Application.Current ?? new System.Windows.Application();
            AgentTakeoverService.Enabled = true;
            AgentTakeoverService.StartAgentTakeover();
            System.Windows.Threading.Dispatcher.CurrentDispatcher.Invoke(() => { }, System.Windows.Threading.DispatcherPriority.Background);
            Assert(AgentTakeoverService.State == TakeoverState.AgentControlling, $"Expected AgentControlling, got {AgentTakeoverService.State}");

            AgentTakeoverService.TakeOver();
            System.Windows.Threading.Dispatcher.CurrentDispatcher.Invoke(() => { }, System.Windows.Threading.DispatcherPriority.Background);
            Assert(AgentTakeoverService.State == TakeoverState.UserControl && AgentTakeoverService.IsUserInControl, "Expected UserControl after TakeOver()");

            var exec = new NativeToolExecutor(new SystemMonitorService(), Path.GetTempPath());
            var cfg = new AgentGuiConfig
            {
                Workspace = Path.GetTempPath(),
                Policy = new AgentPolicyConfig { AllowTier3 = true, MaxTierUnattended = 3, AllowDesktopControl = true },
            };
            var (okWhileUserControls, resBlocked) = exec.ExecuteAsync("mouse_move", "{\"x\":100,\"y\":100}", cfg, CancellationToken.None).GetAwaiter().GetResult();
            Assert(!okWhileUserControls && resBlocked.Contains("Human takeover is active"), $"Expected agent mouse_move to be blocked during UserControl, got: {resBlocked}");

            AgentTakeoverService.ReturnControlToAgent();
            System.Windows.Threading.Dispatcher.CurrentDispatcher.Invoke(() => { }, System.Windows.Threading.DispatcherPriority.Background);
            Assert(AgentTakeoverService.State == TakeoverState.AgentControlling && !AgentTakeoverService.IsUserInControl, "Expected AgentControlling after ReturnControlToAgent()");

            AgentTakeoverService.Shutdown();
            Assert(AgentTakeoverService.State == TakeoverState.Idle, "Expected Idle after Shutdown()");
            tcs.SetResult(true);
        }
        catch (Exception ex)
        {
            AgentTakeoverService.Shutdown();
            tcs.SetException(ex);
        }
    });
    sta.SetApartmentState(ApartmentState.STA);
    sta.Start();
    await tcs.Task;
});

// 18. DeviceAgentRuntimeService text <tool_call> fallback parser & drawing tools in catalog
await RunTest("DeviceAgentRuntimeService: text <tool_call> fallback & drawing tools (mouse_draw_path, mouse_drag)", () =>
{
    var cfg = new AgentGuiConfig();
    var enabled = DeviceAgentRuntimeService.GetEnabledTools(cfg).Select(t => t.Name).ToHashSet();
    Assert(enabled.Contains("mouse_draw_path") && enabled.Contains("mouse_drag") && enabled.Contains("mouse_move") && enabled.Contains("desktop_wait"),
        "Expected mouse_draw_path, mouse_drag, mouse_move, and desktop_wait in enabled tools");

    var calls = DeviceAgentRuntimeService.ParseTextToolCalls(
        "Opening Paint now.\n<tool_call>{\"name\":\"desktop_open_application\",\"arguments\":{\"app\":\"mspaint.exe\"}}</tool_call>\n" +
        "<tool_call>{\"name\":\"mouse_draw_path\",\"arguments\":{\"strokes\":[[[300,400],[600,400]]]}}</tool_call>");
    Assert(calls.Count == 2 && calls[0].Name == "desktop_open_application" && calls[1].Name == "mouse_draw_path",
        $"Expected 2 parsed tool calls, got {calls.Count}");
    return Task.CompletedTask;
});

// 19. Vision-in-the-Loop JPEG compression & multimodal toggle
await RunTest("DeviceAgentRuntimeService: Vision-in-the-Loop compresses PNG screenshots to compact JPEG", () =>
{
    var cfg = new AgentGuiConfig();
    Assert(cfg.DeviceAgent.EnableVisionInLoop, "EnableVisionInLoop should default to true");

    using var bmp = new System.Drawing.Bitmap(1600, 900);
    using (var g = System.Drawing.Graphics.FromImage(bmp))
    {
        g.Clear(System.Drawing.Color.CornflowerBlue);
        g.FillEllipse(System.Drawing.Brushes.Yellow, 200, 200, 300, 300);
    }
    using var ms = new MemoryStream();
    bmp.Save(ms, System.Drawing.Imaging.ImageFormat.Png);
    var pngB64 = Convert.ToBase64String(ms.ToArray());

    var jpegB64 = DeviceAgentRuntimeService.CompressBase64PngToJpegForVision(pngB64, maxWidth: 1280, jpegQuality: 75);
    Assert(!string.IsNullOrEmpty(jpegB64), "Expected non-empty JPEG base64");
    var jpegBytes = Convert.FromBase64String(jpegB64!);
    Assert(jpegBytes.Length > 100 && jpegBytes[0] == 0xFF && jpegBytes[1] == 0xD8, "Expected valid JPEG magic bytes (FF D8)");
    return Task.CompletedTask;
});

// 20. Live HUD action label & RunChatAsync cancellation
await RunTest("AgentTakeoverService & DeviceAgentRuntimeService: Live HUD action label & cancellation", () =>
{
    AgentTakeoverService.UpdateActionLabel("Drawing car wheels");
    var rt = new DeviceAgentRuntimeService(new NativeToolExecutor(new SystemMonitorService(), Path.GetTempPath()));
    using var cts = new CancellationTokenSource();
    cts.Cancel();
    var res = rt.RunChatAsync(new AgentGuiConfig { DeviceAgent = new DeviceAgentSettings { Provider = "openai" } }, "", new[] { ("user", "hi") }, ct: cts.Token).GetAwaiter().GetResult();
    Assert(!res.Ok, "Expected missing API key / cancelled run to fail cleanly");
    return Task.CompletedTask;
});

// 21. Native Offline Windows OCR (ocr_screen & click_text_ocr)
await RunTest("OcrService & NativeToolExecutor: ocr_screen returns structured text and click_text_ocr obeys policy", async () =>
{
    var exec = new NativeToolExecutor(new SystemMonitorService(), Path.GetTempPath());
    var cfg = new AgentGuiConfig
    {
        Workspace = Path.GetTempPath(),
        Policy = new AgentPolicyConfig { AllowTier3 = true, MaxTierUnattended = 3, AllowDesktopControl = false },
    };
    var (okOcr, ocrJson) = await exec.ExecuteAsync("ocr_screen", "{}", cfg, CancellationToken.None);
    Assert(okOcr && ocrJson.Contains("engine") && ocrJson.Contains("words"), $"Expected ocr_screen to succeed, got: {ocrJson}");

    var (okClickDenied, _) = await exec.ExecuteAsync("click_text_ocr", "{\"text\":\"Start\"}", cfg, CancellationToken.None);
    Assert(!okClickDenied, "Expected click_text_ocr to be denied when AllowDesktopControl=false");

    var enabled = DeviceAgentRuntimeService.GetEnabledTools(cfg).Select(t => t.Name).ToHashSet();
    Assert(enabled.Contains("ocr_screen") && enabled.Contains("click_text_ocr"), "Expected OCR tools in agent catalog");
});

// 22. Global Quick-Command Overlay (Ctrl+Shift+Space)
await RunTest("QuickCommandOverlayWindow: instantiates and dispatches submitted command", async () =>
{
    var tcs = new TaskCompletionSource<string>();
    var sta = new Thread(() =>
    {
        try
        {
            _ = System.Windows.Application.Current ?? new System.Windows.Application();
            var win = new AIBridgeClient.Views.QuickCommandOverlayWindow(cmd => tcs.TrySetResult(cmd));
            win.SubmitCommandForTest("draw a car in Paint");
            win.Close();
        }
        catch (Exception ex)
        {
            tcs.TrySetException(ex);
        }
    });
    sta.SetApartmentState(ApartmentState.STA);
    sta.Start();
    var received = await tcs.Task;
    Assert(received == "draw a car in Paint", $"Expected submitted command, got '{received}'");
});

// 23. C++20 WebRTC H.264 Stream CLI argument builder & lifecycle
await RunTest("NamedPipeBridge: BuildCppWebRtcArguments formats monitor, fps, bitrate & session", () =>
{
    var args = NamedPipeBridge.BuildCppWebRtcArguments("https://clideos.us", "tok_123", "sess_abc", 1, 60, 8000);
    Assert(args.Contains("--monitor 1") && args.Contains("--fps 60") && args.Contains("--bitrate-kbps 8000") && args.Contains("--session-id \"sess_abc\""),
        $"Unexpected WebRTC args: {args}");
    NamedPipeBridge.StopCppWebRtcSession();
    Assert(!NamedPipeBridge.IsCppWebRtcRunning, "Expected IsCppWebRtcRunning to be false after stop");
    return Task.CompletedTask;
});

// 24. Human-like input speed profiles & Catmull-Rom curve smoothing
await RunTest("DesktopInputService: Speed profiles and Catmull-Rom spline smoothing", () =>
{
    DesktopInputService.SetSpeedProfile("fast");
    Assert(DesktopInputService.SpeedMultiplier < 0.7, "Expected fast speed multiplier < 0.7");
    DesktopInputService.SetSpeedProfile("careful");
    Assert(DesktopInputService.SpeedMultiplier > 1.3, "Expected careful speed multiplier > 1.3");
    DesktopInputService.SetSpeedProfile("natural");
    Assert(Math.Abs(DesktopInputService.SpeedMultiplier - 1.0) < 0.001, "Expected natural speed multiplier == 1.0");

    var raw = new List<(int X, int Y)> { (100, 100), (200, 140), (300, 110), (400, 160) };
    var smoothed = DesktopInputService.SmoothStrokeCatmullRom(raw, 4);
    Assert(smoothed.Count == 13 && smoothed[0] == (100, 100) && smoothed[^1] == (400, 160),
        $"Expected 13 smoothed points preserving endpoints, got {smoothed.Count}");
    return Task.CompletedTask;
});

// 25. Action Timeline & One-Click Undo for workspace file edits
await RunTest("AgentActionTimelineService: records file edit and restores previous content on Undo", async () =>
{
    var tempWs = Path.Combine(Path.GetTempPath(), $"aib-test-undo-{Guid.NewGuid():N}");
    Directory.CreateDirectory(tempWs);
    try
    {
        AgentActionTimelineService.Clear();
        var filePath = Path.Combine(tempWs, "notes.txt");
        File.WriteAllText(filePath, "original content");

        var cfg = new AgentGuiConfig
        {
            Workspace = tempWs,
            Policy = new AgentPolicyConfig { AllowWrites = true, AllowTier3 = true, MaxTierUnattended = 3 },
        };
        var exec = new NativeToolExecutor(new SystemMonitorService(), Path.Combine(tempWs, "shots"));
        var (okEdit, _) = await exec.ExecuteAsync("edit_workspace_file", "{\"path\":\"notes.txt\",\"old_text\":\"original\",\"new_text\":\"modified\"}", cfg, CancellationToken.None);
        Assert(okEdit && File.ReadAllText(filePath) == "modified content", "Expected edit_workspace_file to modify file");

        var recent = AgentActionTimelineService.GetRecent(10);
        Assert(recent.Count >= 1 && recent[0].ToolName == "edit_workspace_file", "Expected edit_workspace_file in action timeline");

        var (okUndo, undoMsg) = AgentActionTimelineService.UndoLastAction();
        Assert(okUndo && File.ReadAllText(filePath) == "original content", $"Expected Undo to restore original content ({undoMsg})");
    }
    finally
    {
        Directory.Delete(tempWs, recursive: true);
    }
});

// 26. Rich Chat Tool Cards & AgentChatMessage visibility bindings
await RunTest("AgentChatMessage: collapsible ToolDetails visibility & copy bindings", () =>
{
    var msg = new AgentChatMessage
    {
        Role = "assistant",
        Content = "Drew a car in Paint.",
        ToolsSummary = "Windows Copilot · Tools: desktop_open_application, mouse_draw_path",
        ToolDetails = "▶ Executing desktop_open_application...\n✓ desktop_open_application completed",
    };
    Assert(msg.AssistantBubbleVisibility == System.Windows.Visibility.Visible, "Expected assistant bubble visible");
    Assert(msg.ToolsVisibility == System.Windows.Visibility.Visible, "Expected tools summary visible");
    Assert(msg.ToolDetailsVisibility == System.Windows.Visibility.Visible, "Expected collapsible tool details visible");
    return Task.CompletedTask;
});

// 27. Persistent Chat History save/load & Markdown export
await RunTest("ChatHistoryService: persists threads to disk and exports Markdown", () =>
{
    var tmpFile = Path.Combine(Path.GetTempPath(), $"aib-chats-{Guid.NewGuid():N}.json");
    try
    {
        var th = new AgentChatThread { Title = "Draw Car Session", Preview = "Drew car in Paint" };
        th.Messages.Add(new AgentChatMessage { Role = "user", Content = "draw a car", Timestamp = "10:00:00" });
        th.Messages.Add(new AgentChatMessage { Role = "assistant", Content = "Done!", ToolsSummary = "mouse_draw_path", Timestamp = "10:00:04" });
        ChatHistoryService.Save(new[] { th }, tmpFile);

        var loaded = ChatHistoryService.Load(tmpFile);
        Assert(loaded.Count == 1 && loaded[0].Title == "Draw Car Session" && loaded[0].Messages.Count == 2, "Expected persisted thread to round-trip");

        var md = ChatHistoryService.ExportThreadToMarkdown(loaded[0]);
        Assert(md.Contains("# Draw Car Session") && md.Contains("draw a car") && md.Contains("mouse_draw_path"), "Expected valid Markdown export");
    }
    finally
    {
        if (File.Exists(tmpFile)) File.Delete(tmpFile);
    }
    return Task.CompletedTask;
});

// 28. Drag-and-drop file attachment context formatting
await RunTest("DeviceAgentRuntimeService: FormatFileAttachmentContext formats dropped text file", () =>
{
    var tmpFile = Path.Combine(Path.GetTempPath(), $"aib-drop-{Guid.NewGuid():N}.txt");
    File.WriteAllText(tmpFile, "hello dropped file");
    try
    {
        var ctx = DeviceAgentRuntimeService.FormatFileAttachmentContext(tmpFile);
        Assert(ctx.Contains("hello dropped file") && ctx.Contains(Path.GetFileName(tmpFile)), "Expected formatted dropped file context");
    }
    finally
    {
        File.Delete(tmpFile);
    }
    return Task.CompletedTask;
});

// 29. Takeover HUD Pause/Resume, Step Counter & Click Ripples
await RunTest("AgentTakeoverService: TogglePause, UpdateStep & TriggerClickRipple work cleanly", () =>
{
    bool before = AgentTakeoverService.IsPaused;
    AgentTakeoverService.TogglePause();
    Assert(AgentTakeoverService.IsPaused == !before, "Expected TogglePause to flip IsPaused");
    AgentTakeoverService.TogglePause();
    Assert(AgentTakeoverService.IsPaused == before, "Expected second TogglePause to restore IsPaused");
    AgentTakeoverService.UpdateStep(2, 16, "mouse_draw_path");
    AgentTakeoverService.TriggerClickRipple(400, 300);
    return Task.CompletedTask;
});

// 30. Windows 11 Mica backdrop config default
await RunTest("AgentGuiConfig: EnableMicaBackdrop defaults to true", () =>
{
    var cfg = new AgentGuiConfig();
    Assert(cfg.EnableMicaBackdrop, "Expected EnableMicaBackdrop to default to true");
    return Task.CompletedTask;
});

// 31. VoiceInputService Push-to-Talk script builder
await RunTest("VoiceInputService: builds offline System.Speech recognition script cleanly", () =>
{
    var script = VoiceInputService.BuildRecognitionScript(8);
    Assert(script.Contains("SpeechRecognitionEngine") && script.Contains("DictationGrammar") && script.Contains("FromSeconds(8)"),
        "Expected valid Windows SpeechRecognitionEngine script");
    return Task.CompletedTask;
});

// 32. Reusable 1-Click Playbooks persistence
await RunTest("AgentPlaybookService: loads defaults and persists custom playbooks", () =>
{
    var tmpFile = Path.Combine(Path.GetTempPath(), $"aib-pb-{Guid.NewGuid():N}.json");
    try
    {
        var defaults = AgentPlaybookService.Load(tmpFile);
        Assert(defaults.Count >= 3, "Expected >= 3 default playbooks");
        defaults.Insert(0, new AgentPlaybook { Name = "Morning Setup", Prompt = "Open VS Code and check git status" });
        AgentPlaybookService.Save(defaults, tmpFile);

        var reloaded = AgentPlaybookService.Load(tmpFile);
        Assert(reloaded[0].Name == "Morning Setup", "Expected saved playbook to reload first");
    }
    finally
    {
        if (File.Exists(tmpFile)) File.Delete(tmpFile);
    }
    return Task.CompletedTask;
});

// 33. Persistent Agent Memory (remember / forget / prompt block)
await RunTest("AgentMemoryService: persists facts, builds prompt block, and forgets cleanly", () =>
{
    var tmpFile = Path.Combine(Path.GetTempPath(), $"aib-mem-{Guid.NewGuid():N}.json");
    try
    {
        AgentMemoryService.Remember("favorite_browser", "Chrome", tmpFile);
        var mem = AgentMemoryService.Load(tmpFile);
        Assert(mem.TryGetValue("favorite_browser", out var val) && val == "Chrome", "Expected remembered fact");

        var block = AgentMemoryService.BuildPromptBlock(tmpFile);
        Assert(block.Contains("favorite_browser: Chrome"), "Expected prompt block to include saved fact");

        Assert(AgentMemoryService.Forget("favorite_browser", tmpFile), "Expected Forget to return true");
        Assert(AgentMemoryService.Load(tmpFile).Count == 0, "Expected memory empty after Forget");
    }
    finally
    {
        if (File.Exists(tmpFile)) File.Delete(tmpFile);
    }
    return Task.CompletedTask;
});

// 34. Window / Monitor Pinning (Scoped Focus Mode)
await RunTest("NativeToolExecutor: PinnedWindowTitle blocks desktop input when target window is not open", async () =>
{
    var exec = new NativeToolExecutor(new SystemMonitorService(), Path.GetTempPath());
    var cfg = new AgentGuiConfig
    {
        Workspace = Path.GetTempPath(),
        Policy = new AgentPolicyConfig { AllowTier3 = true, MaxTierUnattended = 3, AllowDesktopControl = true },
        DeviceAgent = new DeviceAgentSettings { PinnedWindowTitle = "NonExistentPinnedWindow_XYZ_999", PinnedMonitorIndex = 0 },
    };
    var (ok, res) = await exec.ExecuteAsync("mouse_click", "{\"x\":50,\"y\":50}", cfg, CancellationToken.None);
    Assert(!ok && res.Contains("Scoped Focus Mode"), $"Expected Scoped Focus Mode block, got: {res}");
    var prompt = DeviceAgentRuntimeService.BuildEffectiveSystemPrompt(cfg);
    Assert(prompt.Contains("SCOPED WINDOW PIN ACTIVE") && prompt.Contains("SCOPED MONITOR PIN ACTIVE"), "Expected pinning notices in system prompt");
});

// 35. Auto-Verify Visual Loop classification & defaults
await RunTest("DeviceAgentRuntimeService: AutoVerifyVisualActions defaults true and classifies UI mutations", () =>
{
    var cfg = new AgentGuiConfig();
    Assert(cfg.DeviceAgent.AutoVerifyVisualActions, "Expected AutoVerifyVisualActions to default to true");
    Assert(DeviceAgentRuntimeService.IsVisualMutatingTool("mouse_draw_path"), "Expected mouse_draw_path to be visual mutating");
    Assert(DeviceAgentRuntimeService.IsVisualMutatingTool("desktop_open_application"), "Expected desktop_open_application to be visual mutating");
    Assert(!DeviceAgentRuntimeService.IsVisualMutatingTool("get_system_info"), "Expected get_system_info to NOT be visual mutating");
    return Task.CompletedTask;
});

// 36. Live JPEG frame parity (scale/quality) + secure-desktop honesty fields
await RunTest("ScreenCaptureService: CaptureLiveFrame returns JPEG mime with scale/quality", () =>
{
    var dir = Path.Combine(Path.GetTempPath(), $"aib-live-{Guid.NewGuid():N}");
    Directory.CreateDirectory(dir);
    try
    {
        var frame = ScreenCaptureService.CaptureLiveFrame(dir, monitorIndex: 0, scale: 0.5, quality: 60);
        Assert(frame.MimeType == "image/jpeg", $"Expected image/jpeg, got {frame.MimeType}");
        Assert(Math.Abs(frame.Scale - 0.5) < 0.001, $"Expected scale 0.5, got {frame.Scale}");
        Assert(frame.Quality == 60, $"Expected quality 60, got {frame.Quality}");
        if (frame.Available)
        {
            Assert(!string.IsNullOrWhiteSpace(frame.Base64Data), "Expected base64 JPEG data");
            Assert(frame.Width > 0 && frame.Height > 0, "Expected positive dimensions");
            Assert(frame.OrigWidth >= frame.Width && frame.OrigHeight >= frame.Height, "Expected orig >= scaled");
        }
        else
        {
            Assert(!string.IsNullOrWhiteSpace(frame.Note), "Unavailable frames must explain why");
        }
    }
    finally
    {
        try { Directory.Delete(dir, recursive: true); } catch { /* ignore */ }
    }
    return Task.CompletedTask;
});

// 37. Pipe HMAC never silently uses the insecure legacy default
await RunTest("NamedPipeBridge: RequireHmacSecret refuses legacy default and regenerates", () =>
{
    Assert(NamedPipeBridge.IsInsecureDefaultSecret(NamedPipeBridge.DefaultPipeSecret), "Expected default marked insecure");
    NamedPipeBridge.ActiveHmacSecret = NamedPipeBridge.DefaultPipeSecret;
    var secret = NamedPipeBridge.RequireHmacSecret(NamedPipeBridge.DefaultPipeSecret);
    Assert(!NamedPipeBridge.IsInsecureDefaultSecret(secret), "Expected regenerated secret, not legacy default");
    Assert(secret.Length >= 32, "Expected sufficiently long secret");
    var wire = NamedPipeBridge.BuildCppWireMessage("HEARTBEAT_PING", new { client = "test" }, secret);
    Assert(NamedPipeBridge.TryParseCppWireMessage(wire, secret, out var msgType, out _), msgType == "HEARTBEAT_PING" ? "ok" : "parse failed");
    return Task.CompletedTask;
});

// 38. NativeToolExecutor capture_desktop_frame returns mime_type image/jpeg
await RunTest("NativeToolExecutor: capture_desktop_frame returns JPEG contract fields", async () =>
{
    var tempWs = Path.Combine(Path.GetTempPath(), $"aib-frame-{Guid.NewGuid():N}");
    Directory.CreateDirectory(tempWs);
    try
    {
        var cfg = new AgentGuiConfig
        {
            Workspace = tempWs,
            ScreenshotDirectory = Path.Combine(tempWs, "shots"),
            Policy = new AgentPolicyConfig { AllowTier3 = true, MaxTierUnattended = 3 },
        };
        var exec = new NativeToolExecutor(new SystemMonitorService(), cfg.ScreenshotDirectory);
        var (ok, res) = await exec.ExecuteAsync(
            "capture_desktop_frame",
            "{\"scale\":0.5,\"quality\":55,\"monitor_index\":0}",
            cfg,
            CancellationToken.None);
        Assert(ok, $"Expected capture_desktop_frame success, got: {res}");
        using var doc = JsonDocument.Parse(res.Contains('{') ? res[res.IndexOf('{')..] : res);
        // Result may be wrapped — try root or nested
        var root = doc.RootElement;
        if (root.TryGetProperty("result", out var nested)) root = nested;
        if (root.TryGetProperty("mime_type", out var mime))
        {
            Assert(mime.GetString() == "image/jpeg", $"Expected image/jpeg mime, got {mime}");
        }
        else
        {
            // Executor may return object serialized with mime_type at top level of success payload
            Assert(res.Contains("image/jpeg") || res.Contains("mime_type"), $"Expected JPEG contract in: {res}");
        }
    }
    finally
    {
        try { Directory.Delete(tempWs, recursive: true); } catch { /* ignore */ }
    }
});

// 39. Input permission sync no longer auto-grants on TryMoveMouse
await RunTest("CppNativeBridge: SyncInputPermissions deny blocks TryMoveMouse without re-enable", () =>
{
    CppNativeBridge.SyncInputPermissions(allowDesktopControl: false, allowClipboard: false);
    Assert(!CppNativeBridge.InputDesktopAllowed, "Expected desktop input denied");
    // When denied, TryMoveMouse must return false even if DLL is present (no silent re-grant).
    Assert(!CppNativeBridge.TryMoveMouse(10, 10), "Denied permissions must block TryMoveMouse");
    Assert(!CppNativeBridge.InputDesktopAllowed, "TryMoveMouse must not flip InputDesktopAllowed back on");
    return Task.CompletedTask;
});

// 40. Device Functions stay aligned with server effective policy
await RunTest("AgentFunctionPermissions: SyncFromEffectivePolicy aligns IsToolAllowed with Policy", () =>
{
    var locked = ControlPlaneClient.ParsePolicyFromJsonElement(
        JsonDocument.Parse("""
            {
              "allow_writes": false,
              "allow_tier3": false,
              "allow_file_deletion": false,
              "allow_shell": false,
              "desktop_enabled": false,
              "allow_clipboard": false,
              "allow_full_pc_access": false,
              "max_tier_unattended": 2
            }
            """).RootElement);
    var lockedFns = AgentFunctionPermissions.SyncFromEffectivePolicy(locked);
    Assert(!lockedFns.AllowUiInputAutomation && !lockedFns.AllowShellAndPowerShell && !lockedFns.AllowClipboard,
        "Locked policy must disable desktop/shell/clipboard functions");

    var open = ControlPlaneClient.ParsePolicyFromJsonElement(
        JsonDocument.Parse("""
            {
              "allow_writes": true,
              "allow_tier3": true,
              "allow_file_deletion": true,
              "allow_shell": true,
              "desktop_enabled": true,
              "allow_clipboard": true,
              "allow_full_pc_access": true,
              "allow_unrestricted_apps": true,
              "max_tier_unattended": 4
            }
            """).RootElement);
    var openFns = AgentFunctionPermissions.SyncFromEffectivePolicy(open, new[] { "close_window" });
    Assert(openFns.AllowUiInputAutomation && openFns.AllowShellAndPowerShell && openFns.AllowClipboard,
        "Open tier-4 policy must enable desktop/shell/clipboard functions");
    Assert(openFns.DisabledTools.Contains("close_window"), "Must preserve explicit DisabledTools");

    var cfg = new AgentGuiConfig
    {
        DeviceAgent = new DeviceAgentSettings { Functions = openFns },
        Policy = open,
    };
    Assert(DeviceAgentRuntimeService.IsToolAllowed("mouse_click", cfg, out _), "mouse_click allowed after sync");
    Assert(DeviceAgentRuntimeService.IsToolAllowed("get_clipboard", cfg, out _), "get_clipboard allowed after sync");
    Assert(DeviceAgentRuntimeService.IsToolAllowed("run_shell", cfg, out _), "run_shell allowed after sync");
    Assert(!DeviceAgentRuntimeService.IsToolAllowed("close_window", cfg, out _), "DisabledTools still deny close_window");

    cfg.DeviceAgent.Functions = lockedFns;
    cfg.Policy = locked;
    Assert(!DeviceAgentRuntimeService.IsToolAllowed("mouse_click", cfg, out _), "mouse_click denied after locked sync");
    Assert(!DeviceAgentRuntimeService.IsToolAllowed("get_clipboard", cfg, out _), "get_clipboard denied after locked sync");
    Assert(!DeviceAgentRuntimeService.IsToolAllowed("run_shell", cfg, out _), "run_shell denied after locked sync");
    return Task.CompletedTask;
});

// 41. AllowedCommands rejects shell metacharacter chaining & redirection
await RunTest("NativeToolExecutor: AllowedCommands blocks shell metacharacters and allows clean commands", async () =>
{
    var allowed = new List<string> { "echo", "git" };
    NativeToolExecutor.ValidateAllowedCommand("git status --short", allowed);
    NativeToolExecutor.ValidateAllowedCommand("echo hello world", allowed);

    var badCmds = new[]
    {
        "git status && whoami",
        "git status | findstr a",
        "git status; calc.exe",
        "echo hi > out.txt",
        "echo $(whoami)",
        "echo `whoami`",
        "echo %USERNAME%",
        "git status\nwhoami",
        "powershell.exe -Command dir",
    };
    foreach (var bad in badCmds)
    {
        bool threw = false;
        try
        {
            NativeToolExecutor.ValidateAllowedCommand(bad, allowed);
        }
        catch (UnauthorizedAccessException)
        {
            threw = true;
        }
        Assert(threw, $"Expected AllowedCommands to reject: {bad}");
    }

    var tempWs = Path.Combine(Path.GetTempPath(), $"aib-cmd-{Guid.NewGuid():N}");
    Directory.CreateDirectory(tempWs);
    try
    {
        var cfg = new AgentGuiConfig
        {
            Workspace = tempWs,
            Policy = new AgentPolicyConfig
            {
                AllowTier4 = true,
                AllowShell = true,
                MaxTierUnattended = 4,
                AllowedCommands = allowed,
            },
        };
        var exec = new NativeToolExecutor(new SystemMonitorService(), Path.Combine(tempWs, "shots"));
        var (okChain, _) = await exec.ExecuteAsync("run_shell", "{\"command\":\"echo ok && whoami\"}", cfg, CancellationToken.None);
        Assert(!okChain, "run_shell must reject chained metacharacter command when AllowedCommands is set");
    }
    finally
    {
        try { Directory.Delete(tempWs, recursive: true); } catch { /* ignore */ }
    }
});

// 42. Bounded streaming file reads cap memory and skip secrets in search
await RunTest("NativeToolExecutor: ReadTextBounded & ReadWorkspaceFile cap large reads and skip secrets", async () =>
{
    var tempWs = Path.Combine(Path.GetTempPath(), $"aib-read-{Guid.NewGuid():N}");
    Directory.CreateDirectory(tempWs);
    try
    {
        var bigPath = Path.Combine(tempWs, "big.txt");
        File.WriteAllText(bigPath, new string('A', 20_000));
        File.WriteAllText(Path.Combine(tempWs, ".env"), "SECRET_NEEDLE=1");
        File.WriteAllText(Path.Combine(tempWs, "visible.txt"), "VISIBLE_NEEDLE=1");

        var chunk = NativeToolExecutor.ReadTextBounded(bigPath, 1024, out var wasTruncated);
        Assert(wasTruncated && chunk.Length == 1024, $"Expected bounded read of 1024 chars, got {chunk.Length} (truncated={wasTruncated})");

        var cfg = new AgentGuiConfig { Workspace = tempWs, MaxReadBytes = 1024 };
        var exec = new NativeToolExecutor(new SystemMonitorService(), Path.Combine(tempWs, "shots"));

        var (okBig, resBig) = await exec.ExecuteAsync("read_workspace_file", "{\"path\":\"big.txt\"}", cfg, CancellationToken.None);
        Assert(okBig && resBig.Contains("...[truncated]"), "Expected big.txt to be truncated cleanly");

        var (okSearchSecret, resSearchSecret) = await exec.ExecuteAsync("search_workspace", "{\"query\":\"SECRET_NEEDLE\"}", cfg, CancellationToken.None);
        Assert(okSearchSecret && !resSearchSecret.Contains(".env"), "search_workspace must not match .env secret files");

        var (okSearchVis, resSearchVis) = await exec.ExecuteAsync("search_workspace", "{\"query\":\"VISIBLE_NEEDLE\"}", cfg, CancellationToken.None);
        Assert(okSearchVis && resSearchVis.Contains("visible.txt"), "search_workspace must find visible.txt");
    }
    finally
    {
        try { Directory.Delete(tempWs, recursive: true); } catch { /* ignore */ }
    }
});

// 43. Hybrid Desktop Accessibility Engine (@ref, STALE_REF, password redaction, idempotency, policy & pinning)
await RunTest("HybridA11yService: status, redaction, STALE_REF, idempotent actions, policy & pinned window", async () =>
{
    var exec = new NativeToolExecutor(new SystemMonitorService(), Path.GetTempPath());
    var lockedCfg = new AgentGuiConfig
    {
        Workspace = Path.GetTempPath(),
        Policy = new AgentPolicyConfig { AllowTier3 = false, AllowDesktopControl = false, MaxTierUnattended = 2 },
    };

    var (okStat, statJson) = await exec.ExecuteAsync("desktop_a11y_status", "{}", lockedCfg, CancellationToken.None);
    Assert(okStat && statJson.Contains("windows_uia") && statJson.Contains("visual_ocr_fallback_available"), "Expected valid hybrid a11y status");

    var (okPerm, permJson) = await exec.ExecuteAsync("desktop_a11y_permissions", "{}", lockedCfg, CancellationToken.None);
    Assert(okPerm && permJson.Contains("accessibility_granted"), "Expected permissions check payload");

    Assert(HybridA11yService.ValidateRefSyntax("@s1234abcd:e1"), "Valid @ref syntax should pass");
    Assert(!HybridA11yService.ValidateRefSyntax("bad-ref"), "Invalid @ref syntax should fail");
    Assert(HybridA11yService.IsSensitiveNode("User Password", "", "Edit", false), "Password label must be treated as sensitive");
    Assert(HybridA11yService.IsSensitiveNode("API Key", "", "Edit", false), "API Key label must be treated as sensitive");

    // Seed snapshot 1 with password field + checked checkbox
    var snap1 = HybridA11yService.SeedSnapshotForTesting(new[]
    {
        new HybridA11yRefEntry("@s1111aaaa:e1", "s1111aaaa", "Edit", "Account Password", "pwdBox", "Login Dialog", 10, 10, 120, 24, 70, 22, true, true, "Value", "super-secret", null, null, null),
        new HybridA11yRefEntry("@s1111aaaa:e2", "s1111aaaa", "CheckBox", "Remember Me", "chkRemember", "Login Dialog", 10, 40, 20, 20, 20, 50, true, false, "Toggle", "", true, null, null),
    }, "s1111aaaa");

    var (okVal, valJson) = await exec.ExecuteAsync("desktop_a11y_get_value", "{\"ref\":\"@s1111aaaa:e1\"}", lockedCfg, CancellationToken.None);
    Assert(okVal && valJson.Contains("[REDACTED]") && !valJson.Contains("super-secret"), $"Password value must be redacted: {valJson}");

    // Tier-3 policy denial when desktop control disabled
    var (okDenied, _) = await exec.ExecuteAsync("desktop_a11y_act", "{\"ref\":\"@s1111aaaa:e2\",\"action\":\"check\"}", lockedCfg, CancellationToken.None);
    Assert(!okDenied, "desktop_a11y_act must fail when Tier 3 / desktop control is disabled");

    var openCfg = new AgentGuiConfig
    {
        Workspace = Path.GetTempPath(),
        Policy = new AgentPolicyConfig { AllowTier3 = true, AllowDesktopControl = true, MaxTierUnattended = 3 },
    };

    // Idempotent check on already-checked checkbox succeeds headless with noop=true
    var (okNoop, noopJson) = await exec.ExecuteAsync("desktop_a11y_act", "{\"ref\":\"@s1111aaaa:e2\",\"action\":\"check\"}", openCfg, CancellationToken.None);
    Assert(okNoop && noopJson.Contains("\"noop\": true"), $"Expected idempotent noop check: {noopJson}");

    // Atomic batch of idempotent actions succeeds
    var (okBatch, batchJson) = await exec.ExecuteAsync("desktop_a11y_batch", "{\"steps\":[{\"ref\":\"@s1111aaaa:e2\",\"action\":\"check\"}]}", openCfg, CancellationToken.None);
    Assert(okBatch && batchJson.Contains("\"steps_executed\": 1"), $"Expected batch success: {batchJson}");

    // PinnedWindowTitle rejects acting on an element from a different window
    openCfg.DeviceAgent.PinnedWindowTitle = "Other App";
    var (okPinnedBlock, _) = await exec.ExecuteAsync("desktop_a11y_act", "{\"ref\":\"@s1111aaaa:e2\",\"action\":\"check\"}", openCfg, CancellationToken.None);
    Assert(!okPinnedBlock, "Scoped focus must block ref belonging to non-pinned window");
    openCfg.DeviceAgent.PinnedWindowTitle = "";

    // Seeding a new snapshot invalidates previous refs -> STALE_REF
    HybridA11yService.SeedSnapshotForTesting(Array.Empty<HybridA11yRefEntry>(), "s2222bbbb");
    var (okStale, staleJson) = await exec.ExecuteAsync("desktop_a11y_act", "{\"ref\":\"@s1111aaaa:e2\",\"action\":\"check\"}", openCfg, CancellationToken.None);
    Assert(okStale && staleJson.Contains("STALE_REF"), $"Expected STALE_REF after snapshot superseded: {staleJson}");

    // Runtime function gating recognises desktop_a11y_snapshot and desktop_a11y_act
    openCfg.DeviceAgent.Functions = AgentFunctionPermissions.SyncFromEffectivePolicy(openCfg.Policy);
    Assert(DeviceAgentRuntimeService.IsToolAllowed("desktop_a11y_snapshot", openCfg, out _), "desktop_a11y_snapshot should be allowed when UI tools enabled");
    Assert(DeviceAgentRuntimeService.IsToolAllowed("desktop_a11y_act", openCfg, out _), "desktop_a11y_act should be allowed when UI tools enabled");
    Assert(DeviceAgentRuntimeService.IsVisualMutatingTool("desktop_a11y_act"), "desktop_a11y_act should be classified as visual mutating");
    HybridA11yService.ClearSnapshotStore();
});

Console.WriteLine($"\nSummary: {passed} passed, {failed} failed ({passed + failed} total)");
return failed > 0 ? 1 : 0;
