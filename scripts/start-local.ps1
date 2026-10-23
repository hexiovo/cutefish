$ErrorActionPreference = 'Stop'

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$mainPath = Join-Path $projectRoot 'src\cutefish\main.py'
$hostConfig = Join-Path (Split-Path $projectRoot -Parent) 'manage\host.local.json'
$dataDir = Join-Path $projectRoot 'runtime_data'

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw '未找到本项目虚拟环境。请先在源码目录运行：uv sync --extra dev'
}
if (-not (Test-Path -LiteralPath $mainPath -PathType Leaf)) {
    throw 'CuteFish 服务入口尚未准备好。'
}
if (-not (Test-Path -LiteralPath $hostConfig -PathType Leaf)) {
    throw "未找到本机配置：$hostConfig"
}

New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
$hadDataDir = Test-Path Env:CUTEFISH_DATA_DIR
$oldDataDir = $env:CUTEFISH_DATA_DIR
$hadHostConfig = Test-Path Env:CUTEFISH_HOST_CONFIG
$oldHostConfig = $env:CUTEFISH_HOST_CONFIG
$serverExitCode = 0

$env:CUTEFISH_DATA_DIR = $dataDir
$env:CUTEFISH_HOST_CONFIG = $hostConfig
Push-Location $projectRoot
try {
    & $pythonPath -m uvicorn cutefish.main:app --host 127.0.0.1 --port 8000 --workers 1
    $serverExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
    if ($hadDataDir) { $env:CUTEFISH_DATA_DIR = $oldDataDir } else { Remove-Item Env:CUTEFISH_DATA_DIR -ErrorAction SilentlyContinue }
    if ($hadHostConfig) { $env:CUTEFISH_HOST_CONFIG = $oldHostConfig } else { Remove-Item Env:CUTEFISH_HOST_CONFIG -ErrorAction SilentlyContinue }
}

exit $serverExitCode
