#Requires -Version 7.0
# Native test entrypoint. Every assigned suite is mandatory; no profile is loaded.
[CmdletBinding()]
param([switch]$CI, [switch]$CheckPrerequisites)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

# Hermetic: the provisioning exports (DOTFILES_AUTO_UPDATE=0 and the like,
# dotfiles docs/bootstrap.md) and other dotfiles knobs never reach the suites
# from the caller; each case sets what it needs. The same list as tests/*.sh.
foreach ($knob in @('DOTFILES_AUTO_UPDATE', 'DOTFILES_AUTO_STOW', 'DOTFILES_HOST', 'DOTFILES_DIR',
        '_DOTFILES_CHECKED', 'DOTFILES_STOW_WITHOUT_OH_MY_ZSH', 'DOTFILES_COLOR',
        'AWESOME_SKILLS_AUTO_UPDATE', 'AWESOME_SKILLS_FORCE', 'AWESOME_SKILLS_BG',
        'AWESOME_SKILLS_INSTALLER_URL', 'AWESOME_SKILLS_REFRESH_DAYS', '_AWESOME_SKILLS_CHECKED')) {
    Remove-Item -LiteralPath "Env:$knob" -ErrorAction Ignore
}

if ($CI -and -not $IsWindows) {
    throw 'The native Windows CI entrypoint requires Windows.'
}

foreach ($dependency in @('git', 'node')) {
    if (-not (Get-Command $dependency -CommandType Application -ErrorAction SilentlyContinue)) {
        throw "Required test dependency not found: $dependency"
    }
}
$repoRoot = Split-Path -Parent $PSScriptRoot
$syncPython = if ($env:DOTFILES_SYNC_PYTHON) { $env:DOTFILES_SYNC_PYTHON }
    elseif ($IsWindows) { Join-Path $repoRoot '.venv-sync/Scripts/python.exe' }
    else { Join-Path $repoRoot '.venv-sync/bin/python' }
if (-not (Test-Path -LiteralPath $syncPython -PathType Leaf)) {
    throw 'AI-sync runtime missing; run setup-sync.ps1 explicitly.'
}
$env:DOTFILES_SYNC_PYTHON = $syncPython
& $syncPython -I -B (Join-Path $repoRoot 'lib/config_sync.py') --runtime-check
if ($LASTEXITCODE -ne 0) { throw 'Configuration runtime check failed.' }
if ($CheckPrerequisites) {
    Write-Output 'test-prerequisites=PASS'
    exit 0
}

Write-Output '==> test_config_sync.py'
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'test_config_sync.py') -PathType Leaf)) {
    throw 'Required configuration backend test suite is missing.'
}
& $syncPython -I -B -m unittest discover -s $PSScriptRoot -p 'test_config_sync.py' -v
if ($LASTEXITCODE -ne 0) { throw 'Configuration backend tests failed.' }

Write-Output '==> claude-customizations.cjs'
& node --test (Join-Path $PSScriptRoot 'claude-customizations.cjs')
if ($LASTEXITCODE -ne 0) { throw 'Claude customization tests failed.' }

Write-Output 'test-suite=PASS'
