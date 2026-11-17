using System;
using System.Collections.Generic;
using System.IO;
using System.Text.Json;
using Microsoft.Win32;
using AIBridgeClient.Models;

namespace AIBridgeClient.Services;

public sealed class ConfigService
{
    private const string RegRunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";
    private const string RegAppName = "AIBridgeWindowsClient";
    private const int TaskHistoryLimit = 500;

    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        WriteIndented = true,
        PropertyNameCaseInsensitive = true,
    };

    public string ConfigDir { get; }
    public string ConfigFile { get; }
    public string TokenFile { get; }
    public string TaskHistoryFile { get; }
    public string LogDir { get; }

    public ConfigService()
    {
        var home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        ConfigDir = Path.Combine(home, ".ai-bridge");
        ConfigFile = Path.Combine(ConfigDir, "agent_gui.json");
        TokenFile = Path.Combine(ConfigDir, "device.token");
        TaskHistoryFile = Path.Combine(ConfigDir, "task_history.json");
        LogDir = Path.Combine(ConfigDir, "logs");
        Directory.CreateDirectory(ConfigDir);
        Directory.CreateDirectory(LogDir);
    }

    public AgentGuiConfig LoadConfig()
    {
        try
        {
            if (File.Exists(ConfigFile))
            {
                var json = File.ReadAllText(ConfigFile);
                var cfg = JsonSerializer.Deserialize<AgentGuiConfig>(json, JsonOpts);
                if (cfg is not null)
                {
                    cfg.Policy ??= new AgentPolicyConfig();
                    cfg.Policy.AllowedApps ??= new List<string>();
                    if (string.IsNullOrWhiteSpace(cfg.Workspace))
                    {
                        cfg.Workspace = Path.Combine(
                            Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                            "ai-bridge-workspace");
                    }
                    return cfg;
                }
            }
        }
        catch
        {
            // Fall back to defaults on corrupt config
        }

        return new AgentGuiConfig
        {
            Workspace = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                "ai-bridge-workspace"),
        };
    }

    public void SaveConfig(AgentGuiConfig config)
    {
        Directory.CreateDirectory(ConfigDir);
        var json = JsonSerializer.Serialize(config, JsonOpts);
        var tmp = ConfigFile + ".tmp";
        File.WriteAllText(tmp, json);
        File.Move(tmp, ConfigFile, overwrite: true);
    }

    public string LoadToken()
    {
        try
        {
            return DpapiTokenStore.LoadTokenFile(TokenFile);
        }
        catch
        {
            return string.Empty;
        }
    }

    public void SaveToken(string token)
    {
        DpapiTokenStore.SaveTokenFile(TokenFile, token);
    }

    public List<TaskHistoryRecord> LoadTaskHistory()
    {
        try
        {
            if (File.Exists(TaskHistoryFile))
            {
                var json = File.ReadAllText(TaskHistoryFile);
                return JsonSerializer.Deserialize<List<TaskHistoryRecord>>(json, JsonOpts) ?? new();
            }
        }
        catch
        {
            // Ignore corrupt history
        }
        return new List<TaskHistoryRecord>();
    }

    public void SaveTaskHistory(List<TaskHistoryRecord> records)
    {
        if (records.Count > TaskHistoryLimit)
        {
            records = records.GetRange(0, TaskHistoryLimit);
        }
        var json = JsonSerializer.Serialize(records, JsonOpts);
        var tmp = TaskHistoryFile + ".tmp";
        File.WriteAllText(tmp, json);
        File.Move(tmp, TaskHistoryFile, overwrite: true);
    }

    public bool IsAutoStartEnabled()
    {
        try
        {
            using var key = Registry.CurrentUser.OpenSubKey(RegRunKey, writable: false);
            return key?.GetValue(RegAppName) is string val && !string.IsNullOrWhiteSpace(val);
        }
        catch
        {
            return false;
        }
    }

    public void SetAutoStart(bool enabled)
    {
        using var key = Registry.CurrentUser.OpenSubKey(RegRunKey, writable: true)
            ?? Registry.CurrentUser.CreateSubKey(RegRunKey, writable: true);
        if (enabled)
        {
            var exePath = Environment.ProcessPath ?? "";
            if (!string.IsNullOrWhiteSpace(exePath))
            {
                key.SetValue(RegAppName, $"\"{exePath}\"");
            }
        }
        else
        {
            key.DeleteValue(RegAppName, throwOnMissingValue: false);
        }
    }
}
