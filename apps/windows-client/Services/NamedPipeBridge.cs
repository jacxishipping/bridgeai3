using System;
using System.Diagnostics;
using System.IO;
using System.IO.Pipes;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using AIBridgeClient.Models;

namespace AIBridgeClient.Services;

/// <summary>
/// Named-pipe IPC client &amp; supervisor manager for the native C++ windows-agent (\\.\pipe\AIBridgeDesktopPipe).
/// Implements the exact 5-line HMAC-SHA256 replay-protected wire protocol from
/// windows-agent/src/security/local_security.cpp (SecureNamedPipe::BuildWireMessage / ParseAndVerifyWire):
///   msg_type + "\n" + nonce_hex32 + "\n" + unix_timestamp_sec + "\n" + hmac_tag_hex64 + "\n" + json_payload
/// where tag = HMAC_SHA256(secret, msg_type + "|" + nonce + "|" + timestamp_sec + "|" + json_payload).
/// </summary>
public static class NamedPipeBridge
{
    public const string PipeName = "AIBridgeDesktopPipe";
    public const string DefaultPipeSecret = "aibridge-local-pipe-hmac-v1";
    public static string ActivePipeName { get; set; } = PipeName;
    public static string? ActiveHmacSecret { get; set; } = DefaultPipeSecret;
    public const int MaxMessageBytes = 65536;

    private static Process? _managedServiceProcess;
    private static readonly object _procLock = new();

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool WaitNamedPipeW(string lpNamedPipeName, uint nTimeOut);

    public static bool IsDesktopAgentAvailable(string? pipeName = null)
    {
        try
        {
            var p = NormalizePipeName(pipeName);
            var fullPipe = $@"\\.\pipe\{p}";
            if (WaitNamedPipeW(fullPipe, 50) || File.Exists(fullPipe))
            {
                return true;
            }
            return Directory.GetFiles(@"\\.\pipe\", p).Length > 0;
        }
        catch
        {
            return false;
        }
    }

    private static string NormalizePipeName(string? pipeName)
    {
        var p = string.IsNullOrWhiteSpace(pipeName) ? ActivePipeName : pipeName.Trim();
        const string prefix = @"\\.\pipe\";
        if (p.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
        {
            p = p[prefix.Length..];
        }
        return string.IsNullOrWhiteSpace(p) ? PipeName : p;
    }

    public static string? FindCppExecutable(string exeName)
    {
        var baseDir = AppContext.BaseDirectory;
        var candidates = new[]
        {
            Path.Combine(baseDir, exeName),
            Path.Combine(baseDir, "native", exeName),
            Path.GetFullPath(Path.Combine(baseDir, "..", "..", "..", "..", "..", "windows-agent", "build", "Release", exeName)),
            Path.GetFullPath(Path.Combine(baseDir, "..", "..", "..", "..", "windows-agent", "build", "Release", exeName)),
        };
        foreach (var path in candidates)
        {
            if (File.Exists(path)) return path;
        }
        return null;
    }

    public static bool EnsureCppSupervisorRunning(
        string? pipeName = null,
        string? hmacSecret = null,
        string? controlUrl = null,
        string? deviceToken = null,
        CppAgentSettings? cppSettings = null)
    {
        var targetPipe = NormalizePipeName(pipeName);
        ActivePipeName = targetPipe;
        var secret = string.IsNullOrWhiteSpace(hmacSecret)
            ? (string.IsNullOrWhiteSpace(ActiveHmacSecret) ? DefaultPipeSecret : ActiveHmacSecret!)
            : hmacSecret!;
        ActiveHmacSecret = secret;

        var cfg = cppSettings ?? new CppAgentSettings();
        var home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        var cfgDir = Path.Combine(home, ".ai-bridge");
        Directory.CreateDirectory(cfgDir);
        var cfgPath = Path.Combine(cfgDir, "cpp_service_runtime.json");
        var desktopExe = FindCppExecutable("ai_bridge_desktop.exe") ?? "";

        try
        {
            var cfgJson = JsonSerializer.Serialize(new
            {
                pipe_name = $@"\\.\pipe\{targetPipe}",
                pipe_secret = secret,
                control_url = controlUrl ?? "",
                device_token = deviceToken ?? "",
                desktop_agent_path = desktopExe,
                turn_url = cfg.TurnUrl ?? "",
                turn_username = cfg.TurnUsername ?? "",
                turn_credential = cfg.TurnCredential ?? "",
                target_fps = Math.Clamp(cfg.H264TargetFps, 1, 60),
                bitrate_kbps = Math.Clamp(cfg.H264BitrateKbps, 250, 50_000),
                keyframe_interval_sec = Math.Clamp(cfg.H264KeyframeIntervalSec, 1, 15),
                prefer_dxgi = cfg.PreferDxgiCapture,
                prefer_hardware_h264 = cfg.H264PreferHardware,
                adaptive_bitrate = cfg.H264AdaptiveBitrate,
            }, new JsonSerializerOptions { WriteIndented = true });
            File.WriteAllText(cfgPath, cfgJson, Encoding.UTF8);
        }
        catch
        {
            // Non-fatal if config write fails
        }

        if (IsDesktopAgentAvailable(targetPipe))
        {
            return true;
        }

        if (!cfg.AutoStartSupervisor)
        {
            return false;
        }

        lock (_procLock)
        {
            if (IsDesktopAgentAvailable(targetPipe))
            {
                return true;
            }

            var serviceExe = FindCppExecutable("ai_bridge_service.exe");
            if (serviceExe is null)
            {
                return false;
            }

            try
            {
                var psi = new ProcessStartInfo
                {
                    FileName = serviceExe,
                    Arguments = $"--pipe-server --config \"{cfgPath}\" --pipe-name \"{targetPipe}\"",
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    WindowStyle = ProcessWindowStyle.Hidden,
                    WorkingDirectory = Path.GetDirectoryName(serviceExe) ?? AppContext.BaseDirectory,
                };
                _managedServiceProcess = Process.Start(psi);

                // Wait up to 800 ms for the C++ named pipe server to bind
                var sw = Stopwatch.StartNew();
                while (sw.ElapsedMilliseconds < 800)
                {
                    if (IsDesktopAgentAvailable(targetPipe))
                    {
                        return true;
                    }
                    Thread.Sleep(40);
                }
            }
            catch
            {
                return false;
            }
        }

        return IsDesktopAgentAvailable(targetPipe);
    }

    public static bool RestartCppSupervisor(
        string? pipeName = null,
        string? hmacSecret = null,
        string? controlUrl = null,
        string? deviceToken = null,
        CppAgentSettings? cppSettings = null)
    {
        StopManagedCppSupervisor();
        try
        {
            foreach (var p in Process.GetProcessesByName("ai_bridge_service"))
            {
                try { p.Kill(); p.Dispose(); } catch { }
            }
        }
        catch { }

        var forceSettings = cppSettings is null
            ? new CppAgentSettings { AutoStartSupervisor = true }
            : new CppAgentSettings
            {
                AutoStartSupervisor = true,
                PreferDxgiCapture = cppSettings.PreferDxgiCapture,
                H264TargetFps = cppSettings.H264TargetFps,
                H264BitrateKbps = cppSettings.H264BitrateKbps,
                H264KeyframeIntervalSec = cppSettings.H264KeyframeIntervalSec,
                H264PreferHardware = cppSettings.H264PreferHardware,
                H264AdaptiveBitrate = cppSettings.H264AdaptiveBitrate,
                TurnUrl = cppSettings.TurnUrl,
                TurnUsername = cppSettings.TurnUsername,
                TurnCredential = cppSettings.TurnCredential,
            };
        return EnsureCppSupervisorRunning(pipeName, hmacSecret, controlUrl, deviceToken, forceSettings);
    }

    public static void StopManagedCppSupervisor()
    {
        lock (_procLock)
        {
            try
            {
                if (_managedServiceProcess is { HasExited: false })
                {
                    _managedServiceProcess.Kill(entireProcessTree: true);
                    _managedServiceProcess.Dispose();
                }
            }
            catch
            {
                // Ignore shutdown errors
            }
            finally
            {
                _managedServiceProcess = null;
            }
        }
    }

    public static string ComputeCppTag(string messageType, string nonceHex32, long timestampSeconds, string payloadJson, string secret)
    {
        var message = $"{messageType}|{nonceHex32}|{timestampSeconds}|{payloadJson}";
        using var hmac = new HMACSHA256(Encoding.UTF8.GetBytes(secret));
        return Convert.ToHexStringLower(hmac.ComputeHash(Encoding.UTF8.GetBytes(message)));
    }

    public static string BuildCppWireMessage(string messageType, object payload, string? hmacSecret = null)
    {
        var secret = string.IsNullOrWhiteSpace(hmacSecret)
            ? (string.IsNullOrWhiteSpace(ActiveHmacSecret) ? DefaultPipeSecret : ActiveHmacSecret!)
            : hmacSecret!;
        var payloadJson = payload is string s ? s : JsonSerializer.Serialize(payload);
        Span<byte> nonceBytes = stackalloc byte[16];
        RandomNumberGenerator.Fill(nonceBytes);
        var nonceHex = Convert.ToHexStringLower(nonceBytes);
        var timestampSec = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
        var tag = ComputeCppTag(messageType, nonceHex, timestampSec, payloadJson, secret);

        var wire = $"{messageType}\n{nonceHex}\n{timestampSec}\n{tag}\n{payloadJson}";
        if (Encoding.UTF8.GetByteCount(wire) > MaxMessageBytes)
        {
            throw new InvalidOperationException($"Named-pipe message exceeds {MaxMessageBytes} byte limit.");
        }
        return wire;
    }

    public static bool TryParseCppWireMessage(
        string wire,
        string secret,
        out string messageType,
        out string payloadJson)
    {
        messageType = "";
        payloadJson = "";
        if (string.IsNullOrEmpty(wire)) return false;

        var parts = wire.Split('\n', 5);
        if (parts.Length < 5) return false;

        var type = parts[0].Trim();
        var nonce = parts[1].Trim();
        var tsText = parts[2].Trim();
        var recvTag = parts[3].Trim();
        var payload = parts[4].Trim();

        if (string.IsNullOrEmpty(type) || nonce.Length != 32 || !long.TryParse(tsText, out var ts))
        {
            return false;
        }

        var expectedTag = ComputeCppTag(type, nonce, ts, payload, secret);
        if (!CryptographicOperations.FixedTimeEquals(
                Encoding.ASCII.GetBytes(recvTag.ToLowerInvariant()),
                Encoding.ASCII.GetBytes(expectedTag)))
        {
            return false;
        }

        messageType = type;
        payloadJson = payload;
        return true;
    }

    public static async Task<(bool Ok, string Response)> SendCommandAsync(
        string messageType,
        object payload,
        string? hmacSecret = null,
        CancellationToken ct = default,
        string? pipeName = null)
    {
        var pipeTarget = NormalizePipeName(pipeName);
        var secret = string.IsNullOrWhiteSpace(hmacSecret)
            ? (string.IsNullOrWhiteSpace(ActiveHmacSecret) ? DefaultPipeSecret : ActiveHmacSecret!)
            : hmacSecret!;

        if (!IsDesktopAgentAvailable(pipeTarget))
        {
            EnsureCppSupervisorRunning(pipeTarget, secret);
        }

        if (!IsDesktopAgentAvailable(pipeTarget))
        {
            return (false, $"C++ desktop agent pipe ({pipeTarget}) is not running.");
        }

        try
        {
            var wire = BuildCppWireMessage(messageType, payload, secret);
            var bytes = Encoding.UTF8.GetBytes(wire);

            using var pipe = new NamedPipeClientStream(".", pipeTarget, PipeDirection.InOut, PipeOptions.Asynchronous);
            using var timeoutCts = CancellationTokenSource.CreateLinkedTokenSource(ct);
            timeoutCts.CancelAfter(TimeSpan.FromSeconds(4));

            await pipe.ConnectAsync(timeoutCts.Token);
            await pipe.WriteAsync(bytes, timeoutCts.Token);
            await pipe.FlushAsync(timeoutCts.Token);

            var buf = new byte[MaxMessageBytes];
            int read = await pipe.ReadAsync(buf, timeoutCts.Token);
            var rawReply = Encoding.UTF8.GetString(buf, 0, read);
            if (TryParseCppWireMessage(rawReply, secret, out var replyType, out var replyPayload))
            {
                return (replyType != "ERROR", JsonSerializer.Serialize(new
                {
                    ok = replyType != "ERROR",
                    reply_type = replyType,
                    pipe = $@"\\.\pipe\{pipeTarget}",
                    hmac_verified = true,
                    data = TryParseJsonElement(replyPayload),
                }, new JsonSerializerOptions { WriteIndented = true }));
            }

            return (true, string.IsNullOrWhiteSpace(rawReply) ? "{\"status\":\"sent\"}" : rawReply.Trim());
        }
        catch (Exception ex)
        {
            return (false, $"Named-pipe RPC error: {ex.Message}");
        }
    }

    private static object TryParseJsonElement(string json)
    {
        try
        {
            return JsonSerializer.Deserialize<JsonElement>(json);
        }
        catch
        {
            return json;
        }
    }

    public static async Task<(bool Ok, string Response)> RunCppDesktopProbeAsync(
        int monitorIndex = 0,
        int fps = 30,
        int bitrateKbps = 4500,
        CancellationToken ct = default)
    {
        var desktopExe = FindCppExecutable("ai_bridge_desktop.exe");
        if (desktopExe is null)
        {
            return (CppNativeBridge.IsAvailable(), CppNativeBridge.ProbeCaptureAndH264Json(monitorIndex, fps, bitrateKbps));
        }

        try
        {
            var home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
            var cfgPath = Path.Combine(home, ".ai-bridge", "cpp_service_runtime.json");
            var args = File.Exists(cfgPath)
                ? $"--monitor {monitorIndex} --fps {fps} --bitrate-kbps {bitrateKbps} --config \"{cfgPath}\""
                : $"--monitor {monitorIndex} --fps {fps} --bitrate-kbps {bitrateKbps}";

            var psi = new ProcessStartInfo
            {
                FileName = desktopExe,
                Arguments = args,
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                CreateNoWindow = true,
                WorkingDirectory = Path.GetDirectoryName(desktopExe) ?? AppContext.BaseDirectory,
            };
            using var proc = Process.Start(psi);
            if (proc is null)
            {
                return (CppNativeBridge.IsAvailable(), CppNativeBridge.ProbeCaptureAndH264Json(monitorIndex, fps, bitrateKbps));
            }
            var stdout = await proc.StandardOutput.ReadToEndAsync(ct);
            await proc.WaitForExitAsync(ct);
            var pinvokeJson = CppNativeBridge.ProbeCaptureAndH264Json(monitorIndex, fps, bitrateKbps);
            if (!string.IsNullOrWhiteSpace(stdout))
            {
                return (proc.ExitCode == 0 || proc.ExitCode == 3, $"{stdout.Trim()}\n\n[In-Process P/Invoke DXGI + Media Foundation H.264]\n{pinvokeJson}");
            }
        }
        catch
        {
            // Fall back to direct in-process P/Invoke probe
        }

        return (CppNativeBridge.IsAvailable(), CppNativeBridge.ProbeCaptureAndH264Json(monitorIndex, fps, bitrateKbps));
    }

    public static async Task<(bool Ok, string Output)> RunCppSelfTestsAsync(CancellationToken ct = default)
    {
        var testsExe = FindCppExecutable("ai_bridge_tests.exe");
        if (testsExe is null)
        {
            return (false, "ai_bridge_tests.exe not found in installation directory.");
        }

        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = testsExe,
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                CreateNoWindow = true,
                WorkingDirectory = Path.GetDirectoryName(testsExe) ?? AppContext.BaseDirectory,
            };
            using var proc = Process.Start(psi);
            if (proc is null)
            {
                return (false, "Failed to start ai_bridge_tests.exe.");
            }
            var stdout = await proc.StandardOutput.ReadToEndAsync(ct);
            var stderr = await proc.StandardError.ReadToEndAsync(ct);
            await proc.WaitForExitAsync(ct);
            var combined = string.IsNullOrWhiteSpace(stderr)
                ? stdout.Trim()
                : $"{stdout.Trim()}\n{stderr.Trim()}";
            return (proc.ExitCode == 0, combined);
        }
        catch (Exception ex)
        {
            return (false, $"C++ self-test execution error: {ex.Message}");
        }
    }

    public static Task<(bool Ok, string Response)> PingHeartbeatAsync(CancellationToken ct = default) =>
        SendCommandAsync("HEARTBEAT_PING", new { client = "windows-csharp-v0.3.0" }, ct: ct);

    public static Task<(bool Ok, string Response)> RequestDesktopStatusAsync(CancellationToken ct = default) =>
        SendCommandAsync("DESKTOP_STATUS_REPORT", new { }, ct: ct);

    public static Task<(bool Ok, string Response)> ReleaseAllInputAsync(CancellationToken ct = default)
    {
        CppNativeBridge.ReleaseAllKeys();
        return SendCommandAsync("RELEASE_ALL_INPUT", new { reason = "emergency_stop_or_takeover" }, ct: ct);
    }

    public static Task<(bool Ok, string Response)> UpdateSessionPermissionsAsync(AgentPolicyConfig policy, CancellationToken ct = default)
    {
        CppNativeBridge.SyncInputPermissions(policy.AllowDesktopControl, policy.AllowClipboard);
        return SendCommandAsync("UPDATE_SESSION_PERMISSIONS", new
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
}
