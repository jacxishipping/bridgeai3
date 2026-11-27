using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Threading;

namespace AIBridgeClient.Services;

/// <summary>
/// Native Win32 SendInput controller for mouse and keyboard automation.
/// Only invoked when AgentPolicyConfig.AllowDesktopControl is enabled.
/// </summary>
public static class DesktopInputService
{
    private const uint InputMouse = 0;
    private const uint InputKeyboard = 1;

    private const uint MouseEventMove = 0x0001;
    private const uint MouseEventLeftDown = 0x0002;
    private const uint MouseEventLeftUp = 0x0004;
    private const uint MouseEventRightDown = 0x0008;
    private const uint MouseEventRightUp = 0x0010;
    private const uint MouseEventMiddleDown = 0x0020;
    private const uint MouseEventMiddleUp = 0x0040;
    private const uint MouseEventWheel = 0x0800;
    private const uint MouseEventHWheel = 0x1000;
    private const uint MouseEventVirtualDesk = 0x4000;
    private const uint MouseEventAbsolute = 0x8000;

    private const uint KeyEventKeyUp = 0x0002;
    private const uint KeyEventUnicode = 0x0004;

    [StructLayout(LayoutKind.Sequential)]
    private struct Input
    {
        public uint Type;
        public InputUnion U;
    }

    [StructLayout(LayoutKind.Explicit)]
    private struct InputUnion
    {
        [FieldOffset(0)] public MouseInput Mi;
        [FieldOffset(0)] public KeyboardInput Ki;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct MouseInput
    {
        public int Dx;
        public int Dy;
        public uint MouseData;
        public uint DwFlags;
        public uint Time;
        public IntPtr DwExtraInfo;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct KeyboardInput
    {
        public ushort WVk;
        public ushort WScan;
        public uint DwFlags;
        public uint Time;
        public IntPtr DwExtraInfo;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct PointStruct
    {
        public int X;
        public int Y;
    }

    [DllImport("user32.dll", SetLastError = true)]
    private static extern uint SendInput(uint nInputs, Input[] pInputs, int cbSize);

    [DllImport("user32.dll")]
    private static extern int GetSystemMetrics(int nIndex);

    [DllImport("user32.dll")]
    private static extern bool GetCursorPos(out PointStruct lpPoint);

    public static bool HumanLikeInputEnabled { get; set; } = true;
    public static double SpeedMultiplier { get; set; } = 1.0;

    public static void SetSpeedProfile(string? profile)
    {
        SpeedMultiplier = (profile ?? "natural").Trim().ToLowerInvariant() switch
        {
            "fast" => 0.55,
            "careful" => 1.45,
            _ => 1.0,
        };
    }

    /// <summary>
    /// Smooths a 4+ point freehand polyline using centripetal Catmull-Rom spline subdivision.
    /// </summary>
    public static List<(int X, int Y)> SmoothStrokeCatmullRom(IReadOnlyList<(int X, int Y)> pts, int subdivisions = 4)
    {
        if (pts.Count < 4) return new List<(int X, int Y)>(pts);
        int sub = Math.Clamp(subdivisions, 2, 12);
        var result = new List<(int X, int Y)>(pts.Count * sub);
        for (int i = 0; i < pts.Count - 1; i++)
        {
            var p0 = pts[Math.Max(0, i - 1)];
            var p1 = pts[i];
            var p2 = pts[i + 1];
            var p3 = pts[Math.Min(pts.Count - 1, i + 2)];
            for (int s = 0; s < sub; s++)
            {
                double t = (double)s / sub;
                double t2 = t * t;
                double t3 = t2 * t;
                double x = 0.5 * ((2 * p1.X)
                    + (-p0.X + p2.X) * t
                    + (2 * p0.X - 5 * p1.X + 4 * p2.X - p3.X) * t2
                    + (-p0.X + 3 * p1.X - 3 * p2.X + p3.X) * t3);
                double y = 0.5 * ((2 * p1.Y)
                    + (-p0.Y + p2.Y) * t
                    + (2 * p0.Y - 5 * p1.Y + 4 * p2.Y - p3.Y) * t2
                    + (-p0.Y + 3 * p1.Y - 3 * p2.Y + p3.Y) * t3);
                result.Add(((int)Math.Round(x), (int)Math.Round(y)));
            }
        }
        result.Add(pts[^1]);
        return result;
    }

    public static (int X, int Y) GetCurrentCursorPosition()
    {
        return GetCursorPos(out var pt) ? (pt.X, pt.Y) : (0, 0);
    }

    public static void MoveMouseInstant(int x, int y)
    {
        if (CppNativeBridge.TryMoveMouse(x, y))
        {
            return;
        }
        var (nx, ny) = NormalizeCoordinates(x, y);
        var inputs = new[]
        {
            new Input
            {
                Type = InputMouse,
                U = new InputUnion
                {
                    Mi = new MouseInput
                    {
                        Dx = nx,
                        Dy = ny,
                        DwFlags = MouseEventMove | MouseEventAbsolute | MouseEventVirtualDesk,
                    },
                },
            },
        };
        SendInput((uint)inputs.Length, inputs, Marshal.SizeOf<Input>());
    }

    /// <summary>
    /// Moves the mouse cursor like a human along a subtle cubic Bezier arc with
    /// Fitts's-Law duration and smooth ease-in / ease-out velocity.
    /// </summary>
    public static void MoveMouse(int x, int y)
    {
        if (!HumanLikeInputEnabled)
        {
            MoveMouseInstant(x, y);
            return;
        }

        var (startX, startY) = GetCurrentCursorPosition();
        double dx = x - startX;
        double dy = y - startY;
        double dist = Math.Sqrt(dx * dx + dy * dy);
        if (dist < 4.0)
        {
            MoveMouseInstant(x, y);
            return;
        }

        // Natural Fitts's-Law duration scaled by SpeedMultiplier (fast / natural / careful)
        int durationMs = Math.Clamp((int)((75 + Math.Sqrt(dist) * 7.5) * SpeedMultiplier), 55, 480);
        int steps = Math.Clamp(durationMs / 8, 10, 48);

        // Gentle perpendicular hand arc (2%-6% of distance)
        var rng = Random.Shared;
        double arcSign = rng.Next(2) == 0 ? 1.0 : -1.0;
        double arcMag = Math.Min(36.0, dist * (0.025 + rng.NextDouble() * 0.035)) * arcSign;
        double perpX = -dy / dist;
        double perpY = dx / dist;

        double cp1x = startX + dx * 0.30 + perpX * arcMag;
        double cp1y = startY + dy * 0.30 + perpY * arcMag;
        double cp2x = startX + dx * 0.72 + perpX * (arcMag * 0.45);
        double cp2y = startY + dy * 0.72 + perpY * (arcMag * 0.45);

        int sleepPerStep = Math.Max(4, durationMs / steps);
        for (int i = 1; i <= steps; i++)
        {
            double rawT = (double)i / steps;
            // Quintic smoothstep: gentle acceleration and natural deceleration onto target
            double t = rawT * rawT * rawT * (rawT * (rawT * 6.0 - 15.0) + 10.0);
            double omt = 1.0 - t;
            double curX = omt * omt * omt * startX
                        + 3.0 * omt * omt * t * cp1x
                        + 3.0 * omt * t * t * cp2x
                        + t * t * t * x;
            double curY = omt * omt * omt * startY
                        + 3.0 * omt * omt * t * cp1y
                        + 3.0 * omt * t * t * cp2y
                        + t * t * t * y;
            MoveMouseInstant((int)Math.Round(curX), (int)Math.Round(curY));
            if (i < steps) Thread.Sleep(sleepPerStep);
        }
        MoveMouseInstant(x, y);
    }

    private static (uint Down, uint Up) ResolveMouseButtons(string button) =>
        button.Trim().ToLowerInvariant() switch
        {
            "right" or "r" => (MouseEventRightDown, MouseEventRightUp),
            "middle" or "m" => (MouseEventMiddleDown, MouseEventMiddleUp),
            _ => (MouseEventLeftDown, MouseEventLeftUp),
        };

    public static void DragMouse(int startX, int startY, int endX, int endY, string button = "left", int steps = 0)
    {
        MoveMouse(startX, startY);
        Thread.Sleep(24);
        var (down, up) = ResolveMouseButtons(button);

        SendInput(1, new[] { new Input { Type = InputMouse, U = new InputUnion { Mi = new MouseInput { DwFlags = down } } } }, Marshal.SizeOf<Input>());
        Thread.Sleep(24);

        double dx = endX - startX;
        double dy = endY - startY;
        double dist = Math.Sqrt(dx * dx + dy * dy);
        int actualSteps = steps > 0 ? Math.Clamp(steps, 2, 120) : Math.Clamp((int)Math.Ceiling(dist / 4.0), 8, 90);

        for (int i = 1; i <= actualSteps; i++)
        {
            double rawT = (double)i / actualSteps;
            double t = rawT * rawT * (3.0 - 2.0 * rawT);
            int curX = startX + (int)Math.Round(dx * t);
            int curY = startY + (int)Math.Round(dy * t);
            MoveMouseInstant(curX, curY);
            Thread.Sleep(5);
        }
        MoveMouseInstant(endX, endY);
        Thread.Sleep(22);
        SendInput(1, new[] { new Input { Type = InputMouse, U = new InputUnion { Mi = new MouseInput { DwFlags = up } } } }, Marshal.SizeOf<Input>());
        Thread.Sleep(18);
    }

    public static int DrawPath(IReadOnlyList<IReadOnlyList<(int X, int Y)>> strokes, string button = "left", int stepDelayMs = 4)
    {
        var (down, up) = ResolveMouseButtons(button);
        int delay = Math.Clamp(stepDelayMs, 2, 25);
        int totalPoints = 0;

        foreach (var rawStroke in strokes)
        {
            if (rawStroke is null || rawStroke.Count < 2) continue;
            var stroke = rawStroke.Count >= 4 ? SmoothStrokeCatmullRom(rawStroke, 4) : rawStroke;

            // Glide hand smoothly to the start of the stroke before putting the pen down
            MoveMouse(stroke[0].X, stroke[0].Y);
            Thread.Sleep(26);
            SendInput(1, new[] { new Input { Type = InputMouse, U = new InputUnion { Mi = new MouseInput { DwFlags = down } } } }, Marshal.SizeOf<Input>());
            Thread.Sleep(24);

            for (int seg = 0; seg < stroke.Count - 1; seg++)
            {
                var (x1, y1) = stroke[seg];
                var (x2, y2) = stroke[seg + 1];
                double dx = x2 - x1;
                double dy = y2 - y1;
                double dist = Math.Sqrt(dx * dx + dy * dy);
                // Fine 3px interpolation keeps curves and diagonals razor-smooth in Paint
                int steps = Math.Clamp((int)Math.Ceiling(dist / 3.0), 2, 80);
                for (int i = 1; i <= steps; i++)
                {
                    double t = (double)i / steps;
                    int curX = x1 + (int)Math.Round(dx * t);
                    int curY = y1 + (int)Math.Round(dy * t);
                    MoveMouseInstant(curX, curY);
                    Thread.Sleep(delay);
                }
                totalPoints++;
            }

            // Hold briefly at stroke end so the brush registers the final pixel cleanly
            Thread.Sleep(22);
            SendInput(1, new[] { new Input { Type = InputMouse, U = new InputUnion { Mi = new MouseInput { DwFlags = up } } } }, Marshal.SizeOf<Input>());
            Thread.Sleep(28);
        }

        return totalPoints;
    }

    public static void MouseButton(int x, int y, string button = "left", bool down = true)
    {
        MoveMouseInstant(x, y);
        var (flagDown, flagUp) = ResolveMouseButtons(button);
        uint flag = down ? flagDown : flagUp;
        SendInput(1, new[]
        {
            new Input { Type = InputMouse, U = new InputUnion { Mi = new MouseInput { DwFlags = flag } } },
        }, Marshal.SizeOf<Input>());
    }

    public static void KeyTransition(ushort virtualKey, bool down)
    {
        SendInput(1, new[]
        {
            new Input
            {
                Type = InputKeyboard,
                U = new InputUnion
                {
                    Ki = new KeyboardInput
                    {
                        WVk = virtualKey,
                        DwFlags = down ? 0u : KeyEventKeyUp,
                    },
                },
            },
        }, Marshal.SizeOf<Input>());
    }

    public static void ClickMouse(int x, int y, string button = "left", bool doubleClick = false)
    {
        MoveMouse(x, y);
        AgentTakeoverService.TriggerClickRipple(x, y);
        if (HumanLikeInputEnabled) Thread.Sleep(Random.Shared.Next(16, 32));

        var (down, up) = ResolveMouseButtons(button);
        int count = doubleClick ? 2 : 1;
        for (int i = 0; i < count; i++)
        {
            SendInput(1, new[] { new Input { Type = InputMouse, U = new InputUnion { Mi = new MouseInput { DwFlags = down } } } }, Marshal.SizeOf<Input>());
            if (HumanLikeInputEnabled) Thread.Sleep(Random.Shared.Next(42, 72));
            SendInput(1, new[] { new Input { Type = InputMouse, U = new InputUnion { Mi = new MouseInput { DwFlags = up } } } }, Marshal.SizeOf<Input>());
            if (doubleClick && i == 0) Thread.Sleep(Random.Shared.Next(65, 95));
        }
    }

    public static void ScrollMouse(int delta, int? x = null, int? y = null, bool horizontal = false)
    {
        if (x.HasValue && y.HasValue)
        {
            MoveMouse(x.Value, y.Value);
            if (HumanLikeInputEnabled) Thread.Sleep(20);
        }

        int remaining = delta;
        int notch = delta >= 0 ? 120 : -120;
        while (remaining != 0)
        {
            int step = Math.Abs(remaining) >= 120 ? notch : remaining;
            remaining -= step;
            var inputs = new[]
            {
                new Input
                {
                    Type = InputMouse,
                    U = new InputUnion
                    {
                        Mi = new MouseInput
                        {
                            MouseData = unchecked((uint)step),
                            DwFlags = horizontal ? MouseEventHWheel : MouseEventWheel,
                        },
                    },
                },
            };
            SendInput((uint)inputs.Length, inputs, Marshal.SizeOf<Input>());
            if (remaining != 0 && HumanLikeInputEnabled) Thread.Sleep(Random.Shared.Next(20, 36));
        }
    }

    public static void TypeText(string text)
    {
        if (string.IsNullOrEmpty(text)) return;

        var rng = Random.Shared;
        for (int i = 0; i < text.Length; i++)
        {
            char ch = text[i];
            if (ch == '\r')
            {
                if (i + 1 < text.Length && text[i + 1] == '\n') i++;
                PressKey(0x0D);
                if (HumanLikeInputEnabled) Thread.Sleep(rng.Next(70, 120));
                continue;
            }
            if (ch == '\n')
            {
                PressKey(0x0D);
                if (HumanLikeInputEnabled) Thread.Sleep(rng.Next(70, 120));
                continue;
            }
            if (ch == '\t')
            {
                PressKey(0x09);
                if (HumanLikeInputEnabled) Thread.Sleep(rng.Next(45, 80));
                continue;
            }

            SendInput(1, new[]
            {
                new Input { Type = InputKeyboard, U = new InputUnion { Ki = new KeyboardInput { WScan = ch, DwFlags = KeyEventUnicode } } },
            }, Marshal.SizeOf<Input>());
            if (HumanLikeInputEnabled) Thread.Sleep(rng.Next(16, 30));
            SendInput(1, new[]
            {
                new Input { Type = InputKeyboard, U = new InputUnion { Ki = new KeyboardInput { WScan = ch, DwFlags = KeyEventUnicode | KeyEventKeyUp } } },
            }, Marshal.SizeOf<Input>());

            if (HumanLikeInputEnabled && i + 1 < text.Length)
            {
                int pause = ch is ' ' or '.' or ',' or '!' or '?'
                    ? rng.Next(65, 115)
                    : rng.Next(28, 62);
                Thread.Sleep(pause);
            }
        }
    }

    public static void PressKey(ushort virtualKey)
    {
        SendInput(1, new[]
        {
            new Input { Type = InputKeyboard, U = new InputUnion { Ki = new KeyboardInput { WVk = virtualKey } } },
        }, Marshal.SizeOf<Input>());
        if (HumanLikeInputEnabled) Thread.Sleep(Random.Shared.Next(32, 56));
        SendInput(1, new[]
        {
            new Input { Type = InputKeyboard, U = new InputUnion { Ki = new KeyboardInput { WVk = virtualKey, DwFlags = KeyEventKeyUp } } },
        }, Marshal.SizeOf<Input>());
    }

    public static void PressHotkey(string combo)
    {
        if (string.IsNullOrWhiteSpace(combo))
        {
            throw new ArgumentException("Hotkey combo string is required (e.g. 'ctrl+c').");
        }

        var parts = combo.Split(new[] { '+', '-', ' ' }, StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
        var keys = new List<ushort>(parts.Length);
        foreach (var part in parts)
        {
            keys.Add(ResolveVirtualKey(part));
        }

        for (int i = 0; i < keys.Count; i++)
        {
            SendInput(1, new[]
            {
                new Input { Type = InputKeyboard, U = new InputUnion { Ki = new KeyboardInput { WVk = keys[i] } } },
            }, Marshal.SizeOf<Input>());
            if (HumanLikeInputEnabled) Thread.Sleep(Random.Shared.Next(20, 36));
        }
        if (HumanLikeInputEnabled) Thread.Sleep(Random.Shared.Next(35, 55));
        for (int i = keys.Count - 1; i >= 0; i--)
        {
            SendInput(1, new[]
            {
                new Input { Type = InputKeyboard, U = new InputUnion { Ki = new KeyboardInput { WVk = keys[i], DwFlags = KeyEventKeyUp } } },
            }, Marshal.SizeOf<Input>());
            if (HumanLikeInputEnabled && i > 0) Thread.Sleep(Random.Shared.Next(16, 30));
        }
    }

    public static ushort ResolveVirtualKey(string token)
    {
        var k = token.Trim().ToLowerInvariant();
        if (k.Length == 1)
        {
            char ch = k[0];
            if (ch is >= 'a' and <= 'z') return (ushort)('A' + (ch - 'a'));
            if (ch is >= '0' and <= '9') return (ushort)ch;
        }

        if (k.StartsWith('f') && int.TryParse(k[1..], out int fn) && fn is >= 1 and <= 24)
        {
            return (ushort)(0x70 + fn - 1);
        }

        return k switch
        {
            "ctrl" or "control" or "lctrl" => 0x11,
            "alt" or "menu" or "lalt" => 0x12,
            "shift" or "lshift" => 0x10,
            "win" or "windows" or "lwin" or "meta" or "super" => 0x5B,
            "enter" or "return" => 0x0D,
            "tab" => 0x09,
            "esc" or "escape" => 0x1B,
            "space" => 0x20,
            "backspace" or "back" => 0x08,
            "del" or "delete" => 0x2E,
            "ins" or "insert" => 0x2D,
            "home" => 0x24,
            "end" => 0x23,
            "pgup" or "pageup" => 0x21,
            "pgdn" or "pagedown" => 0x22,
            "left" => 0x25,
            "up" => 0x26,
            "right" => 0x27,
            "down" => 0x28,
            "prtsc" or "printscreen" or "snapshot" => 0x2C,
            "capslock" => 0x14,
            "numlock" => 0x90,
            "scrolllock" => 0x91,
            "pause" or "break" => 0x13,
            "apps" or "contextmenu" => 0x5D,
            "rctrl" => 0xA3,
            "rshift" => 0xA1,
            "ralt" => 0xA5,
            "rwin" => 0x5C,
            "plus" or "equal" => 0xBB,
            "minus" or "dash" => 0xBD,
            "volumeup" or "vol_up" => 0xAF,
            "volumedown" or "vol_down" => 0xAE,
            "mute" or "volumemute" or "vol_mute" => 0xAD,
            "medianext" or "media_next" => 0xB0,
            "mediaprev" or "media_prev" => 0xB1,
            "mediaplay" or "media_play" => 0xB3,
            _ => throw new ArgumentException($"Unsupported key token '{token}'."),
        };
    }

    public static (int Left, int Top, int Width, int Height, int PrimaryWidth, int PrimaryHeight) GetVirtualScreenSize()
    {
        int vx = GetSystemMetrics(76); // SM_XVIRTUALSCREEN
        int vy = GetSystemMetrics(77); // SM_YVIRTUALSCREEN
        int vw = Math.Max(1, GetSystemMetrics(78)); // SM_CXVIRTUALSCREEN
        int vh = Math.Max(1, GetSystemMetrics(79)); // SM_CYVIRTUALSCREEN
        int pw = Math.Max(1, GetSystemMetrics(0));  // SM_CXSCREEN
        int ph = Math.Max(1, GetSystemMetrics(1));  // SM_CYSCREEN
        return (vx, vy, vw, vh, pw, ph);
    }

    private static (int Nx, int Ny) NormalizeCoordinates(int x, int y)
    {
        int vx = GetSystemMetrics(76);
        int vy = GetSystemMetrics(77);
        int vw = Math.Max(1, GetSystemMetrics(78));
        int vh = Math.Max(1, GetSystemMetrics(79));
        int nx = (int)Math.Round((x - vx) * 65535.0 / Math.Max(1, vw - 1));
        int ny = (int)Math.Round((y - vy) * 65535.0 / Math.Max(1, vh - 1));
        return (Math.Clamp(nx, 0, 65535), Math.Clamp(ny, 0, 65535));
    }
}
