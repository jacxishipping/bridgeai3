using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

namespace AIBridgeClient.Services;

public sealed record OcrWordHit(
    string Text,
    int X,
    int Y,
    int Width,
    int Height,
    int CenterX,
    int CenterY);

public sealed record OcrScreenResult(
    string Engine,
    string FullText,
    List<OcrWordHit> Words,
    string ScreenshotPath);

/// <summary>
/// Native offline Windows OCR service using WinRT Windows.Media.Ocr.OcrEngine
/// with automatic fallback to UI Automation visible text extraction when a
/// Windows OCR language pack is not installed.
/// </summary>
public static class OcrService
{
    public static async Task<OcrScreenResult> RecognizeScreenAsync(
        string screenshotDir,
        int monitorIndex = 0,
        string? query = null,
        CancellationToken ct = default)
    {
        var shot = ScreenCaptureService.CapturePrimaryScreen(screenshotDir, monitorIndex);
        var winrtResult = await TryRecognizeWithWinRtOcrAsync(shot.FilePath, query, ct);
        if (winrtResult is not null)
        {
            return winrtResult;
        }

        // Graceful fallback to UI Automation visible text & bounding boxes
        var elements = UiAutomationService.FindVisibleElements(query, 150);
        var words = new List<OcrWordHit>();
        var textLines = new List<string>();
        foreach (var el in elements)
        {
            var label = !string.IsNullOrWhiteSpace(el.Name) ? el.Name.Trim() : el.Value?.Trim() ?? "";
            if (string.IsNullOrWhiteSpace(label)) continue;
            textLines.Add(label);
            words.Add(new OcrWordHit(
                label,
                el.X,
                el.Y,
                el.Width,
                el.Height,
                el.CenterX,
                el.CenterY));
        }

        return new OcrScreenResult(
            "uia_text_fallback",
            string.Join("\n", textLines),
            words,
            shot.FilePath);
    }

    private static async Task<OcrScreenResult?> TryRecognizeWithWinRtOcrAsync(
        string imagePath,
        string? query,
        CancellationToken ct)
    {
        if (!File.Exists(imagePath)) return null;
        try
        {
            var escapedPath = imagePath.Replace("'", "''");
            var script = $@"
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Media.Ocr, ContentType=WindowsRuntime]
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {{ $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' }})[0]
function Await($WinRtTask, $ResultType) {{
    $netTask = $asTaskGeneric.MakeGenericMethod($ResultType).Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    return $netTask.Result
}}
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) {{ exit 2 }}
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync('{escapedPath}')) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$res = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
$words = @()
foreach ($line in $res.Lines) {{
    foreach ($w in $line.Words) {{
        $r = $w.BoundingRect
        $words += [PSCustomObject]@{{
            text = $w.Text
            x = [int][Math]::Round($r.X)
            y = [int][Math]::Round($r.Y)
            width = [int][Math]::Round($r.Width)
            height = [int][Math]::Round($r.Height)
            center_x = [int][Math]::Round($r.X + $r.Width / 2.0)
            center_y = [int][Math]::Round($r.Y + $r.Height / 2.0)
        }}
    }}
}}
[PSCustomObject]@{{ full_text = $res.Text; words = $words }} | ConvertTo-Json -Depth 4 -Compress
";
            var encoded = Convert.ToBase64String(Encoding.Unicode.GetBytes(script));
            var psi = new ProcessStartInfo
            {
                FileName = "powershell.exe",
                Arguments = $"-NoProfile -NonInteractive -EncodedCommand {encoded}",
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                CreateNoWindow = true,
            };
            using var proc = Process.Start(psi);
            if (proc is null) return null;
            using var timeoutCts = CancellationTokenSource.CreateLinkedTokenSource(ct);
            timeoutCts.CancelAfter(TimeSpan.FromSeconds(8));
            var stdout = await proc.StandardOutput.ReadToEndAsync(timeoutCts.Token);
            await proc.WaitForExitAsync(timeoutCts.Token);
            if (proc.ExitCode != 0 || string.IsNullOrWhiteSpace(stdout)) return null;

            using var doc = JsonDocument.Parse(stdout.Trim());
            var root = doc.RootElement;
            var fullText = root.TryGetProperty("full_text", out var ftEl) ? ftEl.GetString() ?? "" : "";
            var hits = new List<OcrWordHit>();
            if (root.TryGetProperty("words", out var wEl))
            {
                if (wEl.ValueKind == JsonValueKind.Array)
                {
                    foreach (var item in wEl.EnumerateArray())
                    {
                        AddWordHit(item, query, hits);
                    }
                }
                else if (wEl.ValueKind == JsonValueKind.Object)
                {
                    AddWordHit(wEl, query, hits);
                }
            }
            return new OcrScreenResult("windows_media_ocr", fullText, hits, imagePath);
        }
        catch
        {
            return null;
        }
    }

    private static void AddWordHit(JsonElement item, string? query, List<OcrWordHit> hits)
    {
        var txt = item.TryGetProperty("text", out var tEl) ? tEl.GetString() ?? "" : "";
        if (string.IsNullOrWhiteSpace(txt)) return;
        if (!string.IsNullOrWhiteSpace(query) && !txt.Contains(query, StringComparison.OrdinalIgnoreCase)) return;
        hits.Add(new OcrWordHit(
            txt,
            item.TryGetProperty("x", out var xEl) ? xEl.GetInt32() : 0,
            item.TryGetProperty("y", out var yEl) ? yEl.GetInt32() : 0,
            item.TryGetProperty("width", out var wEl) ? wEl.GetInt32() : 0,
            item.TryGetProperty("height", out var hEl) ? hEl.GetInt32() : 0,
            item.TryGetProperty("center_x", out var cxEl) ? cxEl.GetInt32() : 0,
            item.TryGetProperty("center_y", out var cyEl) ? cyEl.GetInt32() : 0));
    }
}
