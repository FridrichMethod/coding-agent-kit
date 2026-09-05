#Requires -Version 7.0
<# Explicit checkout-local setup; never invoked by automatic update or login. #>
[CmdletBinding()]
param([string]$Python = 'python')

$ErrorActionPreference = 'Stop'
$SyncVenv = Join-Path $PSScriptRoot '.venv-sync'
$SyncVenvItem = Get-Item -LiteralPath $SyncVenv -Force -ErrorAction SilentlyContinue
if ($null -ne $SyncVenvItem -and ($SyncVenvItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw "Refusing a symlinked or junction virtual environment: $SyncVenv"
}
& $Python -I -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "Python 3.11 or newer is required")'
if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 or newer is required.' }
if ((Test-Path -LiteralPath $SyncVenv) -and -not (Test-Path -LiteralPath (Join-Path $SyncVenv 'pyvenv.cfg') -PathType Leaf)) {
    throw "Refusing to reuse a non-venv directory: $SyncVenv"
}
& $Python -I -m venv $SyncVenv
if ($LASTEXITCODE -ne 0) { throw 'Could not create the AI-sync virtual environment.' }
$SyncRuntime = if ($IsWindows) { Join-Path $SyncVenv 'Scripts/python.exe' } else { Join-Path $SyncVenv 'bin/python' }
& $SyncRuntime -I -m pip install --disable-pip-version-check --require-hashes --only-binary=:all: --no-deps -r (Join-Path $PSScriptRoot 'requirements-sync.txt')
if ($LASTEXITCODE -ne 0) { throw 'Could not install the pinned AI-sync dependency.' }
& $SyncRuntime -I -B (Join-Path $PSScriptRoot 'lib/config_sync.py') --runtime-check
if ($LASTEXITCODE -ne 0) { throw 'AI-sync runtime validation failed.' }
Write-Host "AI-sync runtime ready in $SyncVenv; no shell activation is needed."
