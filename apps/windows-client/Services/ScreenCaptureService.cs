using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Windows.Forms;

namespace AIBridgeClient.Services;

public sealed record MonitorDescriptor(
    int Index,
    string Name,
    int Width,
    int Height,
    bool IsPrimary,
    int BoundsX = 0,
    int BoundsY = 0)
{
    public string DisplayLabel => $"{Index}: {(IsPrimary ? "Primary" : "Display")} ({Width}x{Height})";
    public override string ToString() => DisplayLabel;
}

public sealed record ScreenshotResult(string FilePath, int MonitorIndex, int Width, int Height, long SizeBytes, string Base64Png);

/// <summary>
/// Lightweight JPEG frame for Live Desktop / capture_desktop_frame (parity with Python fleet.capture_desktop_frame).
/// </summary>
public sealed record LiveFrameResult(
    bool Available,
    string FilePath,
    int MonitorIndex,
    int Width,
    int Height,
    int OrigWidth,
    int OrigHeight,
    long SizeBytes,
    string Base64Data,
    string MimeType,
    double Scale,
    int Quality,
    bool FromDxgi,
    bool SecureDesktop,
    string? Note);

public static class ScreenCaptureService
{
    public static List<MonitorDescriptor> GetMonitors()
    {
        var nativeMonitors = CppNativeBridge.TryEnumerateMonitors();
        if (nativeMonitors is { Count: > 0 })
        {
            return nativeMonitors;
        }

        var screens = Screen.AllScreens;
        var list = new List<MonitorDescriptor>(screens.Length);
        for (int i = 0; i < screens.Length; i++)
        {
            var s = screens[i];
            list.Add(new MonitorDescriptor(i, s.DeviceName, s.Bounds.Width, s.Bounds.Height, s.Primary, s.Bounds.X, s.Bounds.Y));
        }
        return list;
    }

    public static ScreenshotResult CapturePrimaryScreen(string outputDir, int monitorIndex = 0)
    {
        Directory.CreateDirectory(outputDir);
        if (CppNativeBridge.TryCaptureMonitor(outputDir, monitorIndex, out var nativeResult, out _) && nativeResult is not null)
        {
            return nativeResult;
        }

        var screens = Screen.AllScreens;
        var screen = (monitorIndex >= 0 && monitorIndex < screens.Length)
            ? screens[monitorIndex]
            : (Screen.PrimaryScreen ?? screens.FirstOrDefault());

        var bounds = screen?.Bounds ?? new Rectangle(0, 0, 1920, 1080);
        return CaptureBounds(outputDir, bounds, monitorIndex, $"screenshot_m{monitorIndex}");
    }

    public static ScreenshotResult CaptureVirtualDesktop(string outputDir)
    {
        var vs = SystemInformation.VirtualScreen;
        return CaptureBounds(outputDir, vs, -1, "screenshot_desktop");
    }

    [DllImport("user32.dll")]
    private static extern bool PrintWindow(IntPtr hwnd, IntPtr hdcBlt, uint nFlags);

    public static ScreenshotResult CaptureWindow(string outputDir, string? titleQuery = null, long? handle = null)
    {
        DesktopWindowInfo win = (string.IsNullOrWhiteSpace(titleQuery) && (handle is null || handle <= 0))
            ? WindowManagementService.ResolveWindow(null, WindowManagementService.GetActiveWindowHandle().ToInt64())
            : WindowManagementService.FocusWindow(titleQuery, handle);

        if (CppNativeBridge.TryCaptureWindow(outputDir, win.Handle, win.Pid, out var nativeWinResult, out _) && nativeWinResult is not null)
        {
            return nativeWinResult;
        }

        var rect = new Rectangle(win.X, win.Y, Math.Max(1, win.Width), Math.Max(1, win.Height));
        try
        {
            return CaptureBounds(outputDir, rect, -2, $"screenshot_win_{win.Pid}");
        }
        catch
        {
            Directory.CreateDirectory(outputDir);
            using var bitmap = new Bitmap(rect.Width, rect.Height, PixelFormat.Format32bppArgb);
            using (var g = Graphics.FromImage(bitmap))
            {
                var hdc = g.GetHdc();
                try
                {
                    PrintWindow(new IntPtr(win.Handle), hdc, 2);
                }
                finally
                {
                    g.ReleaseHdc(hdc);
                }
            }
            var filePath = Path.Combine(outputDir, $"screenshot_win_{win.Pid}_{DateTimeOffset.Now:yyyyMMdd_HHmmss_fff}.png");
            using var ms = new MemoryStream();
            bitmap.Save(ms, ImageFormat.Png);
            var bytes = ms.ToArray();
            File.WriteAllBytes(filePath, bytes);
            return new ScreenshotResult(filePath, -2, rect.Width, rect.Height, bytes.Length, Convert.ToBase64String(bytes));
        }
    }

    /// <summary>
    /// Capture a scaled JPEG frame for real-time streaming (matches Python tools.fleet.capture_desktop_frame).
    /// Reports secure-desktop / capture failure honestly instead of returning a blank image.
    /// </summary>
    public static LiveFrameResult CaptureLiveFrame(
        string outputDir,
        int monitorIndex = 0,
        double scale = 0.75,
        int quality = 70)
    {
        Directory.CreateDirectory(outputDir);
        scale = Math.Clamp(scale, 0.2, 1.0);
        quality = Math.Clamp(quality, 20, 95);

        Bitmap? source = null;
        bool fromDxgi = false;
        bool secureDesktop = false;
        string? note = null;

        try
        {
            if (CppNativeBridge.TryCaptureMonitorBgra(monitorIndex, out var bgra, out var width, out var height, out var metrics)
                && bgra is not null && width > 0 && height > 0)
            {
                fromDxgi = metrics.FromDxgi != 0;
                secureDesktop = metrics.SecureDesktop != 0;
                if (secureDesktop)
                {
                    return new LiveFrameResult(
                        Available: false,
                        FilePath: "",
                        MonitorIndex: monitorIndex,
                        Width: 0,
                        Height: 0,
                        OrigWidth: width,
                        OrigHeight: height,
                        SizeBytes: 0,
                        Base64Data: "",
                        MimeType: "image/jpeg",
                        Scale: scale,
                        Quality: quality,
                        FromDxgi: fromDxgi,
                        SecureDesktop: true,
                        Note: "Secure desktop active (UAC / login screen). Capture requires an interactive user session on the Default desktop.");
                }

                source = BgraToBitmap(bgra, width, height);
            }
            else
            {
                secureDesktop = CppNativeBridge.LastMetrics.SecureDesktop != 0;
                if (secureDesktop)
                {
                    return new LiveFrameResult(
                        Available: false,
                        FilePath: "",
                        MonitorIndex: monitorIndex,
                        Width: 0,
                        Height: 0,
                        OrigWidth: 0,
                        OrigHeight: 0,
                        SizeBytes: 0,
                        Base64Data: "",
                        MimeType: "image/jpeg",
                        Scale: scale,
                        Quality: quality,
                        FromDxgi: false,
                        SecureDesktop: true,
                        Note: "Secure desktop active; DXGI/GDI capture unavailable from this session.");
                }

                var screens = Screen.AllScreens;
                var screen = (monitorIndex >= 0 && monitorIndex < screens.Length)
                    ? screens[monitorIndex]
                    : (Screen.PrimaryScreen ?? screens.FirstOrDefault());
                var bounds = screen?.Bounds ?? new Rectangle(0, 0, 1920, 1080);
                source = new Bitmap(bounds.Width, bounds.Height, PixelFormat.Format32bppArgb);
                using var graphics = Graphics.FromImage(source);
                graphics.CopyFromScreen(bounds.Location, Point.Empty, bounds.Size, CopyPixelOperation.SourceCopy);
            }

            int origW = source.Width;
            int origH = source.Height;
            Bitmap? scaled = null;
            try
            {
                Bitmap encodeSource = source;
                if (scale < 0.99)
                {
                    int newW = Math.Max(100, (int)(origW * scale));
                    int newH = Math.Max(100, (int)(origH * scale));
                    scaled = new Bitmap(newW, newH, PixelFormat.Format24bppRgb);
                    using (var g = Graphics.FromImage(scaled))
                    {
                        g.InterpolationMode = System.Drawing.Drawing2D.InterpolationMode.Bilinear;
                        g.DrawImage(source, 0, 0, newW, newH);
                    }
                    encodeSource = scaled;
                }

                var fileName = $"live_m{monitorIndex}_{DateTimeOffset.Now:yyyyMMdd_HHmmss_fff}.jpg";
                var filePath = Path.Combine(outputDir, fileName);
                byte[] jpgBytes;
                using (var ms = new MemoryStream())
                {
                    SaveJpeg(encodeSource, ms, quality);
                    jpgBytes = ms.ToArray();
                }
                File.WriteAllBytes(filePath, jpgBytes);

                return new LiveFrameResult(
                    Available: true,
                    FilePath: filePath,
                    MonitorIndex: monitorIndex,
                    Width: encodeSource.Width,
                    Height: encodeSource.Height,
                    OrigWidth: origW,
                    OrigHeight: origH,
                    SizeBytes: jpgBytes.Length,
                    Base64Data: Convert.ToBase64String(jpgBytes),
                    MimeType: "image/jpeg",
                    Scale: scale,
                    Quality: quality,
                    FromDxgi: fromDxgi,
                    SecureDesktop: false,
                    Note: note);
            }
            finally
            {
                scaled?.Dispose();
            }
        }
        catch (Exception ex)
        {
            return new LiveFrameResult(
                Available: false,
                FilePath: "",
                MonitorIndex: monitorIndex,
                Width: 0,
                Height: 0,
                OrigWidth: 0,
                OrigHeight: 0,
                SizeBytes: 0,
                Base64Data: "",
                MimeType: "image/jpeg",
                Scale: scale,
                Quality: quality,
                FromDxgi: fromDxgi,
                SecureDesktop: secureDesktop,
                Note: $"Desktop capture unavailable: {ex.Message}. Interactive user session required (not Session 0 / SYSTEM).");
        }
        finally
        {
            source?.Dispose();
        }
    }

    private static Bitmap BgraToBitmap(byte[] bgra, int width, int height)
    {
        var bitmap = new Bitmap(width, height, PixelFormat.Format32bppArgb);
        var bmpData = bitmap.LockBits(
            new Rectangle(0, 0, width, height),
            ImageLockMode.WriteOnly,
            PixelFormat.Format32bppArgb);
        try
        {
            int stride = bmpData.Stride;
            int rowBytes = width * 4;
            for (int y = 0; y < height; y++)
            {
                Marshal.Copy(bgra, y * rowBytes, bmpData.Scan0 + y * stride, rowBytes);
            }
        }
        finally
        {
            bitmap.UnlockBits(bmpData);
        }
        return bitmap;
    }

    private static void SaveJpeg(Image image, Stream stream, int quality)
    {
        var encoder = ImageCodecInfo.GetImageEncoders().FirstOrDefault(e => e.FormatID == ImageFormat.Jpeg.Guid);
        if (encoder is null)
        {
            image.Save(stream, ImageFormat.Jpeg);
            return;
        }
        using var ep = new EncoderParameters(1);
        ep.Param[0] = new EncoderParameter(Encoder.Quality, (long)quality);
        image.Save(stream, encoder, ep);
    }

    private static ScreenshotResult CaptureBounds(string outputDir, Rectangle bounds, int monitorIndex, string prefix)
    {
        Directory.CreateDirectory(outputDir);
        using var bitmap = new Bitmap(bounds.Width, bounds.Height, PixelFormat.Format32bppArgb);
        using (var graphics = Graphics.FromImage(bitmap))
        {
            graphics.CopyFromScreen(bounds.Location, Point.Empty, bounds.Size, CopyPixelOperation.SourceCopy);
        }

        var fileName = $"{prefix}_{DateTimeOffset.Now:yyyyMMdd_HHmmss_fff}.png";
        var filePath = Path.Combine(outputDir, fileName);

        using var ms = new MemoryStream();
        bitmap.Save(ms, ImageFormat.Png);
        var bytes = ms.ToArray();
        File.WriteAllBytes(filePath, bytes);

        return new ScreenshotResult(
            filePath,
            monitorIndex,
            bounds.Width,
            bounds.Height,
            bytes.Length,
            Convert.ToBase64String(bytes));
    }
}
