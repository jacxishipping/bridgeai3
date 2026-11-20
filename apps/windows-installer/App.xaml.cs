using System;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Media;
using AIBridgeInstaller.Models;
using AIBridgeInstaller.Services;

namespace AIBridgeInstaller;

public partial class App : System.Windows.Application
{
    protected override async void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);

        var options = InstallerOptions.ParseArgs(e.Args);
        ApplyTheme(options.Theme);

        // Silent / unattended CLI mode for MDM, Intune, GPO, or CI automation
        if (options.Silent)
        {
            var engine = new InstallEngine();
            var result = options.UninstallMode
                ? await engine.UninstallAsync(options)
                : await engine.InstallAsync(options);
            Shutdown(result.Success ? 0 : 1);
            return;
        }

        var window = new MainWindow(options);
        MainWindow = window;
        window.Closed += (_, _) => Shutdown(0);
        window.Show();
    }

    public static void ApplyTheme(string theme)
    {
        bool isLight = string.Equals(theme, "light", StringComparison.OrdinalIgnoreCase);
        var res = Current.Resources;

        if (isLight)
        {
            SetBrush(res, "BgCanvas", 0xF8, 0xFA, 0xFC);
            SetBrush(res, "BgSidebar", 0xFF, 0xFF, 0xFF);
            SetBrush(res, "BgCard", 0xFF, 0xFF, 0xFF);
            SetBrush(res, "BgElevated", 0xF1, 0xF5, 0xF9);
            SetBrush(res, "BgInput", 0xF8, 0xFA, 0xFC);
            SetBrush(res, "ConsoleBg", 0x0B, 0x0E, 0x17);
            SetBrush(res, "BorderHairline", 0xE2, 0xE8, 0xF0);
            SetBrush(res, "BorderHover", 0xCB, 0xD5, 0xE1);
            SetBrush(res, "TextPrimary", 0x0F, 0x17, 0x2A);
            SetBrush(res, "TextSecondary", 0x47, 0x55, 0x69);
            SetBrush(res, "TextMuted", 0x64, 0x74, 0x8B);
            SetBrush(res, "AccentPrimary", 0x4F, 0x46, 0xE5);
            SetBrush(res, "AccentHover", 0x43, 0x38, 0xCA);
            SetBrush(res, "AccentSoftBg", 0xEE, 0xF2, 0xFF);
            SetBrush(res, "AccentSoftFg", 0x4F, 0x46, 0xE5);
            SetBrush(res, "SuccessBg", 0xEC, 0xFD, 0xF5);
            SetBrush(res, "SuccessFg", 0x05, 0x96, 0x69);
            SetBrush(res, "WarningBg", 0xFF, 0xFB, 0xEB);
            SetBrush(res, "WarningFg", 0xD9, 0x77, 0x06);
            SetBrush(res, "DangerBg", 0xFE, 0xF2, 0xF2);
            SetBrush(res, "DangerHoverBg", 0xFE, 0xE2, 0xE2);
            SetBrush(res, "DangerFg", 0xDC, 0x26, 0x26);
            SetBrush(res, "DangerBorder", 0xFE, 0xCA, 0xCA);
            SetBrush(res, "ToggleOffBg", 0xCB, 0xD5, 0xE1);
        }
        else
        {
            SetBrush(res, "BgCanvas", 0x09, 0x0B, 0x11);
            SetBrush(res, "BgSidebar", 0x0D, 0x10, 0x18);
            SetBrush(res, "BgCard", 0x11, 0x15, 0x20);
            SetBrush(res, "BgElevated", 0x16, 0x1B, 0x28);
            SetBrush(res, "BgInput", 0x0A, 0x0D, 0x14);
            SetBrush(res, "ConsoleBg", 0x06, 0x08, 0x0D);
            SetBrush(res, "BorderHairline", 0x1E, 0x23, 0x31);
            SetBrush(res, "BorderHover", 0x2E, 0x36, 0x4A);
            SetBrush(res, "TextPrimary", 0xF1, 0xF5, 0xF9);
            SetBrush(res, "TextSecondary", 0x94, 0xA3, 0xB8);
            SetBrush(res, "TextMuted", 0x64, 0x74, 0x8B);
            SetBrush(res, "AccentPrimary", 0x63, 0x66, 0xF1);
            SetBrush(res, "AccentHover", 0x4F, 0x46, 0xE5);
            SetBrush(res, "AccentSoftBg", 0x18, 0x1E, 0x38);
            SetBrush(res, "AccentSoftFg", 0x81, 0x8C, 0xF8);
            SetBrush(res, "SuccessBg", 0x0E, 0x2B, 0x24);
            SetBrush(res, "SuccessFg", 0x34, 0xD3, 0x99);
            SetBrush(res, "WarningBg", 0x2B, 0x21, 0x0E);
            SetBrush(res, "WarningFg", 0xFB, 0xBF, 0x24);
            SetBrush(res, "DangerBg", 0x24, 0x13, 0x19);
            SetBrush(res, "DangerHoverBg", 0x36, 0x18, 0x22);
            SetBrush(res, "DangerFg", 0xF8, 0x71, 0x71);
            SetBrush(res, "DangerBorder", 0x4E, 0x1F, 0x28);
            SetBrush(res, "ToggleOffBg", 0x33, 0x41, 0x55);
        }
    }

    private static void SetBrush(ResourceDictionary res, string key, byte r, byte g, byte b)
        => res[key] = new SolidColorBrush(System.Windows.Media.Color.FromRgb(r, g, b));
}
