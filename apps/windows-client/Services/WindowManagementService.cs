using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

namespace AIBridgeClient.Services;

public sealed record DesktopWindowInfo(
    long Handle,
    int Pid,
    string ProcessName,
    string Title,
    int X,
    int Y,
    int Width,
    int Height,
    bool IsMinimized);

/// <summary>
/// Native Win32 window enumeration, focus control, and STA-safe Windows clipboard access.
/// </summary>
public static class WindowManagementService
{
    private const int SwMaximize = 3;
    private const int SwMinimize = 6;
    private const int SwRestore = 9;
    private const uint WmClose = 0x0010;
    private const uint SwpNoZOrder = 0x0004;
    private const uint SwpNoMove = 0x0002;
    private const uint SwpNoSize = 0x0001;

    private delegate bool EnumWindowsProc(IntPtr hwnd, IntPtr lParam);

    [StructLayout(LayoutKind.Sequential)]
    public struct RECT
    {
        public int Left, Top, Right, Bottom;
    }

    [DllImport("user32.dll")]
    private static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);

    [DllImport("user32.dll")]
    private static extern bool IsWindowVisible(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern bool IsIconic(IntPtr hWnd);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);

    [DllImport("user32.dll")]
    private static extern int GetWindowTextLength(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);

    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);

    [DllImport("user32.dll")]
    private static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll")]
    private static extern bool BringWindowToTop(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);

    [DllImport("kernel32.dll")]
    private static extern uint GetCurrentThreadId();

    [DllImport("user32.dll")]
    private static extern bool PostMessage(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll")]
    private static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int x, int y, int cx, int cy, uint uFlags);

    public static List<DesktopWindowInfo> ListVisibleWindows(string? query = null, int limit = 60)
    {
        var results = new List<DesktopWindowInfo>();
        limit = Math.Clamp(limit, 1, 200);

        EnumWindows((hwnd, _) =>
        {
            if (results.Count >= limit) return false;
            if (!IsWindowVisible(hwnd)) return true;

            int len = GetWindowTextLength(hwnd);
            if (len <= 0) return true;

            var sb = new StringBuilder(len + 1);
            GetWindowText(hwnd, sb, sb.Capacity);
            var title = sb.ToString().Trim();
            if (string.IsNullOrEmpty(title)) return true;

            if (!GetWindowRect(hwnd, out var rect)) return true;
            int w = rect.Right - rect.Left;
            int h = rect.Bottom - rect.Top;
            if (w <= 1 || h <= 1) return true;

            GetWindowThreadProcessId(hwnd, out uint pid);
            string procName = "";
            try
            {
                using var p = Process.GetProcessById((int)pid);
                procName = p.ProcessName;
            }
            catch
            {
                // Ignore protected processes
            }

            if (!string.IsNullOrWhiteSpace(query) &&
                !title.Contains(query, StringComparison.OrdinalIgnoreCase) &&
                !procName.Contains(query, StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }

            results.Add(new DesktopWindowInfo(
                hwnd.ToInt64(),
                (int)pid,
                procName,
                title,
                rect.Left,
                rect.Top,
                w,
                h,
                IsIconic(hwnd)));
            return true;
        }, IntPtr.Zero);

        return results;
    }

    public static DesktopWindowInfo ResolveWindow(string? titleQuery, long? handle = null)
    {
        var all = ListVisibleWindows(null, 200);
        if (handle is > 0)
        {
            var byHwnd = all.FirstOrDefault(w => w.Handle == handle.Value);
            if (byHwnd is not null) return byHwnd;
        }
        if (!string.IsNullOrWhiteSpace(titleQuery))
        {
            var match = all.FirstOrDefault(w =>
                w.Title.Contains(titleQuery, StringComparison.OrdinalIgnoreCase) ||
                w.ProcessName.Contains(titleQuery, StringComparison.OrdinalIgnoreCase));
            if (match is not null) return match;
        }
        throw new InvalidOperationException($"No visible window matched '{titleQuery ?? handle?.ToString() ?? ""}'.");
    }

    public static DesktopWindowInfo FocusWindow(string? titleQuery, long? handle = null)
    {
        var target = ResolveWindow(titleQuery, handle);
        var hwnd = new IntPtr(target.Handle);
        if (target.IsMinimized)
        {
            ShowWindow(hwnd, SwRestore);
        }

        var fg = GetForegroundWindow();
        uint curTid = GetCurrentThreadId();
        uint fgTid = fg != IntPtr.Zero ? GetWindowThreadProcessId(fg, out _) : 0;
        bool attached = false;
        try
        {
            if (fgTid != 0 && fgTid != curTid)
            {
                attached = AttachThreadInput(curTid, fgTid, true);
            }
            BringWindowToTop(hwnd);
            SetForegroundWindow(hwnd);
        }
        finally
        {
            if (attached)
            {
                AttachThreadInput(curTid, fgTid, false);
            }
        }
        return target;
    }

    public static DesktopWindowInfo MaximizeWindow(string? titleQuery, long? handle = null)
    {
        var target = FocusWindow(titleQuery, handle);
        ShowWindow(new IntPtr(target.Handle), SwMaximize);
        return ResolveWindow(null, target.Handle);
    }

    public static DesktopWindowInfo MinimizeWindow(string? titleQuery, long? handle = null)
    {
        var target = ResolveWindow(titleQuery, handle);
        ShowWindow(new IntPtr(target.Handle), SwMinimize);
        return target;
    }

    public static DesktopWindowInfo RestoreWindow(string? titleQuery, long? handle = null)
    {
        var target = ResolveWindow(titleQuery, handle);
        ShowWindow(new IntPtr(target.Handle), SwRestore);
        return FocusWindow(null, target.Handle);
    }

    public static DesktopWindowInfo CloseWindow(string? titleQuery, long? handle = null)
    {
        var target = ResolveWindow(titleQuery, handle);
        PostMessage(new IntPtr(target.Handle), WmClose, IntPtr.Zero, IntPtr.Zero);
        return target;
    }

    public static DesktopWindowInfo MoveResizeWindow(
        string? titleQuery,
        long? handle,
        int? x,
        int? y,
        int? width,
        int? height)
    {
        var target = ResolveWindow(titleQuery, handle);
        var hwnd = new IntPtr(target.Handle);
        if (target.IsMinimized)
        {
            ShowWindow(hwnd, SwRestore);
        }
        uint flags = SwpNoZOrder;
        if (!x.HasValue && !y.HasValue) flags |= SwpNoMove;
        if (!width.HasValue && !height.HasValue) flags |= SwpNoSize;
        SetWindowPos(
            hwnd,
            IntPtr.Zero,
            x ?? target.X,
            y ?? target.Y,
            width ?? target.Width,
            height ?? target.Height,
            flags);
        return ResolveWindow(null, target.Handle);
    }

    public static DesktopWindowInfo? GetActiveWindowInfo()
    {
        var fg = GetForegroundWindow();
        if (fg == IntPtr.Zero) return null;
        return ListVisibleWindows(null, 200).FirstOrDefault(w => w.Handle == fg.ToInt64());
    }

    public static IntPtr GetActiveWindowHandle() => GetForegroundWindow();

    public static string GetClipboardText()
    {
        return RunSta(() => System.Windows.Clipboard.ContainsText()
            ? System.Windows.Clipboard.GetText()
            : string.Empty);
    }

    public static void SetClipboardText(string text)
    {
        RunSta(() =>
        {
            System.Windows.Clipboard.SetText(text ?? string.Empty);
            return true;
        });
    }

    private static T RunSta<T>(Func<T> action)
    {
        if (Thread.CurrentThread.GetApartmentState() == ApartmentState.STA)
        {
            return action();
        }

        if (System.Windows.Application.Current?.Dispatcher is { } disp && !disp.HasShutdownStarted)
        {
            return disp.Invoke(action);
        }

        T? result = default;
        Exception? captured = null;
        var thread = new Thread(() =>
        {
            try { result = action(); }
            catch (Exception ex) { captured = ex; }
        });
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start();
        thread.Join(TimeSpan.FromSeconds(5));
        if (captured is not null) throw captured;
        return result!;
    }
}
