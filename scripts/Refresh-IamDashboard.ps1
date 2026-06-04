[CmdletBinding()]
param(
    [string]$ConfigPath = "",
    [string]$OutputPath = "",
    [switch]$OpenDashboard
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$plinkPath = "C:\Program Files\PuTTY\plink.exe"
$projectRoot = Split-Path -Path $PSScriptRoot -Parent

if (-not $ConfigPath) {
    $ConfigPath = Join-Path $projectRoot "config\targets.local.json"
}

if (-not $OutputPath) {
    $OutputPath = Join-Path $projectRoot "data\dashboard-data.js"
}

if (-not (Test-Path -LiteralPath $plinkPath)) {
    throw "PuTTY plink was not found at '$plinkPath'."
}

if (-not (Test-Path -LiteralPath $ConfigPath)) {
    throw "Config file not found: $ConfigPath"
}

$config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json

function Get-Percent {
    param(
        [double]$Part,
        [double]$Whole
    )

    if ($Whole -le 0) {
        return 0
    }

    return [Math]::Round(($Part / $Whole) * 100, 1)
}

function Invoke-TargetCommand {
    param(
        [Parameter(Mandatory)]
        [pscustomobject]$Target,
        [Parameter(Mandatory)]
        [string]$Command
    )

    $arguments = @(
        "-ssh",
        "-l", $Target.username,
        "-pw", $Target.password,
        "-batch",
        "-no-antispoof"
    )

    foreach ($hostKey in $Target.hostKeys) {
        $arguments += @("-hostkey", $hostKey)
    }

    $arguments += @($Target.host, $Command)

    $output = & $plinkPath @arguments 2>&1
    $exitCode = $LASTEXITCODE

    return [pscustomobject]@{
        ExitCode = $exitCode
        Output   = (($output | ForEach-Object { "$_" }) -join "`n").Trim()
    }
}

function Get-LastNonEmptyLine {
    param([string]$Text)

    $lines = @($Text -split "`r?`n" | Where-Object { $_.Trim() })
    if ($lines.Count -eq 0) {
        return $null
    }

    return $lines[-1]
}

function Parse-MemoryMetrics {
    param([string]$Text)

    $line = ($Text -split "`r?`n" | Where-Object { $_ -match "^\s*Mem:" } | Select-Object -First 1)
    if (-not $line) {
        return $null
    }

    $parts = ($line -replace "^\s+", "") -split "\s+"
    if ($parts.Count -lt 7) {
        return $null
    }

    $totalMb = [int]$parts[1]
    $usedMb = [int]$parts[2]
    $freeMb = [int]$parts[3]
    $availableMb = [int]$parts[6]

    return [ordered]@{
        totalMb      = $totalMb
        usedMb       = $usedMb
        freeMb       = $freeMb
        availableMb  = $availableMb
        usedPercent  = Get-Percent -Part $usedMb -Whole $totalMb
    }
}

function Parse-DiskMetrics {
    param([string]$Text)

    $line = Get-LastNonEmptyLine -Text $Text
    if (-not $line) {
        return $null
    }

    $parts = ($line -replace "^\s+", "") -split "\s+"
    if ($parts.Count -lt 6) {
        return $null
    }

    return [ordered]@{
        filesystem   = $parts[0]
        size         = $parts[1]
        used         = $parts[2]
        available    = $parts[3]
        usedPercent  = [double]($parts[4] -replace "%", "")
        mount        = $parts[5]
    }
}

function Parse-UptimeMetrics {
    param(
        [string]$Text,
        [int]$CpuCount
    )

    $load1 = 0.0
    $load5 = 0.0
    $load15 = 0.0

    if ($Text -match "load average:\s*([0-9.]+),\s*([0-9.]+),\s*([0-9.]+)") {
        $load1 = [double]$matches[1]
        $load5 = [double]$matches[2]
        $load15 = [double]$matches[3]
    }

    $cpuPressure = if ($CpuCount -gt 0) {
        Get-Percent -Part $load1 -Whole $CpuCount
    }
    else {
        0
    }

    return [ordered]@{
        raw         = $Text
        load1       = $load1
        load5       = $load5
        load15      = $load15
        cpuCount    = $CpuCount
        cpuPressure = $cpuPressure
    }
}

function Get-ServerSnapshot {
    param([pscustomobject]$Target)

    $hostnameResult = Invoke-TargetCommand -Target $Target -Command "hostname"
    if ($hostnameResult.ExitCode -ne 0) {
        return [ordered]@{
            reachable      = $false
            status         = "down"
            actualHostname = $null
            error          = if ($hostnameResult.Output) { $hostnameResult.Output } else { "SSH connection failed." }
        }
    }

    $kernelResult = Invoke-TargetCommand -Target $Target -Command "uname -r"
    $osResult = Invoke-TargetCommand -Target $Target -Command "grep '^PRETTY_NAME=' /etc/os-release | cut -d= -f2-"
    $cpuResult = Invoke-TargetCommand -Target $Target -Command "nproc"
    $uptimeResult = Invoke-TargetCommand -Target $Target -Command "uptime"
    $memoryResult = Invoke-TargetCommand -Target $Target -Command "free -m"
    $rootDiskResult = Invoke-TargetCommand -Target $Target -Command "df -P -h /"
    $refreshDiskResult = Invoke-TargetCommand -Target $Target -Command "if [ -d /refresh ]; then df -P -h /refresh; fi"

    $actualHostname = $hostnameResult.Output.Trim()
    $cpuCount = 0
    if ($cpuResult.Output -match "^\d+$") {
        $cpuCount = [int]$cpuResult.Output
    }

    $scripts = @()
    if ($Target.scriptDirectory) {
        $escapedDirectory = $Target.scriptDirectory
        $scriptCommand = "if [ -d '$escapedDirectory' ]; then ls '$escapedDirectory' | head -n 12; fi"
        $scriptResult = Invoke-TargetCommand -Target $Target -Command $scriptCommand
        if ($scriptResult.Output) {
            $scripts = @($scriptResult.Output -split "`r?`n" | Where-Object { $_.Trim() })
        }
    }

    $processes = @()
    if ($Target.processMatchers -and @($Target.processMatchers).Count -gt 0) {
        $pattern = (@($Target.processMatchers) | ForEach-Object { $_.Trim() } | Where-Object { $_ }) -join "|"
        if ($pattern) {
            $processResult = Invoke-TargetCommand -Target $Target -Command ("ps -eo user=,pid=,comm=,args= --sort=user | egrep -i '{0}' | grep -v egrep | head -n 10" -f $pattern)
            if ($processResult.Output) {
                $processes = @($processResult.Output -split "`r?`n" | Where-Object { $_.Trim() })
            }
        }
    }

    return [ordered]@{
        reachable      = $true
        status         = "healthy"
        actualHostname = $actualHostname
        kernel         = $kernelResult.Output
        os             = ($osResult.Output -replace '^"|"$', "")
        uptime         = Parse-UptimeMetrics -Text $uptimeResult.Output -CpuCount $cpuCount
        memory         = Parse-MemoryMetrics -Text $memoryResult.Output
        rootDisk       = Parse-DiskMetrics -Text $rootDiskResult.Output
        refreshDisk    = Parse-DiskMetrics -Text $refreshDiskResult.Output
        scriptDirectory = $Target.scriptDirectory
        scripts        = $scripts
        processes      = $processes
    }
}

function Get-AppCheckResult {
    param(
        [pscustomobject]$Target,
        [pscustomobject]$Check
    )

    $escapedUrl = $Check.url
    $command = "if command -v curl >/dev/null 2>&1; then curl -k -L -s -o /dev/null -w '%{http_code} %{time_total}' '$escapedUrl'; else echo NO_CURL; fi"
    $result = Invoke-TargetCommand -Target $Target -Command $command

    $status = "down"
    $statusText = "No response"
    $httpCode = $null
    $responseTimeMs = $null

    if ($result.Output -match "^(\d{3})\s+([0-9.]+)$") {
        $httpCode = [int]$matches[1]
        $responseTimeMs = [Math]::Round(([double]$matches[2]) * 1000, 0)

        if ($httpCode -ge 200 -and $httpCode -lt 400) {
            $status = "healthy"
            $statusText = "Reachable"
        }
        elseif ($httpCode -eq 401 -or $httpCode -eq 403) {
            $status = "warning"
            $statusText = "Responding with authentication gate"
        }
        elseif ($httpCode -eq 0) {
            $status = "down"
            $statusText = "Connection failed or service is not listening"
        }
        else {
            $status = "down"
            $statusText = "HTTP $httpCode"
        }
    }
    elseif ($result.Output -eq "NO_CURL") {
        $statusText = "curl not available on remote host"
    }
    elseif ($result.Output) {
        $statusText = $result.Output
    }

    return [ordered]@{
        name           = $Check.name
        url            = $Check.url
        status         = $status
        statusText     = $statusText
        httpCode       = $httpCode
        responseTimeMs = $responseTimeMs
    }
}

function Get-TargetStatus {
    param(
        [hashtable]$Server,
        [object[]]$AppChecks
    )

    $AppChecks = @($AppChecks)

    if (-not $Server.reachable) {
        return "down"
    }

    if ($AppChecks.Count -eq 0) {
        return "healthy"
    }

    $healthy = @($AppChecks | Where-Object { $_.status -eq "healthy" }).Count
    $warning = @($AppChecks | Where-Object { $_.status -eq "warning" }).Count

    if ($healthy -eq $AppChecks.Count) {
        return "healthy"
    }

    if ($healthy -gt 0 -or $warning -gt 0) {
        return "warning"
    }

    return "down"
}

$targets = @()

foreach ($target in $config.targets) {
    $server = Get-ServerSnapshot -Target $target
    $appChecks = @()

    foreach ($check in @($target.appChecks)) {
        $appChecks += [pscustomobject](Get-AppCheckResult -Target $target -Check $check)
    }

    $targetStatus = Get-TargetStatus -Server $server -AppChecks $appChecks
    if ($server.reachable) {
        $server.status = $targetStatus
    }

    $targets += [pscustomobject]@{
        name        = $target.name
        role        = $target.role
        host        = $target.host
        status      = $targetStatus
        server      = [pscustomobject]$server
        appChecks   = $appChecks
    }
}

$healthyTargets = @($targets | Where-Object { $_.status -eq "healthy" }).Count
$warningTargets = @($targets | Where-Object { $_.status -eq "warning" }).Count
$downTargets = @($targets | Where-Object { $_.status -eq "down" }).Count
$allAppChecks = @($targets | ForEach-Object { $_.appChecks })
$healthyApps = @($allAppChecks | Where-Object { $_.status -eq "healthy" }).Count
$warningApps = @($allAppChecks | Where-Object { $_.status -eq "warning" }).Count
$downApps = @($allAppChecks | Where-Object { $_.status -eq "down" }).Count

$dashboardData = [ordered]@{
    title            = $config.dashboardTitle
    generatedAt      = (Get-Date).ToString("o")
    generatedAtLocal = (Get-Date).ToString("dddd, MMMM d yyyy HH:mm:ss zzz")
    notes            = @($config.notes)
    summary          = [ordered]@{
        totalTargets   = $targets.Count
        healthyTargets = $healthyTargets
        warningTargets = $warningTargets
        downTargets    = $downTargets
        totalApps      = $allAppChecks.Count
        healthyApps    = $healthyApps
        warningApps    = $warningApps
        downApps       = $downApps
    }
    targets          = $targets
}

$json = $dashboardData | ConvertTo-Json -Depth 10
$directory = Split-Path -Path $OutputPath -Parent
if (-not (Test-Path -LiteralPath $directory)) {
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
}

$content = "window.IAM_DASHBOARD_DATA = $json;"
Set-Content -LiteralPath $OutputPath -Value $content -Encoding UTF8

Write-Host "Dashboard data refreshed:"
Write-Host "  Config : $ConfigPath"
Write-Host "  Output : $OutputPath"
Write-Host "  Time   : $($dashboardData.generatedAtLocal)"

if ($OpenDashboard) {
    Start-Process (Join-Path $PSScriptRoot "..\index.html")
}
