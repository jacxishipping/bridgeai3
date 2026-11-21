using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Windows.Threading;
using AIBridgeClient.Views;

namespace AIBridgeClient.Services;

public enum TakeoverState
{
    /// <summary>No agent is driving the desktop; the user has normal input.</summary>
    Idle,

    /// <summary>The agent is driving the desktop. Physical user input is blocked and the glow border is shown.</summary>
    AgentControlling,

    /// <summary>The user pressed "Take Over". Agent desktop input is refused until control is returned.</summary>
    UserControl,
}

/// <summary>
/// Agent-takeover mode: while the agent injects mouse/keyboard input, the user's physical input is blocked
/// (low-level hooks swallow every non-injected event) and a glowing border overlay is displayed.
/// The user regains control only by clicking "Take Over" (header button / floating pill) or pressing Ctrl+Alt+T.
/// Agent input is always injected (SendInput) so it passes through the hooks untouched.
/// </summary>
public static class AgentTakeoverService
{
    private const int WH_KEYBOARD_LL = 13;
    private const int WH_MOUSE_LL = 14;
    private const int WM_MOUSEMOVE = 0x0200;
    private const uint LLMHF_INJECTED = 0x01;
    private const uint LLMHF_LOWER_IL_INJECTED = 0x02;
    private const uint LLKHF_INJECTED = 0x10;
    private const uint LLKHF_LOWER_IL_INJECTED = 0x02;
    private const int WM_KEYDOWN = 0x0100;
    private const int WM_SYSKEYDOWN = 0x0104;
    private const int VK_CONTROL = 0x11;
    private const int VK_MENU = 0x12;
    private const int VK_T = 0x54;

    /// <summary>Agent is considered finished when no interactive tool ran for this long.</summary>
    private const long IdleTimeoutMs = 15_000;

    private delegate IntPtr HookProc(int nCode, IntPtr wParam, IntPtr lParam);

    [StructLayout(LayoutKind.Sequential)]
    private struct POINT
    {
        public int X;
        public int Y;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct MSLLHOOKSTRUCT
    {
        public POINT Pt;
        public uint MouseData;
        public uint Flags;
        public uint Time;
        public UIntPtr ExtraInfo;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct KBDLLHOOKSTRUCT
    {
        public uint VkCode;
        public uint ScanCode;
        public uint Flags;
        public uint Time;
        public UIntPtr ExtraInfo;
    }

    [DllImport("user32.dll", SetLastError = true)]
    private static extern IntPtr SetWindowsHookEx(int idHook, HookProc lpfn, IntPtr hMod, uint dwThreadId);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool UnhookWindowsHookEx(IntPtr hhk);

    [DllImport("user32.dll")]
    private static extern IntPtr CallNextHookEx(IntPtr hhk, int nCode, IntPtr wParam, IntPtr lParam);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr GetModuleHandle(string? lpModuleName);

    [DllImport("user32.dll")]
    private static extern short GetAsyncKeyState(int vKey);

    private static readonly object Sync = new();
    private static readonly HookProc MouseProcDelegate = MouseHookProc;
    private static readonly HookProc KeyboardProcDelegate = KeyboardHookProc;

    private static IntPtr _mouseHook = IntPtr.Zero;
    private static IntPtr _keyboardHook = IntPtr.Zero;
    private static DispatcherTimer? _idleTimer;
    private static long _lastActivityTick;
    private static volatile TakeoverState _state = TakeoverState.Idle;
    private static AgentGlowOverlayWindow? _overlay;
    private static AgentTakeoverPillWindow? _pill;

    public static TakeoverState State => _state;

    public static bool IsUserInControl => _state == TakeoverState.UserControl;

    /// <summary>Master switch (Settings → Security). When false the glow/blocking never activates.</summary>
    public static bool Enabled { get; set; } = true;

    /// <summary>Raised on the UI thread whenever the takeover state changes.</summary>
    public static event Action<TakeoverState>? StateChanged;

    /// <summary>
    /// Supplies screen-pixel rectangles where physical clicks stay allowed while the agent is in control
    /// (e.g. the header "Take Over" button).
    /// </summary>
    public static Func<IEnumerable<System.Windows.Rect>>? ExtraAllowedRegions { get; set; }

    /// <summary>Called by NativeToolExecutor whenever the agent runs an interactive desktop tool.</summary>
    public static void NotifyAgentActivity()
    {
        if (!Enabled) return;
        var dispatcher = System.Windows.Application.Current?.Dispatcher;
        if (dispatcher is null) return;
        _lastActivityTick = Environment.TickCount64;
        if (_state != TakeoverState.Idle) return;
        dispatcher.BeginInvoke(() =>
        {
            if (_state == TakeoverState.Idle)
            {
                SetState(TakeoverState.AgentControlling);
            }
        });
    }

    /// <summary>User clicked "Take Over": stop blocking input and refuse agent desktop input.</summary>
    public static void TakeOver() => RunOnUi(() =>
    {
        if (_state == TakeoverState.AgentControlling)
        {
            CppNativeBridge.ReleaseAllKeys();
            SetState(TakeoverState.UserControl);
        }
    });

    /// <summary>Manually engage Agent Screen Takeover (e.g. from UI preview/test or stream start).</summary>
    public static void StartAgentTakeover() => RunOnUi(() =>
    {
        if (!Enabled) return;
        _lastActivityTick = Environment.TickCount64;
        SetState(TakeoverState.AgentControlling);
    });

    /// <summary>Updates the floating HUD pill text with the currently executing agent action.</summary>
    public static void UpdateActionLabel(string actionText) => RunOnUi(() =>
    {
        if (_state == TakeoverState.AgentControlling)
        {
            _lastActivityTick = Environment.TickCount64;
            _pill?.UpdateActionText(actionText);
        }
    });

    /// <summary>User clicked "Return Control to Agent".</summary>
    public static void ReturnControlToAgent() => RunOnUi(() =>
    {
        if (_state == TakeoverState.UserControl)
        {
            SetState(TakeoverState.Idle);
        }
    });

    public static void Shutdown() => RunOnUi(() =>
    {
        UninstallHooks();
        _idleTimer?.Stop();
        _overlay?.Close();
        _pill?.Close();
        _overlay = null;
        _pill = null;
        _state = TakeoverState.Idle;
    });

    private static void RunOnUi(Action action)
    {
        var dispatcher = System.Windows.Application.Current?.Dispatcher;
        if (dispatcher is null) return;
        if (dispatcher.CheckAccess()) action();
        else dispatcher.BeginInvoke(action);
    }

    private static void SetState(TakeoverState next)
    {
        if (_state == next) return;
        _state = next;

        if (next == TakeoverState.AgentControlling)
        {
            _lastActivityTick = Environment.TickCount64;
            InstallHooks();
            EnsureIdleTimer();
            _idleTimer!.Start();
            _overlay ??= new AgentGlowOverlayWindow();
            _overlay.ShowGlow();
            _pill ??= new AgentTakeoverPillWindow();
            _pill.ShowFor(next);
        }
        else
        {
            UninstallHooks();
            _idleTimer?.Stop();
            _overlay?.HideGlow();
            if (next == TakeoverState.UserControl)
            {
                _pill ??= new AgentTakeoverPillWindow();
                _pill.ShowFor(next);
            }
            else
            {
                _pill?.HidePill();
            }
        }

        StateChanged?.Invoke(next);
    }

    private static void EnsureIdleTimer()
    {
        if (_idleTimer is not null) return;
        _idleTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(500) };
        _idleTimer.Tick += (_, _) =>
        {
            if (_state == TakeoverState.AgentControlling &&
                Environment.TickCount64 - _lastActivityTick > IdleTimeoutMs)
            {
                SetState(TakeoverState.Idle);
            }
        };
    }

    private static void InstallHooks()
    {
        lock (Sync)
        {
            var module = GetModuleHandle(null);
            if (_mouseHook == IntPtr.Zero)
            {
                _mouseHook = SetWindowsHookEx(WH_MOUSE_LL, MouseProcDelegate, module, 0);
            }
            if (_keyboardHook == IntPtr.Zero)
            {
                _keyboardHook = SetWindowsHookEx(WH_KEYBOARD_LL, KeyboardProcDelegate, module, 0);
            }
        }
    }

    private static void UninstallHooks()
    {
        lock (Sync)
        {
            if (_mouseHook != IntPtr.Zero)
            {
                UnhookWindowsHookEx(_mouseHook);
                _mouseHook = IntPtr.Zero;
            }
            if (_keyboardHook != IntPtr.Zero)
            {
                UnhookWindowsHookEx(_keyboardHook);
                _keyboardHook = IntPtr.Zero;
            }
        }
    }

    private static bool InAllowedRegion(int x, int y)
    {
        try
        {
            var pt = new System.Windows.Point(x, y);
            if (_pill is not null && _pill.GetScreenPixelBounds() is { } pillRect && pillRect.Contains(pt))
            {
                return true;
            }
            var extra = ExtraAllowedRegions?.Invoke();
            if (extra is not null)
            {
                foreach (var r in extra)
                {
                    if (r.Contains(pt)) return true;
                }
            }
        }
        catch
        {
            // Never let hook callbacks throw.
        }
        return false;
    }

    private static IntPtr MouseHookProc(int nCode, IntPtr wParam, IntPtr lParam)
    {
        if (nCode >= 0 && _state == TakeoverState.AgentControlling)
        {
            var info = Marshal.PtrToStructure<MSLLHOOKSTRUCT>(lParam);
            bool injected = (info.Flags & (LLMHF_INJECTED | LLMHF_LOWER_IL_INJECTED)) != 0;
            int msg = wParam.ToInt32();
            // Pointer movement stays allowed so the user can always reach the Take Over button;
            // every physical click / wheel event outside that button is swallowed.
            if (!injected && msg != WM_MOUSEMOVE && !InAllowedRegion(info.Pt.X, info.Pt.Y))
            {
                return (IntPtr)1;
            }
        }
        return CallNextHookEx(IntPtr.Zero, nCode, wParam, lParam);
    }

    private static IntPtr KeyboardHookProc(int nCode, IntPtr wParam, IntPtr lParam)
    {
        if (nCode >= 0 && _state == TakeoverState.AgentControlling)
        {
            var info = Marshal.PtrToStructure<KBDLLHOOKSTRUCT>(lParam);
            bool injected = (info.Flags & (LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED)) != 0;
            if (!injected)
            {
                int msg = wParam.ToInt32();
                bool ctrl = (GetAsyncKeyState(VK_CONTROL) & 0x8000) != 0;
                bool alt = (GetAsyncKeyState(VK_MENU) & 0x8000) != 0;
                // Safety hotkey: Ctrl+Alt+T always lets the user take over.
                if ((msg == WM_KEYDOWN || msg == WM_SYSKEYDOWN) && info.VkCode == VK_T && ctrl && alt)
                {
                    TakeOver();
                }
                return (IntPtr)1;
            }
        }
        return CallNextHookEx(IntPtr.Zero, nCode, wParam, lParam);
    }
}
