using System;
using System.Buffers.Binary;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Net.Sockets;
using System.Net.WebSockets;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using AIBridgeClient.Models;

namespace AIBridgeClient.Services;

public sealed record Nsl1Frame(
    byte Version,
    byte MsgType,
    ushort Flags,
    uint Generation,
    uint Sequence,
    byte[] Payload)
{
    public bool IsKeyframe => (Flags & ScreenRelayService.Nsl1FlagKeyframe) != 0;
}

public sealed record Nsl1InputRecord(
    byte Type,
    ushort Code,
    int Value,
    double XNorm,
    double YNorm,
    ushort Buttons,
    ushort Modifiers);

/// <summary>
/// Outbound-only <c>nsl1</c> binary desktop streaming client compatible with
/// <c>vercel-labs/screen-relay</c> and AI Bridge's Control Plane WebCodecs relay.
/// Primary encoder: spawns local <c>relay-engine.exe</c> on <c>127.0.0.1:&lt;ephemeral&gt;</c>
/// and bridges its <c>/stream?take=1&amp;clipboard=1</c> packets zero-copy over verified WSS.
/// Warm fallback: streams live DXGI + Media Foundation H.264 Annex-B frames via <see cref="CppNativeBridge"/>.
/// </summary>
public static class ScreenRelayService
{
    public const int Nsl1HeaderSize = 20;
    public const int Nsl1VideoMetaSize = 12;
    public const int Nsl1CursorMetaSize = 8;
    public const int Nsl1InputRecordSize = 16;
    public const int Nsl1MaxPayloadBytes = 8 * 1024 * 1024 + 12;
    public const int Nsl1MaxInputBatch = 256;

    public const byte Nsl1Version = 1;

    // Exact upstream NSL1 message types (PROTOCOL.md)
    public const byte Nsl1MsgVideo = 1;
    public const byte Nsl1MsgAck = 2;
    public const byte Nsl1MsgInput = 3;
    public const byte Nsl1MsgControlReady = 4;
    public const byte Nsl1MsgControl = 5;
    public const byte Nsl1MsgCursor = 6;
    public const byte Nsl1MsgClipboard = 7;

    public const ushort Nsl1FlagKeyframe = 0x0001;

    // Exact upstream 16-byte input event types (PROTOCOL.md)
    public const byte InputKeyDown = 1;
    public const byte InputKeyUp = 2;
    public const byte InputPointerMove = 3;
    public const byte InputButtonDown = 4;
    public const byte InputButtonUp = 5;
    public const byte InputWheel = 6;
    public const byte InputReleaseAll = 7;
    public const byte InputText = 8;

    // Backwards-compatible aliases
    public const byte InputPointerButton = InputButtonDown;
    public const byte InputUnicode = InputText;

    // Exact upstream mouse button codes (1=left, 2=middle, 3=right)
    public const ushort ButtonLeft = 1;
    public const ushort ButtonMiddle = 2;
    public const ushort ButtonRight = 3;

    private sealed class NativeRelayEngineHandle : IDisposable
    {
        public required Process Process { get; init; }
        public required int Port { get; init; }
        public required string Token { get; init; }
        public required string RuntimeDir { get; init; }

        public bool IsRunning
        {
            get
            {
                try { return !Process.HasExited; }
                catch { return false; }
            }
        }

        public void Dispose()
        {
            try
            {
                if (!Process.HasExited)
                {
                    Process.Kill(entireProcessTree: true);
                    Process.WaitForExit(1500);
                }
            }
            catch { }
            try { Process.Dispose(); } catch { }
            try
            {
                if (Directory.Exists(RuntimeDir))
                {
                    Directory.Delete(RuntimeDir, recursive: true);
                }
            }
            catch { }
        }
    }

    private sealed class ActiveRelaySession : IDisposable
    {
        public string SessionId { get; init; } = "";
        public CancellationTokenSource Cts { get; } = new();
        public volatile int MonitorIndex;
        public volatile int ForceKeyframe = 1;
        public long LastAckSeq;
        public long LastViewerAckTicks;
        public long FramesSent;
        public int Width;
        public int Height;
        public string Backend = "cpp_mf_h264_nsl1";
        public byte[]? LatestKeyframePacket;
        public NativeRelayEngineHandle? NativeEngine;

        public void Dispose()
        {
            try { Cts.Cancel(); } catch { }
            try { NativeEngine?.Dispose(); } catch { }
            NativeEngine = null;
            Cts.Dispose();
        }
    }

    private static readonly ConcurrentDictionary<string, ActiveRelaySession> ActiveSessions = new(StringComparer.Ordinal);

    public static string? FindRelayEngineBinary()
    {
        var envBin = Environment.GetEnvironmentVariable("AI_BRIDGE_SCREEN_RELAY_BIN")?.Trim();
        if (!string.IsNullOrWhiteSpace(envBin) && File.Exists(envBin))
        {
            return envBin;
        }
        var candidates = new[]
        {
            Path.Combine(AppContext.BaseDirectory, "relay-engine.exe"),
            Path.Combine(AppContext.BaseDirectory, "bin", "relay-engine.exe"),
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".ai-bridge", "bin", "relay-engine.exe"),
        };
        foreach (var c in candidates)
        {
            if (File.Exists(c)) return c;
        }
        var pathEnv = Environment.GetEnvironmentVariable("PATH") ?? "";
        foreach (var dir in pathEnv.Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries))
        {
            var cand = Path.Combine(dir.Trim(), "relay-engine.exe");
            if (File.Exists(cand)) return cand;
        }
        return null;
    }

    public static object DescribeStatus()
    {
        var bin = FindRelayEngineBinary();
        bool cppReady = CppNativeBridge.IsAvailable();
        bool hasBin = !string.IsNullOrEmpty(bin);
        return new
        {
            available = hasBin || cppReady,
            mode = hasBin ? "relay_engine" : (cppReady ? "cpp_mf_h264_nsl1" : "unavailable"),
            protocol = "nsl1",
            codec = "H264",
            webcodecs = true,
            loopback_only = true,
            native_binary_installed = hasBin,
            native_binary_path = bin ?? "",
            cpp_h264_available = cppReady,
            max_fps = hasBin ? 120 : 60,
            active_sessions = ActiveSessions.Count,
        };
    }

    public static byte[] EncodeFrame(
        byte msgType,
        ReadOnlySpan<byte> payload,
        ushort flags = 0,
        uint generation = 1,
        uint sequence = 0)
    {
        if (payload.Length > Nsl1MaxPayloadBytes)
        {
            throw new ArgumentOutOfRangeException(nameof(payload), $"NSL1 payload exceeds 8 MiB limit ({payload.Length} bytes).");
        }
        ushort cleanFlags = msgType == Nsl1MsgVideo ? (ushort)(flags & Nsl1FlagKeyframe) : (ushort)0;
        uint cleanGen = generation == 0 ? 1u : generation;
        var packet = new byte[Nsl1HeaderSize + payload.Length];
        packet[0] = (byte)'N';
        packet[1] = (byte)'S';
        packet[2] = (byte)'L';
        packet[3] = (byte)'1';
        packet[4] = Nsl1Version;
        packet[5] = msgType;
        BinaryPrimitives.WriteUInt16BigEndian(packet.AsSpan(6, 2), cleanFlags);
        BinaryPrimitives.WriteUInt32BigEndian(packet.AsSpan(8, 4), cleanGen);
        BinaryPrimitives.WriteUInt32BigEndian(packet.AsSpan(12, 4), sequence);
        BinaryPrimitives.WriteUInt32BigEndian(packet.AsSpan(16, 4), (uint)payload.Length);
        payload.CopyTo(packet.AsSpan(Nsl1HeaderSize));
        return packet;
    }

    public static byte[] EncodeAckFrame(uint sequence, uint generation = 1, uint? viewerWaitUs = null)
    {
        if (!viewerWaitUs.HasValue)
        {
            return EncodeFrame(Nsl1MsgAck, ReadOnlySpan<byte>.Empty, 0, generation, sequence);
        }
        Span<byte> waitBuf = stackalloc byte[4];
        BinaryPrimitives.WriteUInt32BigEndian(waitBuf, viewerWaitUs.Value);
        return EncodeFrame(Nsl1MsgAck, waitBuf, 0, generation, sequence);
    }

    public static Nsl1Frame DecodeFrame(ReadOnlySpan<byte> raw)
    {
        if (raw.Length < Nsl1HeaderSize)
        {
            throw new ArgumentException($"NSL1 frame too short ({raw.Length} < {Nsl1HeaderSize}).");
        }
        if (raw[0] != (byte)'N' || raw[1] != (byte)'S' || raw[2] != (byte)'L' || raw[3] != (byte)'1')
        {
            throw new ArgumentException("Invalid NSL1 magic header.");
        }
        byte ver = raw[4];
        if (ver != Nsl1Version)
        {
            throw new ArgumentException($"Unsupported NSL1 version {ver}.");
        }
        byte msgType = raw[5];
        ushort flags = BinaryPrimitives.ReadUInt16BigEndian(raw.Slice(6, 2));
        uint generation = BinaryPrimitives.ReadUInt32BigEndian(raw.Slice(8, 4));
        uint seq = BinaryPrimitives.ReadUInt32BigEndian(raw.Slice(12, 4));
        uint payloadLen = BinaryPrimitives.ReadUInt32BigEndian(raw.Slice(16, 4));
        if (payloadLen > Nsl1MaxPayloadBytes)
        {
            throw new ArgumentException($"NSL1 payload exceeds 8 MiB limit ({payloadLen}).");
        }
        if (raw.Length - Nsl1HeaderSize != payloadLen)
        {
            throw new ArgumentException("NSL1 payload length mismatch.");
        }
        return new Nsl1Frame(ver, msgType, flags, generation, seq, raw.Slice(Nsl1HeaderSize).ToArray());
    }

    public static byte[] EncodeVideoFrame(
        ReadOnlySpan<byte> annexbBytes,
        int width,
        int height,
        ulong timestampUs,
        bool isKeyframe,
        uint generation = 1,
        uint sequence = 1)
    {
        var payload = new byte[Nsl1VideoMetaSize + annexbBytes.Length];
        BinaryPrimitives.WriteUInt16BigEndian(payload.AsSpan(0, 2), (ushort)Math.Clamp(width, 1, ushort.MaxValue));
        BinaryPrimitives.WriteUInt16BigEndian(payload.AsSpan(2, 2), (ushort)Math.Clamp(height, 1, ushort.MaxValue));
        BinaryPrimitives.WriteUInt64BigEndian(payload.AsSpan(4, 8), timestampUs);
        annexbBytes.CopyTo(payload.AsSpan(Nsl1VideoMetaSize));
        ushort flags = isKeyframe ? Nsl1FlagKeyframe : (ushort)0;
        return EncodeFrame(Nsl1MsgVideo, payload, flags, generation, sequence == 0 ? 1u : sequence);
    }

    public static (int Width, int Height, ulong TimestampUs, byte[] AnnexB) DecodeVideoPayload(ReadOnlySpan<byte> payload)
    {
        if (payload.Length < Nsl1VideoMetaSize)
        {
            throw new ArgumentException("NSL1 video payload shorter than metadata header.");
        }
        int w = BinaryPrimitives.ReadUInt16BigEndian(payload.Slice(0, 2));
        int h = BinaryPrimitives.ReadUInt16BigEndian(payload.Slice(2, 2));
        ulong ts = BinaryPrimitives.ReadUInt64BigEndian(payload.Slice(4, 8));
        return (w, h, ts, payload.Slice(Nsl1VideoMetaSize).ToArray());
    }

    public static List<Nsl1InputRecord> DecodeInputRecords(ReadOnlySpan<byte> payload)
    {
        if (payload.Length == 0 || payload.Length % Nsl1InputRecordSize != 0)
        {
            throw new ArgumentException("NSL1 binary input payload must be a non-empty multiple of 16 bytes.");
        }
        int count = payload.Length / Nsl1InputRecordSize;
        if (count > Nsl1MaxInputBatch)
        {
            throw new ArgumentException($"NSL1 binary input batch exceeds {Nsl1MaxInputBatch} records.");
        }
        var records = new List<Nsl1InputRecord>(count);
        for (int i = 0; i < count; i++)
        {
            var slice = payload.Slice(i * Nsl1InputRecordSize, Nsl1InputRecordSize);
            byte type = slice[0];
            ushort code = BinaryPrimitives.ReadUInt16BigEndian(slice.Slice(2, 2));
            int value = BinaryPrimitives.ReadInt32BigEndian(slice.Slice(4, 4));
            ushort xU16 = BinaryPrimitives.ReadUInt16BigEndian(slice.Slice(8, 2));
            ushort yU16 = BinaryPrimitives.ReadUInt16BigEndian(slice.Slice(10, 2));
            ushort buttons = (ushort)(BinaryPrimitives.ReadUInt16BigEndian(slice.Slice(12, 2)) & 0x001F);
            ushort modifiers = (ushort)(BinaryPrimitives.ReadUInt16BigEndian(slice.Slice(14, 2)) & 0x00FF);
            records.Add(new Nsl1InputRecord(type, code, value, xU16 / 65535.0, yU16 / 65535.0, buttons, modifiers));
        }
        return records;
    }

    public static ushort HidUsageToVirtualKey(ushort usage)
    {
        if (usage is >= 4 and <= 29) return (ushort)('A' + (usage - 4));
        if (usage is >= 30 and <= 38) return (ushort)('1' + (usage - 30));
        if (usage == 39) return '0';
        if (usage is >= 58 and <= 69) return (ushort)(0x70 + (usage - 58)); // F1..F12
        if (usage is >= 104 and <= 115) return (ushort)(0x7C + (usage - 104)); // F13..F24
        return usage switch
        {
            40 or 88 => 0x0D, // Enter / NumpadEnter
            41 => 0x1B,       // Escape
            42 => 0x08,       // Backspace
            43 => 0x09,       // Tab
            44 => 0x20,       // Space
            45 => 0xBD,       // Minus
            46 => 0xBB,       // Equal
            47 => 0xDB,       // [
            48 => 0xDD,       // ]
            49 => 0xDC,       // \
            51 => 0xBA,       // ;
            52 => 0xDE,       // '
            53 => 0xC0,       // `
            54 => 0xBC,       // ,
            55 => 0xBE,       // .
            56 => 0xBF,       // /
            57 => 0x14,       // CapsLock
            70 => 0x2C,       // PrintScreen
            71 => 0x91,       // ScrollLock
            72 => 0x13,       // Pause
            73 => 0x2D,       // Insert
            74 => 0x24,       // Home
            75 => 0x21,       // PageUp
            76 => 0x2E,       // Delete
            77 => 0x23,       // End
            78 => 0x22,       // PageDown
            79 => 0x27,       // Right
            80 => 0x25,       // Left
            81 => 0x28,       // Down
            82 => 0x26,       // Up
            83 => 0x90,       // NumLock
            224 => 0xA2,      // Left Ctrl
            225 => 0xA0,      // Left Shift
            226 => 0xA4,      // Left Alt
            227 => 0x5B,      // Left Win
            228 => 0xA3,      // Right Ctrl
            229 => 0xA1,      // Right Shift
            230 => 0xA5,      // Right Alt
            231 => 0x5C,      // Right Win
            _ => 0,
        };
    }

    public static int ExecutePolicyGatedInputRecords(
        IReadOnlyList<Nsl1InputRecord> records,
        AgentGuiConfig config,
        int screenWidth,
        int screenHeight)
    {
        if (config?.Policy is null || !config.Policy.AllowDesktopControl)
        {
            return 0;
        }
        int executed = 0;
        int w = Math.Max(1, screenWidth);
        int h = Math.Max(1, screenHeight);
        foreach (var rec in records)
        {
            int px = (int)Math.Round(Math.Clamp(rec.XNorm, 0.0, 1.0) * w);
            int py = (int)Math.Round(Math.Clamp(rec.YNorm, 0.0, 1.0) * h);
            switch (rec.Type)
            {
                case InputPointerMove:
                    DesktopInputService.MoveMouseInstant(px, py);
                    executed++;
                    break;
                case InputButtonDown:
                case InputButtonUp:
                    {
                        string btn = rec.Code == ButtonRight ? "right" : (rec.Code == ButtonMiddle ? "middle" : "left");
                        DesktopInputService.MouseButton(px, py, btn, down: rec.Type == InputButtonDown);
                        executed++;
                        break;
                    }
                case InputWheel:
                    // Upstream positive delta = scroll down, whereas Win32 WHEEL_DELTA positive = scroll up
                    DesktopInputService.ScrollMouse(
                        rec.Code == 0 ? -rec.Value : rec.Value,
                        px,
                        py,
                        horizontal: rec.Code == 1);
                    executed++;
                    break;
                case InputText:
                    if (rec.Value > 0 && rec.Value <= 0x10FFFF && (rec.Value < 0xD800 || rec.Value > 0xDFFF))
                    {
                        DesktopInputService.TypeText(char.ConvertFromUtf32(rec.Value));
                        executed++;
                    }
                    break;
                case InputKeyDown:
                case InputKeyUp:
                    {
                        ushort vk = HidUsageToVirtualKey(rec.Code);
                        if (vk != 0)
                        {
                            DesktopInputService.KeyTransition(vk, down: rec.Type == InputKeyDown);
                            executed++;
                        }
                        break;
                    }
                case InputReleaseAll:
                    foreach (ushort modVk in new ushort[] { 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x5B, 0x5C })
                    {
                        DesktopInputService.KeyTransition(modVk, down: false);
                    }
                    executed++;
                    break;
            }
        }
        return executed;
    }

    public static object StartSession(
        string controlUrl,
        string deviceToken,
        string sessionId,
        AgentGuiConfig config,
        int monitorIndex = 0,
        int fps = 60,
        int bitrateKbps = 4500)
    {
        if (string.IsNullOrWhiteSpace(controlUrl) || string.IsNullOrWhiteSpace(deviceToken))
        {
            throw new InvalidOperationException("Control URL and device token are required to start screen-relay.");
        }
        if (string.IsNullOrWhiteSpace(sessionId))
        {
            throw new ArgumentException("session_id is required.");
        }
        var relayBin = FindRelayEngineBinary();
        bool cppAvailable = CppNativeBridge.IsAvailable();
        if (string.IsNullOrEmpty(relayBin) && !cppAvailable)
        {
            throw new InvalidOperationException("Neither relay-engine.exe nor ai_bridge_native.dll is available to encode H.264 NSL1 stream.");
        }

        StopSession(sessionId);

        bool preferNativeEngine = !string.IsNullOrEmpty(relayBin);
        int effFps = preferNativeEngine ? Math.Clamp(fps, 10, 120) : Math.Clamp(fps, 1, 60);
        int effKbps = Math.Clamp(bitrateKbps, 1000, 40_000);

        var session = new ActiveRelaySession
        {
            SessionId = sessionId,
            MonitorIndex = Math.Max(0, monitorIndex),
            Backend = preferNativeEngine ? "relay_engine" : "cpp_mf_h264_nsl1",
        };
        ActiveSessions[sessionId] = session;

        _ = Task.Run(() => RunOutboundLoopAsync(controlUrl, deviceToken, session, config, relayBin, effFps, effKbps));

        return new
        {
            session_id = sessionId,
            status = "started",
            transport = "screen_relay",
            protocol = "nsl1",
            codec = "H264",
            backend = session.Backend,
            fps = effFps,
            bitrate_kbps = effKbps,
            monitor_index = session.MonitorIndex,
        };
    }

    public static object StopSession(string? sessionId)
    {
        if (string.IsNullOrWhiteSpace(sessionId))
        {
            int stopped = 0;
            foreach (var key in ActiveSessions.Keys)
            {
                if (ActiveSessions.TryRemove(key, out var s))
                {
                    s.Dispose();
                    stopped++;
                }
            }
            return new { status = stopped > 0 ? "stopped" : "not_running", stopped_count = stopped };
        }
        if (ActiveSessions.TryRemove(sessionId, out var existing))
        {
            existing.Dispose();
            return new { session_id = sessionId, status = "stopped" };
        }
        return new { session_id = sessionId, status = "not_running" };
    }

    private static int PickFreeLoopbackPort()
    {
        using var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        return ((IPEndPoint)listener.LocalEndpoint).Port;
    }

    private static string EnsureViewerAssetsDir(string runtimeDir, string binaryPath)
    {
        var binDir = Path.GetDirectoryName(Path.GetFullPath(binaryPath)) ?? runtimeDir;
        var bundledViewer = Path.Combine(binDir, "viewer");
        if (File.Exists(Path.Combine(bundledViewer, "index.html")) &&
            File.Exists(Path.Combine(bundledViewer, "viewer.js")))
        {
            return bundledViewer;
        }
        var stubViewer = Path.Combine(runtimeDir, "viewer");
        Directory.CreateDirectory(stubViewer);
        File.WriteAllText(Path.Combine(stubViewer, "index.html"), "<!doctype html><title>AI Bridge Relay Loopback</title>\n", Encoding.UTF8);
        File.WriteAllText(Path.Combine(stubViewer, "viewer.js"), "// loopback headless viewer stub\n", Encoding.UTF8);
        return stubViewer;
    }

    private static void RestrictFileToCurrentUser(string filePath)
    {
        try
        {
            var user = Environment.UserName;
            if (string.IsNullOrWhiteSpace(user)) return;
            using var icacls = Process.Start(new ProcessStartInfo
            {
                FileName = "icacls",
                ArgumentList = { filePath, "/inheritance:r", "/grant:r", $"{user}:(R,W)" },
                CreateNoWindow = true,
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
            });
            icacls?.WaitForExit(2500);
        }
        catch { }
    }

    private static async Task<NativeRelayEngineHandle?> TryLaunchNativeRelayEngineAsync(
        string? binaryPath,
        int fps,
        int bitrateKbps,
        CancellationToken ct)
    {
        if (string.IsNullOrWhiteSpace(binaryPath) || !File.Exists(binaryPath))
        {
            return null;
        }
        string runtimeDir = Path.Combine(Path.GetTempPath(), $"aibridge-relay-{Guid.NewGuid():N}");
        Directory.CreateDirectory(runtimeDir);
        string tokenHex = Convert.ToHexStringLower(RandomNumberGenerator.GetBytes(32));
        string tokenFile = Path.Combine(runtimeDir, "session.token");
        await File.WriteAllTextAsync(tokenFile, tokenHex + "\n", Encoding.ASCII, ct);
        RestrictFileToCurrentUser(tokenFile);
        string viewerDir = EnsureViewerAssetsDir(runtimeDir, binaryPath);

        int port = PickFreeLoopbackPort();
        long bpsStart = Math.Clamp((long)bitrateKbps * 1000L, 1_500_000L, 8_000_000L);
        long bpsMin = Math.Min(1_500_000L, bpsStart);
        long bpsMax = Math.Max(8_000_000L, bpsStart);

        var psi = new ProcessStartInfo
        {
            FileName = binaryPath,
            CreateNoWindow = true,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            WorkingDirectory = Path.GetDirectoryName(Path.GetFullPath(binaryPath)) ?? runtimeDir,
        };
        psi.ArgumentList.Add("--bind");
        psi.ArgumentList.Add("127.0.0.1");
        psi.ArgumentList.Add("--port");
        psi.ArgumentList.Add(port.ToString());
        psi.ArgumentList.Add("--fps");
        psi.ArgumentList.Add(Math.Clamp(fps, 10, 120).ToString());
        psi.ArgumentList.Add("--bitrate-min");
        psi.ArgumentList.Add(bpsMin.ToString());
        psi.ArgumentList.Add("--bitrate-start");
        psi.ArgumentList.Add(bpsStart.ToString());
        psi.ArgumentList.Add("--bitrate-max");
        psi.ArgumentList.Add(bpsMax.ToString());
        psi.ArgumentList.Add("--token-file");
        psi.ArgumentList.Add(tokenFile);
        psi.ArgumentList.Add("--viewer-dir");
        psi.ArgumentList.Add(viewerDir);

        Process? proc = null;
        try
        {
            proc = Process.Start(psi);
            if (proc is null)
            {
                Directory.Delete(runtimeDir, recursive: true);
                return null;
            }
            var handle = new NativeRelayEngineHandle
            {
                Process = proc,
                Port = port,
                Token = tokenHex,
                RuntimeDir = runtimeDir,
            };

            using var http = new HttpClient { Timeout = TimeSpan.FromMilliseconds(600) };
            http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", tokenHex);
            var sw = Stopwatch.StartNew();
            while (sw.ElapsedMilliseconds < 4500 && !ct.IsCancellationRequested)
            {
                if (!handle.IsRunning)
                {
                    handle.Dispose();
                    return null;
                }
                try
                {
                    using var resp = await http.GetAsync($"http://127.0.0.1:{port}/capabilities", ct);
                    if (resp.IsSuccessStatusCode)
                    {
                        return handle;
                    }
                }
                catch
                {
                    await Task.Delay(80, ct);
                }
            }
            handle.Dispose();
            return null;
        }
        catch
        {
            try { proc?.Kill(entireProcessTree: true); } catch { }
            try { proc?.Dispose(); } catch { }
            try { Directory.Delete(runtimeDir, recursive: true); } catch { }
            return null;
        }
    }

    private static Uri BuildSessionWsUri(string controlUrl, string sessionId)
    {
        var trimmed = controlUrl.Trim().TrimEnd('/');
        string wsBase = trimmed.StartsWith("https://", StringComparison.OrdinalIgnoreCase)
            ? "wss://" + trimmed["https://".Length..]
            : (trimmed.StartsWith("http://", StringComparison.OrdinalIgnoreCase)
                ? "ws://" + trimmed["http://".Length..]
                : trimmed);
        return new Uri($"{wsBase}/api/v1/ws/sessions/{Uri.EscapeDataString(sessionId)}");
    }

    private static ClientWebSocket CreateControlPlaneWebSocket(Uri targetUri, string deviceToken)
    {
        var ws = new ClientWebSocket();
        ws.Options.SetRequestHeader("X-Device-Token", deviceToken);
        ws.Options.KeepAliveInterval = TimeSpan.FromSeconds(20);
        if (string.Equals(targetUri.Scheme, "wss", StringComparison.OrdinalIgnoreCase))
        {
            var httpsUri = new UriBuilder(targetUri) { Scheme = Uri.UriSchemeHttps }.Uri;
            ws.Options.RemoteCertificateValidationCallback = (_, cert, _, sslErrors) =>
            {
                X509Certificate2? cert2 = cert as X509Certificate2 ?? (cert is not null ? new X509Certificate2(cert) : null);
                return ControlPlaneClient.IsServerCertificateTrusted(httpsUri, cert2, sslErrors);
            };
        }
        return ws;
    }

    private static async Task<byte[]?> ReceiveFullMessageAsync(
        ClientWebSocket ws,
        byte[] scratch,
        CancellationToken ct)
    {
        using var ms = new MemoryStream();
        WebSocketReceiveResult result;
        do
        {
            result = await ws.ReceiveAsync(scratch, ct);
            if (result.MessageType == WebSocketMessageType.Close) return null;
            ms.Write(scratch, 0, result.Count);
        } while (!result.EndOfMessage && ms.Length <= Nsl1MaxPayloadBytes + 64);

        if (result.MessageType == WebSocketMessageType.Text)
        {
            // Prefix text with 0x00 sentinel? Better: return raw via tuple helper below.
            return null;
        }
        return ms.ToArray();
    }

    private static async Task<(WebSocketMessageType Type, byte[] Data)?> ReceiveWsEnvelopeAsync(
        ClientWebSocket ws,
        byte[] scratch,
        CancellationToken ct)
    {
        using var ms = new MemoryStream();
        WebSocketReceiveResult result;
        do
        {
            result = await ws.ReceiveAsync(scratch, ct);
            if (result.MessageType == WebSocketMessageType.Close) return null;
            ms.Write(scratch, 0, result.Count);
        } while (!result.EndOfMessage && ms.Length <= Nsl1MaxPayloadBytes + 64);
        return (result.MessageType, ms.ToArray());
    }

    private static async Task RunOutboundLoopAsync(
        string controlUrl,
        string deviceToken,
        ActiveRelaySession session,
        AgentGuiConfig config,
        string? relayBinaryPath,
        int fps,
        int bitrateKbps)
    {
        var ct = session.Cts.Token;
        var targetUri = BuildSessionWsUri(controlUrl, session.SessionId);
        try
        {
            session.NativeEngine = await TryLaunchNativeRelayEngineAsync(relayBinaryPath, fps, bitrateKbps, ct);
            if (session.NativeEngine is null)
            {
                if (!CppNativeBridge.IsAvailable())
                {
                    return;
                }
                session.Backend = "cpp_mf_h264_nsl1";
            }
            else
            {
                session.Backend = "relay_engine";
            }

            int backoffMs = 500;
            while (!ct.IsCancellationRequested)
            {
                Interlocked.Exchange(ref session.ForceKeyframe, 1);
                using var cpWs = CreateControlPlaneWebSocket(targetUri, deviceToken);
                try
                {
                    await cpWs.ConnectAsync(targetUri, ct);
                    backoffMs = 500;

                    if (session.NativeEngine is { IsRunning: true } nativeEng)
                    {
                        await BridgeNativeEngineToControlPlaneAsync(nativeEng, cpWs, session, config, ct);
                    }
                    else
                    {
                        await StreamCppFallbackToControlPlaneAsync(cpWs, session, config, Math.Clamp(fps, 1, 60), bitrateKbps, ct);
                    }
                }
                catch (OperationCanceledException) when (ct.IsCancellationRequested)
                {
                    break;
                }
                catch
                {
                    if (ct.IsCancellationRequested) break;
                    try { await Task.Delay(backoffMs, ct); } catch { break; }
                    backoffMs = Math.Min(8000, backoffMs * 2);
                }
            }
        }
        finally
        {
            ActiveSessions.TryRemove(session.SessionId, out _);
            session.Dispose();
        }
    }

    private static async Task BridgeNativeEngineToControlPlaneAsync(
        NativeRelayEngineHandle nativeEng,
        ClientWebSocket cpWs,
        ActiveRelaySession session,
        AgentGuiConfig config,
        CancellationToken ct)
    {
        using var engWs = new ClientWebSocket();
        engWs.Options.AddSubProtocol("nsl1");
        engWs.Options.AddSubProtocol($"bearer.{nativeEng.Token}");
        var localUri = new Uri($"ws://127.0.0.1:{nativeEng.Port}/stream?take=1&clipboard=1");
        await engWs.ConnectAsync(localUri, ct);

        using var linkedCts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        var lct = linkedCts.Token;
        using var cpSendLock = new SemaphoreSlim(1, 1);
        using var engSendLock = new SemaphoreSlim(1, 1);

        async Task SafeSendCpAsync(byte[] payload)
        {
            await cpSendLock.WaitAsync(lct);
            try
            {
                if (cpWs.State == WebSocketState.Open)
                {
                    await cpWs.SendAsync(payload, WebSocketMessageType.Binary, true, lct);
                }
            }
            finally
            {
                cpSendLock.Release();
            }
        }

        async Task SafeSendEngAsync(byte[] payload)
        {
            await engSendLock.WaitAsync(lct);
            try
            {
                if (engWs.State == WebSocketState.Open)
                {
                    await engWs.SendAsync(payload, WebSocketMessageType.Binary, true, lct);
                }
            }
            finally
            {
                engSendLock.Release();
            }
        }

        uint currentGen = 1;

        var pumpEngineToCp = Task.Run(async () =>
        {
            var buf = new byte[64 * 1024];
            while (!lct.IsCancellationRequested && engWs.State == WebSocketState.Open && cpWs.State == WebSocketState.Open)
            {
                var pkt = await ReceiveFullMessageAsync(engWs, buf, lct);
                if (pkt is null) break;
                try
                {
                    var frame = DecodeFrame(pkt);
                    currentGen = frame.Generation;
                    if (frame.MsgType == Nsl1MsgVideo)
                    {
                        Interlocked.Increment(ref session.FramesSent);
                        if (frame.Payload.Length >= Nsl1VideoMetaSize)
                        {
                            session.Width = BinaryPrimitives.ReadUInt16BigEndian(frame.Payload.AsSpan(0, 2));
                            session.Height = BinaryPrimitives.ReadUInt16BigEndian(frame.Payload.AsSpan(2, 2));
                        }
                        if (frame.IsKeyframe)
                        {
                            session.LatestKeyframePacket = pkt;
                        }
                        long lastViewerAck = Interlocked.Read(ref session.LastViewerAckTicks);
                        long elapsedAckMs = lastViewerAck == 0
                            ? 5000
                            : (Stopwatch.GetTimestamp() - lastViewerAck) * 1000L / Stopwatch.Frequency;
                        if (elapsedAckMs > 1500)
                        {
                            await SafeSendEngAsync(EncodeAckFrame(frame.Sequence, frame.Generation));
                        }
                    }
                    await SafeSendCpAsync(pkt);
                }
                catch
                {
                    // Ignore malformed packet
                }
            }
            linkedCts.Cancel();
        }, lct);

        var pumpCpToEngine = Task.Run(async () =>
        {
            var buf = new byte[64 * 1024];
            while (!lct.IsCancellationRequested && cpWs.State == WebSocketState.Open && engWs.State == WebSocketState.Open)
            {
                var env = await ReceiveWsEnvelopeAsync(cpWs, buf, lct);
                if (env is null) break;
                var (msgType, data) = env.Value;
                if (msgType == WebSocketMessageType.Binary)
                {
                    try
                    {
                        var frame = DecodeFrame(data);
                        if (frame.MsgType == Nsl1MsgAck)
                        {
                            Interlocked.Exchange(ref session.LastAckSeq, frame.Sequence);
                            Interlocked.Exchange(ref session.LastViewerAckTicks, Stopwatch.GetTimestamp());
                            await SafeSendEngAsync(data);
                        }
                        else if (frame.MsgType == Nsl1MsgControl)
                        {
                            var idr = session.LatestKeyframePacket;
                            if (idr is not null) await SafeSendCpAsync(idr);
                        }
                        else if (frame.MsgType == Nsl1MsgInput)
                        {
                            if (config?.Policy is not null && config.Policy.AllowDesktopControl &&
                                frame.Payload.Length >= Nsl1InputRecordSize &&
                                frame.Payload.Length % Nsl1InputRecordSize == 0)
                            {
                                int count = Math.Min(Nsl1MaxInputBatch, frame.Payload.Length / Nsl1InputRecordSize);
                                for (int i = 0; i < count; i++)
                                {
                                    var recSlice = frame.Payload.AsSpan(i * Nsl1InputRecordSize, Nsl1InputRecordSize);
                                    var singleInput = EncodeFrame(Nsl1MsgInput, recSlice, flags: 0, generation: currentGen, sequence: 0);
                                    await SafeSendEngAsync(singleInput);
                                }
                            }
                        }
                    }
                    catch { }
                }
                else if (msgType == WebSocketMessageType.Text)
                {
                    try
                    {
                        using var jdoc = JsonDocument.Parse(Encoding.UTF8.GetString(data));
                        var root = jdoc.RootElement;
                        var mtype = root.TryGetProperty("type", out var tEl) ? (tEl.GetString() ?? "").ToLowerInvariant() : "";
                        if (mtype == "session_terminated")
                        {
                            session.Cts.Cancel();
                            linkedCts.Cancel();
                            return;
                        }
                        if (mtype == "request_keyframe")
                        {
                            var idr = session.LatestKeyframePacket;
                            if (idr is not null) await SafeSendCpAsync(idr);
                        }
                    }
                    catch { }
                }
            }
            linkedCts.Cancel();
        }, lct);

        await Task.WhenAny(pumpEngineToCp, pumpCpToEngine);
        linkedCts.Cancel();
        try { await Task.WhenAll(pumpEngineToCp, pumpCpToEngine); } catch { }
        if (!ct.IsCancellationRequested)
        {
            throw new WebSocketException("Screen relay stream loop disconnected; triggering reconnect.");
        }
    }

    private static async Task StreamCppFallbackToControlPlaneAsync(
        ClientWebSocket ws,
        ActiveRelaySession session,
        AgentGuiConfig config,
        int fps,
        int bitrateKbps,
        CancellationToken ct)
    {
        using var linkedCts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        var lct = linkedCts.Token;

        var recvTask = Task.Run(async () =>
        {
            var buf = new byte[64 * 1024];
            while (!lct.IsCancellationRequested && ws.State == WebSocketState.Open)
            {
                var env = await ReceiveWsEnvelopeAsync(ws, buf, lct);
                if (env is null) break;
                var (msgType, data) = env.Value;
                if (msgType == WebSocketMessageType.Binary)
                {
                    try
                    {
                        var frame = DecodeFrame(data);
                        if (frame.MsgType == Nsl1MsgAck)
                        {
                            Interlocked.Exchange(ref session.LastAckSeq, frame.Sequence);
                            Interlocked.Exchange(ref session.LastViewerAckTicks, Stopwatch.GetTimestamp());
                        }
                        else if (frame.MsgType == Nsl1MsgControl)
                        {
                            Interlocked.Exchange(ref session.ForceKeyframe, 1);
                        }
                        else if (frame.MsgType == Nsl1MsgInput)
                        {
                            var recs = DecodeInputRecords(frame.Payload);
                            ExecutePolicyGatedInputRecords(
                                recs,
                                config,
                                session.Width > 0 ? session.Width : 1920,
                                session.Height > 0 ? session.Height : 1080);
                        }
                    }
                    catch { }
                }
                else if (msgType == WebSocketMessageType.Text)
                {
                    try
                    {
                        using var jdoc = JsonDocument.Parse(Encoding.UTF8.GetString(data));
                        var root = jdoc.RootElement;
                        var mtype = root.TryGetProperty("type", out var tEl) ? (tEl.GetString() ?? "").ToLowerInvariant() : "";
                        if (mtype == "session_terminated")
                        {
                            session.Cts.Cancel();
                            linkedCts.Cancel();
                            return;
                        }
                        if ((mtype == "switch_monitor" || mtype == "set_monitor") &&
                            root.TryGetProperty("monitor_index", out var mEl) && mEl.TryGetInt32(out var nextMon))
                        {
                            session.MonitorIndex = Math.Max(0, nextMon);
                            Interlocked.Exchange(ref session.ForceKeyframe, 1);
                        }
                        else if (mtype == "request_keyframe")
                        {
                            Interlocked.Exchange(ref session.ForceKeyframe, 1);
                        }
                    }
                    catch { }
                }
            }
            linkedCts.Cancel();
        }, lct);

        var sw = Stopwatch.StartNew();
        int intervalMs = Math.Max(16, 1000 / Math.Max(1, fps));
        int adaptiveKbps = Math.Clamp(bitrateKbps, 1500, 8000);
        uint seq = 0;

        while (!lct.IsCancellationRequested && ws.State == WebSocketState.Open)
        {
            long tickStart = sw.ElapsedMilliseconds;
            long lastAck = Interlocked.Read(ref session.LastAckSeq);
            long lastAckTicks = Interlocked.Read(ref session.LastViewerAckTicks);
            long ackAgeMs = lastAckTicks == 0 ? 5000 : (Stopwatch.GetTimestamp() - lastAckTicks) * 1000L / Stopwatch.Frequency;
            long inFlight = lastAck > 0 ? (long)seq - lastAck : 0;

            if (inFlight > 20 && ackAgeMs < 2500)
            {
                adaptiveKbps = Math.Max(1500, adaptiveKbps - 400);
                await Task.Delay(16, lct);
                continue;
            }
            if (inFlight <= 4 && adaptiveKbps < Math.Clamp(bitrateKbps, 1500, 8000))
            {
                adaptiveKbps = Math.Min(8000, adaptiveKbps + 150);
            }

            if (CppNativeBridge.TryCaptureMonitorBgra(session.MonitorIndex, out var bgra, out var w, out var h, out _) && bgra is not null)
            {
                session.Width = w;
                session.Height = h;
                bool key = Interlocked.Exchange(ref session.ForceKeyframe, 0) != 0 || (seq % (uint)Math.Max(15, fps * 2) == 0);
                if (CppNativeBridge.TryEncodeH264Frame(bgra, w, h, fps, adaptiveKbps, key, out var nal, out _) && nal is not null && nal.Length > 0)
                {
                    seq++;
                    ulong tsUs = (ulong)(sw.ElapsedTicks * 1_000_000L / Stopwatch.Frequency);
                    var packet = EncodeVideoFrame(nal, w, h, tsUs, key, generation: 1, sequence: seq);
                    if (key) session.LatestKeyframePacket = packet;
                    await ws.SendAsync(packet, WebSocketMessageType.Binary, endOfMessage: true, lct);
                    Interlocked.Increment(ref session.FramesSent);
                }
            }

            int elapsed = (int)(sw.ElapsedMilliseconds - tickStart);
            if (elapsed < intervalMs)
            {
                await Task.Delay(intervalMs - elapsed, lct);
            }
        }

        linkedCts.Cancel();
        try { await recvTask; } catch { }
        if (!ct.IsCancellationRequested)
        {
            throw new WebSocketException("Control Plane WebSocket closed; reconnecting.");
        }
    }
}
