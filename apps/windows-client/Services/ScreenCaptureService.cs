using System;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Windows.Forms;

namespace AIBridgeClient.Services;

public sealed record ScreenshotResult(string FilePath, int Width, int Height, long SizeBytes, string Base64Png);

public static class ScreenCaptureService
{
    public static ScreenshotResult CapturePrimaryScreen(string outputDir)
    {
        Directory.CreateDirectory(outputDir);
        var bounds = Screen.PrimaryScreen?.Bounds ?? new Rectangle(0, 0, 1920, 1080);

        using var bitmap = new Bitmap(bounds.Width, bounds.Height, PixelFormat.Format32bppArgb);
        using (var graphics = Graphics.FromImage(bitmap))
        {
            graphics.CopyFromScreen(bounds.Location, Point.Empty, bounds.Size, CopyPixelOperation.SourceCopy);
        }

        var fileName = $"screenshot_{DateTimeOffset.Now:yyyyMMdd_HHmmss}.png";
        var filePath = Path.Combine(outputDir, fileName);

        using var ms = new MemoryStream();
        bitmap.Save(ms, ImageFormat.Png);
        var bytes = ms.ToArray();
        File.WriteAllBytes(filePath, bytes);

        return new ScreenshotResult(
            filePath,
            bounds.Width,
            bounds.Height,
            bytes.Length,
            Convert.ToBase64String(bytes));
    }
}
