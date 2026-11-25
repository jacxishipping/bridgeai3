using System;
using System.Buffers.Binary;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net.WebSockets;
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
    uint Sequence);

/// <summary>
/// Outbound-only <c>nsl1</c> binary desktop streaming client compatible with
/// <c>vercel-labs/screen-relay</c> and AI Bridge's Control Plane WebCodecs relay.
/// Streams live DXGI + Media Foundation H.264 Annex-B frames via <see cref="CppNativeBridge"/>
/// over <c>WS /api/v1/ws/sessions/{session_id}</c> with policy-gated 16-byte binary input.
/// </summary>
public static class ScreenRelayService
{
    public const int Nsl1HeaderSize = 20;
    public const int Nsl1VideoMetaSize = 12;
    public const int Nsl1InputRecordSize = 16;
    public const int Nsl1MaxPayloadBytes = 8 * 1024 * 1024;
    public const int Nsl1MaxInputBatch = 256;

    public const byte Nsl1Version = 1;
    public const byte Nsl1MsgVideo = 1;
    public const byte Nsl1MsgCursor = 2;
    public const byte Nsl1MsgAck = 3;
    public const byte Nsl1MsgInput = 4;
    public const byte Nsl1MsgControl = 5;

    public const ushort Nsl1FlagKeyframe = 0x0001;
    public const ushort Nsl1FlagConfigIncluded = 0x0002;

    public const byte InputPointerMove = 1;
    public const byte InputPointerButton = 2;
    public const byte InputWheel = 3;
    public const byte InputKeyDown = 4;
    public const byte InputKeyUp = 5;
    public const byte InputUnicode = 6;

    public const ushort ButtonLeft = 1;
    public const ushort ButtonRight = 2;
    public const ushort ButtonMiddle = 3;

    private sealed class ActiveRelaySession : IDisposable
    {
        public string SessionId { get; init; } = "";
        public CancellationTokenSource Cts { get; } = new();
        public volatile int MonitorIndex;
        public volatile int ForceKeyframe = 1;
        public long LastAckSeq;
        public long FramesSent;
        public int Width;
        public int Height;

        public void Dispose()
        {
            try { Cts.Cancel(); } catch { }
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
        return new
        {
            available = cppReady || !string.IsNullOrEmpty(bin),
            mode = !string.IsNullOrEmpty(bin) ? "relay_engine" : (cppReady ? "cpp_mf_h264_nsl1" : "unavailable"),
            protocol = "nsl1",
            codec = "H264",
            webcodecs = true,
            loopback_only = true,
            native_binary_installed = !string.IsNullOrEmpty(bin),
            native_binary_path = bin ?? "",
            cpp_h264_available = cppReady,
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
        var packet = new byte[Nsl1HeaderSize + payload.Length];
        packet[0] = (byte)'N';
        packet[1] = (byte)'S';
        packet[2] = (byte)'L';
        packet[3] = (byte)'1';
        packet[4] = Nsl1Version;
        packet[5] = msgType;
        BinaryPrimitives.WriteUInt16BigEndian(packet.AsSpan(6, 2), flags);
        BinaryPrimitives.WriteUInt32BigEndian(packet.AsSpan(8, 4), generation);
        BinaryPrimitives.WriteUInt32BigEndian(packet.AsSpan(12, 4), sequence);
        BinaryPrimitives.WriteUInt32BigEndian(packet.AsSpan(16, 4), (uint)payload.Length);
        payload.CopyTo(packet.AsSpan(Nsl1HeaderSize));
        return packet;
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
        uint sequence = 0)
    {
        var payload = new byte[Nsl1VideoMetaSize + annexbBytes.Length];
        BinaryPrimitives.WriteUInt16BigEndian(payload.AsSpan(0, 2), (ushort)Math.Clamp(width, 0, ushort.MaxValue));
        BinaryPrimitives.WriteUInt16BigEndian(payload.AsSpan(2, 2), (ushort)Math.Clamp(height, 0, ushort.MaxValue));
        BinaryPrimitives.WriteUInt64BigEndian(payload.AsSpan(4, 8), timestampUs);
        annexbBytes.CopyTo(payload.AsSpan(Nsl1VideoMetaSize));
        ushort flags = isKeyframe ? (ushort)(Nsl1FlagKeyframe | Nsl1FlagConfigIncluded) : (ushort)0;
        return EncodeFrame(Nsl1MsgVideo, payload, flags, generation, sequence);
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
            uint seq = BinaryPrimitives.ReadUInt32BigEndian(slice.Slice(12, 4));
            records.Add(new Nsl1InputRecord(type, code, value, xU16 / 65535.0, yU16 / 65535.0, seq));
        }
        return records;
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
                    DesktopInputService.MoveMouse(px, py);
                    executed++;
                    break;
                case InputPointerButton:
                    if (rec.Value == 1)
                    {
                        string btn = rec.Code == ButtonRight ? "right" : (rec.Code == ButtonMiddle ? "middle" : "left");
                        DesktopInputService.ClickMouse(px, py, btn, doubleClick: false);
                        executed++;
                    }
                    break;
                case InputWheel:
                    DesktopInputService.ScrollMouse(rec.Value, px, py);
                    executed++;
                    break;
                case InputUnicode:
                    if (rec.Value > 0 && rec.Value <= 0x10FFFF)
                    {
                        DesktopInputService.TypeText(char.ConvertFromUtf32(rec.Value));
                        executed++;
                    }
                    break;
                case InputKeyDown:
                    if (rec.Code is >= 8 and <= 255)
                    {
                        DesktopInputService.PressKey((byte)rec.Code);
                        executed++;
                    }
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
        int fps = 30,
        int bitrateKbps = 3500)
    {
        if (string.IsNullOrWhiteSpace(controlUrl) || string.IsNullOrWhiteSpace(deviceToken))
        {
            throw new InvalidOperationException("Control URL and device token are required to start screen-relay.");
        }
        if (string.IsNullOrWhiteSpace(sessionId))
        {
            throw new ArgumentException("session_id is required.");
        }
        if (!CppNativeBridge.IsAvailable())
        {
            throw new InvalidOperationException("ai_bridge_native.dll is unavailable; cannot encode H.264 NSL1 stream.");
        }

        StopSession(sessionId);

        var session = new ActiveRelaySession
        {
            SessionId = sessionId,
            MonitorIndex = Math.Max(0, monitorIndex),
        };
        ActiveSessions[sessionId] = session;

        int effFps = Math.Clamp(fps, 1, 60);
        int effKbps = Math.Clamp(bitrateKbps, 300, 20_000);
        _ = Task.Run(() => RunOutboundLoopAsync(controlUrl, deviceToken, session, config, effFps, effKbps));

        return new
        {
            session_id = sessionId,
            status = "started",
            transport = "screen_relay",
            protocol = "nsl1",
            codec = "H264",
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

    private static async Task RunOutboundLoopAsync(
        string controlUrl,
        string deviceToken,
        ActiveRelaySession session,
        AgentGuiConfig config,
        int fps,
        int bitrateKbps)
    {
        var ct = session.Cts.Token;
        using var ws = new ClientWebSocket();
        ws.Options.SetRequestHeader("X-Device-Token", deviceToken);
        try
        {
            await ws.ConnectAsync(BuildSessionWsUri(controlUrl, session.SessionId), ct);

            var recvTask = Task.Run(async () =>
            {
                var buf = new byte[64 * 1024];
                while (!ct.IsCancellationRequested && ws.State == WebSocketState.Open)
                {
                    using var ms = new MemoryStream();
                    WebSocketReceiveResult result;
                    do
                    {
                        result = await ws.ReceiveAsync(buf, ct);
                        if (result.MessageType == WebSocketMessageType.Close) return;
                        ms.Write(buf, 0, result.Count);
                    } while (!result.EndOfMessage && ms.Length <= Nsl1MaxPayloadBytes + 64);

                    if (result.MessageType == WebSocketMessageType.Binary)
                    {
                        try
                        {
                            var frame = DecodeFrame(ms.ToArray());
                            if (frame.MsgType == Nsl1MsgAck)
                            {
                                Interlocked.Exchange(ref session.LastAckSeq, frame.Sequence);
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
                        catch
                        {
                            // Ignore malformed binary packets
                        }
                    }
                    else if (result.MessageType == WebSocketMessageType.Text)
                    {
                        try
                        {
                            using var jdoc = JsonDocument.Parse(Encoding.UTF8.GetString(ms.ToArray()));
                            var root = jdoc.RootElement;
                            var mtype = root.TryGetProperty("type", out var tEl) ? (tEl.GetString() ?? "").ToLowerInvariant() : "";
                            if (mtype == "session_terminated")
                            {
                                session.Cts.Cancel();
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
                        catch
                        {
                            // Ignore transient signaling JSON
                        }
                    }
                }
            }, ct);

            var sw = Stopwatch.StartNew();
            int intervalMs = Math.Max(16, 1000 / Math.Max(1, fps));
            uint seq = 0;

            while (!ct.IsCancellationRequested && ws.State == WebSocketState.Open)
            {
                long tickStart = sw.ElapsedMilliseconds;
                long lastAck = Interlocked.Read(ref session.LastAckSeq);
                if (lastAck > 0 && seq - lastAck > 15)
                {
                    await Task.Delay(20, ct);
                    continue;
                }

                if (CppNativeBridge.TryCaptureMonitorBgra(session.MonitorIndex, out var bgra, out var w, out var h, out _) && bgra is not null)
                {
                    session.Width = w;
                    session.Height = h;
                    bool key = Interlocked.Exchange(ref session.ForceKeyframe, 0) != 0 || (seq % (uint)Math.Max(15, fps * 2) == 0);
                    if (CppNativeBridge.TryEncodeH264Frame(bgra, w, h, fps, bitrateKbps, key, out var nal, out _) && nal is not null && nal.Length > 0)
                    {
                        seq++;
                        ulong tsUs = (ulong)(sw.ElapsedTicks * 1_000_000L / Stopwatch.Frequency);
                        var packet = EncodeVideoFrame(nal, w, h, tsUs, key, generation: 1, sequence: seq);
                        await ws.SendAsync(packet, WebSocketMessageType.Binary, endOfMessage: true, ct);
                        Interlocked.Increment(ref session.FramesSent);
                    }
                }

                int elapsed = (int)(sw.ElapsedMilliseconds - tickStart);
                if (elapsed < intervalMs)
                {
                    await Task.Delay(intervalMs - elapsed, ct);
                }
            }

            try { await recvTask; } catch { }
        }
        catch
        {
            // Clean exit on cancellation or disconnect
        }
        finally
        {
            ActiveSessions.TryRemove(session.SessionId, out _);
            session.Dispose();
        }
    }
}
