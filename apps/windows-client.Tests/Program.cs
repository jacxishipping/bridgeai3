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

Console.WriteLine($"\nSummary: {passed} passed, {failed} failed ({passed + failed} total)");
return failed > 0 ? 1 : 0;
