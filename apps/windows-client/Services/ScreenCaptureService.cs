using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Linq;
using System.Windows.Forms;

namespace AIBridgeClient.Services;

public sealed record MonitorDescriptor(int Index, string Name, int Width, int Height, bool IsPrimary)
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
            list.Add(new MonitorDescriptor(i, s.DeviceName, s.Bounds.Width, s.Bounds.Height, s.Primary));
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

        using var bitmap = new Bitmap(bounds.Width, bounds.Height, PixelFormat.Format32bppArgb);
        using (var graphics = Graphics.FromImage(bitmap))
        {
            graphics.CopyFromScreen(bounds.Location, Point.Empty, bounds.Size, CopyPixelOperation.SourceCopy);
        }

        var fileName = $"screenshot_m{monitorIndex}_{DateTimeOffset.Now:yyyyMMdd_HHmmss}.png";
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
