using System;
using System.IO;

namespace AIBridgeInstaller.Models;

public sealed class InstallerOptions
{
    public const string ProductName = "AI Bridge Windows Client";
    public const string ProductVersion = "0.3.0";
    public const string Publisher = "AI Bridge";
    public const string ClientExeName = "AIBridgeClient.exe";
    public const string SetupExeName = "AIBridgeSetup.exe";

    public string InstallDirectory { get; set; } = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "Programs",
        "AIBridgeClient");

    public string WorkspaceDirectory { get; set; } = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
        "ai-bridge-workspace");

    public string? CustomPayloadSourceDir { get; set; }

    public bool CreateDesktopShortcut { get; set; } = true;
    public bool CreateStartMenuShortcut { get; set; } = true;
    public bool StartWithWindows { get; set; } = true;
    public bool LaunchAfterInstall { get; set; } = true;

    // Optional initial Control Plane configuration
    public string ControlPlaneUrl { get; set; } = "http://127.0.0.1:8000";
    public string DeviceToken { get; set; } = string.Empty;
    public string DeviceId { get; set; } = Environment.MachineName.ToLowerInvariant();
    public string TenantId { get; set; } = "default";

    // Fail-closed default guardrails matching AgentPolicyConfig
    public bool AllowWrites { get; set; } = true;
    public bool AllowTier3 { get; set; } = false;
    public bool AllowShell { get; set; } = false;
    public bool AllowDesktopControl { get; set; } = false;

    public string Theme { get; set; } = "dark";
    public bool KeepUserConfigOnUninstall { get; set; } = true;
    public bool Silent { get; set; } = false;
    public bool UninstallMode { get; set; } = false;

    public static InstallerOptions ParseArgs(string[] args)
    {
        var opts = new InstallerOptions();
        for (int i = 0; i < args.Length; i++)
        {
            var arg = args[i].Trim();
            if (arg.Equals("--silent", StringComparison.OrdinalIgnoreCase) ||
                arg.Equals("/S", StringComparison.OrdinalIgnoreCase))
            {
                opts.Silent = true;
                opts.LaunchAfterInstall = false;
            }
            else if (arg.Equals("--uninstall", StringComparison.OrdinalIgnoreCase) ||
                     arg.Equals("/U", StringComparison.OrdinalIgnoreCase))
            {
                opts.UninstallMode = true;
            }
            else if (arg.Equals("--no-desktop-shortcut", StringComparison.OrdinalIgnoreCase))
            {
                opts.CreateDesktopShortcut = false;
            }
            else if (arg.Equals("--no-startmenu-shortcut", StringComparison.OrdinalIgnoreCase))
            {
                opts.CreateStartMenuShortcut = false;
            }
            else if (arg.Equals("--no-autostart", StringComparison.OrdinalIgnoreCase))
            {
                opts.StartWithWindows = false;
            }
            else if (arg.Equals("--purge-config", StringComparison.OrdinalIgnoreCase))
            {
                opts.KeepUserConfigOnUninstall = false;
            }
            else if (arg.Equals("--dir", StringComparison.OrdinalIgnoreCase) && i + 1 < args.Length)
            {
                opts.InstallDirectory = args[++i];
            }
            else if (arg.Equals("--payload", StringComparison.OrdinalIgnoreCase) && i + 1 < args.Length)
            {
                opts.CustomPayloadSourceDir = args[++i];
            }
            else if (arg.Equals("--control-url", StringComparison.OrdinalIgnoreCase) && i + 1 < args.Length)
            {
                opts.ControlPlaneUrl = args[++i];
            }
            else if (arg.Equals("--token", StringComparison.OrdinalIgnoreCase) && i + 1 < args.Length)
            {
                opts.DeviceToken = args[++i];
            }
            else if (arg.Equals("--device-id", StringComparison.OrdinalIgnoreCase) && i + 1 < args.Length)
            {
                opts.DeviceId = args[++i];
            }
        }
        return opts;
    }
}

public sealed record InstallProgressEvent(
    int Percent,
    int StepIndex,
    string StepTitle,
    string LogMessage,
    bool IsError = false);

public sealed record InstallResult(
    bool Success,
    string Message,
    string InstallDirectory,
    string? ClientExePath,
    string? ClientSha256);

public sealed record ConnectionTestResult(
    bool Reachable,
    long LatencyMs,
    string Version,
    string Message);
