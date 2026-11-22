using System;
using System.Diagnostics;
using System.IO;
using System.IO.Pipes;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Encodings.Web;
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
    /// <summary>Legacy insecure default — never use silently. Kept only to detect/refuse misconfiguration.</summary>
    public const string DefaultPipeSecret = "aibridge-local-pipe-hmac-v1";
    public static string ActivePipeName { get; set; } = PipeName;
    public static string? ActiveHmacSecret { get; set; }
    public const int MaxMessageBytes = 65536;

    public static bool IsInsecureDefaultSecret(string? secret) =>
        string.Equals(secret?.Trim(), DefaultPipeSecret, StringComparison.Ordinal);

    /// <summary>
    /// Resolve the active pipe HMAC. Never falls back to <see cref="DefaultPipeSecret"/>.
    /// Generates a cryptographically random secret when none is configured.
    /// </summary>
    public static string RequireHmacSecret(string? preferred = null)
    {
        var candidate = !string.IsNullOrWhiteSpace(preferred)
            ? preferred.Trim()
            : (!string.IsNullOrWhiteSpace(ActiveHmacSecret) ? ActiveHmacSecret!.Trim() : null);
        if (string.IsNullOrWhiteSpace(candidate) || IsInsecureDefaultSecret(candidate))
        {
            candidate = Convert.ToHexStringLower(RandomNumberGenerator.GetBytes(24));
        }
        ActiveHmacSecret = candidate;
        return candidate;
    }

    private static Process? _managedServiceProcess;
    private static Process? _managedWebRtcProcess;
    private static readonly object _procLock = new();

    public static bool IsCppWebRtcRunning
    {
        get
        {
            lock (_procLock)
            {
                return _managedWebRtcProcess is { HasExited: false };
            }
        }
    }

    public static string BuildCppWebRtcArguments(
        string controlUrl,
        string deviceToken,
        string sessionId,
        int monitorIndex = 0,
        int fps = 30,
        int bitrateKbps = 4500,
        string? configPath = null)
    {
        int clampedFps = Math.Clamp(fps, 1, 60);
        int clampedKbps = Math.Clamp(bitrateKbps, 250, 50_000);
        var parts = new System.Collections.Generic.List<string>
        {
            $"--monitor {Math.Max(0, monitorIndex)}",
            $"--fps {clampedFps}",
            $"--bitrate-kbps {clampedKbps}",
        };
        if (!string.IsNullOrWhiteSpace(controlUrl))
        {
            parts.Add($"--control-url \"{controlUrl.Trim()}\"");
        }
        if (!string.IsNullOrWhiteSpace(deviceToken))
        {
            parts.Add($"--device-token \"{deviceToken.Trim()}\"");
        }
        if (!string.IsNullOrWhiteSpace(sessionId))
        {
            parts.Add($"--session-id \"{sessionId.Trim()}\"");
        }
        if (!string.IsNullOrWhiteSpace(configPath) && File.Exists(configPath))
        {
            parts.Add($"--config \"{configPath}\"");
        }
        return string.Join(" ", parts);
    }

    public static (bool Ok, string Detail) StartCppWebRtcSession(
        string controlUrl,
        string deviceToken,
        string sessionId,
        int monitorIndex = 0,
        int fps = 30,
        int bitrateKbps = 4500)
    {
        StopCppWebRtcSession();
        var desktopExe = FindCppExecutable("ai_bridge_desktop.exe");
        if (desktopExe is null)
        {
            return (false, "ai_bridge_desktop.exe not found in application directory.");
        }
        lock (_procLock)
        {
            try
            {
                var home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
                var cfgDir = Path.Combine(home, ".ai-bridge");
                // Prefer sealed runtime JSON (DPAPI + \\uXXXX extract fixed);
                // fall back to plaintext companion when sealed is missing.
                var plainCfg = Path.Combine(cfgDir, "cpp_service_runtime.plain.json");
                var sealedCfg = Path.Combine(cfgDir, "cpp_service_runtime.json");
                var cfgPath = File.Exists(sealedCfg) ? sealedCfg : (File.Exists(plainCfg) ? plainCfg : null);
                var args = BuildCppWebRtcArguments(controlUrl, deviceToken, sessionId, monitorIndex, fps, bitrateKbps, cfgPath);
                var psi = new ProcessStartInfo
                {
                    FileName = desktopExe,
                    Arguments = args,
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    WindowStyle = ProcessWindowStyle.Hidden,
                    WorkingDirectory = Path.GetDirectoryName(desktopExe) ?? AppContext.BaseDirectory,
                    RedirectStandardError = true,
                    RedirectStandardOutput = true,
                };
                _managedWebRtcProcess = Process.Start(psi);
                if (_managedWebRtcProcess is null)
                {
                    return (false, "Failed to launch ai_bridge_desktop.exe.");
                }
                var pid = _managedWebRtcProcess.Id;
                // Drain a short window of stderr so early fatals (no backend,
                // DPAPI, missing session) surface instead of a false "started".
                var errBuf = new StringBuilder();
                var outBuf = new StringBuilder();
                _managedWebRtcProcess.ErrorDataReceived += (_, e) =>
                {
                    if (!string.IsNullOrEmpty(e.Data)) errBuf.AppendLine(e.Data);
                };
                _managedWebRtcProcess.OutputDataReceived += (_, e) =>
                {
                    if (!string.IsNullOrEmpty(e.Data)) outBuf.AppendLine(e.Data);
                };
                _managedWebRtcProcess.BeginErrorReadLine();
                _managedWebRtcProcess.BeginOutputReadLine();
                var exitedEarly = _managedWebRtcProcess.WaitForExit(1500);
                if (exitedEarly || _managedWebRtcProcess.HasExited)
                {
                    var code = _managedWebRtcProcess.HasExited ? _managedWebRtcProcess.ExitCode : -1;
                    var detail = (errBuf.ToString() + outBuf.ToString()).Trim();
                    if (string.IsNullOrWhiteSpace(detail))
                    {
                        detail = $"ai_bridge_desktop.exe exited immediately (code {code}).";
                    }
                    try { _managedWebRtcProcess.Dispose(); } catch { }
                    _managedWebRtcProcess = null;
                    return (false, detail.Length > 500 ? detail[..500] : detail);
                }
                return (true, $"Started C++20 H.264 WebRTC agent (PID {pid}, {Math.Clamp(fps, 1, 60)} FPS @ {Math.Clamp(bitrateKbps, 250, 50_000)} kbps).");
            }
            catch (Exception ex)
            {
                return (false, $"WebRTC launch error: {ex.Message}");
            }
        }
    }

    public static void StopCppWebRtcSession()
    {
        lock (_procLock)
        {
            try
            {
                if (_managedWebRtcProcess is { HasExited: false })
                {
                    _managedWebRtcProcess.Kill(entireProcessTree: true);
                    _managedWebRtcProcess.Dispose();
                }
            }
            catch { }
            finally
            {
                _managedWebRtcProcess = null;
            }
        }
    }

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
        var secret = RequireHmacSecret(hmacSecret);

        var cfg = cppSettings ?? new CppAgentSettings();
        var home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        var cfgDir = Path.Combine(home, ".ai-bridge");
        Directory.CreateDirectory(cfgDir);
        var cfgPath = Path.Combine(cfgDir, "cpp_service_runtime.json");
        var desktopExe = FindCppExecutable("ai_bridge_desktop.exe") ?? "";
        WriteRuntimeConfig(
            cfgPath,
            targetPipe,
            secret,
            controlUrl ?? "",
            deviceToken ?? "",
            desktopExe,
            cfg,
            allowDesktopControl: CppNativeBridge.InputDesktopAllowed,
            allowClipboard: CppNativeBridge.InputClipboardAllowed);

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
        StopCppWebRtcSession();
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

    public static string SignEnvelope(string messageType, object payload, string? hmacSecret = null)
    {
        var wire = BuildCppWireMessage(messageType, payload, hmacSecret);
        var parts = wire.Split('\n', 5);
        return JsonSerializer.Serialize(new
        {
            body = $"{parts[0]}|{parts[4]}",
            nonce = parts[1],
            timestamp = parts[2],
            hmac = parts[3],
            wire,
        });
    }

    public static string BuildCppWireMessage(string messageType, object payload, string? hmacSecret = null)
    {
        var secret = RequireHmacSecret(hmacSecret);
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
        var secret = RequireHmacSecret(hmacSecret);

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
        // Keep DLL (in-proc), pipe supervisor, and desktop WebRTC runtime config in sync.
        CppNativeBridge.SyncInputPermissions(policy.AllowDesktopControl, policy.AllowClipboard);
        try
        {
            var home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
            var cfgPath = Path.Combine(home, ".ai-bridge", "cpp_service_runtime.json");
            var cfgDir = Path.GetDirectoryName(cfgPath)!;
            Directory.CreateDirectory(cfgDir);
            WriteRuntimeConfig(
                cfgPath,
                ActivePipeName,
                RequireHmacSecret(),
                controlUrl: ReadRuntimeString(cfgPath, "control_url"),
                deviceToken: ReadRuntimeString(cfgPath, "device_token"),
                desktopExe: FindCppExecutable("ai_bridge_desktop.exe") ?? ReadRuntimeString(cfgPath, "desktop_agent_path"),
                cfg: new CppAgentSettings(),
                allowDesktopControl: policy.AllowDesktopControl,
                allowClipboard: policy.AllowClipboard,
                preserveExistingMediaSettings: true);
            // Sidecar file polled by ai_bridge_desktop.exe during an active WebRTC session.
            File.WriteAllText(
                Path.Combine(cfgDir, "session_permissions.json"),
                JsonSerializer.Serialize(new
                {
                    allow_desktop_control = policy.AllowDesktopControl,
                    allow_clipboard = policy.AllowClipboard,
                    updated_at = DateTimeOffset.UtcNow.ToUnixTimeSeconds(),
                }),
                Encoding.UTF8);
        }
        catch
        {
            // Non-fatal: pipe + DLL still updated below.
        }
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

    private static void WriteRuntimeConfig(
        string cfgPath,
        string targetPipe,
        string secret,
        string controlUrl,
        string deviceToken,
        string desktopExe,
        CppAgentSettings cfg,
        bool allowDesktopControl,
        bool allowClipboard,
        bool preserveExistingMediaSettings = false)
    {
        int fps = Math.Clamp(cfg.H264TargetFps, 1, 60);
        int kbps = Math.Clamp(cfg.H264BitrateKbps, 250, 50_000);
        int keyframe = Math.Clamp(cfg.H264KeyframeIntervalSec, 1, 15);
        bool preferDxgi = cfg.PreferDxgiCapture;
        bool preferHw = cfg.H264PreferHardware;
        bool adaptive = cfg.H264AdaptiveBitrate;
        string turnUrl = cfg.TurnUrl ?? "";
        string turnUser = cfg.TurnUsername ?? "";
        string turnCred = cfg.TurnCredential ?? "";

        if (preserveExistingMediaSettings && File.Exists(cfgPath))
        {
            try
            {
                using var doc = JsonDocument.Parse(File.ReadAllText(cfgPath));
                var root = doc.RootElement;
                if (root.TryGetProperty("target_fps", out var f) && f.TryGetInt32(out var fv)) fps = Math.Clamp(fv, 1, 60);
                if (root.TryGetProperty("bitrate_kbps", out var b) && b.TryGetInt32(out var bv)) kbps = Math.Clamp(bv, 250, 50_000);
                if (root.TryGetProperty("keyframe_interval_sec", out var k) && k.TryGetInt32(out var kv)) keyframe = Math.Clamp(kv, 1, 15);
                if (root.TryGetProperty("prefer_dxgi", out var d) && (d.ValueKind == JsonValueKind.True || d.ValueKind == JsonValueKind.False)) preferDxgi = d.GetBoolean();
                if (root.TryGetProperty("prefer_hardware_h264", out var h) && (h.ValueKind == JsonValueKind.True || h.ValueKind == JsonValueKind.False)) preferHw = h.GetBoolean();
                if (root.TryGetProperty("adaptive_bitrate", out var a) && (a.ValueKind == JsonValueKind.True || a.ValueKind == JsonValueKind.False)) adaptive = a.GetBoolean();
                if (root.TryGetProperty("turn_url", out var tu) && tu.ValueKind == JsonValueKind.String) turnUrl = tu.GetString() ?? turnUrl;
                if (root.TryGetProperty("turn_username", out var tn) && tn.ValueKind == JsonValueKind.String) turnUser = tn.GetString() ?? turnUser;
                if (root.TryGetProperty("turn_credential", out var tc) && tc.ValueKind == JsonValueKind.String) turnCred = tc.GetString() ?? turnCred;
            }
            catch
            {
                // Keep defaults from cfg
            }
        }

        // Seal secrets for on-disk config (idempotent if already aib-dpapi-v1:).
        var sealedSecret = SealIfNeeded(secret);
        var sealedToken = string.IsNullOrWhiteSpace(deviceToken) ? "" : SealIfNeeded(deviceToken);
        var pipeName = $@"\\.\pipe\{targetPipe}";
        // UnsafeRelaxedJsonEscaping keeps '+' '/' in DPAPI base64 as literals.
        // The default encoder emits \\u002B which older C++ extractors corrupted.
        var jsonOpts = new JsonSerializerOptions
        {
            WriteIndented = true,
            Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
        };
        var cfgJson = JsonSerializer.Serialize(new
        {
            pipe_name = pipeName,
            pipe_secret = sealedSecret,
            control_url = controlUrl ?? "",
            device_token = sealedToken,
            desktop_agent_path = desktopExe,
            turn_url = turnUrl,
            turn_username = turnUser,
            turn_credential = turnCred,
            target_fps = fps,
            bitrate_kbps = kbps,
            keyframe_interval_sec = keyframe,
            prefer_dxgi = preferDxgi,
            prefer_hardware_h264 = preferHw,
            adaptive_bitrate = adaptive,
            allow_desktop_control = allowDesktopControl,
            allow_clipboard = allowClipboard,
        }, jsonOpts);
        File.WriteAllText(cfgPath, cfgJson, new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));

        // Companion plaintext config for operators / fallback launches.
        // ACL is left to the user profile directory permissions.
        var plainPath = Path.Combine(
            Path.GetDirectoryName(cfgPath) ?? "",
            "cpp_service_runtime.plain.json");
        if (!string.IsNullOrEmpty(plainPath))
        {
            var plainSecret = secret.StartsWith(DpapiTokenStore.DpapiMagic, StringComparison.Ordinal)
                ? DpapiTokenStore.UnsealToken(secret)
                : secret;
            var plainToken = string.IsNullOrWhiteSpace(deviceToken)
                ? ""
                : (deviceToken.StartsWith(DpapiTokenStore.DpapiMagic, StringComparison.Ordinal)
                    ? DpapiTokenStore.UnsealToken(deviceToken)
                    : deviceToken);
            var plainJson = JsonSerializer.Serialize(new
            {
                pipe_name = pipeName,
                pipe_secret = plainSecret,
                control_url = controlUrl ?? "",
                device_token = plainToken,
                desktop_agent_path = desktopExe,
                turn_url = turnUrl,
                turn_username = turnUser,
                turn_credential = turnCred,
                target_fps = fps,
                bitrate_kbps = kbps,
                keyframe_interval_sec = keyframe,
                prefer_dxgi = preferDxgi,
                prefer_hardware_h264 = preferHw,
                adaptive_bitrate = adaptive,
                allow_desktop_control = allowDesktopControl,
                allow_clipboard = allowClipboard,
            }, jsonOpts);
            File.WriteAllText(plainPath, plainJson, new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));
        }
    }

    private static string ReadRuntimeString(string cfgPath, string key)
    {
        try
        {
            if (!File.Exists(cfgPath)) return "";
            using var doc = JsonDocument.Parse(File.ReadAllText(cfgPath));
            if (doc.RootElement.TryGetProperty(key, out var el) && el.ValueKind == JsonValueKind.String)
            {
                return el.GetString() ?? "";
            }
        }
        catch
        {
            // ignore
        }
        return "";
    }

    private static string SealIfNeeded(string value)
    {
        if (string.IsNullOrWhiteSpace(value)) return "";
        if (value.StartsWith("aib-dpapi-v1:", StringComparison.Ordinal)) return value;
        return DpapiTokenStore.SealToken(value);
    }
}
