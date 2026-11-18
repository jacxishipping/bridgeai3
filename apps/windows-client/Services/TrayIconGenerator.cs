using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Runtime.InteropServices;

namespace AIBridgeClient.Services;

public static class TrayIconGenerator
{
    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool DestroyIcon(IntPtr hIcon);

    public static Icon CreateStatusIcon(string tone)
    {
        using var bmp = new Bitmap(32, 32);
        using var g = Graphics.FromImage(bmp);
        g.SmoothingMode = SmoothingMode.AntiAlias;
        g.Clear(Color.Transparent);

        // Brand blue rounded badge
        using var bgBrush = new SolidBrush(Color.FromArgb(37, 99, 235));
        using var path = RoundedRect(new Rectangle(2, 2, 28, 28), 7);
        g.FillPath(bgBrush, path);

        // Subtle "A" / bridge glyph
        using var pen = new Pen(Color.White, 2.4f) { StartCap = LineCap.Round, EndCap = LineCap.Round };
        g.DrawLine(pen, 10, 21, 16, 9);
        g.DrawLine(pen, 16, 9, 22, 21);
        g.DrawLine(pen, 12.5f, 17, 19.5f, 17);

        // Live status dot in bottom-right corner
        var dotColor = tone switch
        {
            "ok" => Color.FromArgb(16, 185, 129),
            "warn" => Color.FromArgb(245, 158, 11),
            "err" => Color.FromArgb(239, 68, 68),
            _ => Color.FromArgb(100, 116, 139),
        };
        using var borderBrush = new SolidBrush(Color.FromArgb(9, 13, 22));
        using var dotBrush = new SolidBrush(dotColor);
        g.FillEllipse(borderBrush, 19, 19, 12, 12);
        g.FillEllipse(dotBrush, 21, 21, 8, 8);

        IntPtr hIcon = bmp.GetHicon();
        try
        {
            using var temp = Icon.FromHandle(hIcon);
            return (Icon)temp.Clone();
        }
        finally
        {
            DestroyIcon(hIcon);
        }
    }

    private static GraphicsPath RoundedRect(Rectangle bounds, int radius)
    {
        int d = radius * 2;
        var path = new GraphicsPath();
        path.AddArc(bounds.X, bounds.Y, d, d, 180, 90);
        path.AddArc(bounds.Right - d, bounds.Y, d, d, 270, 90);
        path.AddArc(bounds.Right - d, bounds.Bottom - d, d, d, 0, 90);
        path.AddArc(bounds.X, bounds.Bottom - d, d, d, 90, 90);
        path.CloseFigure();
        return path;
    }
}
