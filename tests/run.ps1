#Requires -Version 7.0
# Native test entrypoint. Every assigned suite is mandatory; no profile is loaded.
# It runs the Node helper suite and the managed install.ps1 suite.
[CmdletBinding()]
param([switch]$CI, [switch]$CheckPrerequisites)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

# Hermetic: a caller's kit configuration, colour choice and XDG directories
# never reach the suites; each case sets what it needs. The same names, in
# the same order, as the unset statement of tests/*.sh.
foreach ($knob in @('CODING_AGENT_KIT_CONFIG', 'CODING_AGENT_KIT_COLOR', 'CODING_AGENT_KIT_USAGE_FILE', 'CODING_AGENT_KIT_TOKEN_ID', 'CODING_AGENT_KIT_CLAUDE_PID', 'CODING_AGENT_KIT_REPO_URL', 'XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'NO_COLOR')) {
    Remove-Item -LiteralPath "Env:$knob" -ErrorAction Ignore
}

if ($CI -and -not $IsWindows) {
    throw 'The native Windows CI entrypoint requires Windows.'
}

foreach ($dependency in @('git', 'node', 'pwsh')) {
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

# The managed install.ps1 suite changes its own process's environment, so it
# runs in a pwsh of its own.
Write-Output '==> managed-install.ps1'
& pwsh -NoProfile -NonInteractive -File (Join-Path $PSScriptRoot 'managed-install.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Managed install.ps1 tests failed.' }

Write-Output 'test-suite=PASS'
