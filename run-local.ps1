param([switch]$NoBrowser)

$ErrorActionPreference = "Stop"

$projectDir = $PSScriptRoot
$venvDir = Join-Path $projectDir ".venv"
$pythonExe = Join-Path (Join-Path $venvDir "Scripts") "python.exe"

if (-not (Test-Path $pythonExe)) {
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if (-not $pyLauncher) {
        throw "需要先安装 Python 3.11，并确保 py 启动器可用。"
    }
    & $pyLauncher.Source -3.11 -m venv $venvDir
    if ($LASTEXITCODE -ne 0) {
        throw "创建 Python 虚拟环境失败。"
    }
}

$requirements = Join-Path $projectDir "requirements.txt"
if (-not (Test-Path $requirements)) {
    throw "找不到 requirements.txt。"
}

$sitePackages = Join-Path $venvDir "Lib\site-packages"
New-Item -ItemType Directory -Force -Path $sitePackages | Out-Null
& $pythonExe -m pip --isolated install --upgrade --target $sitePackages --disable-pip-version-check -r $requirements
if ($LASTEXITCODE -ne 0) {
    throw "安装 Python 依赖失败。"
}

$dataDir = Join-Path $projectDir "data"
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
$env:NEWSROOM_DATA_DIR = $dataDir
$env:TZ = "Asia/Shanghai"
$env:COOKIE_SECURE = "0"
$env:SETUP_TOKEN = ""

Push-Location $projectDir
try {
    $localArgs = @("-m", "app.local")
    if ($NoBrowser) { $localArgs += "--no-browser" }
    & $pythonExe @localArgs
    if ($LASTEXITCODE -ne 0) {
        throw "讯览进程退出，检查上方日志。"
    }
}
finally {
    Pop-Location
}
