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
if ($CheckPrerequisites) {
    Write-Output 'test-prerequisites=PASS'
    exit 0
}

Write-Output '==> claude-customizations.cjs'
& node --test (Join-Path $PSScriptRoot 'claude-customizations.cjs')
if ($LASTEXITCODE -ne 0) { throw 'Claude customization tests failed.' }

Write-Output 'test-suite=PASS'
