using System;
using System.Diagnostics;
using System.IO;
using System.Net.NetworkInformation;
using System.Runtime.InteropServices;
using AIBridgeClient.Models;

namespace AIBridgeClient.Services;

public sealed class SystemMonitorService
{
    private ulong _prevIdle;
    private ulong _prevKernel;
    private ulong _prevUser;
    private bool _hasCpuSample;

    private long _prevBytesSent;
    private long _prevBytesRecv;
    private DateTimeOffset _prevNetTime;
    private bool _hasNetSample;

    [StructLayout(LayoutKind.Sequential)]
    private struct MemoryStatusEx
    {
        public uint dwLength;
        public uint dwMemoryLoad;
        public ulong ullTotalPhys;
        public ulong ullAvailPhys;
        public ulong ullTotalPageFile;
        public ulong ullAvailPageFile;
        public ulong ullTotalVirtual;
        public ulong ullAvailVirtual;
        public ulong ullAvailExtendedVirtual;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct FileTime
    {
        public uint dwLowDateTime;
        public uint dwHighDateTime;

        public ulong ToUInt64() => ((ulong)dwHighDateTime << 32) | dwLowDateTime;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GlobalMemoryStatusEx(ref MemoryStatusEx lpBuffer);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetSystemTimes(out FileTime lpIdleTime, out FileTime lpKernelTime, out FileTime lpUserTime);

    public HardwareTelemetry Sample()
    {
        var cpu = SampleCpuPercent();

        var memStatus = new MemoryStatusEx { dwLength = (uint)Marshal.SizeOf<MemoryStatusEx>() };
        GlobalMemoryStatusEx(ref memStatus);
        var memTotalGb = memStatus.ullTotalPhys / (1024.0 * 1024.0 * 1024.0);
        var memUsedGb = (memStatus.ullTotalPhys - memStatus.ullAvailPhys) / (1024.0 * 1024.0 * 1024.0);
        var memPct = memStatus.dwMemoryLoad;

        double diskTotalGb = 0;
        double diskUsedGb = 0;
        double diskPct = 0;
        try
        {
            var sysRoot = Path.GetPathRoot(Environment.SystemDirectory) ?? "C:\\";
            var drive = new DriveInfo(sysRoot);
            if (drive.IsReady && drive.TotalSize > 0)
            {
                diskTotalGb = drive.TotalSize / (1024.0 * 1024.0 * 1024.0);
                diskUsedGb = (drive.TotalSize - drive.AvailableFreeSpace) / (1024.0 * 1024.0 * 1024.0);
                diskPct = Math.Round((diskUsedGb / diskTotalGb) * 100.0, 1);
            }
        }
        catch
        {
            // Ignore drive access errors
        }

        var (sentKbps, recvKbps) = SampleNetworkKbps();
        var procCount = Process.GetProcesses().Length;

        return new HardwareTelemetry
        {
            CpuPercent = Math.Round(cpu, 1),
            MemoryPercent = memPct,
            MemoryUsedGb = Math.Round(memUsedGb, 2),
            MemoryTotalGb = Math.Round(memTotalGb, 2),
            DiskPercent = diskPct,
            DiskUsedGb = Math.Round(diskUsedGb, 1),
            DiskTotalGb = Math.Round(diskTotalGb, 1),
            NetworkSentKbps = Math.Round(sentKbps, 1),
            NetworkRecvKbps = Math.Round(recvKbps, 1),
            ProcessCount = procCount,
            SystemUptime = TimeSpan.FromMilliseconds(Environment.TickCount64),
        };
    }

    private double SampleCpuPercent()
    {
        if (!GetSystemTimes(out var idle, out var kernel, out var user))
        {
            return 0;
        }

        var curIdle = idle.ToUInt64();
        var curKernel = kernel.ToUInt64();
        var curUser = user.ToUInt64();

        if (!_hasCpuSample)
        {
            _prevIdle = curIdle;
            _prevKernel = curKernel;
            _prevUser = curUser;
            _hasCpuSample = true;
            return 0;
        }

        var idleDiff = curIdle - _prevIdle;
        var totalDiff = (curKernel - _prevKernel) + (curUser - _prevUser);

        _prevIdle = curIdle;
        _prevKernel = curKernel;
        _prevUser = curUser;

        if (totalDiff == 0)
        {
            return 0;
        }

        var busy = totalDiff > idleDiff ? (double)(totalDiff - idleDiff) : 0.0;
        return Math.Clamp((busy / totalDiff) * 100.0, 0.0, 100.0);
    }

    private (double SentKbps, double RecvKbps) SampleNetworkKbps()
    {
        long totalSent = 0;
        long totalRecv = 0;
        try
        {
            foreach (var ni in NetworkInterface.GetAllNetworkInterfaces())
            {
                if (ni.OperationalStatus != OperationalStatus.Up ||
                    ni.NetworkInterfaceType == NetworkInterfaceType.Loopback)
                {
                    continue;
                }
                var stats = ni.GetIPv4Statistics();
                totalSent += stats.BytesSent;
                totalRecv += stats.BytesReceived;
            }
        }
        catch
        {
            return (0, 0);
        }

        var now = DateTimeOffset.UtcNow;
        if (!_hasNetSample)
        {
            _prevBytesSent = totalSent;
            _prevBytesRecv = totalRecv;
            _prevNetTime = now;
            _hasNetSample = true;
            return (0, 0);
        }

        var elapsed = (now - _prevNetTime).TotalSeconds;
        if (elapsed <= 0.05)
        {
            return (0, 0);
        }

        var sentKbps = Math.Max(0, (totalSent - _prevBytesSent) / 1024.0 / elapsed);
        var recvKbps = Math.Max(0, (totalRecv - _prevBytesRecv) / 1024.0 / elapsed);

        _prevBytesSent = totalSent;
        _prevBytesRecv = totalRecv;
        _prevNetTime = now;

        return (sentKbps, recvKbps);
    }
}
