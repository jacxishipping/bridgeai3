using System;
using System.IO;
using System.IO.Pipes;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using AIBridgeClient.Models;

namespace AIBridgeClient.Services;

/// <summary>
/// Named-pipe IPC client for the native C++ windows-agent (\\.\pipe\AIBridgeDesktopPipe)
/// implementing shared/protocols/named_pipe_protocol.json v1.0.0.
/// </summary>
public static class NamedPipeBridge
{
    public const string PipeName = "AIBridgeDesktopPipe";
    public static string ActivePipeName { get; set; } = PipeName;
    public static string? ActiveHmacSecret { get; set; }
    public const int MaxMessageBytes = 65536;

    public static bool IsDesktopAgentAvailable(string? pipeName = null)
    {
        try
        {
            var p = string.IsNullOrWhiteSpace(pipeName) ? ActivePipeName : pipeName.Trim();
            return File.Exists($@"\\.\pipe\{p}");
        }
        catch
        {
            return false;
        }
    }

    public static string SignEnvelope(string messageType, object payload, string? hmacSecret = null)
    {
        var bodyJson = JsonSerializer.Serialize(new
        {
            protocol = "ai-bridge-windows-named-pipe",
            version = "1.0.0",
            type = messageType,
            timestamp = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
            nonce = Guid.NewGuid().ToString("N"),
            payload,
        });

        string sig = "";
        if (!string.IsNullOrEmpty(hmacSecret))
        {
            using var hmac = new HMACSHA256(Encoding.UTF8.GetBytes(hmacSecret));
            sig = Convert.ToHexStringLower(hmac.ComputeHash(Encoding.UTF8.GetBytes(bodyJson)));
        }

        var envelope = JsonSerializer.Serialize(new { body = bodyJson, hmac = sig });
        if (Encoding.UTF8.GetByteCount(envelope) > MaxMessageBytes)
        {
            throw new InvalidOperationException($"Named-pipe message exceeds {MaxMessageBytes} byte limit.");
        }
        return envelope;
    }

    public static async Task<(bool Ok, string Response)> SendCommandAsync(
        string messageType,
        object payload,
        string? hmacSecret = null,
        CancellationToken ct = default,
        string? pipeName = null)
    {
        var pipeTarget = string.IsNullOrWhiteSpace(pipeName) ? ActivePipeName : pipeName.Trim();
        var secret = hmacSecret ?? ActiveHmacSecret;
        if (!IsDesktopAgentAvailable(pipeTarget))
        {
            return (false, $"C++ desktop agent pipe ({pipeTarget}) is not running.");
        }

        var envelope = SignEnvelope(messageType, payload, secret);
        var bytes = Encoding.UTF8.GetBytes(envelope + "\n");

        using var pipe = new NamedPipeClientStream(".", pipeTarget, PipeDirection.InOut, PipeOptions.Asynchronous);
        using var timeoutCts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        timeoutCts.CancelAfter(TimeSpan.FromSeconds(4));

        await pipe.ConnectAsync(timeoutCts.Token);
        await pipe.WriteAsync(bytes, timeoutCts.Token);
        await pipe.FlushAsync(timeoutCts.Token);

        var buf = new byte[MaxMessageBytes];
        int read = await pipe.ReadAsync(buf, timeoutCts.Token);
        var reply = Encoding.UTF8.GetString(buf, 0, read).Trim();
        return (true, string.IsNullOrEmpty(reply) ? "{\"status\":\"sent\"}" : reply);
    }

    public static Task<(bool Ok, string Response)> PingHeartbeatAsync(CancellationToken ct = default) =>
        SendCommandAsync("HEARTBEAT_PING", new { client = "windows-csharp-v0.3.0" }, ct: ct);

    public static Task<(bool Ok, string Response)> RequestDesktopStatusAsync(CancellationToken ct = default) =>
        SendCommandAsync("DESKTOP_STATUS_REPORT", new { }, ct: ct);

    public static Task<(bool Ok, string Response)> ReleaseAllInputAsync(CancellationToken ct = default) =>
        SendCommandAsync("RELEASE_ALL_INPUT", new { reason = "emergency_stop_or_takeover" }, ct: ct);

    public static Task<(bool Ok, string Response)> UpdateSessionPermissionsAsync(AgentPolicyConfig policy, CancellationToken ct = default) =>
        SendCommandAsync("UPDATE_SESSION_PERMISSIONS", new
        {
            allow_writes = policy.AllowWrites,
            allow_tier3 = policy.AllowTier3,
            allow_tier4 = policy.AllowTier4,
            allow_shell = policy.AllowShell,
            allow_desktop_control = policy.AllowDesktopControl,
            allow_clipboard = policy.AllowClipboard,
            allow_package_install = policy.AllowPackageInstall,
            allow_unrestricted_apps = policy.AllowUnrestrictedApps,
            allow_full_pc_access = policy.AllowFullPcAccess,
            max_tier_unattended = policy.MaxTierUnattended,
            allowed_apps = policy.AllowedApps,
            allowed_commands = policy.AllowedCommands,
        }, ct: ct);
}
