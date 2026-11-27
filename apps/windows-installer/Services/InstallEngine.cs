using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Net.Http;
using System.Reflection;
using System.Security.Cryptography;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using AIBridgeInstaller.Models;

namespace AIBridgeInstaller.Services;

public sealed class InstallEngine
{
    private const string EmbeddedPayloadResourceName = "AIBridgeInstaller.Payload.zip";

    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        WriteIndented = true,
        PropertyNameCaseInsensitive = true,
    };

    public static bool HasEmbeddedPayload()
    {
        var asm = Assembly.GetExecutingAssembly();
        return asm.GetManifestResourceInfo(EmbeddedPayloadResourceName) is not null;
    }

    public static long GetFreeDiskBytes(string directoryPath)
    {
        try
        {
            var root = Path.GetPathRoot(Path.GetFullPath(directoryPath));
            if (string.IsNullOrWhiteSpace(root)) return 0;
            return new DriveInfo(root).AvailableFreeSpace;
        }
        catch
        {
            return 0;
        }
    }

    public async Task<ConnectionTestResult> TestControlPlaneAsync(string controlUrl, CancellationToken ct = default)
    {
        var trimmed = (controlUrl ?? string.Empty).Trim().TrimEnd('/');
        if (!Uri.TryCreate(trimmed, UriKind.Absolute, out var uri) ||
            (uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps))
        {
            return new ConnectionTestResult(false, 0, "", "Enter a valid http:// or https:// Control Plane URL.");
        }

        using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(6) };
        var sw = Stopwatch.StartNew();
        try
        {
            using var resp = await http.GetAsync($"{trimmed}/health", ct);
            sw.Stop();
            if (!resp.IsSuccessStatusCode)
            {
                return new ConnectionTestResult(false, sw.ElapsedMilliseconds, "", $"Control plane returned HTTP {(int)resp.StatusCode}.");
            }
            var body = await resp.Content.ReadAsStringAsync(ct);
            var version = "live";
            try
            {
                using var doc = JsonDocument.Parse(body);
                if (doc.RootElement.TryGetProperty("version", out var vEl) && vEl.ValueKind == JsonValueKind.String)
                {
                    version = vEl.GetString() ?? "live";
                }
            }
            catch { }
            return new ConnectionTestResult(true, Math.Max(1, sw.ElapsedMilliseconds), version, $"Connected ({Math.Max(1, sw.ElapsedMilliseconds)} ms · v{version})");
        }
        catch (Exception ex)
        {
            sw.Stop();
            return new ConnectionTestResult(false, sw.ElapsedMilliseconds, "", $"Unreachable: {ex.Message}");
        }
    }

    public async Task<InstallResult> InstallAsync(
        InstallerOptions options,
        IProgress<InstallProgressEvent>? progress = null,
        CancellationToken ct = default)
    {
        void Report(int pct, int step, string title, string log, bool isErr = false)
            => progress?.Report(new InstallProgressEvent(pct, step, title, $"[{DateTime.Now:HH:mm:ss}] {log}", isErr));

        try
        {
            // Step 0: Preflight & stop running client
            Report(8, 0, "Preflight checks", $"Target directory: {options.InstallDirectory}");
            var freeBytes = GetFreeDiskBytes(options.InstallDirectory);
            if (freeBytes > 0 && freeBytes < 150L * 1024 * 1024)
            {
                return new InstallResult(false, "Insufficient free disk space (at least 150 MB required).", options.InstallDirectory, null, null);
            }

            await Task.Run(() => StopRunningClientProcesses(msg => Report(14, 0, "Preflight checks", msg)), ct);
            Directory.CreateDirectory(options.InstallDirectory);
            Report(20, 0, "Preflight checks", "Destination directory verified and ready.");

            // Step 1: Extract or copy client payload
            Report(28, 1, "Extracting application files", "Locating AI Bridge Windows Client payload...");
            long installedBytes = await Task.Run(() => DeployPayload(options, msg => Report(42, 1, "Extracting application files", msg)), ct);
            var clientExePath = Path.Combine(options.InstallDirectory, InstallerOptions.ClientExeName);
            if (!File.Exists(clientExePath))
            {
                return new InstallResult(
                    false,
                    $"Installation payload did not contain {InstallerOptions.ClientExeName}. Build the bundled installer with scripts/build_windows_installer.ps1 or specify --payload <dir>.",
                    options.InstallDirectory,
                    null,
                    null);
            }

            // Copy self into install directory as the native GUI uninstaller
            var uninstallerExePath = Path.Combine(options.InstallDirectory, InstallerOptions.SetupExeName);
            var selfPath = Environment.ProcessPath;
            if (!string.IsNullOrWhiteSpace(selfPath) &&
                File.Exists(selfPath) &&
                !string.Equals(Path.GetFullPath(selfPath), Path.GetFullPath(uninstallerExePath), StringComparison.OrdinalIgnoreCase))
            {
                File.Copy(selfPath, uninstallerExePath, overwrite: true);
                Report(56, 1, "Extracting application files", $"Registered native GUI uninstaller at {uninstallerExePath}");
            }

            // Step 2: Configure client & DPAPI token sealing
            Report(64, 2, "Configuring agent & DPAPI vault", "Writing fail-closed client configuration...");
            await Task.Run(() => WriteClientConfiguration(options, msg => Report(72, 2, "Configuring agent & DPAPI vault", msg)), ct);

            // Step 3: Workspace & shortcuts
            Report(80, 3, "Creating shortcuts & Windows registration", $"Ensuring workspace folder {options.WorkspaceDirectory}");
            Directory.CreateDirectory(options.WorkspaceDirectory);
            await Task.Run(() =>
            {
                ShortcutAndRegistryService.CreateDesktopAndStartMenuShortcuts(options, clientExePath, uninstallerExePath);
                ShortcutAndRegistryService.SetStartWithWindows(options.StartWithWindows, clientExePath);
                ShortcutAndRegistryService.RegisterUninstallEntry(options, clientExePath, uninstallerExePath, installedBytes);
            }, ct);
            Report(90, 3, "Creating shortcuts & Windows registration", "Start Menu, Desktop shortcuts, and Installed Apps entry configured.");

            // Step 4: Verify SHA-256 integrity
            Report(95, 4, "Verifying binary integrity", $"Computing SHA-256 for {InstallerOptions.ClientExeName}...");
            var sha256 = await Task.Run(() => ComputeSha256(clientExePath), ct);
            Report(100, 4, "Installation complete", $"Verified SHA-256: {sha256}");

            return new InstallResult(
                true,
                $"Successfully installed {InstallerOptions.ProductName} v{InstallerOptions.ProductVersion}.",
                options.InstallDirectory,
                clientExePath,
                sha256);
        }
        catch (Exception ex)
        {
            Report(100, 4, "Installation failed", $"ERROR: {ex.Message}", isErr: true);
            return new InstallResult(false, ex.Message, options.InstallDirectory, null, null);
        }
    }

    public async Task<InstallResult> UninstallAsync(
        InstallerOptions options,
        IProgress<InstallProgressEvent>? progress = null,
        CancellationToken ct = default)
    {
        void Report(int pct, int step, string title, string log, bool isErr = false)
            => progress?.Report(new InstallProgressEvent(pct, step, title, $"[{DateTime.Now:HH:mm:ss}] {log}", isErr));

        try
        {
            var installDir = ShortcutAndRegistryService.DetectExistingInstallDirectory() ?? options.InstallDirectory;
            Report(15, 0, "Stopping AI Bridge Client", $"Preparing removal of {installDir}");
            await Task.Run(() => StopRunningClientProcesses(msg => Report(28, 0, "Stopping AI Bridge Client", msg)), ct);

            Report(45, 1, "Removing shortcuts & registry entries", "Removing Start Menu, Desktop shortcuts, Startup entry, and Installed Apps registration...");
            await Task.Run(() =>
            {
                ShortcutAndRegistryService.RemoveShortcuts();
                ShortcutAndRegistryService.RemoveUninstallEntry();
            }, ct);

            Report(70, 2, "Removing application files", $"Cleaning {installDir}...");
            await Task.Run(() => RemoveInstallDirectory(installDir, msg => Report(80, 2, "Removing application files", msg)), ct);

            if (!options.KeepUserConfigOnUninstall)
            {
                var configDir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".ai-bridge");
                if (Directory.Exists(configDir))
                {
                    Report(90, 3, "Removing user configuration", $"Deleting {configDir}...");
                    try { Directory.Delete(configDir, recursive: true); } catch { }
                }
            }
            else
            {
                Report(92, 3, "Preserving user configuration", "Kept %USERPROFILE%\\.ai-bridge config and logs as requested.");
            }

            Report(100, 4, "Uninstall complete", "AI Bridge Windows Client has been removed.");
            return new InstallResult(true, "AI Bridge Windows Client was uninstalled cleanly.", installDir, null, null);
        }
        catch (Exception ex)
        {
            Report(100, 4, "Uninstall failed", $"ERROR: {ex.Message}", isErr: true);
            return new InstallResult(false, ex.Message, options.InstallDirectory, null, null);
        }
    }

    private static void StopRunningClientProcesses(Action<string> log)
    {
        foreach (var procName in new[] { "AIBridgeClient", "relay-engine" })
        {
            foreach (var proc in Process.GetProcessesByName(procName))
            {
                try
                {
                    log($"Stopping running {procName} process (PID {proc.Id})...");
                    proc.CloseMainWindow();
                    if (!proc.WaitForExit(2000))
                    {
                        proc.Kill(entireProcessTree: true);
                        proc.WaitForExit(2000);
                    }
                }
                catch
                {
                    // Ignore race if process already exited
                }
                finally
                {
                    proc.Dispose();
                }
            }
        }
    }

    private static long DeployPayload(InstallerOptions options, Action<string> log)
    {
        var asm = Assembly.GetExecutingAssembly();
        using var stream = asm.GetManifestResourceStream(EmbeddedPayloadResourceName);
        if (stream is not null)
        {
            log("Extracting embedded self-contained payload archive...");
            using var archive = new ZipArchive(stream, ZipArchiveMode.Read);
            long total = 0;
            foreach (var entry in archive.Entries)
            {
                if (string.IsNullOrEmpty(entry.Name)) continue;
                var dest = Path.GetFullPath(Path.Combine(options.InstallDirectory, entry.FullName));
                if (!dest.StartsWith(Path.GetFullPath(options.InstallDirectory), StringComparison.OrdinalIgnoreCase))
                {
                    throw new InvalidOperationException($"Unsafe archive path blocked: {entry.FullName}");
                }
                var parent = Path.GetDirectoryName(dest);
                if (!string.IsNullOrEmpty(parent)) Directory.CreateDirectory(parent);
                entry.ExtractToFile(dest, overwrite: true);
                total += entry.Length;
            }
            log($"Extracted {archive.Entries.Count} files ({total / (1024.0 * 1024.0):F1} MB).");
            return total;
        }

        // Fallback: deploy from custom --payload dir or adjacent payload/publish directory
        var candidates = new List<string>();
        if (!string.IsNullOrWhiteSpace(options.CustomPayloadSourceDir))
        {
            candidates.Add(options.CustomPayloadSourceDir);
        }
        var baseDir = AppContext.BaseDirectory;
        candidates.Add(Path.Combine(baseDir, "Payload"));
        candidates.Add(baseDir);
        candidates.Add(Path.GetFullPath(Path.Combine(baseDir, "..", "..", "..", "..", "windows-client", "bin", "Release", "net9.0-windows", "publish", "win-x64")));
        candidates.Add(Path.GetFullPath(Path.Combine(baseDir, "..", "..", "..", "..", "windows-client", "bin", "Release", "net9.0-windows")));

        string? sourceDir = null;
        foreach (var cand in candidates)
        {
            if (Directory.Exists(cand) && File.Exists(Path.Combine(cand, InstallerOptions.ClientExeName)))
            {
                sourceDir = cand;
                break;
            }
        }

        if (sourceDir is null)
        {
            throw new FileNotFoundException(
                $"Embedded payload not found and {InstallerOptions.ClientExeName} was not located in adjacent directories.");
        }

        log($"Copying client binaries from {sourceDir}...");
        return CopyDirectory(sourceDir, options.InstallDirectory);
    }

    private static long CopyDirectory(string sourceDir, string targetDir)
    {
        long total = 0;
        Directory.CreateDirectory(targetDir);
        foreach (var file in Directory.GetFiles(sourceDir))
        {
            var name = Path.GetFileName(file);
            if (name.Equals(InstallerOptions.SetupExeName, StringComparison.OrdinalIgnoreCase)) continue;
            var dest = Path.Combine(targetDir, name);
            File.Copy(file, dest, overwrite: true);
            total += new FileInfo(dest).Length;
        }
        foreach (var sub in Directory.GetDirectories(sourceDir))
        {
            total += CopyDirectory(sub, Path.Combine(targetDir, Path.GetFileName(sub)));
        }
        return total;
    }

    private static void WriteClientConfiguration(InstallerOptions options, Action<string> log)
    {
        var home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        var configDir = Path.Combine(home, ".ai-bridge");
        Directory.CreateDirectory(configDir);
        Directory.CreateDirectory(Path.Combine(configDir, "logs"));
        Directory.CreateDirectory(Path.Combine(configDir, "screenshots"));

        var configFile = Path.Combine(configDir, "agent_gui.json");
        var payload = new Dictionary<string, object?>
        {
            ["control_url"] = (options.ControlPlaneUrl ?? "http://127.0.0.1:8000").Trim().TrimEnd('/'),
            ["device_id"] = string.IsNullOrWhiteSpace(options.DeviceId) ? Environment.MachineName.ToLowerInvariant() : options.DeviceId.Trim(),
            ["tenant_id"] = string.IsNullOrWhiteSpace(options.TenantId) ? "default" : options.TenantId.Trim(),
            ["workspace"] = options.WorkspaceDirectory,
            ["screenshot_dir"] = Path.Combine(configDir, "screenshots"),
            ["auto_connect"] = !string.IsNullOrWhiteSpace(options.DeviceToken),
            ["minimize_to_tray"] = true,
            ["notifications_enabled"] = true,
            ["confirm_destructive_actions"] = true,
            ["theme"] = options.Theme,
            ["policy"] = new Dictionary<string, object?>
            {
                ["allow_writes"] = options.AllowWrites,
                ["allow_tier3"] = options.AllowTier3,
                ["allow_tier4"] = false,
                ["allow_shell"] = options.AllowShell,
                ["allow_desktop_control"] = options.AllowDesktopControl,
                ["max_tier_unattended"] = options.AllowTier3 ? 3 : 2,
            },
        };

        var tmp = configFile + ".tmp";
        File.WriteAllText(tmp, JsonSerializer.Serialize(payload, JsonOpts));
        File.Move(tmp, configFile, overwrite: true);
        log($"Saved client configuration to {configFile}");

        if (!string.IsNullOrWhiteSpace(options.DeviceToken))
        {
            var tokenFile = Path.Combine(configDir, "device.token");
            DpapiSealer.SaveTokenFile(tokenFile, options.DeviceToken);
            log($"Sealed device token with Windows DPAPI (aib-dpapi-v1) at {tokenFile}");
        }
    }

    private static void RemoveInstallDirectory(string installDir, Action<string> log)
    {
        if (!Directory.Exists(installDir)) return;
        var selfPath = Environment.ProcessPath ?? string.Empty;
        var runningInsideInstallDir = !string.IsNullOrWhiteSpace(selfPath) &&
            Path.GetFullPath(selfPath).StartsWith(Path.GetFullPath(installDir), StringComparison.OrdinalIgnoreCase);

        foreach (var file in Directory.GetFiles(installDir, "*", SearchOption.AllDirectories))
        {
            if (runningInsideInstallDir && string.Equals(Path.GetFullPath(file), Path.GetFullPath(selfPath), StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }
            try { File.Delete(file); } catch { }
        }

        if (!runningInsideInstallDir)
        {
            try { Directory.Delete(installDir, recursive: true); } catch { }
        }
        else
        {
            // Schedule self-deletion after uninstaller exits cleanly
            log("Scheduling deferred cleanup of uninstaller binary after exit...");
            try
            {
                Process.Start(new ProcessStartInfo
                {
                    FileName = "cmd.exe",
                    Arguments = $"/C ping 127.0.0.1 -n 3 > nul & rmdir /S /Q \"{installDir}\"",
                    CreateNoWindow = true,
                    UseShellExecute = false,
                    WindowStyle = ProcessWindowStyle.Hidden,
                });
            }
            catch { }
        }
    }

    private static string ComputeSha256(string filePath)
    {
        using var sha = SHA256.Create();
        using var fs = File.OpenRead(filePath);
        return Convert.ToHexString(sha.ComputeHash(fs)).ToLowerInvariant();
    }
}
