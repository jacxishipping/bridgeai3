using System;
using System.IO;
using System.Reflection;
using Microsoft.Win32;
using AIBridgeInstaller.Models;

namespace AIBridgeInstaller.Services;

/// <summary>
/// Manages Windows Start Menu / Desktop shortcuts, Startup Run registry key,
/// and Add/Remove Programs (Uninstall) registration under HKCU (no UAC needed).
/// </summary>
public static class ShortcutAndRegistryService
{
    public const string RegRunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";
    public const string RegRunValueName = "AIBridgeWindowsClient";
    public const string RegUninstallSubKey = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\AIBridgeWindowsClient";

    public static string? DetectExistingInstallDirectory()
    {
        try
        {
            using var key = Registry.CurrentUser.OpenSubKey(RegUninstallSubKey, false);
            var loc = key?.GetValue("InstallLocation") as string;
            if (!string.IsNullOrWhiteSpace(loc) && Directory.Exists(loc))
            {
                return loc;
            }
        }
        catch
        {
            // Ignore registry lookup failures
        }

        var defaultDir = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "Programs",
            "AIBridgeClient");
        if (File.Exists(Path.Combine(defaultDir, InstallerOptions.ClientExeName)))
        {
            return defaultDir;
        }
        return null;
    }

    public static string? DetectInstalledVersion()
    {
        try
        {
            using var key = Registry.CurrentUser.OpenSubKey(RegUninstallSubKey, false);
            return key?.GetValue("DisplayVersion") as string;
        }
        catch
        {
            return null;
        }
    }

    public static void CreateShortcut(string shortcutPath, string targetPath, string workingDirectory, string description, string arguments = "")
    {
        var dir = Path.GetDirectoryName(shortcutPath);
        if (!string.IsNullOrEmpty(dir))
        {
            Directory.CreateDirectory(dir);
        }

        var shellType = Type.GetTypeFromProgID("WScript.Shell");
        if (shellType is null)
        {
            throw new InvalidOperationException("WScript.Shell COM automation is unavailable on this host.");
        }

        object? shell = null;
        object? shortcut = null;
        try
        {
            shell = Activator.CreateInstance(shellType);
            shortcut = shellType.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { shortcutPath });
            if (shortcut is null)
            {
                throw new InvalidOperationException("Failed to initialize Windows shortcut object.");
            }

            var st = shortcut.GetType();
            st.InvokeMember("TargetPath", BindingFlags.SetProperty, null, shortcut, new object[] { targetPath });
            st.InvokeMember("WorkingDirectory", BindingFlags.SetProperty, null, shortcut, new object[] { workingDirectory });
            st.InvokeMember("Description", BindingFlags.SetProperty, null, shortcut, new object[] { description });
            st.InvokeMember("IconLocation", BindingFlags.SetProperty, null, shortcut, new object[] { $"{targetPath},0" });
            if (!string.IsNullOrWhiteSpace(arguments))
            {
                st.InvokeMember("Arguments", BindingFlags.SetProperty, null, shortcut, new object[] { arguments });
            }
            st.InvokeMember("Save", BindingFlags.InvokeMethod, null, shortcut, null);
        }
        finally
        {
            if (shortcut is not null && OperatingSystem.IsWindows())
            {
                try { System.Runtime.InteropServices.Marshal.FinalReleaseComObject(shortcut); } catch { }
            }
            if (shell is not null && OperatingSystem.IsWindows())
            {
                try { System.Runtime.InteropServices.Marshal.FinalReleaseComObject(shell); } catch { }
            }
        }
    }

    public static void CreateDesktopAndStartMenuShortcuts(InstallerOptions options, string clientExePath, string uninstallerExePath)
    {
        if (options.CreateDesktopShortcut)
        {
            var desktop = Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory);
            var lnk = Path.Combine(desktop, "AI Bridge Client.lnk");
            CreateShortcut(lnk, clientExePath, options.InstallDirectory, "AI Bridge Windows Client — Enterprise Endpoint Agent");
        }

        if (options.CreateStartMenuShortcut)
        {
            var programs = Environment.GetFolderPath(Environment.SpecialFolder.Programs);
            var folder = Path.Combine(programs, "AI Bridge");
            Directory.CreateDirectory(folder);

            CreateShortcut(
                Path.Combine(folder, "AI Bridge Client.lnk"),
                clientExePath,
                options.InstallDirectory,
                "Launch AI Bridge Windows Client");

            if (File.Exists(uninstallerExePath))
            {
                CreateShortcut(
                    Path.Combine(folder, "Uninstall AI Bridge Client.lnk"),
                    uninstallerExePath,
                    options.InstallDirectory,
                    "Uninstall AI Bridge Windows Client",
                    "--uninstall");
            }
        }
    }

    public static void RemoveShortcuts()
    {
        try
        {
            var desktopLnk = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), "AI Bridge Client.lnk");
            if (File.Exists(desktopLnk)) File.Delete(desktopLnk);
        }
        catch { }

        try
        {
            var startFolder = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Programs), "AI Bridge");
            if (Directory.Exists(startFolder)) Directory.Delete(startFolder, recursive: true);
        }
        catch { }
    }

    public static void SetStartWithWindows(bool enabled, string clientExePath)
    {
        using var key = Registry.CurrentUser.OpenSubKey(RegRunKey, writable: true)
            ?? Registry.CurrentUser.CreateSubKey(RegRunKey);
        if (key is null) return;

        if (enabled)
        {
            key.SetValue(RegRunValueName, $"\"{clientExePath}\" --minimized", RegistryValueKind.String);
        }
        else
        {
            key.DeleteValue(RegRunValueName, throwOnMissingValue: false);
        }
    }

    public static void RegisterUninstallEntry(InstallerOptions options, string clientExePath, string uninstallerExePath, long installedBytes)
    {
        using var key = Registry.CurrentUser.CreateSubKey(RegUninstallSubKey);
        if (key is null) return;

        var estimatedKb = (int)Math.Max(1024, installedBytes / 1024);
        key.SetValue("DisplayName", InstallerOptions.ProductName, RegistryValueKind.String);
        key.SetValue("DisplayVersion", InstallerOptions.ProductVersion, RegistryValueKind.String);
        key.SetValue("Publisher", InstallerOptions.Publisher, RegistryValueKind.String);
        key.SetValue("InstallLocation", options.InstallDirectory, RegistryValueKind.String);
        key.SetValue("DisplayIcon", $"{clientExePath},0", RegistryValueKind.String);
        key.SetValue("UninstallString", $"\"{uninstallerExePath}\" --uninstall", RegistryValueKind.String);
        key.SetValue("QuietUninstallString", $"\"{uninstallerExePath}\" --uninstall --silent", RegistryValueKind.String);
        key.SetValue("InstallDate", DateTime.Now.ToString("yyyyMMdd"), RegistryValueKind.String);
        key.SetValue("EstimatedSize", estimatedKb, RegistryValueKind.DWord);
        key.SetValue("NoModify", 1, RegistryValueKind.DWord);
        key.SetValue("NoRepair", 0, RegistryValueKind.DWord);
    }

    public static void RemoveUninstallEntry()
    {
        try
        {
            Registry.CurrentUser.DeleteSubKeyTree(RegUninstallSubKey, throwOnMissingSubKey: false);
        }
        catch { }

        try
        {
            using var runKey = Registry.CurrentUser.OpenSubKey(RegRunKey, writable: true);
            runKey?.DeleteValue(RegRunValueName, throwOnMissingValue: false);
        }
        catch { }
    }
}
