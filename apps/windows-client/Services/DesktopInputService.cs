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
    private const uint MouseEventWheel = 0x0800;
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

    [DllImport("user32.dll", SetLastError = true)]
    private static extern uint SendInput(uint nInputs, Input[] pInputs, int cbSize);

    [DllImport("user32.dll")]
    private static extern int GetSystemMetrics(int nIndex);

    public static void MoveMouse(int x, int y)
    {
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

    public static void DragMouse(int startX, int startY, int endX, int endY, string button = "left", int steps = 12)
    {
        MoveMouse(startX, startY);
        var (down, up) = button.Equals("right", StringComparison.OrdinalIgnoreCase)
            ? (MouseEventRightDown, MouseEventRightUp)
            : (MouseEventLeftDown, MouseEventLeftUp);

        SendInput(1, new[] { new Input { Type = InputMouse, U = new InputUnion { Mi = new MouseInput { DwFlags = down } } } }, Marshal.SizeOf<Input>());
        steps = Math.Clamp(steps, 1, 60);
        for (int i = 1; i <= steps; i++)
        {
            int curX = startX + (int)Math.Round((endX - startX) * (double)i / steps);
            int curY = startY + (int)Math.Round((endY - startY) * (double)i / steps);
            MoveMouse(curX, curY);
            Thread.Sleep(8);
        }
        SendInput(1, new[] { new Input { Type = InputMouse, U = new InputUnion { Mi = new MouseInput { DwFlags = up } } } }, Marshal.SizeOf<Input>());
    }

    public static void ClickMouse(int x, int y, string button = "left", bool doubleClick = false)
    {
        MoveMouse(x, y);
        var (down, up) = button.Equals("right", StringComparison.OrdinalIgnoreCase)
            ? (MouseEventRightDown, MouseEventRightUp)
            : (MouseEventLeftDown, MouseEventLeftUp);

        int count = doubleClick ? 2 : 1;
        for (int i = 0; i < count; i++)
        {
            var inputs = new[]
            {
                new Input { Type = InputMouse, U = new InputUnion { Mi = new MouseInput { DwFlags = down } } },
                new Input { Type = InputMouse, U = new InputUnion { Mi = new MouseInput { DwFlags = up } } },
            };
            SendInput((uint)inputs.Length, inputs, Marshal.SizeOf<Input>());
        }
    }

    public static void ScrollMouse(int delta)
    {
        var inputs = new[]
        {
            new Input
            {
                Type = InputMouse,
                U = new InputUnion
                {
                    Mi = new MouseInput
                    {
                        MouseData = unchecked((uint)delta),
                        DwFlags = MouseEventWheel,
                    },
                },
            },
        };
        SendInput((uint)inputs.Length, inputs, Marshal.SizeOf<Input>());
    }

    public static void TypeText(string text)
    {
        if (string.IsNullOrEmpty(text)) return;

        var inputs = new Input[text.Length * 2];
        for (int i = 0; i < text.Length; i++)
        {
            ushort ch = text[i];
            inputs[i * 2] = new Input
            {
                Type = InputKeyboard,
                U = new InputUnion
                {
                    Ki = new KeyboardInput { WScan = ch, DwFlags = KeyEventUnicode },
                },
            };
            inputs[i * 2 + 1] = new Input
            {
                Type = InputKeyboard,
                U = new InputUnion
                {
                    Ki = new KeyboardInput { WScan = ch, DwFlags = KeyEventUnicode | KeyEventKeyUp },
                },
            };
        }
        SendInput((uint)inputs.Length, inputs, Marshal.SizeOf<Input>());
    }

    public static void PressKey(ushort virtualKey)
    {
        var inputs = new[]
        {
            new Input { Type = InputKeyboard, U = new InputUnion { Ki = new KeyboardInput { WVk = virtualKey } } },
            new Input { Type = InputKeyboard, U = new InputUnion { Ki = new KeyboardInput { WVk = virtualKey, DwFlags = KeyEventKeyUp } } },
        };
        SendInput((uint)inputs.Length, inputs, Marshal.SizeOf<Input>());
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

        var inputs = new Input[keys.Count * 2];
        for (int i = 0; i < keys.Count; i++)
        {
            inputs[i] = new Input
            {
                Type = InputKeyboard,
                U = new InputUnion { Ki = new KeyboardInput { WVk = keys[i] } },
            };
        }
        for (int i = 0; i < keys.Count; i++)
        {
            inputs[keys.Count + i] = new Input
            {
                Type = InputKeyboard,
                U = new InputUnion { Ki = new KeyboardInput { WVk = keys[keys.Count - 1 - i], DwFlags = KeyEventKeyUp } },
            };
        }
        SendInput((uint)inputs.Length, inputs, Marshal.SizeOf<Input>());
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
