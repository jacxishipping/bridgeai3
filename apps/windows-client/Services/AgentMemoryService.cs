using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json;

namespace AIBridgeClient.Services;

/// <summary>
/// Persists per-device AI Agent memories &amp; user preferences to ~/.ai-bridge/agent_memory.json
/// so the agent retains context across restarts.
/// </summary>
public static class AgentMemoryService
{
    private static readonly object Sync = new();
    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        WriteIndented = true,
        PropertyNameCaseInsensitive = true,
    };

    public static string GetDefaultPath()
    {
        var home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        return Path.Combine(home, ".ai-bridge", "agent_memory.json");
    }

    public static Dictionary<string, string> Load(string? filePath = null)
    {
        var path = string.IsNullOrWhiteSpace(filePath) ? GetDefaultPath() : filePath;
        lock (Sync)
        {
            try
            {
                if (File.Exists(path))
                {
                    var dict = JsonSerializer.Deserialize<Dictionary<string, string>>(File.ReadAllText(path), JsonOpts);
                    if (dict is not null) return new Dictionary<string, string>(dict, StringComparer.OrdinalIgnoreCase);
                }
            }
            catch { }
            return new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        }
    }

    public static void Save(Dictionary<string, string> memory, string? filePath = null)
    {
        var path = string.IsNullOrWhiteSpace(filePath) ? GetDefaultPath() : filePath;
        lock (Sync)
        {
            try
            {
                var dir = Path.GetDirectoryName(path);
                if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);
                var tmp = path + ".tmp";
                File.WriteAllText(tmp, JsonSerializer.Serialize(memory, JsonOpts), Encoding.UTF8);
                File.Move(tmp, path, overwrite: true);
            }
            catch { }
        }
    }

    public static void Remember(string key, string value, string? filePath = null)
    {
        if (string.IsNullOrWhiteSpace(key)) throw new ArgumentException("Memory key is required.");
        var mem = Load(filePath);
        mem[key.Trim()] = value?.Trim() ?? "";
        Save(mem, filePath);
    }

    public static bool Forget(string key, string? filePath = null)
    {
        if (string.IsNullOrWhiteSpace(key)) return false;
        var mem = Load(filePath);
        bool removed = mem.Remove(key.Trim());
        if (removed) Save(mem, filePath);
        return removed;
    }

    public static string BuildPromptBlock(string? filePath = null)
    {
        var mem = Load(filePath);
        if (mem.Count == 0) return "";
        var sb = new StringBuilder();
        sb.AppendLine("Remembered User Preferences & Device Facts:");
        foreach (var kv in mem.Take(40))
        {
            sb.AppendLine($"- {kv.Key}: {kv.Value}");
        }
        return sb.ToString();
    }
}
