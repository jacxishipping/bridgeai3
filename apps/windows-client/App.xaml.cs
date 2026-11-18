using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Threading;
using System.Windows;

namespace AIBridgeClient;

public partial class App : System.Windows.Application
{
    private const string MutexName = @"Local\AIBridgeWindowsClient";
    private Mutex? _instanceMutex;
    private MainWindow? _mainWindow;

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr FindWindow(string? lpClassName, string lpWindowName);

    [DllImport("user32.dll")]
    private static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr hWnd);

    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);

        _instanceMutex = new Mutex(true, MutexName, out bool createdNew);
        if (!createdNew)
        {
            if (TryFocusExistingCSharpWindow())
            {
                Shutdown(0);
                return;
            }

            // Mutex is likely held by the legacy Python gui_app.py process
            var answer = System.Windows.MessageBox.Show(
                "Another AI Bridge client (such as the legacy Python GUI) is currently running.\n\nStop the other client and launch the native C# client?",
                "AI Bridge Windows Client",
                MessageBoxButton.YesNo,
                MessageBoxImage.Question);

            if (answer == MessageBoxResult.Yes && TryStopLegacyPythonClient())
            {
                _instanceMutex.Dispose();
                Thread.Sleep(400);
                _instanceMutex = new Mutex(true, MutexName, out createdNew);
            }

            if (!createdNew)
            {
                System.Windows.MessageBox.Show(
                    "AI Bridge Windows Client is still running in the system tray.",
                    "AI Bridge Windows Client",
                    MessageBoxButton.OK,
                    MessageBoxImage.Information);
                Shutdown(0);
                return;
            }
        }

        DispatcherUnhandledException += (_, args) =>
        {
            LogCrash(args.Exception);
            System.Windows.MessageBox.Show(
                $"Unexpected error: {args.Exception.Message}",
                "AI Bridge Error",
                MessageBoxButton.OK,
                MessageBoxImage.Error);
            args.Handled = true;
        };

        int initialTab = 0;
        bool explicitTab = false;
        foreach (var arg in e.Args)
        {
            if (arg.StartsWith("--tab=", StringComparison.OrdinalIgnoreCase) &&
                int.TryParse(arg[6..], out var parsedTab))
            {
                initialTab = parsedTab;
                explicitTab = true;
            }
        }

        _mainWindow = new MainWindow(initialTab, suppressAutoSignIn: explicitTab);
        _mainWindow.Show();
    }

    private static bool TryFocusExistingCSharpWindow()
    {
        var currentPid = Environment.ProcessId;
        var existing = Process.GetProcessesByName("AIBridgeClient")
            .FirstOrDefault(p => p.Id != currentPid);
        if (existing is null)
        {
            return false;
        }

        var hwnd = existing.MainWindowHandle;
        if (hwnd == IntPtr.Zero)
        {
            hwnd = FindWindow(null, "AI Bridge Windows Client (Native C#)");
        }
        if (hwnd != IntPtr.Zero)
        {
            ShowWindow(hwnd, 9); // SW_RESTORE
            SetForegroundWindow(hwnd);
        }
        return true;
    }

    private static bool TryStopLegacyPythonClient()
    {
        bool stoppedAny = false;
        foreach (var proc in Process.GetProcessesByName("pythonw").Concat(Process.GetProcessesByName("python")))
        {
            try
            {
                proc.Kill(entireProcessTree: true);
                proc.WaitForExit(2000);
                stoppedAny = true;
            }
            catch
            {
                // Ignore processes we cannot stop
            }
        }
        return stoppedAny;
    }

    protected override void OnExit(ExitEventArgs e)
    {
        try
        {
            _instanceMutex?.ReleaseMutex();
        }
        catch
        {
            // Ignore if not owned
        }
        _instanceMutex?.Dispose();
        base.OnExit(e);
    }

    private static void LogCrash(Exception ex)
    {
        try
        {
            var logDir = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                ".ai-bridge",
                "logs");
            Directory.CreateDirectory(logDir);
            var logPath = Path.Combine(logDir, "csharp-client.log");
            File.AppendAllText(
                logPath,
                $"[{DateTimeOffset.Now:O}] UNHANDLED: {ex}{Environment.NewLine}");
        }
        catch
        {
            // Best-effort crash log
        }
    }
}
