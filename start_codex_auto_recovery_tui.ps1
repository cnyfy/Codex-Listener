param(
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
$scriptPath = Join-Path $PSScriptRoot 'codex_auto_recovery_tui.py'
$vendorPath = Join-Path $PSScriptRoot 'vendor'
$downloadUrl = 'https://www.python.org/downloads/windows/'

function Stop-WithPythonGuide {
    param([string]$Reason)
    Write-Host ''
    Write-Host 'Codex 自动恢复控制台 V1 需要 Python 3.10 或更高版本。' -ForegroundColor Yellow
    Write-Host $Reason -ForegroundColor Yellow
    Write-Host '即将打开 Python 官方下载页面。安装时请勾选 Add Python to PATH。' -ForegroundColor Cyan
    Start-Process $downloadUrl
    Read-Host '安装完成后请关闭此窗口，再重新启动。按 Enter 退出'
    exit 2
}

if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
    Write-Error 'TUI 文件不存在，压缩包可能不完整。'
    exit 2
}
if (-not (Test-Path -LiteralPath (Join-Path $vendorPath 'textual') -PathType Container)) {
    Write-Error 'TUI 依赖不存在，压缩包可能不完整。'
    exit 2
}

$pythonPath = $null
$useLauncher = $false
$pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
if ($null -ne $pythonCommand) {
    if ($pythonCommand.Path) { $pythonPath = [string]$pythonCommand.Path } else { $pythonPath = [string]$pythonCommand.Source }
}
if ($null -eq $pythonPath) {
    $pyCommand = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($null -ne $pyCommand) {
        $useLauncher = $true
        if ($pyCommand.Path) { $pythonPath = [string]$pyCommand.Path } else { $pythonPath = [string]$pyCommand.Source }
    }
}
if ($null -eq $pythonPath) {
    Stop-WithPythonGuide '当前电脑没有找到 Python。'
}

if ($useLauncher) { $versionOutput = & py.exe -3 --version 2>&1 } else { $versionOutput = & python.exe --version 2>&1 }
if ($LASTEXITCODE -ne 0) {
    if ($useLauncher) { $versionOutput = & py.exe --version 2>&1 } else { $versionOutput = & python.exe --version 2>&1 }
}
$version = $null
if ($versionOutput -match '(\d+)\.(\d+)(?:\.(\d+))?') {
    $patch = if ($Matches[3]) { $Matches[3] } else { '0' }
    $version = [version]("{0}.{1}.{2}" -f $Matches[1], $Matches[2], $patch)
}
if ($null -eq $version -or $version -lt [version]'3.10.0') {
    Stop-WithPythonGuide ("找到的 Python 版本不可用：{0}" -f $versionOutput)
}
if ($CheckOnly) {
    Write-Output ("BOOTSTRAP_OK Version=V1 Python={0} Vendor=Included" -f $version)
    exit 0
}

$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = if ($env:PYTHONPATH) { $vendorPath + ';' + $PSScriptRoot + ';' + $env:PYTHONPATH } else { $vendorPath + ';' + $PSScriptRoot }
if ($useLauncher) { & py.exe -X utf8 $scriptPath @args } else { & python.exe -X utf8 $scriptPath @args }
exit $LASTEXITCODE
