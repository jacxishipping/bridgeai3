using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

namespace AIBridgeClient.Services;

/// <summary>
/// DPAPI token sealing compatible with agents/windows/app_runtime.py ("aib-dpapi-v1:" prefix).
/// Uses native Win32 CryptProtectData / CryptUnprotectData with zero external NuGet dependencies.
/// </summary>
public static class DpapiTokenStore
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

    [DllImport("crypt32.dll", SetLastError = true, CharSet = CharSet.Auto)]
    private static extern bool CryptUnprotectData(
        ref DataBlob pDataIn,
        IntPtr ppszDataDescr,
        IntPtr pOptionalEntropy,
        IntPtr pvReserved,
        IntPtr pPromptStruct,
        uint dwFlags,
        ref DataBlob pDataOut);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr LocalFree(IntPtr hMem);

    public static string SealToken(string token)
    {
        if (string.IsNullOrEmpty(token))
        {
            return string.Empty;
        }

        var raw = Encoding.UTF8.GetBytes(token);
        var sealedBytes = Protect(raw);
        return DpapiMagic + Convert.ToBase64String(sealedBytes);
    }

    public static string UnsealToken(string stored)
    {
        var trimmed = stored.Trim();
        if (string.IsNullOrEmpty(trimmed))
        {
            return string.Empty;
        }

        if (!trimmed.StartsWith(DpapiMagic, StringComparison.Ordinal))
        {
            return trimmed;
        }

        var b64 = trimmed[DpapiMagic.Length..];
        var cipher = Convert.FromBase64String(b64);
        var plain = Unprotect(cipher);
        return Encoding.UTF8.GetString(plain);
    }

    public static void SaveTokenFile(string path, string token)
    {
        var dir = Path.GetDirectoryName(path);
        if (!string.IsNullOrEmpty(dir))
        {
            Directory.CreateDirectory(dir);
        }

        var content = SealToken(token);
        var tmp = path + ".tmp";
        File.WriteAllText(tmp, content, Encoding.UTF8);
        File.Move(tmp, path, overwrite: true);
    }

    public static string LoadTokenFile(string path)
    {
        if (!File.Exists(path))
        {
            return string.Empty;
        }

        var raw = File.ReadAllText(path, Encoding.UTF8);
        return UnsealToken(raw);
    }

    private static byte[] Protect(byte[] data)
    {
        var inBlob = new DataBlob();
        var outBlob = new DataBlob();
        var handle = GCHandle.Alloc(data, GCHandleType.Pinned);
        try
        {
            inBlob.cbData = (uint)data.Length;
            inBlob.pbData = handle.AddrOfPinnedObject();

            if (!CryptProtectData(ref inBlob, null, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, CryptProtectUiForbidden, ref outBlob))
            {
                throw new InvalidOperationException($"CryptProtectData failed with Win32 error {Marshal.GetLastWin32Error()}");
            }

            var result = new byte[outBlob.cbData];
            Marshal.Copy(outBlob.pbData, result, 0, (int)outBlob.cbData);
            return result;
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

    private static byte[] Unprotect(byte[] cipher)
    {
        var inBlob = new DataBlob();
        var outBlob = new DataBlob();
        var handle = GCHandle.Alloc(cipher, GCHandleType.Pinned);
        try
        {
            inBlob.cbData = (uint)cipher.Length;
            inBlob.pbData = handle.AddrOfPinnedObject();

            if (!CryptUnprotectData(ref inBlob, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, CryptProtectUiForbidden, ref outBlob))
            {
                throw new InvalidOperationException($"CryptUnprotectData failed with Win32 error {Marshal.GetLastWin32Error()}");
            }

            var result = new byte[outBlob.cbData];
            Marshal.Copy(outBlob.pbData, result, 0, (int)outBlob.cbData);
            return result;
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
}
