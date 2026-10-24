param([string]$Python = 'python')

$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
    if (Test-Path -LiteralPath $Python) {
        $Python = (Resolve-Path -LiteralPath $Python).Path
    } else {
        $Python = (Get-Command $Python -ErrorAction Stop).Source
    }
    & $Python -c "import sys,struct; assert sys.platform == 'win32' and sys.version_info[:2] == (3,12) and struct.calcsize('P') == 8, 'Use Windows Python 3.12 x64'"
    if ($LASTEXITCODE -ne 0) { throw 'Unsupported build environment' }

    & $Python -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'Dependency check failed' }
    & $Python -m unittest discover -p 'test_*.py' -v
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed' }

    $buildDir = Join-Path $PSScriptRoot 'build'
    $specDir = Join-Path $buildDir 'spec'
    $distDir = Join-Path $PSScriptRoot 'dist'
    New-Item -ItemType Directory -Path $specDir -Force | Out-Null
    & $Python -m PyInstaller --noconfirm --clean --onedir --windowed `
        --name CodexMonitor --distpath $distDir --workpath (Join-Path $buildDir 'pyinstaller') `
        --specpath $specDir --icon (Join-Path $PSScriptRoot 'app.ico') `
        --add-data "$(Join-Path $PSScriptRoot 'web');web" `
        --add-data "$(Join-Path $PSScriptRoot 'collector.py');." `
        --add-data "$(Join-Path $PSScriptRoot 'app.ico');." `
        (Join-Path $PSScriptRoot 'main.py')
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed' }

    $exe = Join-Path $distDir 'CodexMonitor\CodexMonitor.exe'
    $probe = Start-Process -FilePath $exe -ArgumentList '--self-test' -PassThru -WindowStyle Hidden
    if (!$probe.WaitForExit(30000)) {
        Stop-Process -Id $probe.Id -ErrorAction SilentlyContinue
        throw 'Packaged application self-test timed out'
    }
    if ($probe.ExitCode -ne 0) { throw 'Packaged application self-test failed' }

    & $Python (Join-Path $PSScriptRoot 'scripts\package_release.py')
    if ($LASTEXITCODE -ne 0) { throw 'Release packaging failed' }
    Write-Host 'Done. Release archives and SHA256SUMS.txt are in release/.'
} finally {
    Pop-Location
}
