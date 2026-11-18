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

public static class ScreenCaptureService
{
    public static List<MonitorDescriptor> GetMonitors()
    {
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
