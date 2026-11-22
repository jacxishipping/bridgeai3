using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json;

namespace AIBridgeClient.Services;

public sealed class AgentPlaybook
{
    public string Id { get; set; } = Guid.NewGuid().ToString("N")[..8];
    public string Name { get; set; } = "";
    public string Prompt { get; set; } = "";
    public string Description { get; set; } = "";
}

/// <summary>
/// Persists reusable 1-click AI Agent Playbooks / Saved Skills to ~/.ai-bridge/agent_playbooks.json.
/// </summary>
public static class AgentPlaybookService
{
    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        WriteIndented = true,
        PropertyNameCaseInsensitive = true,
    };

    public static string GetDefaultPath()
    {
        var home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        return Path.Combine(home, ".ai-bridge", "agent_playbooks.json");
    }

    public static List<AgentPlaybook> GetDefaultPlaybooks() => new()
    {
        new AgentPlaybook
        {
            Id = "pb-car",
            Name = "Draw a Car in Paint",
            Description = "Launch Paint, maximize, and draw a car using smooth human-like strokes",
            Prompt = "Open Paint, maximize it, and draw a detailed car with smooth circular wheels, windows, headlights, and road, then capture a screenshot.",
        },
        new AgentPlaybook
        {
            Id = "pb-health",
            Name = "Workstation Health Audit",
            Description = "Check CPU, RAM, disk, top processes, and C++ pipe status",
            Prompt = "Check system info, CPU/RAM/disk resource usage, top 10 memory processes, and desktop pipe status, then summarize any issues.",
        },
        new AgentPlaybook
        {
            Id = "pb-som",
            Name = "Visual UI Map",
            Description = "Capture Set-of-Marks screenshot and list interactive controls",
            Prompt = "Capture a Set-of-Marks screenshot of the active window and summarize the key clickable buttons and inputs.",
        },
    };

    public static List<AgentPlaybook> Load(string? filePath = null)
    {
        var path = string.IsNullOrWhiteSpace(filePath) ? GetDefaultPath() : filePath;
        try
        {
            if (File.Exists(path))
            {
                var items = JsonSerializer.Deserialize<List<AgentPlaybook>>(File.ReadAllText(path), JsonOpts);
                if (items is { Count: > 0 }) return items;
            }
        }
        catch { }
        return GetDefaultPlaybooks();
    }

    public static void Save(IEnumerable<AgentPlaybook> playbooks, string? filePath = null)
    {
        var path = string.IsNullOrWhiteSpace(filePath) ? GetDefaultPath() : filePath;
        try
        {
            var dir = Path.GetDirectoryName(path);
            if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);
            var tmp = path + ".tmp";
            File.WriteAllText(tmp, JsonSerializer.Serialize(playbooks.Take(50).ToList(), JsonOpts), Encoding.UTF8);
            File.Move(tmp, path, overwrite: true);
        }
        catch { }
    }
}
