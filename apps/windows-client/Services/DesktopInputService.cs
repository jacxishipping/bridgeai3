using System;
using System.Runtime.InteropServices;

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
                        DwFlags = MouseEventMove | MouseEventAbsolute,
                    },
                },
            },
        };
        SendInput((uint)inputs.Length, inputs, Marshal.SizeOf<Input>());
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

    private static (int Nx, int Ny) NormalizeCoordinates(int x, int y)
    {
        int screenW = Math.Max(1, GetSystemMetrics(0)); // SM_CXSCREEN
        int screenH = Math.Max(1, GetSystemMetrics(1)); // SM_CYSCREEN
        int nx = (int)Math.Round(x * 65535.0 / Math.Max(1, screenW - 1));
        int ny = (int)Math.Round(y * 65535.0 / Math.Max(1, screenH - 1));
        return (Math.Clamp(nx, 0, 65535), Math.Clamp(ny, 0, 65535));
    }
}
