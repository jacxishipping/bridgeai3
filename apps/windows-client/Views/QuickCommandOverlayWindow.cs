using System;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Effects;

namespace AIBridgeClient.Views;

/// <summary>
/// Spotlight-style floating global quick-command overlay triggered from anywhere on Windows
/// via Ctrl+Shift+Space, dispatching directly to the local AI Bridge Agent.
/// </summary>
public sealed class QuickCommandOverlayWindow : Window
{
    private readonly System.Windows.Controls.TextBox _input;
    private readonly Action<string> _onSubmit;

    public QuickCommandOverlayWindow(Action<string> onSubmit)
    {
        _onSubmit = onSubmit;
        WindowStyle = WindowStyle.None;
        AllowsTransparency = true;
        Background = System.Windows.Media.Brushes.Transparent;
        Topmost = true;
        ShowInTaskbar = false;
        ResizeMode = ResizeMode.NoResize;
        Width = 640;
        SizeToContent = SizeToContent.Height;
        Title = "AI Bridge Quick Command";

        var badge = new Border
        {
            Width = 28,
            Height = 28,
            CornerRadius = new CornerRadius(8),
            Background = new SolidColorBrush(System.Windows.Media.Color.FromRgb(0x63, 0x66, 0xF1)),
            Margin = new Thickness(0, 0, 12, 0),
            VerticalAlignment = VerticalAlignment.Center,
            Child = new TextBlock
            {
                Text = "AI",
                Foreground = System.Windows.Media.Brushes.White,
                FontWeight = FontWeights.Bold,
                FontSize = 11.5,
                HorizontalAlignment = System.Windows.HorizontalAlignment.Center,
                VerticalAlignment = VerticalAlignment.Center,
            },
        };

        _input = new System.Windows.Controls.TextBox
        {
            Background = System.Windows.Media.Brushes.Transparent,
            BorderThickness = new Thickness(0),
            Foreground = System.Windows.Media.Brushes.White,
            CaretBrush = new SolidColorBrush(System.Windows.Media.Color.FromRgb(0x38, 0xBD, 0xF8)),
            FontSize = 15,
            VerticalContentAlignment = VerticalAlignment.Center,
            VerticalAlignment = VerticalAlignment.Center,
            ToolTip = "Type any Windows task (e.g. 'draw a car in Paint', 'open Notepad and write a note') and press Enter",
        };
        _input.PreviewKeyDown += OnInputKeyDown;

        var runBtn = new System.Windows.Controls.Button
        {
            Content = "Run ↵",
            Padding = new Thickness(14, 7, 14, 7),
            Margin = new Thickness(10, 0, 0, 0),
            Background = new SolidColorBrush(System.Windows.Media.Color.FromRgb(0x63, 0x66, 0xF1)),
            Foreground = System.Windows.Media.Brushes.White,
            FontWeight = FontWeights.SemiBold,
            FontSize = 12,
            BorderThickness = new Thickness(0),
            Cursor = System.Windows.Input.Cursors.Hand,
        };
        runBtn.Click += (_, _) => SubmitAndHide();

        var inputGrid = new Grid();
        inputGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        inputGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        inputGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        Grid.SetColumn(badge, 0);
        Grid.SetColumn(_input, 1);
        Grid.SetColumn(runBtn, 2);
        inputGrid.Children.Add(badge);
        inputGrid.Children.Add(_input);
        inputGrid.Children.Add(runBtn);

        var hint = new TextBlock
        {
            Text = "Global Agent Launcher (Ctrl+Shift+Space)  ·  Enter to execute on this PC  ·  Esc to dismiss",
            Foreground = new SolidColorBrush(System.Windows.Media.Color.FromRgb(0x94, 0xA3, 0xB8)),
            FontSize = 11,
            Margin = new Thickness(40, 8, 0, 0),
        };

        var stack = new StackPanel();
        stack.Children.Add(inputGrid);
        stack.Children.Add(hint);

        Content = new Border
        {
            Background = new SolidColorBrush(System.Windows.Media.Color.FromArgb(0xF4, 0x0B, 0x10, 0x1E)),
            BorderBrush = new SolidColorBrush(System.Windows.Media.Color.FromRgb(0x38, 0xBD, 0xF8)),
            BorderThickness = new Thickness(1.5),
            CornerRadius = new CornerRadius(16),
            Padding = new Thickness(18, 14, 14, 12),
            Margin = new Thickness(24),
            Child = stack,
            Effect = new DropShadowEffect
            {
                Color = System.Windows.Media.Color.FromRgb(0x38, 0xBD, 0xF8),
                BlurRadius = 30,
                ShadowDepth = 0,
                Opacity = 0.65,
            },
        };

        Deactivated += (_, _) =>
        {
            if (IsVisible) Hide();
        };
    }

    public void ShowAndFocus()
    {
        var work = SystemParameters.WorkArea;
        Left = work.Left + (work.Width - Width) / 2;
        Top = work.Top + Math.Max(40, work.Height * 0.14);
        _input.Clear();
        Show();
        Activate();
        _input.Focus();
    }

    public void SubmitCommandForTest(string command)
    {
        _input.Text = command;
        SubmitAndHide();
    }

    private void OnInputKeyDown(object sender, System.Windows.Input.KeyEventArgs e)
    {
        if (e.Key == Key.Escape)
        {
            e.Handled = true;
            Hide();
        }
        else if (e.Key == Key.Enter)
        {
            e.Handled = true;
            SubmitAndHide();
        }
    }

    private void SubmitAndHide()
    {
        var text = _input.Text.Trim();
        Hide();
        if (!string.IsNullOrWhiteSpace(text))
        {
            _onSubmit(text);
        }
    }
}
