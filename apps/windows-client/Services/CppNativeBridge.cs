using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;

namespace AIBridgeClient.Services;

/// <summary>
/// P/Invoke interop bridge to the native C++20 windows-agent shared library (ai_bridge_native.dll).
/// Provides hardware-accelerated DXGI Desktop Duplication capture, Media Foundation H.264 encoding,
/// multi-monitor DPI-normalized SendInput injection with held-key safety, and native system telemetry.
/// </summary>
public static class CppNativeBridge
{
    public const string DllName = "ai_bridge_native.dll";

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Ansi)]
    public struct AIBridgeCaptureMetrics
    {
        public int Valid;
        public int FromDxgi;
        public int SecureDesktop;
        public int Width;
        public int Height;
        public int Stride;
        public int MonitorIndex;
        public int CursorX;
        public int CursorY;
        public int CursorVisible;
        public int DirtyRectCount;
        public int MoveRectCount;
        public double CaptureTimeMs;
        public double EncodeTimeMs;
        public int EncodedBytes;
        public int IsHardwareEncoder;

        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)]
        public string EncoderName;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Ansi)]
    public struct AIBridgeMonitorInfo
    {
        public int Index;
        public int Left;
        public int Top;
        public int Right;
        public int Bottom;
        public int Width;
        public int Height;
        public int RotationDegrees;
        public int IsPrimary;

        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 64)]
        public string Name;

        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 64)]
        public string DeviceName;

        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)]
        public string AdapterName;
    }

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
    private static extern int aibridge_capture_init(int monitorIndex);

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
    private static extern void aibridge_capture_shutdown();

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
    private static extern int aibridge_enumerate_monitors([In, Out] AIBridgeMonitorInfo[]? outMonitors, int maxMonitors);

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
    private static extern int aibridge_capture_frame(
        int monitorIndex,
        [Out] byte[]? outBgraBuffer,
        int maxBytes,
        out AIBridgeCaptureMetrics outMetrics);

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
    private static extern int aibridge_capture_window(
        ulong hwndValue,
        [Out] byte[]? outBgraBuffer,
        int maxBytes,
        out AIBridgeCaptureMetrics outMetrics);

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
    private static extern int aibridge_encode_h264_frame(
        [In] byte[] bgraBuffer,
        int width,
        int height,
        int fps,
        int bitrateKbps,
        int forceKeyframe,
        [Out] byte[]? outNalBuffer,
        int maxNalBytes,
        ref AIBridgeCaptureMetrics ioMetrics);

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
    private static extern int aibridge_set_encoder_bitrate(int bitrateKbps);

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
    private static extern int aibridge_set_input_permissions(
        int allowMouse,
        int allowKeyboard,
        int allowClipboard,
        int sessionActive);

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
    private static extern int aibridge_send_mouse_move(
        double x,
        double y,
        int isNormalized,
        int monX,
        int monY,
        int monW,
        int monH);

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl, CharSet = CharSet.Ansi)]
    private static extern int aibridge_send_mouse_button(
        string button,
        string state,
        double x,
        double y,
        int isNormalized,
        int monX,
        int monY,
        int monW,
        int monH);

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
    private static extern int aibridge_send_mouse_scroll(int deltaY, int deltaX);

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl, CharSet = CharSet.Ansi)]
    private static extern int aibridge_send_key_event(string keyToken, string state);

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
    private static extern int aibridge_release_all_keys();

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
    private static extern int aibridge_get_held_key_count();

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
    private static extern int aibridge_get_system_health_json([Out] byte[] outJson, int maxLen);

    private static bool? _dllAvailable;
    private static readonly object _sync = new();
    public static AIBridgeCaptureMetrics LastMetrics { get; private set; }
    public static bool PreferDxgiCapture { get; set; } = true;
    public static int DefaultH264Fps { get; set; } = 30;
    public static int DefaultH264BitrateKbps { get; set; } = 4500;

    public static bool TrySetEncoderBitrate(int bitrateKbps)
    {
        if (!IsAvailable()) return false;
        try
        {
            int clamped = Math.Clamp(bitrateKbps, 250, 50_000);
            DefaultH264BitrateKbps = clamped;
            return aibridge_set_encoder_bitrate(clamped) != 0;
        }
        catch
        {
            return false;
        }
    }

    public static bool ReinitializeCapture(int monitorIndex = 0)
    {
        if (!IsAvailable()) return false;
        try
        {
            aibridge_capture_shutdown();
            return aibridge_capture_init(monitorIndex) != 0;
        }
        catch
        {
            return false;
        }
    }

    public static bool IsAvailable()
    {
        if (_dllAvailable.HasValue) return _dllAvailable.Value;
        lock (_sync)
        {
            if (_dllAvailable.HasValue) return _dllAvailable.Value;
            try
            {
                var dllPath = Path.Combine(AppContext.BaseDirectory, DllName);
                if (File.Exists(dllPath))
                {
                    NativeLibrary.Load(dllPath);
                }
                int count = aibridge_enumerate_monitors(null, 0);
                _dllAvailable = count >= 0;
            }
            catch
            {
                _dllAvailable = false;
            }
            return _dllAvailable.Value;
        }
    }

    public static List<MonitorDescriptor>? TryEnumerateMonitors()
    {
        if (!IsAvailable()) return null;
        try
        {
            var infos = new AIBridgeMonitorInfo[16];
            int count = aibridge_enumerate_monitors(infos, infos.Length);
            if (count <= 0) return null;
            int actual = Math.Min(count, infos.Length);
            var list = new List<MonitorDescriptor>(actual);
            for (int i = 0; i < actual; i++)
            {
                var m = infos[i];
                var label = !string.IsNullOrWhiteSpace(m.AdapterName)
                    ? $"{m.DeviceName} ({m.AdapterName})"
                    : (!string.IsNullOrWhiteSpace(m.DeviceName) ? m.DeviceName : $"Display {m.Index}");
                list.Add(new MonitorDescriptor(
                    m.Index,
                    label,
                    m.Width > 0 ? m.Width : (m.Right - m.Left),
                    m.Height > 0 ? m.Height : (m.Bottom - m.Top),
                    m.IsPrimary != 0,
                    m.Left,
                    m.Top));
            }
            return list;
        }
        catch
        {
            return null;
        }
    }

    public static bool TryCaptureMonitor(
        string outputDir,
        int monitorIndex,
        out ScreenshotResult? result,
        out AIBridgeCaptureMetrics metrics)
    {
        result = null;
        metrics = default;
        if (!PreferDxgiCapture || !IsAvailable()) return false;

        try
        {
            // Probe dimensions first or allocate 4K BGRA buffer (3840x2160x4 = ~33 MB)
            var monitors = TryEnumerateMonitors();
            int estW = 3840;
            int estH = 2160;
            if (monitors is not null)
            {
                var mon = monitors.Find(m => m.Index == monitorIndex) ?? (monitors.Count > 0 ? monitors[0] : null);
                if (mon is not null && mon.Width > 0 && mon.Height > 0)
                {
                    estW = Math.Max(mon.Width, 1920);
                    estH = Math.Max(mon.Height, 1080);
                }
            }

            var buf = new byte[estW * estH * 4];
            int ok = aibridge_capture_frame(monitorIndex, buf, buf.Length, out metrics);
            if (ok == 0 || metrics.Valid == 0 || metrics.Width <= 0 || metrics.Height <= 0)
            {
                return false;
            }

            int needed = metrics.Width * metrics.Height * 4;
            if (needed > buf.Length)
            {
                buf = new byte[needed];
                ok = aibridge_capture_frame(monitorIndex, buf, buf.Length, out metrics);
                if (ok == 0 || metrics.Valid == 0) return false;
            }

            LastMetrics = metrics;
            result = SaveBgraBufferToPng(outputDir, buf, metrics.Width, metrics.Height, monitorIndex, $"screenshot_dxgi_m{monitorIndex}");
            return result is not null;
        }
        catch
        {
            return false;
        }
    }

    public static bool TryCaptureWindow(
        string outputDir,
        long hwnd,
        int pid,
        out ScreenshotResult? result,
        out AIBridgeCaptureMetrics metrics)
    {
        result = null;
        metrics = default;
        if (!PreferDxgiCapture || !IsAvailable() || hwnd <= 0) return false;

        try
        {
            var buf = new byte[3840 * 2160 * 4];
            int ok = aibridge_capture_window((ulong)hwnd, buf, buf.Length, out metrics);
            if (ok == 0 || metrics.Valid == 0 || metrics.Width <= 0 || metrics.Height <= 0)
            {
                return false;
            }

            LastMetrics = metrics;
            result = SaveBgraBufferToPng(outputDir, buf, metrics.Width, metrics.Height, -2, $"screenshot_cpp_win_{pid}");
            return result is not null;
        }
        catch
        {
            return false;
        }
    }

    private static ScreenshotResult? SaveBgraBufferToPng(
        string outputDir,
        byte[] bgraPixels,
        int width,
        int height,
        int monitorIndex,
        string prefix)
    {
        if (width <= 0 || height <= 0 || bgraPixels.Length < width * height * 4) return null;
        Directory.CreateDirectory(outputDir);
        using var bitmap = new Bitmap(width, height, PixelFormat.Format32bppArgb);
        var rect = new Rectangle(0, 0, width, height);
        var bmpData = bitmap.LockBits(rect, ImageLockMode.WriteOnly, PixelFormat.Format32bppArgb);
        try
        {
            int rowBytes = width * 4;
            if (bmpData.Stride == rowBytes)
            {
                Marshal.Copy(bgraPixels, 0, bmpData.Scan0, rowBytes * height);
            }
            else
            {
                for (int y = 0; y < height; y++)
                {
                    Marshal.Copy(bgraPixels, y * rowBytes, IntPtr.Add(bmpData.Scan0, y * bmpData.Stride), rowBytes);
                }
            }
        }
        finally
        {
            bitmap.UnlockBits(bmpData);
        }

        var fileName = $"{prefix}_{DateTimeOffset.Now:yyyyMMdd_HHmmss_fff}.png";
        var filePath = Path.Combine(outputDir, fileName);
        using var ms = new MemoryStream();
        bitmap.Save(ms, ImageFormat.Png);
        var pngBytes = ms.ToArray();
        File.WriteAllBytes(filePath, pngBytes);
        return new ScreenshotResult(filePath, monitorIndex, width, height, pngBytes.Length, Convert.ToBase64String(pngBytes));
    }

    public static void SyncInputPermissions(bool allowDesktopControl, bool allowClipboard)
    {
        if (!IsAvailable()) return;
        try
        {
            int m = allowDesktopControl ? 1 : 0;
            int k = allowDesktopControl ? 1 : 0;
            int c = allowClipboard ? 1 : 0;
            int s = (allowDesktopControl || allowClipboard) ? 1 : 0;
            aibridge_set_input_permissions(m, k, c, s);
        }
        catch
        {
            // Ignore when DLL unavailable
        }
    }

    public static bool TryMoveMouse(int x, int y)
    {
        if (!IsAvailable()) return false;
        try
        {
            SyncInputPermissions(allowDesktopControl: true, allowClipboard: true);
            return aibridge_send_mouse_move(x, y, 0, 0, 0, 0, 0) != 0;
        }
        catch
        {
            return false;
        }
    }

    public static bool TryMouseButton(string button, string state, int x, int y)
    {
        if (!IsAvailable()) return false;
        try
        {
            SyncInputPermissions(allowDesktopControl: true, allowClipboard: true);
            return aibridge_send_mouse_button(button, state, x, y, 0, 0, 0, 0, 0) != 0;
        }
        catch
        {
            return false;
        }
    }

    public static bool TryScrollMouse(int deltaY, int deltaX = 0)
    {
        if (!IsAvailable()) return false;
        try
        {
            SyncInputPermissions(allowDesktopControl: true, allowClipboard: true);
            return aibridge_send_mouse_scroll(deltaY, deltaX) != 0;
        }
        catch
        {
            return false;
        }
    }

    public static bool TryKeyEvent(string keyToken, string state)
    {
        if (!IsAvailable()) return false;
        try
        {
            SyncInputPermissions(allowDesktopControl: true, allowClipboard: true);
            return aibridge_send_key_event(keyToken, state) != 0;
        }
        catch
        {
            return false;
        }
    }

    public static int ReleaseAllKeys()
    {
        if (!IsAvailable()) return 0;
        try
        {
            return aibridge_release_all_keys();
        }
        catch
        {
            return 0;
        }
    }

    public static int GetHeldKeyCount()
    {
        if (!IsAvailable()) return 0;
        try
        {
            return aibridge_get_held_key_count();
        }
        catch
        {
            return 0;
        }
    }

    public static string? TryGetSystemHealthJson()
    {
        if (!IsAvailable()) return null;
        try
        {
            var buf = new byte[4096];
            int len = aibridge_get_system_health_json(buf, buf.Length);
            if (len <= 0) return null;
            return Encoding.UTF8.GetString(buf, 0, len);
        }
        catch
        {
            return null;
        }
    }

    public static string ProbeCaptureAndH264Json(int monitorIndex = 0, int fps = 30, int bitrateKbps = 4500)
    {
        if (!IsAvailable())
        {
            return JsonSerializer.Serialize(new
            {
                available = false,
                engine = "csharp_fallback",
                reason = "ai_bridge_native.dll not found in base directory",
            });
        }

        try
        {
            var buf = new byte[3840 * 2160 * 4];
            int capOk = aibridge_capture_frame(monitorIndex, buf, buf.Length, out var metrics);
            int encodedBytes = 0;
            if (capOk != 0 && metrics.Valid != 0 && metrics.Width > 0 && metrics.Height > 0)
            {
                var nalBuf = new byte[Math.Max(512 * 1024, metrics.Width * metrics.Height)];
                encodedBytes = aibridge_encode_h264_frame(
                    buf,
                    metrics.Width,
                    metrics.Height,
                    fps,
                    bitrateKbps,
                    1,
                    nalBuf,
                    nalBuf.Length,
                    ref metrics);
            }
            LastMetrics = metrics;
            return JsonSerializer.Serialize(new
            {
                available = true,
                engine = "ai_bridge_native.dll (C++20 DXGI + Media Foundation)",
                capture_ok = capOk != 0 && metrics.Valid != 0,
                from_dxgi = metrics.FromDxgi != 0,
                secure_desktop = metrics.SecureDesktop != 0,
                width = metrics.Width,
                height = metrics.Height,
                monitor_index = metrics.MonitorIndex,
                cursor_x = metrics.CursorX,
                cursor_y = metrics.CursorY,
                cursor_visible = metrics.CursorVisible != 0,
                dirty_rects = metrics.DirtyRectCount,
                move_rects = metrics.MoveRectCount,
                capture_time_ms = Math.Round(metrics.CaptureTimeMs, 2),
                encode_time_ms = Math.Round(metrics.EncodeTimeMs, 2),
                encoded_h264_bytes = encodedBytes > 0 ? encodedBytes : metrics.EncodedBytes,
                is_hardware_encoder = metrics.IsHardwareEncoder != 0,
                encoder_name = metrics.EncoderName ?? "",
                held_keys = GetHeldKeyCount(),
            }, new JsonSerializerOptions { WriteIndented = true });
        }
        catch (Exception ex)
        {
            return JsonSerializer.Serialize(new
            {
                available = false,
                error = ex.Message,
            });
        }
    }
}
