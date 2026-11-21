using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;

namespace AIBridgeClient.Services;

public sealed record AgentTimelineEntry(
    string Id,
    string Timestamp,
    string ToolName,
    string Kind, // "file" | "ui"
    string Summary,
    string? TargetPath = null,
    bool FileExistedBefore = false,
    string? PreviousFileContent = null);

/// <summary>
/// Records an in-memory action timeline for AI Agent mutations (file edits &amp; desktop UI actions)
/// and provides one-click Undo (restoring previous file content or sending Ctrl+Z for UI edits).
/// </summary>
public static class AgentActionTimelineService
{
    private const int MaxEntries = 100;
    private const int MaxSnapshotBytes = 512 * 1024;
    private static readonly object Sync = new();
    private static readonly List<AgentTimelineEntry> Entries = new();

    public static void RecordFileBackupBeforeWrite(string toolName, string fullPath)
    {
        try
        {
            bool existed = File.Exists(fullPath);
            string? prev = null;
            if (existed && new FileInfo(fullPath).Length <= MaxSnapshotBytes)
            {
                prev = File.ReadAllText(fullPath);
            }
            var entry = new AgentTimelineEntry(
                Guid.NewGuid().ToString("N")[..8],
                DateTimeOffset.Now.ToString("HH:mm:ss"),
                toolName,
                "file",
                $"{toolName}: {Path.GetFileName(fullPath)}",
                fullPath,
                existed,
                prev);
            AddEntry(entry);
        }
        catch
        {
            // Best-effort snapshot
        }
    }

    public static void RecordUiAction(string toolName, string summary)
    {
        var entry = new AgentTimelineEntry(
            Guid.NewGuid().ToString("N")[..8],
            DateTimeOffset.Now.ToString("HH:mm:ss"),
            toolName,
            "ui",
            summary);
        AddEntry(entry);
    }

    private static void AddEntry(AgentTimelineEntry entry)
    {
        lock (Sync)
        {
            Entries.Insert(0, entry);
            if (Entries.Count > MaxEntries)
            {
                Entries.RemoveRange(MaxEntries, Entries.Count - MaxEntries);
            }
        }
    }

    public static List<AgentTimelineEntry> GetRecent(int limit = 30)
    {
        lock (Sync)
        {
            return Entries.Take(Math.Clamp(limit, 1, MaxEntries)).ToList();
        }
    }

    public static void Clear()
    {
        lock (Sync)
        {
            Entries.Clear();
        }
    }

    public static (bool Ok, string Detail) UndoLastAction()
    {
        AgentTimelineEntry? top;
        lock (Sync)
        {
            if (Entries.Count == 0)
            {
                return (false, "No agent actions recorded to undo.");
            }
            top = Entries[0];
            Entries.RemoveAt(0);
        }

        if (top.Kind == "file" && !string.IsNullOrWhiteSpace(top.TargetPath))
        {
            try
            {
                if (!top.FileExistedBefore)
                {
                    if (File.Exists(top.TargetPath)) File.Delete(top.TargetPath);
                    return (true, $"Undid {top.ToolName}: removed newly created '{Path.GetFileName(top.TargetPath)}'.");
                }
                if (top.PreviousFileContent is not null)
                {
                    File.WriteAllText(top.TargetPath, top.PreviousFileContent);
                    return (true, $"Undid {top.ToolName}: restored previous contents of '{Path.GetFileName(top.TargetPath)}'.");
                }
                return (false, $"File '{Path.GetFileName(top.TargetPath)}' was too large to snapshot before edit.");
            }
            catch (Exception ex)
            {
                return (false, $"Failed to restore '{top.TargetPath}': {ex.Message}");
            }
        }

        // UI action undo -> send Ctrl+Z to the active window
        try
        {
            DesktopInputService.PressHotkey("ctrl+z");
            return (true, $"Sent Ctrl+Z to undo UI action: {top.Summary}");
        }
        catch (Exception ex)
        {
            return (false, $"Undo Ctrl+Z failed: {ex.Message}");
        }
    }
}
