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
    public string PipeHmacSecretFile { get; }
    public string AgentApiKeyFile { get; }
    public string TaskHistoryFile { get; }
    public string PolicyAuditFile { get; }
    public string LogDir { get; }
    public string ClientLogFile { get; }
    public string DefaultScreenshotDir { get; }
    private readonly object _logLock = new();

    public ConfigService()
    {
        var home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        ConfigDir = Path.Combine(home, ".ai-bridge");
        ConfigFile = Path.Combine(ConfigDir, "agent_gui.json");
        TokenFile = Path.Combine(ConfigDir, "device.token");
        PipeHmacSecretFile = Path.Combine(ConfigDir, "pipe_hmac.token");
        AgentApiKeyFile = Path.Combine(ConfigDir, "agent_api.token");
        TaskHistoryFile = Path.Combine(ConfigDir, "task_history.json");
        PolicyAuditFile = Path.Combine(ConfigDir, "policy_audit.json");
        LogDir = Path.Combine(ConfigDir, "logs");
        ClientLogFile = Path.Combine(LogDir, "ai-bridge-client.log");
        DefaultScreenshotDir = Path.Combine(ConfigDir, "screenshots");
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
                    cfg.Policy.AllowedCommands ??= new List<string>();
                    cfg.DeviceAgent ??= new DeviceAgentSettings();
                    cfg.DeviceAgent.Functions ??= new AgentFunctionPermissions();
                    cfg.DeviceAgent.Functions.DisabledTools ??= new List<string>();
                    if (string.IsNullOrWhiteSpace(cfg.Workspace))
                    {
                        cfg.Workspace = Path.Combine(
                            Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                            "ai-bridge-workspace");
                    }
                    if (string.IsNullOrWhiteSpace(cfg.ScreenshotDirectory))
                    {
                        cfg.ScreenshotDirectory = DefaultScreenshotDir;
                    }
                    if (string.IsNullOrWhiteSpace(cfg.DesktopPipeName))
                    {
                        cfg.DesktopPipeName = "AIBridgeDesktopPipe";
                    }
                    if (string.IsNullOrWhiteSpace(cfg.TenantId))
                    {
                        cfg.TenantId = "default";
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
            ScreenshotDirectory = DefaultScreenshotDir,
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

    public void ClearToken()
    {
        try { if (File.Exists(TokenFile)) File.Delete(TokenFile); } catch { }
    }

    public string LoadPipeHmacSecret()
    {
        try { return DpapiTokenStore.LoadTokenFile(PipeHmacSecretFile); } catch { return string.Empty; }
    }

    public void SavePipeHmacSecret(string secret)
    {
        if (string.IsNullOrWhiteSpace(secret))
        {
            try { if (File.Exists(PipeHmacSecretFile)) File.Delete(PipeHmacSecretFile); } catch { }
            return;
        }
        DpapiTokenStore.SaveTokenFile(PipeHmacSecretFile, secret.Trim());
    }

    public string LoadAgentApiKey()
    {
        try { return DpapiTokenStore.LoadTokenFile(AgentApiKeyFile); } catch { return string.Empty; }
    }

    public void SaveAgentApiKey(string apiKey)
    {
        if (string.IsNullOrWhiteSpace(apiKey))
        {
            try { if (File.Exists(AgentApiKeyFile)) File.Delete(AgentApiKeyFile); } catch { }
            return;
        }
        DpapiTokenStore.SaveTokenFile(AgentApiKeyFile, apiKey.Trim());
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

    public void SaveTaskHistory(List<TaskHistoryRecord> records, int maxLimit = TaskHistoryLimit)
    {
        int cap = Math.Clamp(maxLimit, 50, 2000);
        if (records.Count > cap)
        {
            records = records.GetRange(0, cap);
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

    public void AppendLogLine(string line, int maxFileMb = 2)
    {
        try
        {
            long maxBytes = Math.Clamp(maxFileMb, 1, 50) * 1024L * 1024L;
            lock (_logLock)
            {
                Directory.CreateDirectory(LogDir);
                if (File.Exists(ClientLogFile) && new FileInfo(ClientLogFile).Length > maxBytes)
                {
                    var rotated = ClientLogFile + ".1";
                    File.Move(ClientLogFile, rotated, overwrite: true);
                }
                File.AppendAllText(ClientLogFile, line + Environment.NewLine);
            }
        }
        catch
        {
            // Never break UI logging on disk write failure
        }
    }

    public List<string> LoadPolicyAudit()
    {
        try
        {
            if (File.Exists(PolicyAuditFile))
            {
                var json = File.ReadAllText(PolicyAuditFile);
                return JsonSerializer.Deserialize<List<string>>(json, JsonOpts) ?? new();
            }
        }
        catch
        {
            // Ignore corrupt audit log
        }
        return new List<string>();
    }

    public void SavePolicyAudit(IEnumerable<string> entries)
    {
        try
        {
            var capped = new List<string>();
            foreach (var e in entries)
            {
                capped.Add(e);
                if (capped.Count >= 200) break;
            }
            var json = JsonSerializer.Serialize(capped, JsonOpts);
            var tmp = PolicyAuditFile + ".tmp";
            File.WriteAllText(tmp, json);
            File.Move(tmp, PolicyAuditFile, overwrite: true);
        }
        catch
        {
            // Best-effort persistence
        }
    }
}
