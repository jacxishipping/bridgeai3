<#
.SYNOPSIS
    Release 1 Windows Clean-Image Reboot Soak Harness (PreReboot -> Reboot -> PostReboot -> Uninstall).
.DESCRIPTION
    Automates the Release 1 Windows clean-image reboot soak on a dedicated Windows VM
    with elevated PowerShell privileges. Writes JSON evidence for each phase so the
    final report can be archived with a Release 1 candidate.
.EXAMPLE
    # Phase 1 (before reboot, elevated PowerShell on clean VM):
    .\scripts\windows_reboot_soak.ps1 -Phase PreReboot -ControlUrl https://staging.example -DeviceToken <tok> -DeviceId <id>
    Restart-Computer -Force

    # Phase 2 (after reboot):
    .\scripts\windows_reboot_soak.ps1 -Phase PostReboot -ControlUrl https://staging.example -AdminToken <adm> -DeviceId <id>

    # Phase 3 (clean uninstall & zero-residue check):
    .\scripts\windows_reboot_soak.ps1 -Phase Uninstall
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("PreReboot", "PostReboot", "Uninstall")]
    [string]$Phase,

    [string]$ControlUrl = "",
    [string]$DeviceToken = "",
    [string]$AdminToken = "",
    [string]$DeviceId = "",
    [string]$EvidencePath = "$PSScriptRoot\..\.run\windows_reboot_soak_evidence.json"
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path
$EvidenceDir = Split-Path -Parent $EvidencePath
if ($EvidenceDir -and -not (Test-Path $EvidenceDir)) {
    New-Item -ItemType Directory -Force -Path $EvidenceDir | Out-Null
}

function ConvertTo-PlainHashtable($obj) {
    if ($null -eq $obj) { return $null }
    if ($obj -is [System.Collections.IDictionary]) {
        $ht = @{}
        foreach ($k in $obj.Keys) { $ht[$k] = ConvertTo-PlainHashtable $obj[$k] }
        return $ht
    }
    if ($obj -is [System.Management.Automation.PSCustomObject]) {
        $ht = @{}
        foreach ($p in $obj.PSObject.Properties) { $ht[$p.Name] = ConvertTo-PlainHashtable $p.Value }
        return $ht
    }
    if ($obj -is [System.Collections.IEnumerable] -and -not ($obj -is [string])) {
        return @($obj | ForEach-Object { ConvertTo-PlainHashtable $_ })
    }
    return $obj
}

$Report = if (Test-Path $EvidencePath) {
    ConvertTo-PlainHashtable (Get-Content $EvidencePath -Raw | ConvertFrom-Json)
} else {
    @{
        gate = "release-1-windows-clean-image-reboot-soak"
        host = $env:COMPUTERNAME
        os = [System.Environment]::OSVersion.VersionString
        phases = @{}
    }
}
if (-not $Report.ContainsKey("phases") -or $null -eq $Report.phases) {
    $Report.phases = @{}
}

if ($Phase -eq "PreReboot") {
    if (-not $ControlUrl -or -not $DeviceToken) {
        throw "PreReboot requires -ControlUrl and -DeviceToken."
    }
    $bootBefore = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString("o")
    & powershell -ExecutionPolicy Bypass -File (Join-Path $RepoRoot "deployment\windows\Install-AIBridge.ps1") `
        -ControlUrl $ControlUrl -DeviceToken $DeviceToken -Mode Service -Silent
    $installExit = $LASTEXITCODE
    $taskRaw = (& schtasks /query /tn "AI Bridge Agent" /v /fo LIST 2>&1 | Out-String)
    $taskExists = ($LASTEXITCODE -eq 0)
    $tokenNotInTask = ($taskRaw -notlike "*$DeviceToken*")
    $launcher = Join-Path $env:ProgramData "ai-bridge\run-agent.cmd"
    $aclRaw = if (Test-Path $launcher) { (& icacls $launcher 2>&1 | Out-String) } else { "" }
    $aclLocked = (Test-Path $launcher) -and ($aclRaw -notmatch "BUILTIN\\Users:")

    $Report.phases.pre_reboot = @{
        timestamp_utc = (Get-Date).ToUniversalTime().ToString("o")
        boot_time_before_utc = $bootBefore
        install_exit_code = $installExit
        scheduled_task_exists = $taskExists
        token_absent_from_task_def = $tokenNotInTask
        launcher_acl_locked = $aclLocked
        ok = ($installExit -eq 0 -and $taskExists -and $tokenNotInTask -and $aclLocked)
    }
    $Report | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 $EvidencePath
    Write-Host "PreReboot recorded -> $EvidencePath (ok=$($Report.phases.pre_reboot.ok)). Now run: Restart-Computer -Force"
    if (-not $Report.phases.pre_reboot.ok) { exit 1 }
}

elseif ($Phase -eq "PostReboot") {
    if (-not $ControlUrl -or -not $AdminToken -or -not $DeviceId) {
        throw "PostReboot requires -ControlUrl, -AdminToken, and -DeviceId."
    }
    $bootAfter = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString("o")
    $headers = @{
        "X-Admin-Token" = $AdminToken
        "Content-Type" = "application/json"
        "User-Agent" = "AI-Bridge-RebootSoak/0.3"
    }
    $base = $ControlUrl.TrimEnd("/")

    # Wait up to 90s for post-boot heartbeat
    $dev = $null
    $deadline = (Get-Date).AddSeconds(90)
    while ((Get-Date) -lt $deadline) {
        $devs = Invoke-RestMethod "$base/api/devices" -Headers $headers
        $dev = $devs | Where-Object { $_.id -eq $DeviceId } | Select-Object -First 1
        if ($dev -and $dev.status -eq "online") { break }
        Start-Sleep -Seconds 3
    }

    # Safe Tier-1 task
    $t1 = Invoke-RestMethod -Method Post "$base/api/tasks" -Headers $headers `
        -Body (@{ device_id = $DeviceId; tool = "get_system_info"; args = @{} } | ConvertTo-Json)
    $t1Status = "queued"
    for ($i = 0; $i -lt 30; $i++) {
        $g = Invoke-RestMethod "$base/api/tasks/$($t1.task_id)" -Headers $headers
        $t1Status = $g.status
        if ($t1Status -in @("completed", "failed")) { break }
        Start-Sleep -Seconds 2
    }

    # Tier-3 approval flow
    $t3 = Invoke-RestMethod -Method Post "$base/api/tasks" -Headers $headers `
        -Body (@{ device_id = $DeviceId; tool = "run_shell"; args = @{ command = "echo reboot-soak" } } | ConvertTo-Json)
    $blockedBefore = ($t3.status -eq "needs_approval")
    if ($blockedBefore -and $t3.approval_id) {
        Invoke-RestMethod -Method Post "$base/api/approvals/$($t3.approval_id)/decide" -Headers $headers `
            -Body '{"decision":"approved","reason":"Windows reboot soak"}' | Out-Null
    }
    $t3Status = "needs_approval"
    for ($i = 0; $i -lt 30; $i++) {
        $g3 = Invoke-RestMethod "$base/api/tasks/$($t3.task_id)" -Headers $headers
        $t3Status = $g3.status
        if ($t3Status -in @("completed", "failed")) { break }
        Start-Sleep -Seconds 2
    }

    # Revoke device
    Invoke-RestMethod -Method Post "$base/api/devices/$DeviceId/revoke" -Headers $headers | Out-Null

    $bootChanged = $true
    if ($Report.phases.pre_reboot -and $Report.phases.pre_reboot.boot_time_before_utc) {
        $bootChanged = ($bootAfter -ne $Report.phases.pre_reboot.boot_time_before_utc)
    }

    $Report.phases.post_reboot = @{
        timestamp_utc = (Get-Date).ToUniversalTime().ToString("o")
        boot_time_after_utc = $bootAfter
        boot_time_changed = $bootChanged
        device_online = ($null -ne $dev -and $dev.status -eq "online")
        safe_task_status = $t1Status
        tier3_blocked_before_approval = $blockedBefore
        tier3_status_after_approval = $t3Status
        revoked = $true
        ok = ($bootChanged -and ($null -ne $dev -and $dev.status -eq "online") -and $t1Status -eq "completed" -and $blockedBefore -and $t3Status -in @("completed", "failed"))
    }
    $Report | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 $EvidencePath
    Write-Host "PostReboot recorded -> $EvidencePath (ok=$($Report.phases.post_reboot.ok))."
    if (-not $Report.phases.post_reboot.ok) { exit 1 }
}

elseif ($Phase -eq "Uninstall") {
    & powershell -ExecutionPolicy Bypass -File (Join-Path $RepoRoot "deployment\windows\Uninstall-AIBridge.ps1") -PurgeConfig -Silent
    & powershell -ExecutionPolicy Bypass -File (Join-Path $RepoRoot "deployment\windows\uninstall-agent.ps1")

    & schtasks /query /tn "AI Bridge Agent" 2>$null | Out-Null
    $taskGone = ($LASTEXITCODE -ne 0)
    $progDataGone = -not (Test-Path (Join-Path $env:ProgramData "ai-bridge"))
    $homeConfigGone = -not (Test-Path (Join-Path $HOME ".ai-bridge"))
    $procs = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -like "*agent_main.py*" -or $_.CommandLine -like "*gui_app.py*"
    })
    $noProcs = ($procs.Count -eq 0)

    $Report.phases.uninstall = @{
        timestamp_utc = (Get-Date).ToUniversalTime().ToString("o")
        scheduled_task_removed = $taskGone
        programdata_removed = $progDataGone
        home_config_removed = $homeConfigGone
        no_stray_agent_processes = $noProcs
        ok = ($taskGone -and $progDataGone -and $homeConfigGone -and $noProcs)
    }
    $Report | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 $EvidencePath
    Write-Host "Uninstall recorded -> $EvidencePath (ok=$($Report.phases.uninstall.ok))."
    if (-not $Report.phases.uninstall.ok) { exit 1 }
}
