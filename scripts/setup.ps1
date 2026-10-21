[CmdletBinding()]
param(
    [string]$Python,
    [switch]$WithDownloads,
    [switch]$SkipChecks
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'

function Invoke-Checked {
    param([string]$Executable, [string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "El comando fallo (codigo $LASTEXITCODE): $Executable $($Arguments -join ' ')"
    }
}

Push-Location -LiteralPath $projectRoot
try {
    if (-not (Test-Path -LiteralPath $venvPython)) {
        $candidates = @()
        if ($Python) {
            $candidates += @{ File = $Python; Prefix = @() }
        } else {
            foreach ($name in @('py', 'python', 'python3')) {
                $command = Get-Command $name -ErrorAction SilentlyContinue
                if ($command) {
                    $prefix = if ($name -eq 'py') { @('-3.12') } else { @() }
                    $candidates += @{ File = $command.Source; Prefix = $prefix }
                }
            }
        }
        $selected = $null
        foreach ($candidate in $candidates) {
            try {
                $probeArgs = @($candidate.Prefix) + @('-c', 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)')
                & $candidate.File @probeArgs 2>$null
                if ($LASTEXITCODE -eq 0) { $selected = $candidate; break }
            } catch { }
        }
        if (-not $selected) {
            throw 'Se necesita Python 3.12 para el entorno fijado. Ejecuta scripts/setup.ps1 -Python C:\ruta\python.exe'
        }
        Invoke-Checked -Executable $selected.File -Arguments (@($selected.Prefix) + @('-m', 'venv', '.venv'))
    }
    Invoke-Checked -Executable $venvPython -Arguments @('-c', 'import sys; assert sys.version_info[:2] == (3, 12), "El entorno fijado requiere Python 3.12"')
    $lockFile = if ($WithDownloads) { 'requirements-download.lock' } else { 'requirements.lock' }
    Invoke-Checked -Executable $venvPython -Arguments @('-m', 'pip', 'install', '--disable-pip-version-check', '-r', $lockFile)
    Invoke-Checked -Executable $venvPython -Arguments @('-m', 'pip', 'install', '--no-deps', '--no-build-isolation', '-e', '.')
    Invoke-Checked -Executable $venvPython -Arguments @('-m', 'pip', 'check')
    Invoke-Checked -Executable $venvPython -Arguments @('-m', 'scripts.data_baseline')
    if (-not $SkipChecks) {
        Invoke-Checked -Executable $venvPython -Arguments @('-m', 'scripts.check', '--suite', 'all', '--report', 'artifacts/preparation/checks.json')
    }
    Write-Host 'Entorno preparado. Python local: .venv\Scripts\python.exe'
} finally {
    Pop-Location
}
