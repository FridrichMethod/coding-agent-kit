#Requires -Version 7.0
# Managed installs with install.ps1 from a local repository with release tags
# and no network: the bootstrap, channels, -Update, the lock, refusals,
# -Uninstall and -Purge, with a temporary USERPROFILE (HOME off Windows) and
# LOCALAPPDATA. run.ps1 starts it in a process of its own.

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
Set-StrictMode -Version 3.0

$Repository = Split-Path -Parent $PSScriptRoot
# The runner's own temporary directory has no 8.3 short name in its path.
$Base = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { [IO.Path]::GetTempPath() }
$Temp = [IO.Path]::Combine($Base, "kit-managed-install-$PID")
$Source = Join-Path $Temp 'source'
$Remote = Join-Path $Temp 'remote.git'
$Url = ([Uri]$Remote).AbsoluteUri
$Out = Join-Path $Temp 'out'
$Log = Join-Path $Temp 'release.log'
$Boot = [IO.Path]::Combine($Temp, 'download', 'install.ps1')
$HomeName = if ($IsWindows) { 'USERPROFILE' } else { 'HOME' }
$Helpers = @('statusline', 'notify', 'check-git-hooks')

function Fail-Test([string]$Message) {
    throw "managed-install.ps1: $Message"
}

function Invoke-Fixture {
    $ErrorActionPreference = 'Continue'
    $output = & git -c init.defaultBranch=main -c user.name=fixture -c user.email=fixture@example.invalid @args 2>&1
    if ($LASTEXITCODE -ne 0) { Fail-Test "git $($args -join ' '): $($output -join ' ')" }
}

# Publish TAG [CHANGE]: commit the kit under test as release TAG and push it,
# with a line in its install.ps1 that logs which release's code ran and how;
# CHANGE appends a comment to helpers/notify.cjs, so that helper differs.
function Publish([string]$Tag, [string]$Change = '') {
    Get-ChildItem -LiteralPath $Source -Force | Where-Object Name -NE '.git' | Remove-Item -Recurse -Force
    foreach ($directory in @('bin', 'lib', 'completions', 'helpers', 'docs')) {
        Copy-Item -LiteralPath (Join-Path $Repository $directory) -Destination $Source -Recurse
    }
    Copy-Item -LiteralPath (Join-Path $Repository 'install.sh') -Destination $Source
    $lines = [IO.File]::ReadAllText((Join-Path $Repository 'install.ps1')).Split("`n")
    $marker = "if (`$env:KIT_FIXTURE_LOG) { Add-Content -LiteralPath `$env:KIT_FIXTURE_LOG -Value ('$Tag ' + (`$args -join ' ')).TrimEnd() }"
    [IO.File]::WriteAllText((Join-Path $Source 'install.ps1'), ((@($lines[0], $marker) + $lines[1..($lines.Count - 1)]) -join "`n"))
    if ($Change) {
        $notify = [IO.Path]::Combine($Source, 'helpers', 'notify.cjs')
        [IO.File]::AppendAllText($notify, "// $Change`n")
    }
    Invoke-Fixture -C $Source add -A
    Invoke-Fixture -C $Source commit -q -m "release $Tag"
    Invoke-Fixture -C $Source tag -a -m $Tag $Tag
    Invoke-Fixture -C $Source push -q $Remote main "refs/tags/$Tag"
}

# Invoke-Installer LABEL SCRIPT ARGS...: run SCRIPT in a new pwsh; the
# result has its exit code and its stdout and stderr lines.
function Invoke-Installer([string]$Label, [string]$Script) {
    $stdout = Join-Path $Out "$Label.out"
    $stderr = Join-Path $Out "$Label.err"
    & pwsh -NoProfile -NonInteractive -File $Script @args >$stdout 2>$stderr
    $code = $LASTEXITCODE
    return [pscustomobject]@{
        Label = $Label
        Code = $code
        Out = @(Get-Content -LiteralPath $stdout)
        Err = @(Get-Content -LiteralPath $stderr)
    }
}

function Assert-Result($Result, [int]$Code, [string[]]$Out, [string[]]$Err = @()) {
    $actualOut = $Result.Out -join "`n"
    $actualErr = $Result.Err -join "`n"
    if ($Result.Code -ne $Code -or $actualOut -cne ($Out -join "`n") -or $actualErr -cne ($Err -join "`n")) {
        Fail-Test ("$($Result.Label): exit $($Result.Code), expected $Code`n--- stdout`n$actualOut`n--- expected`n" +
            "$($Out -join "`n")`n--- stderr`n$actualErr`n--- expected`n$($Err -join "`n")")
    }
}

function Assert-Logged([string[]]$Lines) {
    $actual = if (Test-Path -LiteralPath $Log) { @(Get-Content -LiteralPath $Log) } else { @() }
    if (($actual -join "`n") -cne ($Lines -join "`n")) {
        Fail-Test "release log:`n$($actual -join "`n")`nexpected:`n$($Lines -join "`n")"
    }
    Set-Content -LiteralPath $Log -Value $null
}

# Get-Snapshot DIR: every path below DIR with its size and write time.
function Get-Snapshot([string]$Directory) {
    if (-not (Test-Path -LiteralPath $Directory)) { return '' }
    $items = @(Get-Item -LiteralPath $Directory -Force) + @(Get-ChildItem -LiteralPath $Directory -Recurse -Force)
    return ($items | Sort-Object FullName | ForEach-Object {
            $size = if ($_.PSIsContainer) { '' } else { $_.Length }
            "$($_.FullName) $size $($_.LastWriteTimeUtc.Ticks)"
        }) -join "`n"
}

function Get-Hash([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
}

# New-Home NAME: fresh home and LOCALAPPDATA for the installer processes.
function New-Home([string]$Name) {
    $script:HomeDir = [IO.Path]::Combine($Temp, $Name, 'home')
    $script:Data = [IO.Path]::Combine($Temp, $Name, 'data')
    New-Item -ItemType Directory -Force -Path $script:HomeDir, $script:Data | Out-Null
    [Environment]::SetEnvironmentVariable($HomeName, $script:HomeDir)
    $env:LOCALAPPDATA = $script:Data
    $script:Managed = Join-Path $script:Data 'coding-agent-kit'
    $script:Claude = [IO.Path]::Combine($script:HomeDir, '.claude', 'coding-agent-kit')
    $script:Entry = Join-Path $script:Managed 'install.ps1'
    Set-Content -LiteralPath $Log -Value $null
}

function Get-Installed([string]$Release) {
    $version = $Release.Substring(1)
    $lines = @("coding-agent-kit: $Release (channel $($version.Substring(0, $version.LastIndexOf('.')))) in $script:Managed")
    foreach ($name in $Helpers) {
        $lines += "$(Join-Path $script:Claude "$name.cjs") <- $([IO.Path]::Combine($script:Managed, 'releases', $Release, 'helpers', "$name.cjs"))"
    }
    return $lines + (Get-Commands)
}

function Get-Commands {
    $lines = @('Claude Code helpers, as commands in settings.json:')
    foreach ($entry in @(@('statusLine:            ', 'statusline'), @('Notification hook:     ', 'notify'),
            @('PreToolUse hook (Bash):', 'check-git-hooks'))) {
        $lines += "  $($entry[0]) node -e `"require(require('os').homedir() + '/.claude/coding-agent-kit/$($entry[1]).cjs').main()`""
    }
    return $lines
}

function Assert-Installed([string]$Release, [string]$Channel) {
    $tree = [IO.Path]::Combine($script:Managed, 'releases', $Release)
    if ((Get-Content -LiteralPath (Join-Path $script:Managed 'current')) -cne $Release) { Fail-Test "current is not $Release" }
    if ((Get-Content -LiteralPath (Join-Path $script:Managed 'channel')) -cne $Channel) { Fail-Test "channel is not $Channel" }
    if ((Get-Hash $script:Entry) -ne (Get-Hash (Join-Path $tree 'install.ps1'))) { Fail-Test "the entry point is not $Release's" }
    foreach ($name in $Helpers) {
        if ((Get-Hash (Join-Path $script:Claude "$name.cjs")) -ne (Get-Hash ([IO.Path]::Combine($tree, 'helpers', "$name.cjs")))) {
            Fail-Test "$name.cjs is not $Release's copy"
        }
    }
}

function Get-Releases {
    return (@(Get-ChildItem -LiteralPath (Join-Path $script:Managed 'releases') -Directory | ForEach-Object Name) -join ' ')
}

New-Item -ItemType Directory -Force -Path $Temp, $Source, $Out, (Split-Path -Parent $Boot) | Out-Null
$saved = @{}
foreach ($name in @($HomeName, 'LOCALAPPDATA', 'CODING_AGENT_KIT_REPO_URL', 'KIT_FIXTURE_LOG', 'GIT_CONFIG_NOSYSTEM', 'GIT_CONFIG_GLOBAL')) {
    $saved[$name] = [Environment]::GetEnvironmentVariable($name)
}
try {
    $env:GIT_CONFIG_NOSYSTEM = '1'
    $env:GIT_CONFIG_GLOBAL = Join-Path $Temp 'gitconfig'
    $env:KIT_FIXTURE_LOG = $Log
    $env:CODING_AGENT_KIT_REPO_URL = $Url
    Invoke-Fixture init -q $Source
    Invoke-Fixture init -q --bare $Remote
    Publish 'v0.5.0'
    Publish 'v0.6.0'
    Publish 'v0.6.1'
    Publish 'v0.7.0'
    # The bootstrap is a downloaded copy of install.ps1, outside any checkout.
    Copy-Item -LiteralPath (Join-Path $Repository 'install.ps1') -Destination $Boot

    Write-Output '==> the bootstrap installs the newest patch of the channel and copies the helpers'
    New-Home 'main'
    $result = Invoke-Installer boot $Boot -Channel 0.6
    Assert-Result $result 0 (Get-Installed 'v0.6.1')
    Assert-Logged @('v0.6.1 -Activate -Channel 0.6')
    Assert-Installed 'v0.6.1' '0.6'
    if ((Get-Releases) -cne 'v0.6.1') { Fail-Test "releases: $(Get-Releases)" }
    # run.ps1 requires node; the copied hook helper answers through the home-relative form.
    $ErrorActionPreference = 'Continue'
    $hook = '{"tool_input": {"command": "git commit --no-verify"}}' |
        & node -e "require(require('os').homedir() + '/.claude/coding-agent-kit/check-git-hooks.cjs').main()" 2>&1
    $ErrorActionPreference = 'Stop'
    if ($LASTEXITCODE -ne 2 -or "$hook" -notmatch 'Git hooks must run') { Fail-Test "the copied hook helper: $hook" }

    Write-Output '==> the bootstrap again and -Update write nothing and say so in one line'
    $before = Get-Snapshot $script:Managed
    $beforeHelpers = Get-Snapshot (Split-Path -Parent $script:Claude)
    Assert-Result (Invoke-Installer again $Boot -Channel 0.6) 0 @('coding-agent-kit: v0.6.1 is already current (channel 0.6).')
    # The entry point hands every install or update to the release's own -Activate.
    Assert-Logged @('v0.6.1 -Channel 0.6', 'v0.6.1 -Activate -Channel 0.6')
    Assert-Result (Invoke-Installer update $script:Entry -Update) 0 @('coding-agent-kit: v0.6.1 is already current (channel 0.6).')
    Assert-Logged @('v0.6.1 -Update', 'v0.6.1 -Activate -Channel 0.6')
    Assert-Result (Invoke-Installer update-gnu $script:Entry --update) 0 @('coding-agent-kit: v0.6.1 is already current (channel 0.6).')
    Assert-Logged @('v0.6.1 --update', 'v0.6.1 -Activate -Channel 0.6')
    if ((Get-Snapshot $script:Managed) -cne $before -or (Get-Snapshot (Split-Path -Parent $script:Claude)) -cne $beforeHelpers) {
        Fail-Test 'a run with nothing to change wrote'
    }

    Write-Output '==> the lock: a held lock exits 75 and changes nothing'
    $lock = [IO.File]::Open((Join-Path $script:Managed '.lock'), [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    try {
        foreach ($arguments in @(@('-Update'), @('-Uninstall'))) {
            $result = Invoke-Installer locked $script:Entry @arguments
            Assert-Result $result 75 @() @("install.ps1: another install.ps1 holds $(Join-Path $script:Managed '.lock'); nothing changed.")
        }
    } finally {
        $lock.Dispose()
    }
    Assert-Logged @('v0.6.1 -Update', 'v0.6.1 -Uninstall')
    if ((Get-Snapshot $script:Managed) -cne $before) { Fail-Test 'a locked run wrote' }

    Write-Output '==> -Update takes a newer patch only and replaces a changed helper'
    Publish 'v0.6.2' 'fixture v0.6.2'
    $result = Invoke-Installer patch $script:Entry -Update
    Assert-Result $result 0 (@('coding-agent-kit: switched from v0.6.1 to v0.6.2 (channel 0.6).',
            "$(Join-Path $script:Claude 'notify.cjs') <- $([IO.Path]::Combine($script:Managed, 'releases', 'v0.6.2', 'helpers', 'notify.cjs'))") +
        (Get-Commands))
    Assert-Logged @('v0.6.1 -Update', 'v0.6.2 -Activate -Channel 0.6')
    Assert-Installed 'v0.6.2' '0.6'
    if ((Get-Releases) -cne 'v0.6.1 v0.6.2') { Fail-Test "releases after the patch: $(Get-Releases)" }
    Publish 'v0.6.3'
    Assert-Result (Invoke-Installer prune $script:Entry -Update) 0 (@('coding-agent-kit: switched from v0.6.2 to v0.6.3 (channel 0.6).',
            "$(Join-Path $script:Claude 'notify.cjs') <- $([IO.Path]::Combine($script:Managed, 'releases', 'v0.6.3', 'helpers', 'notify.cjs'))") +
        (Get-Commands))
    Assert-Logged @('v0.6.2 -Update', 'v0.6.3 -Activate -Channel 0.6')
    if ((Get-Releases) -cne 'v0.6.2 v0.6.3') { Fail-Test "releases after pruning: $(Get-Releases)" }

    Write-Output '==> a different -Channel switches; -Update never leaves the channel'
    Assert-Result (Invoke-Installer channel $script:Entry -Channel 0.7) 0 @('coding-agent-kit: switched from v0.6.3 to v0.7.0 (channel 0.7).')
    Assert-Logged @('v0.6.3 -Channel 0.7', 'v0.7.0 -Activate -Channel 0.7')
    Assert-Installed 'v0.7.0' '0.7'
    Publish 'v0.6.4'
    Assert-Result (Invoke-Installer channel-update $script:Entry -Update) 0 @('coding-agent-kit: v0.7.0 is already current (channel 0.7).')
    Assert-Logged @('v0.7.0 -Update', 'v0.7.0 -Activate -Channel 0.7')

    Write-Output '==> a foreign helper refuses an update, which keeps the current release'
    Publish 'v0.7.1'
    $statusline = Join-Path $script:Claude 'statusline.cjs'
    [IO.File]::WriteAllText($statusline, "my own status line`n")
    $result = Invoke-Installer foreign $script:Entry -Update
    Assert-Result $result 1 @() @("install.ps1: refusing ${statusline}: a file that is no release's copy",
        'install.ps1: nothing changed; move those paths aside.')
    Assert-Logged @('v0.7.0 -Update', 'v0.7.1 -Activate -Channel 0.7')
    if ((Get-Content -LiteralPath (Join-Path $script:Managed 'current')) -cne 'v0.7.0') { Fail-Test 'a refused update switched' }

    Write-Output '==> -Uninstall removes only the installer''s copies; -Purge the managed root'
    $result = Invoke-Installer uninstall $script:Entry -Uninstall
    Assert-Result $result 0 @("kept ${statusline}: a file that is no release's copy",
        "removed $(Join-Path $script:Claude 'notify.cjs')", "removed $(Join-Path $script:Claude 'check-git-hooks.cjs')")
    Assert-Logged @('v0.7.0 -Uninstall')
    Remove-Item -LiteralPath $statusline
    Assert-Result (Invoke-Installer none $script:Entry -Uninstall) 0 @('coding-agent-kit: no managed install copies to remove.')
    Assert-Result (Invoke-Installer purge $Boot -Uninstall -Purge) 0 @("removed $script:Managed")
    Assert-Logged @('v0.7.0 -Uninstall', 'v0.7.0 -Uninstall -Purge')
    if (Test-Path -LiteralPath $script:Managed) { Fail-Test '-Purge left the managed root' }

    Write-Output '==> the default channel, foreign paths before a first install, usage errors'
    New-Home 'default'
    Assert-Result (Invoke-Installer default $Boot) 0 (Get-Installed 'v0.7.1')
    Assert-Logged @('v0.7.1 -Activate -Channel 0.7')
    New-Home 'refusal'
    New-Item -ItemType Directory -Force -Path (Join-Path $script:Claude 'notify.cjs') | Out-Null
    $result = Invoke-Installer refusal $Boot -Channel 0.6
    Assert-Result $result 1 @() @("install.ps1: refusing $(Join-Path $script:Claude 'notify.cjs'): a directory",
        'install.ps1: nothing changed; move those paths aside.')
    Assert-Logged @('v0.6.4 -Activate -Channel 0.6')
    if (Test-Path -LiteralPath (Join-Path $script:Managed 'current')) { Fail-Test 'a refused first install made current' }
    if (@(Get-ChildItem -LiteralPath $script:Claude -Force).Count -ne 1) { Fail-Test 'a refused first install copied a helper' }
    $result = Invoke-Installer no-install $Boot -Update
    Assert-Result $result 1 @() @("install.ps1: no managed install in $script:Managed to update; install one first with -Channel MAJOR.MINOR.")
    New-Home 'usage'
    foreach ($arguments in @(@('-Bogus'), @('-Channel', '0.5'), @('-Channel', '1'), @('-Channel'), @('-Purge'),
            @('-Update', '-Activate'), @('-Uninstall', '-Channel', '0.6'))) {
        $result = Invoke-Installer usage $Boot @arguments
        if ($result.Code -ne 2 -or $result.Out.Count -ne 0 -or $result.Err.Count -eq 0) {
            Fail-Test "install.ps1 $($arguments -join ' '): exit $($result.Code), expected 2"
        }
    }
    $env:CODING_AGENT_KIT_REPO_URL = ([Uri](Join-Path $Temp 'missing.git')).AbsoluteUri
    $result = Invoke-Installer offline $Boot -Channel 0.6
    if ($result.Code -ne 1 -or $result.Err.Count -ne 1 -or $result.Err[0] -notlike "install.ps1: cannot list the releases at *") {
        Fail-Test "offline: exit $($result.Code): $($result.Err -join ' ')"
    }
    if (Test-Path -LiteralPath $script:Claude) { Fail-Test 'a failed install copied a helper' }
    Write-Output 'managed-install-ps1=PASS'
} finally {
    foreach ($name in $saved.Keys) { [Environment]::SetEnvironmentVariable($name, $saved[$name]) }
    Remove-Item -LiteralPath $Temp -Recurse -Force -ErrorAction Ignore
}
