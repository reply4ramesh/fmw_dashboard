[CmdletBinding()]
param(
    [string]$SourceDir = "server-app",
    [string]$OutputDir = "dist"
)

$ErrorActionPreference = "Stop"

$workspaceRoot = Split-Path -Parent $PSScriptRoot
$sourcePath = Join-Path $workspaceRoot $SourceDir
$outputPath = Join-Path $workspaceRoot $OutputDir

if (-not (Test-Path -LiteralPath $sourcePath)) {
    throw "Source directory not found: $sourcePath"
}

$versionFile = Join-Path $sourcePath "VERSION"
if (-not (Test-Path -LiteralPath $versionFile)) {
    throw "VERSION file not found: $versionFile"
}

$version = (Get-Content -LiteralPath $versionFile | Select-Object -First 1).Trim()
if (-not $version) {
    throw "VERSION file is empty: $versionFile"
}

$packageName = "iam-monitoring-$version"
$stagingRoot = Join-Path $workspaceRoot ".package-staging"
$stagingPath = Join-Path $stagingRoot $packageName
$zipPath = Join-Path $outputPath "$packageName.zip"
$tarPath = Join-Path $outputPath "$packageName.tar.gz"

if (Test-Path -LiteralPath $stagingRoot) {
    Remove-Item -LiteralPath $stagingRoot -Recurse -Force
}

New-Item -ItemType Directory -Force -Path $stagingPath | Out-Null
New-Item -ItemType Directory -Force -Path $outputPath | Out-Null

Get-ChildItem -LiteralPath $sourcePath -Force | Copy-Item -Destination $stagingPath -Recurse -Force

$excludedDirectories = @(
    (Join-Path $stagingPath "venv"),
    (Join-Path $stagingPath "state")
)

foreach ($excludedDirectory in $excludedDirectories) {
    if (Test-Path -LiteralPath $excludedDirectory) {
        Remove-Item -LiteralPath $excludedDirectory -Recurse -Force
    }
}

Get-ChildItem -LiteralPath $stagingPath -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force

Get-ChildItem -LiteralPath $stagingPath -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object {
        $_.Extension -in @(".pyc", ".pyo") -or
        $_.Name -like "local-*.err.log" -or
        $_.Name -like "local-*.out.log"
    } |
    Remove-Item -Force

if (Test-Path -LiteralPath $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}

if (Test-Path -LiteralPath $tarPath) {
    Remove-Item -LiteralPath $tarPath -Force
}

Compress-Archive -Path $stagingPath -DestinationPath $zipPath -CompressionLevel Optimal
tar.exe -czf $tarPath -C $stagingRoot $packageName

Remove-Item -LiteralPath $stagingRoot -Recurse -Force

Get-Item -LiteralPath $zipPath, $tarPath | Select-Object FullName, Length, LastWriteTime
