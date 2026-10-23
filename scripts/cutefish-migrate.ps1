param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('validate', 'import-staging')]
    [string]$Command,

    [Parameter(Mandatory = $true)]
    [string]$BundleRelativePath,

    [string]$TargetRelativePath
)

$ErrorActionPreference = 'Stop'

if ($Command -eq 'import-staging' -and [string]::IsNullOrWhiteSpace($TargetRelativePath)) {
    throw 'import-staging 需要提供 -TargetRelativePath。'
}
if ($Command -eq 'validate' -and -not [string]::IsNullOrWhiteSpace($TargetRelativePath)) {
    throw 'validate 不接受 -TargetRelativePath。'
}

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$hostConfig = Join-Path (Split-Path $projectRoot -Parent) 'manage\host.local.json'
$dataDir = Join-Path $projectRoot 'runtime_data'

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw '未找到本项目虚拟环境；请先按项目 README 准备依赖。'
}
if (-not (Test-Path -LiteralPath $hostConfig -PathType Leaf)) {
    throw "未找到本机配置：$hostConfig"
}

$hadDataDir = Test-Path Env:CUTEFISH_DATA_DIR
$oldDataDir = $env:CUTEFISH_DATA_DIR
$hadHostConfig = Test-Path Env:CUTEFISH_HOST_CONFIG
$oldHostConfig = $env:CUTEFISH_HOST_CONFIG
$cliArgs = @($Command, '--bundle-relative-path', $BundleRelativePath)
if ($Command -eq 'import-staging') {
    $cliArgs += @('--target-relative-path', $TargetRelativePath)
}

$exitCode = 0
$env:CUTEFISH_DATA_DIR = $dataDir
$env:CUTEFISH_HOST_CONFIG = $hostConfig
Push-Location $projectRoot
try {
    & $pythonPath -m cutefish.migration.cli @cliArgs
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
    if ($hadDataDir) { $env:CUTEFISH_DATA_DIR = $oldDataDir } else { Remove-Item Env:CUTEFISH_DATA_DIR -ErrorAction SilentlyContinue }
    if ($hadHostConfig) { $env:CUTEFISH_HOST_CONFIG = $oldHostConfig } else { Remove-Item Env:CUTEFISH_HOST_CONFIG -ErrorAction SilentlyContinue }
}

exit $exitCode
