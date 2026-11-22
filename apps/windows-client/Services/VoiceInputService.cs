using System;
using System.Diagnostics;
using System.Text;
using System.Threading;
using System.Threading.Tasks;

namespace AIBridgeClient.Services;

/// <summary>
/// Offline Windows Push-to-Talk voice command recognition using built-in System.Speech
/// (zero external NuGet dependencies; gracefully reports if no microphone is present).
/// </summary>
public static class VoiceInputService
{
    public static string BuildRecognitionScript(int timeoutSeconds = 8)
    {
        int sec = Math.Clamp(timeoutSeconds, 3, 20);
        return $@"
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$rec = New-Object System.Speech.Recognition.SpeechRecognitionEngine
try {{
    $rec.SetInputToDefaultAudioDevice()
}} catch {{
    Write-Output 'ERR:NO_MICROPHONE'
    exit 2
}}
$rec.LoadGrammar((New-Object System.Speech.Recognition.DictationGrammar))
$res = $rec.Recognize([TimeSpan]::FromSeconds({sec}))
if ($null -eq $res -or [string]::IsNullOrWhiteSpace($res.Text)) {{
    Write-Output 'ERR:NO_SPEECH'
    exit 3
}}
Write-Output ('OK:' + $res.Text)
";
    }

    public static async Task<(bool Ok, string Transcript, string Error)> ListenOnceAsync(
        int timeoutSeconds = 8,
        CancellationToken ct = default)
    {
        try
        {
            var script = BuildRecognitionScript(timeoutSeconds);
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
            if (proc is null)
            {
                return (false, "", "Failed to start Windows Speech Recognition host.");
            }
            using var timeoutCts = CancellationTokenSource.CreateLinkedTokenSource(ct);
            timeoutCts.CancelAfter(TimeSpan.FromSeconds(Math.Clamp(timeoutSeconds, 3, 20) + 4));
            var stdout = (await proc.StandardOutput.ReadToEndAsync(timeoutCts.Token)).Trim();
            await proc.WaitForExitAsync(timeoutCts.Token);

            if (stdout.StartsWith("OK:", StringComparison.Ordinal))
            {
                return (true, stdout[3..].Trim(), "");
            }
            if (stdout.Contains("NO_MICROPHONE", StringComparison.Ordinal))
            {
                return (false, "", "No default Windows microphone detected.");
            }
            if (stdout.Contains("NO_SPEECH", StringComparison.Ordinal))
            {
                return (false, "", "No speech detected — try speaking closer to the mic.");
            }
            return (false, "", string.IsNullOrWhiteSpace(stdout) ? "Speech recognition did not return text." : stdout);
        }
        catch (OperationCanceledException)
        {
            return (false, "", "Voice input cancelled.");
        }
        catch (Exception ex)
        {
            return (false, "", $"Voice input error: {ex.Message}");
        }
    }
}
