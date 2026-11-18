using System;
using System.Windows;
using System.Windows.Media;

namespace AIBridgeClient.Themes;

public static class ThemeManager
{
    public static void ApplyTheme(string theme)
    {
        bool isLight = string.Equals(theme, "light", StringComparison.OrdinalIgnoreCase);
        var res = System.Windows.Application.Current.Resources;

        if (isLight)
        {
            SetBrush(res, "BgCanvas", 0xF8, 0xFA, 0xFC);       // --bg-main: #F8FAFC
            SetBrush(res, "BgSidebar", 0xFF, 0xFF, 0xFF);      // --bg-sidebar: #FFFFFF
            SetBrush(res, "BgCard", 0xFF, 0xFF, 0xFF);         // --bg-card: #FFFFFF
            SetBrush(res, "BgElevated", 0xF1, 0xF5, 0xF9);     // --bg-elevated: #F1F5F9
            SetBrush(res, "BgInput", 0xF8, 0xFA, 0xFC);        // --bg-input: #F8FAFC
            SetBrush(res, "ConsoleCardBg", 0x0B, 0x0E, 0x17);  // --bg-terminal: #0B0E17
            SetBrush(res, "BorderHairline", 0xE2, 0xE8, 0xF0);
            SetBrush(res, "BorderHover", 0xCB, 0xD5, 0xE1);
            SetBrush(res, "TextPrimary", 0x0F, 0x17, 0x2A);    // #0F172A
            SetBrush(res, "TextSecondary", 0x47, 0x55, 0x69);  // #475569
            SetBrush(res, "TextMuted", 0x64, 0x74, 0x8B);      // #64748B
            SetBrush(res, "NavActiveBg", 0xEE, 0xF2, 0xFF);    // #EEF2FF
            SetBrush(res, "NavActiveText", 0x31, 0x2E, 0x81);  // #312E81
            SetBrush(res, "NavActiveAccent", 0x4F, 0x46, 0xE5);// #4F46E5
            SetBrush(res, "AccentPrimary", 0x4F, 0x46, 0xE5);  // #4F46E5
            SetBrush(res, "AccentHover", 0x43, 0x38, 0xCA);    // #4338CA
            SetBrush(res, "ToggleOffBg", 0xCB, 0xD5, 0xE1);
            SetBrush(res, "GridHover", 0xF8, 0xFA, 0xFC);
            SetBrush(res, "ScrollThumbBg", 0xCB, 0xD5, 0xE1);

            // Danger button tokens (Light)
            SetBrush(res, "DangerBg", 0xFE, 0xF2, 0xF2);
            SetBrush(res, "DangerHoverBg", 0xFE, 0xE2, 0xE2);
            SetBrush(res, "DangerFg", 0xDC, 0x26, 0x26);
            SetBrush(res, "DangerBorder", 0xFE, 0xCA, 0xCA);

            // Semantic text contrast tokens (Light)
            SetBrush(res, "SuccessText", 0x05, 0x96, 0x69);    // #059669
            SetBrush(res, "IndigoText", 0x4F, 0x46, 0xE5);     // #4F46E5
            SetBrush(res, "ErrorText", 0xDC, 0x26, 0x26);      // #DC2626
            SetBrush(res, "WarningText", 0xD9, 0x77, 0x06);    // #D97706
        }
        else
        {
            SetBrush(res, "BgCanvas", 0x09, 0x0B, 0x11);       // --bg-main: #090B11
            SetBrush(res, "BgSidebar", 0x0D, 0x10, 0x18);      // --bg-sidebar: #0D1018
            SetBrush(res, "BgCard", 0x11, 0x15, 0x20);         // --bg-card: #111520
            SetBrush(res, "BgElevated", 0x16, 0x1B, 0x28);     // --bg-elevated: #161B28
            SetBrush(res, "BgInput", 0x0A, 0x0D, 0x14);        // --bg-input: #0A0D14
            SetBrush(res, "ConsoleCardBg", 0x06, 0x08, 0x0D);  // --bg-terminal: #06080D
            SetBrush(res, "BorderHairline", 0x1E, 0x23, 0x31);
            SetBrush(res, "BorderHover", 0x2E, 0x36, 0x4A);
            SetBrush(res, "TextPrimary", 0xF1, 0xF5, 0xF9);    // #F1F5F9
            SetBrush(res, "TextSecondary", 0x94, 0xA3, 0xB8);  // #94A3B8
            SetBrush(res, "TextMuted", 0x64, 0x74, 0x8B);      // #64748B
            SetBrush(res, "NavActiveBg", 0x18, 0x1E, 0x38);    // rgba(99,102,241,0.12)
            SetBrush(res, "NavActiveText", 0xF8, 0xFA, 0xFC);  // #F8FAFC
            SetBrush(res, "NavActiveAccent", 0x81, 0x8C, 0xF8);// #818CF8
            SetBrush(res, "AccentPrimary", 0x63, 0x66, 0xF1);  // #6366F1
            SetBrush(res, "AccentHover", 0x4F, 0x46, 0xE5);    // #4F46E5
            SetBrush(res, "ToggleOffBg", 0x33, 0x41, 0x55);    // #334155
            SetBrush(res, "GridHover", 0x16, 0x1B, 0x28);
            SetBrush(res, "ScrollThumbBg", 0x2D, 0x35, 0x48);

            // Danger button tokens (Dark)
            SetBrush(res, "DangerBg", 0x24, 0x13, 0x19);
            SetBrush(res, "DangerHoverBg", 0x36, 0x18, 0x22);
            SetBrush(res, "DangerFg", 0xF8, 0x71, 0x71);
            SetBrush(res, "DangerBorder", 0x4E, 0x1F, 0x28);

            // Semantic text contrast tokens (Dark)
            SetBrush(res, "SuccessText", 0x34, 0xD3, 0x99);    // #34D399
            SetBrush(res, "IndigoText", 0x81, 0x8C, 0xF8);     // #818CF8
            SetBrush(res, "ErrorText", 0xF8, 0x71, 0x71);      // #F87171
            SetBrush(res, "WarningText", 0xFB, 0xBF, 0x24);    // #FBBF24
        }

        SetBrush(res, "SuccessColor", 0x10, 0xB9, 0x81);
        SetBrush(res, "WarningColor", 0xF5, 0x9E, 0x0B);
        SetBrush(res, "ErrorColor", 0xEF, 0x44, 0x44);
    }

    private static void SetBrush(ResourceDictionary res, string key, byte r, byte g, byte b)
    {
        res[key] = new SolidColorBrush(System.Windows.Media.Color.FromRgb(r, g, b));
    }
}
