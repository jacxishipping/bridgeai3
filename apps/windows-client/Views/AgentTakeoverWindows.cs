using System;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Media.Effects;
using System.Windows.Shapes;
using AIBridgeClient.Services;

namespace AIBridgeClient.Views;

internal static class TakeoverNative
{
    public const int GWL_EXSTYLE = -20;
    public const int WS_EX_TRANSPARENT = 0x00000020;
    public const int WS_EX_TOOLWINDOW = 0x00000080;
    public const int WS_EX_NOACTIVATE = 0x08000000;

    [DllImport("user32.dll")]
    public static extern int GetWindowLong(IntPtr hWnd, int nIndex);

    [DllImport("user32.dll")]
    public static extern int SetWindowLong(IntPtr hWnd, int nIndex, int dwNewLong);
}

/// <summary>
/// Full-virtual-screen, click-through, always-on-top window that draws an animated glowing
/// border around the whole desktop while the agent controls it.
/// </summary>
public sealed class AgentGlowOverlayWindow : Window
{
    private readonly RotateTransform _rotate = new(0, 0.5, 0.5);
    private readonly Border _outer;
    private readonly Border _inner;
    private Storyboard? _storyboard;

    public AgentGlowOverlayWindow()
    {
        WindowStyle = WindowStyle.None;
        AllowsTransparency = true;
        Background = System.Windows.Media.Brushes.Transparent;
        Topmost = true;
        ShowInTaskbar = false;
        ShowActivated = false;
        ResizeMode = ResizeMode.NoResize;
        Focusable = false;
        IsHitTestVisible = false;
        Title = "AI Bridge Agent Control Overlay";

        var gradient = new LinearGradientBrush
        {
            StartPoint = new System.Windows.Point(0, 0),
            EndPoint = new System.Windows.Point(1, 1),
            RelativeTransform = _rotate,
            SpreadMethod = GradientSpreadMethod.Reflect,
        };
        gradient.GradientStops.Add(new GradientStop(System.Windows.Media.Color.FromRgb(0x22, 0xD3, 0xEE), 0.0));
        gradient.GradientStops.Add(new GradientStop(System.Windows.Media.Color.FromRgb(0x63, 0x66, 0xF1), 0.33));
        gradient.GradientStops.Add(new GradientStop(System.Windows.Media.Color.FromRgb(0xEC, 0x48, 0x99), 0.66));
        gradient.GradientStops.Add(new GradientStop(System.Windows.Media.Color.FromRgb(0x22, 0xD3, 0xEE), 1.0));

        _outer = new Border
        {
            BorderBrush = gradient,
            BorderThickness = new Thickness(7),
            IsHitTestVisible = false,
            Effect = new DropShadowEffect
            {
                Color = System.Windows.Media.Color.FromRgb(0x38, 0xBD, 0xF8),
                BlurRadius = 46,
                ShadowDepth = 0,
                Opacity = 0.95,
            },
        };

        _inner = new Border
        {
            Margin = new Thickness(7),
            BorderBrush = gradient,
            BorderThickness = new Thickness(34),
            Opacity = 0.14,
            IsHitTestVisible = false,
            Effect = new BlurEffect { Radius = 38 },
        };

        var grid = new Grid { IsHitTestVisible = false };
        grid.Children.Add(_inner);
        grid.Children.Add(_outer);
        Content = grid;

        SourceInitialized += (_, _) => MakeClickThrough();
    }

    private void MakeClickThrough()
    {
        var hwnd = new WindowInteropHelper(this).Handle;
        int style = TakeoverNative.GetWindowLong(hwnd, TakeoverNative.GWL_EXSTYLE);
        TakeoverNative.SetWindowLong(
            hwnd,
            TakeoverNative.GWL_EXSTYLE,
            style | TakeoverNative.WS_EX_TRANSPARENT | TakeoverNative.WS_EX_NOACTIVATE | TakeoverNative.WS_EX_TOOLWINDOW);
    }

    private void FitVirtualScreen()
    {
        Left = SystemParameters.VirtualScreenLeft;
        Top = SystemParameters.VirtualScreenTop;
        Width = SystemParameters.VirtualScreenWidth;
        Height = SystemParameters.VirtualScreenHeight;
    }

    public void ShowGlow()
    {
        FitVirtualScreen();
        Opacity = 0;
        Show();

        _storyboard?.Stop();
        _storyboard = new Storyboard();

        var spin = new DoubleAnimation(0, 360, TimeSpan.FromSeconds(5)) { RepeatBehavior = RepeatBehavior.Forever };
        Storyboard.SetTarget(spin, this);
        Storyboard.SetTargetProperty(spin, new PropertyPath("(0)", GlowAngleProperty));
        _storyboard.Children.Add(spin);

        var pulse = new DoubleAnimation(0.62, 1.0, TimeSpan.FromSeconds(1.1))
        {
            AutoReverse = true,
            RepeatBehavior = RepeatBehavior.Forever,
            EasingFunction = new SineEase { EasingMode = EasingMode.EaseInOut },
        };
        Storyboard.SetTarget(pulse, _outer);
        Storyboard.SetTargetProperty(pulse, new PropertyPath(OpacityProperty));
        _storyboard.Children.Add(pulse);

        _storyboard.Begin(this, true);
        BeginAnimation(OpacityProperty, new DoubleAnimation(0, 1, TimeSpan.FromMilliseconds(350)));
    }

    public void HideGlow()
    {
        _storyboard?.Stop(this);
        var fade = new DoubleAnimation(Opacity, 0, TimeSpan.FromMilliseconds(250));
        fade.Completed += (_, _) =>
        {
            if (AgentTakeoverService.State != TakeoverState.AgentControlling)
            {
                Hide();
            }
        };
        BeginAnimation(OpacityProperty, fade);
    }

    public static readonly DependencyProperty GlowAngleProperty = DependencyProperty.Register(
        nameof(GlowAngle),
        typeof(double),
        typeof(AgentGlowOverlayWindow),
        new PropertyMetadata(0.0, (d, e) => ((AgentGlowOverlayWindow)d)._rotate.Angle = (double)e.NewValue));

    public double GlowAngle
    {
        get => (double)GetValue(GlowAngleProperty);
        set => SetValue(GlowAngleProperty, value);
    }
}

/// <summary>
/// Small always-on-top pill at the top-center of the primary screen with the "Take Over" /
/// "Return Control to Agent" action. Clicks on it are the only physical clicks allowed while
/// the agent is in control.
/// </summary>
public sealed class AgentTakeoverPillWindow : Window
{
    private readonly TextBlock _label;
    private readonly System.Windows.Controls.Button _button;
    private readonly Ellipse _dot;

    public AgentTakeoverPillWindow()
    {
        WindowStyle = WindowStyle.None;
        AllowsTransparency = true;
        Background = System.Windows.Media.Brushes.Transparent;
        Topmost = true;
        ShowInTaskbar = false;
        ShowActivated = false;
        ResizeMode = ResizeMode.NoResize;
        SizeToContent = SizeToContent.WidthAndHeight;
        Title = "AI Bridge Agent Control";

        _dot = new Ellipse
        {
            Width = 9,
            Height = 9,
            Fill = new SolidColorBrush(System.Windows.Media.Color.FromRgb(0x22, 0xD3, 0xEE)),
            VerticalAlignment = VerticalAlignment.Center,
            Margin = new Thickness(0, 0, 9, 0),
        };
        _dot.BeginAnimation(OpacityProperty, new DoubleAnimation(0.3, 1, TimeSpan.FromMilliseconds(700))
        {
            AutoReverse = true,
            RepeatBehavior = RepeatBehavior.Forever,
        });

        _label = new TextBlock
        {
            Foreground = System.Windows.Media.Brushes.White,
            FontSize = 12.5,
            FontWeight = FontWeights.SemiBold,
            VerticalAlignment = VerticalAlignment.Center,
            Margin = new Thickness(0, 0, 14, 0),
        };

        _button = new System.Windows.Controls.Button
        {
            Padding = new Thickness(14, 6, 14, 6),
            FontSize = 12,
            FontWeight = FontWeights.SemiBold,
            Foreground = System.Windows.Media.Brushes.White,
            Background = new SolidColorBrush(System.Windows.Media.Color.FromRgb(0xEF, 0x44, 0x44)),
            BorderThickness = new Thickness(0),
            Cursor = System.Windows.Input.Cursors.Hand,
            Focusable = false,
        };
        _button.Click += (_, _) =>
        {
            if (AgentTakeoverService.State == TakeoverState.AgentControlling) AgentTakeoverService.TakeOver();
            else AgentTakeoverService.ReturnControlToAgent();
        };

        var row = new StackPanel { Orientation = System.Windows.Controls.Orientation.Horizontal };
        row.Children.Add(_dot);
        row.Children.Add(_label);
        row.Children.Add(_button);

        Content = new Border
        {
            Background = new SolidColorBrush(System.Windows.Media.Color.FromArgb(0xEE, 0x0B, 0x10, 0x1E)),
            BorderBrush = new SolidColorBrush(System.Windows.Media.Color.FromRgb(0x38, 0xBD, 0xF8)),
            BorderThickness = new Thickness(1.5),
            CornerRadius = new CornerRadius(22),
            Padding = new Thickness(18, 9, 10, 9),
            Margin = new Thickness(26),
            Child = row,
            Effect = new DropShadowEffect
            {
                Color = System.Windows.Media.Color.FromRgb(0x38, 0xBD, 0xF8),
                BlurRadius = 24,
                ShadowDepth = 0,
                Opacity = 0.8,
            },
        };

        SourceInitialized += (_, _) =>
        {
            var hwnd = new WindowInteropHelper(this).Handle;
            int style = TakeoverNative.GetWindowLong(hwnd, TakeoverNative.GWL_EXSTYLE);
            TakeoverNative.SetWindowLong(
                hwnd,
                TakeoverNative.GWL_EXSTYLE,
                style | TakeoverNative.WS_EX_NOACTIVATE | TakeoverNative.WS_EX_TOOLWINDOW);
        };
        SizeChanged += (_, _) => Reposition();
    }

    private void Reposition()
    {
        var work = SystemParameters.WorkArea;
        Left = work.Left + (work.Width - ActualWidth) / 2;
        Top = work.Top - 14;
    }

    public void ShowFor(TakeoverState state)
    {
        bool agent = state == TakeoverState.AgentControlling;
        _label.Text = agent
            ? "AI agent is controlling your screen  ·  Ctrl+Alt+T"
            : "You are in control  ·  agent input paused";
        _button.Content = agent ? "Take Over" : "Return Control to Agent";
        _button.Background = new SolidColorBrush(agent
            ? System.Windows.Media.Color.FromRgb(0xEF, 0x44, 0x44)
            : System.Windows.Media.Color.FromRgb(0x10, 0xB9, 0x81));
        _dot.Fill = new SolidColorBrush(agent
            ? System.Windows.Media.Color.FromRgb(0x22, 0xD3, 0xEE)
            : System.Windows.Media.Color.FromRgb(0x10, 0xB9, 0x81));
        if (!IsVisible) Show();
        Reposition();
    }

    public void UpdateActionText(string action)
    {
        if (AgentTakeoverService.State != TakeoverState.AgentControlling) return;
        _label.Text = string.IsNullOrWhiteSpace(action)
            ? "AI agent is controlling your screen  ·  Ctrl+Alt+T"
            : $"AI Agent: {action}  ·  Ctrl+Alt+T";
        Reposition();
    }

    public void HidePill()
    {
        if (IsVisible) Hide();
    }

    /// <summary>Pill bounds in physical screen pixels (matches low-level mouse hook coordinates).</summary>
    public Rect? GetScreenPixelBounds()
    {
        if (!IsVisible || !IsLoaded) return null;
        var dpi = VisualTreeHelper.GetDpi(this);
        var topLeft = PointToScreen(new System.Windows.Point(0, 0));
        return new Rect(topLeft.X, topLeft.Y, ActualWidth * dpi.DpiScaleX, ActualHeight * dpi.DpiScaleY);
    }
}
