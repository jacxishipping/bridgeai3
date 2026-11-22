using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json;
using AIBridgeClient.Models;

namespace AIBridgeClient.Services;

/// <summary>
/// Persists Agent chat threads to ~/.ai-bridge/agent_chats.json and exports threads to Markdown.
/// </summary>
public static class ChatHistoryService
{
    private const int MaxThreads = 40;
    private const int MaxMessagesPerThread = 80;

    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        WriteIndented = true,
        PropertyNameCaseInsensitive = true,
    };

    private sealed class StoredMessage
    {
        public string Role { get; set; } = "assistant";
        public string Content { get; set; } = "";
        public string ToolsSummary { get; set; } = "";
        public string ToolDetails { get; set; } = "";
        public string Timestamp { get; set; } = "";
    }

    private sealed class StoredThread
    {
        public string Id { get; set; } = "";
        public string Title { get; set; } = "New Chat";
        public string Preview { get; set; } = "";
        public List<StoredMessage> Messages { get; set; } = new();
    }

    public static string GetDefaultPath()
    {
        var home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        return Path.Combine(home, ".ai-bridge", "agent_chats.json");
    }

    public static List<AgentChatThread> Load(string? filePath = null)
    {
        var path = string.IsNullOrWhiteSpace(filePath) ? GetDefaultPath() : filePath;
        var result = new List<AgentChatThread>();
        try
        {
            if (!File.Exists(path)) return result;
            var json = File.ReadAllText(path);
            var stored = JsonSerializer.Deserialize<List<StoredThread>>(json, JsonOpts);
            if (stored is null) return result;
            foreach (var st in stored.Take(MaxThreads))
            {
                var th = new AgentChatThread
                {
                    Id = string.IsNullOrWhiteSpace(st.Id) ? Guid.NewGuid().ToString("N")[..8] : st.Id,
                    Title = string.IsNullOrWhiteSpace(st.Title) ? "Saved Chat" : st.Title,
                    Preview = st.Preview ?? "",
                };
                foreach (var m in (st.Messages ?? new List<StoredMessage>()).TakeLast(MaxMessagesPerThread))
                {
                    th.Messages.Add(new AgentChatMessage
                    {
                        Role = m.Role,
                        Content = m.Content,
                        ToolsSummary = m.ToolsSummary,
                        ToolDetails = m.ToolDetails,
                        Timestamp = m.Timestamp,
                    });
                }
                result.Add(th);
            }
        }
        catch
        {
            // Ignore corrupt chat history
        }
        return result;
    }

    public static void Save(IEnumerable<AgentChatThread> threads, string? filePath = null)
    {
        var path = string.IsNullOrWhiteSpace(filePath) ? GetDefaultPath() : filePath;
        try
        {
            var dir = Path.GetDirectoryName(path);
            if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);
            var payload = threads
                .Where(t => t.Messages.Count > 0)
                .Take(MaxThreads)
                .Select(t => new StoredThread
                {
                    Id = t.Id,
                    Title = t.Title,
                    Preview = t.Preview,
                    Messages = t.Messages
                        .TakeLast(MaxMessagesPerThread)
                        .Select(m => new StoredMessage
                        {
                            Role = m.Role,
                            Content = m.Content,
                            ToolsSummary = m.ToolsSummary,
                            ToolDetails = m.ToolDetails,
                            Timestamp = m.Timestamp,
                        })
                        .ToList(),
                })
                .ToList();

            var tmp = path + ".tmp";
            File.WriteAllText(tmp, JsonSerializer.Serialize(payload, JsonOpts), Encoding.UTF8);
            File.Move(tmp, path, overwrite: true);
        }
        catch
        {
            // Best-effort persistence
        }
    }

    public static string ExportThreadToMarkdown(AgentChatThread thread)
    {
        var sb = new StringBuilder();
        sb.AppendLine($"# {thread.Title}");
        sb.AppendLine();
        foreach (var m in thread.Messages)
        {
            var who = string.Equals(m.Role, "user", StringComparison.OrdinalIgnoreCase) ? "User" : "AI Bridge Agent";
            sb.AppendLine($"### {who} ({m.Timestamp})");
            if (!string.IsNullOrWhiteSpace(m.ToolsSummary))
            {
                sb.AppendLine($"> Tools: `{m.ToolsSummary}`");
                sb.AppendLine();
            }
            sb.AppendLine(m.Content);
            sb.AppendLine();
        }
        return sb.ToString();
    }
}
