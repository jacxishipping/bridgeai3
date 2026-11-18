using System;
using System.Collections.Generic;
using System.Linq;
using System.Windows;
using System.Windows.Automation;

namespace AIBridgeClient.Services;

public sealed record UiElementInfo(
    string Name,
    string AutomationId,
    string ControlType,
    int X,
    int Y,
    int Width,
    int Height);

/// <summary>
/// Native Windows UI Automation service for element inspection, text lookup, and click targeting.
/// </summary>
public static class UiAutomationService
{
    public static List<UiElementInfo> FindVisibleElements(string? query = null, int maxResults = 60)
    {
        var results = new List<UiElementInfo>();
        var root = AutomationElement.RootElement;
        if (root is null) return results;

        var walker = TreeWalker.ControlViewWalker;
        var queue = new Queue<(AutomationElement El, int Depth)>();
        var child = walker.GetFirstChild(root);
        while (child is not null)
        {
            queue.Enqueue((child, 0));
            child = walker.GetNextSibling(child);
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
                    bool matches = string.IsNullOrWhiteSpace(query) ||
                                   name.Contains(query, StringComparison.OrdinalIgnoreCase) ||
                                   autoId.Contains(query, StringComparison.OrdinalIgnoreCase);

                    if (matches && (!string.IsNullOrWhiteSpace(name) || !string.IsNullOrWhiteSpace(autoId)))
                    {
                        var r = current.BoundingRectangle;
                        results.Add(new UiElementInfo(
                            name,
                            autoId,
                            current.ControlType.ProgrammaticName.Replace("ControlType.", ""),
                            (int)r.X,
                            (int)r.Y,
                            (int)r.Width,
                            (int)r.Height));
                    }
                }

                if (depth < 4)
                {
                    var sub = walker.GetFirstChild(el);
                    int siblings = 0;
                    while (sub is not null && siblings < 30)
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

    public static UiElementInfo ClickElement(string query, string button = "left")
    {
        if (string.IsNullOrWhiteSpace(query))
        {
            throw new ArgumentException("Element query (name or automation_id) is required.");
        }

        var match = FindVisibleElements(query, 10).FirstOrDefault()
            ?? throw new InvalidOperationException($"No visible UI element matching '{query}' was found.");

        int centerX = match.X + Math.Max(1, match.Width / 2);
        int centerY = match.Y + Math.Max(1, match.Height / 2);
        DesktopInputService.ClickMouse(centerX, centerY, button);
        return match;
    }
}
