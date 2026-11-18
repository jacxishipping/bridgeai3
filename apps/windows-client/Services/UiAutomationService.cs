using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Linq;
using System.Windows.Automation;

namespace AIBridgeClient.Services;

public sealed record UiElementInfo(
    string Name,
    string AutomationId,
    string ControlType,
    int X,
    int Y,
    int Width,
    int Height,
    int CenterX = 0,
    int CenterY = 0,
    bool IsEnabled = true,
    string Patterns = "",
    string Value = "");

/// <summary>
/// Native Windows UI Automation service for scoped tree inspection, pattern invocation,
/// direct input value assignment, visual Set-of-Marks capture, and click targeting.
/// </summary>
public static class UiAutomationService
{
    private static readonly ConcurrentDictionary<string, UiElementInfo> LastMarks = new(StringComparer.OrdinalIgnoreCase);

    public static List<UiElementInfo> FindVisibleElements(
        string? query = null,
        int maxResults = 80,
        string? windowTitle = null,
        string? controlType = null,
        int maxDepth = 6)
    {
        var results = new List<UiElementInfo>();
        AutomationElement? startNode = null;
        if (!string.IsNullOrWhiteSpace(windowTitle))
        {
            var win = WindowManagementService.ResolveWindow(windowTitle);
            startNode = AutomationElement.FromHandle(new IntPtr(win.Handle));
            maxDepth = Math.Clamp(maxDepth < 7 ? 8 : maxDepth, 2, 10);
        }
        else
        {
            startNode = AutomationElement.RootElement;
            maxDepth = Math.Clamp(maxDepth, 2, 8);
        }

        if (startNode is null) return results;

        var walker = TreeWalker.ControlViewWalker;
        var queue = new Queue<(AutomationElement El, int Depth)>();
        if (!string.IsNullOrWhiteSpace(windowTitle))
        {
            queue.Enqueue((startNode, 0));
        }
        else
        {
            var child = walker.GetFirstChild(startNode);
            while (child is not null)
            {
                queue.Enqueue((child, 0));
                child = walker.GetNextSibling(child);
            }
        }

        while (queue.Count > 0 && results.Count < maxResults)
        {
            var (el, depth) = queue.Dequeue();
            try
            {
                var current = el.Current;
                if (!current.IsOffscreen && !current.BoundingRectangle.IsEmpty)
                {
                    var name = current.Name ?? "";
                    var autoId = current.AutomationId ?? "";
                    var typeName = current.ControlType.ProgrammaticName.Replace("ControlType.", "");
                    bool matchesQuery = string.IsNullOrWhiteSpace(query) ||
                                        name.Contains(query, StringComparison.OrdinalIgnoreCase) ||
                                        autoId.Contains(query, StringComparison.OrdinalIgnoreCase);
                    bool matchesType = string.IsNullOrWhiteSpace(controlType) ||
                                       typeName.Contains(controlType, StringComparison.OrdinalIgnoreCase);

                    if (matchesQuery && matchesType && (!string.IsNullOrWhiteSpace(name) || !string.IsNullOrWhiteSpace(autoId)))
                    {
                        var r = current.BoundingRectangle;
                        int rx = (int)r.X;
                        int ry = (int)r.Y;
                        int rw = (int)r.Width;
                        int rh = (int)r.Height;
                        var (patterns, val) = InspectPatternsAndValue(el, current.IsPassword);
                        results.Add(new UiElementInfo(
                            name,
                            autoId,
                            typeName,
                            rx,
                            ry,
                            rw,
                            rh,
                            rx + Math.Max(1, rw / 2),
                            ry + Math.Max(1, rh / 2),
                            current.IsEnabled,
                            patterns,
                            val));
                    }
                }

                if (depth < maxDepth)
                {
                    var sub = walker.GetFirstChild(el);
                    int siblings = 0;
                    int maxSiblings = string.IsNullOrWhiteSpace(windowTitle) ? 35 : 80;
                    while (sub is not null && siblings < maxSiblings)
                    {
                        queue.Enqueue((sub, depth + 1));
                        sub = walker.GetNextSibling(sub);
                        siblings++;
                    }
                }
            }
            catch
            {
                // Ignore transient/inaccessible UI Automation elements
            }
        }

        return results;
    }

    private static (string Patterns, string Value) InspectPatternsAndValue(AutomationElement el, bool isPassword)
    {
        var tags = new List<string>(4);
        string val = "";
        try
        {
            if (el.TryGetCurrentPattern(InvokePattern.Pattern, out _)) tags.Add("Invoke");
            if (el.TryGetCurrentPattern(TogglePattern.Pattern, out _)) tags.Add("Toggle");
            if (el.TryGetCurrentPattern(SelectionItemPattern.Pattern, out _)) tags.Add("Select");
            if (el.TryGetCurrentPattern(ExpandCollapsePattern.Pattern, out _)) tags.Add("Expand");
            if (el.TryGetCurrentPattern(ValuePattern.Pattern, out var vpObj) && vpObj is ValuePattern vp)
            {
                tags.Add("Value");
                if (isPassword)
                {
                    val = "***";
                }
                else
                {
                    var raw = vp.Current.Value ?? "";
                    val = raw.Length > 80 ? raw[..80] + "..." : raw;
                }
            }
        }
        catch { }
        return (string.Join(",", tags), val);
    }

    private static (AutomationElement El, UiElementInfo Info) FindFirstAutomationElement(
        string query,
        string? windowTitle = null)
    {
        if (string.IsNullOrWhiteSpace(query))
        {
            throw new ArgumentException("Element query (name or automation_id) is required.");
        }

        AutomationElement root = !string.IsNullOrWhiteSpace(windowTitle)
            ? AutomationElement.FromHandle(new IntPtr(WindowManagementService.ResolveWindow(windowTitle).Handle))
            : AutomationElement.RootElement;

        var walker = TreeWalker.ControlViewWalker;
        var queue = new Queue<(AutomationElement El, int Depth)>();
        queue.Enqueue((root, 0));
        int maxDepth = string.IsNullOrWhiteSpace(windowTitle) ? 6 : 9;

        while (queue.Count > 0)
        {
            var (el, depth) = queue.Dequeue();
            try
            {
                var current = el.Current;
                if (!current.IsOffscreen && !current.BoundingRectangle.IsEmpty)
                {
                    var name = current.Name ?? "";
                    var autoId = current.AutomationId ?? "";
                    if (name.Contains(query, StringComparison.OrdinalIgnoreCase) ||
                        autoId.Contains(query, StringComparison.OrdinalIgnoreCase))
                    {
                        var r = current.BoundingRectangle;
                        int rx = (int)r.X;
                        int ry = (int)r.Y;
                        int rw = (int)r.Width;
                        int rh = (int)r.Height;
                        var (patterns, val) = InspectPatternsAndValue(el, current.IsPassword);
                        var info = new UiElementInfo(
                            name,
                            autoId,
                            current.ControlType.ProgrammaticName.Replace("ControlType.", ""),
                            rx,
                            ry,
                            rw,
                            rh,
                            rx + Math.Max(1, rw / 2),
                            ry + Math.Max(1, rh / 2),
                            current.IsEnabled,
                            patterns,
                            val);
                        return (el, info);
                    }
                }

                if (depth < maxDepth)
                {
                    var sub = walker.GetFirstChild(el);
                    int siblings = 0;
                    while (sub is not null && siblings < 80)
                    {
                        queue.Enqueue((sub, depth + 1));
                        sub = walker.GetNextSibling(sub);
                        siblings++;
                    }
                }
            }
            catch { }
        }

        throw new InvalidOperationException($"No visible UI element matching '{query}' was found.");
    }

    public static UiElementInfo ClickElement(
        string query,
        string button = "left",
        bool doubleClick = false,
        string? windowTitle = null,
        string? markId = null)
    {
        UiElementInfo match;
        if (!string.IsNullOrWhiteSpace(markId) && LastMarks.TryGetValue(markId.Trim(), out var marked))
        {
            match = marked;
        }
        else
        {
            match = FindVisibleElements(query, 10, windowTitle).FirstOrDefault()
                ?? throw new InvalidOperationException($"No visible UI element matching '{query}' was found.");
        }

        int centerX = match.X + Math.Max(1, match.Width / 2);
        int centerY = match.Y + Math.Max(1, match.Height / 2);
        DesktopInputService.ClickMouse(centerX, centerY, button, doubleClick);
        return match;
    }

    public static UiElementInfo InvokeElement(string query, string? windowTitle = null)
    {
        var (el, info) = FindFirstAutomationElement(query, windowTitle);
        try
        {
            if (el.TryGetCurrentPattern(InvokePattern.Pattern, out var invObj) && invObj is InvokePattern inv)
            {
                inv.Invoke();
                return info;
            }
            if (el.TryGetCurrentPattern(TogglePattern.Pattern, out var togObj) && togObj is TogglePattern tog)
            {
                tog.Toggle();
                return info;
            }
            if (el.TryGetCurrentPattern(SelectionItemPattern.Pattern, out var selObj) && selObj is SelectionItemPattern sel)
            {
                sel.Select();
                return info;
            }
            if (el.TryGetCurrentPattern(ExpandCollapsePattern.Pattern, out var expObj) && expObj is ExpandCollapsePattern exp)
            {
                if (exp.Current.ExpandCollapseState == ExpandCollapseState.Collapsed)
                    exp.Expand();
                else
                    exp.Collapse();
                return info;
            }
        }
        catch { }

        DesktopInputService.ClickMouse(info.CenterX, info.CenterY, "left", false);
        return info;
    }

    public static UiElementInfo SetElementValue(string query, string value, bool submit = false, string? windowTitle = null)
    {
        var (el, info) = FindFirstAutomationElement(query, windowTitle);
        try { el.SetFocus(); } catch { }

        if (el.TryGetCurrentPattern(ValuePattern.Pattern, out var vpObj) && vpObj is ValuePattern vp && !vp.Current.IsReadOnly)
        {
            vp.SetValue(value ?? "");
            if (submit) DesktopInputService.PressKey(0x0D);
            return info;
        }

        DesktopInputService.ClickMouse(info.CenterX, info.CenterY, "left", false);
        DesktopInputService.PressHotkey("ctrl+a");
        DesktopInputService.TypeText(value ?? "");
        if (submit) DesktopInputService.PressKey(0x0D);
        return info;
    }

    public static UiElementInfo FocusElement(string query, string? windowTitle = null)
    {
        var (el, info) = FindFirstAutomationElement(query, windowTitle);
        try
        {
            el.SetFocus();
        }
        catch
        {
            DesktopInputService.ClickMouse(info.CenterX, info.CenterY, "left", false);
        }
        return info;
    }

    public static (ScreenshotResult Shot, List<(int Mark, UiElementInfo Element)> Marks) CaptureSetOfMarks(
        string outputDir,
        int maxMarks = 40,
        string? windowTitle = null)
    {
        var baseShot = string.IsNullOrWhiteSpace(windowTitle)
            ? ScreenCaptureService.CapturePrimaryScreen(outputDir, 0)
            : ScreenCaptureService.CaptureWindow(outputDir, windowTitle);

        var elements = FindVisibleElements(
                query: null,
                maxResults: Math.Clamp(maxMarks, 5, 80),
                windowTitle: windowTitle)
            .Where(e => e.Width >= 8 && e.Height >= 8 && e.Width < 1600 && e.Height < 900)
            .Take(Math.Clamp(maxMarks, 5, 80))
            .ToList();

        var indexed = new List<(int Mark, UiElementInfo Element)>(elements.Count);
        LastMarks.Clear();

        byte[] baseBytes = Convert.FromBase64String(baseShot.Base64Png);
        using var inMs = new MemoryStream(baseBytes);
        using var bmp = new Bitmap(inMs);
        using (var g = Graphics.FromImage(bmp))
        using (var pen = new Pen(Color.FromArgb(230, 99, 102, 241), 2f))
        using (var badgeBrush = new SolidBrush(Color.FromArgb(235, 79, 70, 229)))
        using (var textBrush = new SolidBrush(Color.White))
        using (var font = new Font("Consolas", 9f, FontStyle.Bold))
        {
            for (int i = 0; i < elements.Count; i++)
            {
                var el = elements[i];
                int mark = i + 1;
                indexed.Add((mark, el));
                LastMarks[mark.ToString()] = el;

                int x = Math.Clamp(el.X, 0, Math.Max(0, bmp.Width - 10));
                int y = Math.Clamp(el.Y, 0, Math.Max(0, bmp.Height - 10));
                int w = Math.Clamp(el.Width, 6, Math.Max(6, bmp.Width - x));
                int h = Math.Clamp(el.Height, 6, Math.Max(6, bmp.Height - y));

                g.DrawRectangle(pen, x, y, w, h);
                var label = $"[{mark}]";
                var sz = g.MeasureString(label, font);
                int by = Math.Max(0, y - (int)sz.Height - 2);
                g.FillRectangle(badgeBrush, x, by, sz.Width + 4, sz.Height + 1);
                g.DrawString(label, font, textBrush, x + 2, by);
            }
        }

        Directory.CreateDirectory(outputDir);
        var path = Path.Combine(outputDir, $"som_{DateTimeOffset.Now:yyyyMMdd_HHmmss_fff}.png");
        using var outMs = new MemoryStream();
        bmp.Save(outMs, ImageFormat.Png);
        var bytes = outMs.ToArray();
        File.WriteAllBytes(path, bytes);

        var annotated = new ScreenshotResult(
            path,
            baseShot.MonitorIndex,
            bmp.Width,
            bmp.Height,
            bytes.Length,
            Convert.ToBase64String(bytes));
        return (annotated, indexed);
    }
}
