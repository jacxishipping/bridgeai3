using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

namespace AIBridgeInstaller.Services;

/// <summary>
/// Win32 DPAPI token sealer bit-compatible with AIBridgeClient.Services.DpapiTokenStore
/// and agents/windows/app_runtime.py ("aib-dpapi-v1:" prefix).
/// </summary>
public static class DpapiSealer
{
    public const string DpapiMagic = "aib-dpapi-v1:";
    private const uint CryptProtectUiForbidden = 0x1;

    [StructLayout(LayoutKind.Sequential)]
    private struct DataBlob
    {
        public uint cbData;
        public IntPtr pbData;
    }

    [DllImport("crypt32.dll", SetLastError = true, CharSet = CharSet.Auto)]
    private static extern bool CryptProtectData(
        ref DataBlob pDataIn,
        string? szDataDescr,
        IntPtr pOptionalEntropy,
        IntPtr pvReserved,
        IntPtr pPromptStruct,
        uint dwFlags,
        ref DataBlob pDataOut);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr LocalFree(IntPtr hMem);

    public static string SealToken(string token)
    {
        if (string.IsNullOrWhiteSpace(token))
        {
            return string.Empty;
        }

        var raw = Encoding.UTF8.GetBytes(token.Trim());
        var inBlob = new DataBlob();
        var outBlob = new DataBlob();
        var handle = GCHandle.Alloc(raw, GCHandleType.Pinned);
        try
        {
            inBlob.cbData = (uint)raw.Length;
            inBlob.pbData = handle.AddrOfPinnedObject();

            if (!CryptProtectData(ref inBlob, null, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, CryptProtectUiForbidden, ref outBlob))
            {
                throw new InvalidOperationException($"CryptProtectData failed with Win32 error {Marshal.GetLastWin32Error()}");
            }

            var sealedBytes = new byte[outBlob.cbData];
            Marshal.Copy(outBlob.pbData, sealedBytes, 0, (int)outBlob.cbData);
            return DpapiMagic + Convert.ToBase64String(sealedBytes);
        }
        finally
        {
            handle.Free();
            if (outBlob.pbData != IntPtr.Zero)
            {
                LocalFree(outBlob.pbData);
            }
        }
    }

    public static void SaveTokenFile(string path, string token)
    {
        var dir = Path.GetDirectoryName(path);
        if (!string.IsNullOrEmpty(dir))
        {
            Directory.CreateDirectory(dir);
        }

        var sealedContent = SealToken(token);
        var tmp = path + ".tmp";
        File.WriteAllText(tmp, sealedContent, Encoding.UTF8);
        File.Move(tmp, path, overwrite: true);
    }
}
