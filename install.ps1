#Requires -Version 7.0
# install.ps1 [-Channel MAJOR.MINOR] [-Update | -Reinstall] | -Uninstall [-Purge]: the managed
# install of coding-agent-kit on native Windows, where only the three Claude
# Code helpers run; they are copied to $HOME\.claude\coding-agent-kit\.
#
# The managed root $env:LOCALAPPDATA\coding-agent-kit holds a bare clone
# (repo.git), exported release trees (releases\vX.Y.Z), the file current that
# names one of them, the remembered channel, the lock file .lock (exit 75
# while another run holds it) and install.ps1, a copy of the current
# release's own installer, which is the update entry point. A run lists the
# tags of the repository, exports the highest vMAJOR.MINOR.PATCH of its
# channel and hands over to that release's own install.ps1 -Activate, which
# checks every destination, copies the helpers and the entry point by a
# rename in their directories, replaces current, keeps the previous release
# and prunes older ones. A destination is the installer's own when it is
# missing or a plain file equal to the helper of a release in the managed
# root; anything else is refused and kept. A copy of this file outside the
# managed root is the bootstrap: once the entry point exists, it hands every
# run to it, so only released code changes an existing install; -Reinstall
# instead exports the newest patch of the channel again from a fresh clone.
# A git transfer below 1000 bytes/s for 60 s fails, so no run holds the lock
# for long. Requires PowerShell 7 and git on PATH.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$PSNativeCommandUseErrorActionPreference = $false
Set-StrictMode -Version 3.0

$Helpers = @('statusline', 'notify', 'check-git-hooks')
$DefaultUrl = 'https://github.com/FridrichMethod/coding-agent-kit.git'

function Fail([int]$Code, [string]$Message) {
    [Console]::Error.WriteLine("install.ps1: $Message")
    exit $Code
}

function Show-Usage([int]$Code) {
    $text = @(
        'Usage: install.ps1 [-Channel MAJOR.MINOR] [-Update]'
        '       install.ps1 -Reinstall [-Channel MAJOR.MINOR]'
        '       install.ps1 -Uninstall [-Purge]'
        'Installs the newest release of the channel (default: that of the newest release) in'
        '$env:LOCALAPPDATA\coding-agent-kit and copies its Claude Code helpers to'
        '$HOME\.claude\coding-agent-kit\{statusline,notify,check-git-hooks}.cjs; -Update takes only a newer'
        'patch of the remembered channel; -Reinstall exports it again instead of running the installed entry'
        'point; -Purge also removes the managed root. Exit 75: another run holds the lock. Options may also be'
        'spelled --channel, --update, --reinstall, --uninstall and --purge.'
    ) -join [Environment]::NewLine
    if ($Code -eq 0) { [Console]::Out.WriteLine($text) } else { [Console]::Error.WriteLine($text) }
    exit $Code
}

function Test-Version([string]$Text, [int]$Parts) {
    $number = '(0|[1-9][0-9]{0,8})'
    return [regex]::IsMatch($Text, '^' + (@($number) * $Parts -join '\.') + '$')
}

function Test-Managed([string]$Channel) {
    $major, $minor = $Channel.Split('.')
    return ([int]$major -gt 0) -or ([int]$minor -ge 6)
}

function Get-Inner([System.Exception]$Exception) {
    while ($Exception -is [System.Management.Automation.MethodInvocationException] -and $Exception.InnerException) {
        $Exception = $Exception.InnerException
    }
    return $Exception
}

# Test-SamePath A B: the two absolute paths name the same place, as Windows compares them.
function Test-SamePath([string]$A, [string]$B) {
    $comparison = if ($IsWindows) { [StringComparison]::OrdinalIgnoreCase } else { [StringComparison]::Ordinal }
    $separators = [char[]]@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    $left = [IO.Path]::GetFullPath($A).TrimEnd($separators)
    $right = [IO.Path]::GetFullPath($B).TrimEnd($separators)
    return [string]::Equals($left, $right, $comparison)
}

# Invoke-Git ARGS: git's status and its output lines, stderr included.
function Invoke-Git {
    $ErrorActionPreference = 'Continue'
    $output = & git -c gc.auto=0 -c maintenance.auto=false -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=60 @args 2>&1 |
        ForEach-Object { "$_" }
    return [pscustomobject]@{ Code = $LASTEXITCODE; Output = @($output) }
}

function Get-Lock {
    $variable = Get-Variable -Name CodingAgentKitInstallLock -Scope Global -ErrorAction Ignore
    if ($null -eq $variable) { return $null }
    return $variable.Value
}

function Join-Parts([string[]]$Parts) {
    return [IO.Path]::Combine($Parts)
}

# Lock: take the managed root's lock, unless the run that started this one
# holds it, and drop what a killed run left behind, there and beside the
# helpers. Only a sharing or lock violation (32, 33) that lasts a second is
# another run; any other error is one.
function Lock {
    if ($null -ne (Get-Lock)) { return }
    $path = Join-Path $script:Root '.lock'
    for ($try = 1; ; $try++) {
        try {
            $global:CodingAgentKitInstallLock = [IO.File]::Open($path, [IO.FileMode]::OpenOrCreate,
                [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
            $script:OwnsLock = $true
            break
        } catch {
            $inner = Get-Inner $_.Exception
            $busy = $inner -is [IO.IOException] -and $inner -isnot [IO.DirectoryNotFoundException] -and
                $inner -isnot [IO.FileNotFoundException] -and (-not $IsWindows -or ($inner.HResult -band 0xFFFF) -in 32, 33)
            if (-not $busy) { Fail 1 "cannot open the lock file ${path}: $($inner.Message)" }
            if ($try -ge 5) { Fail 75 "another install.ps1 holds $path; nothing changed." }
            Start-Sleep -Milliseconds 200
        }
    }
    foreach ($leftover in @(Get-ChildItem -LiteralPath $script:Root -Filter '.tmp.*' -Force -ErrorAction Ignore) +
        @(Get-ChildItem -LiteralPath (Join-Path $script:Root 'releases') -Filter '.tmp.*' -Force -ErrorAction Ignore) +
        @(Get-ChildItem -LiteralPath $script:ClaudeDir -Filter '.tmp.*.cjs.*' -File -Force -ErrorAction Ignore)) {
        Remove-Item -LiteralPath $leftover.FullName -Recurse -Force
    }
}

function Unlock {
    if ($script:OwnsLock -and $null -ne (Get-Lock)) {
        $global:CodingAgentKitInstallLock.Dispose()
        $global:CodingAgentKitInstallLock = $null
        $script:OwnsLock = $false
    }
}

function Get-State([string]$Name) {
    $path = Join-Path $script:Root $Name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return '' }
    $value = Get-Content -LiteralPath $path -TotalCount 1
    if ($null -eq $value) { return '' }
    return "$value".Trim()
}

function Get-Temp([string]$Path) {
    return Join-Path (Split-Path -Parent $Path) ".tmp.$(Split-Path -Leaf $Path).$PID"
}

# Move-Replace TEMP PATH: rename TEMP over PATH, again for 1.5 s while another
# process (node, a scanner) has PATH open.
function Move-Replace([string]$Temp, [string]$Path) {
    for ($try = 1; ; $try++) {
        try {
            [IO.File]::Move($Temp, $Path, $true)
            return
        } catch {
            $inner = Get-Inner $_.Exception
            if ($try -ge 10 -or -not ($inner -is [IO.IOException] -or $inner -is [UnauthorizedAccessException])) { throw }
            Start-Sleep -Milliseconds 150
        }
    }
}

# Write-Replace PATH TEXT: write a temporary file beside PATH, then rename it over PATH.
function Write-Replace([string]$Path, [string]$Text) {
    [IO.File]::WriteAllText((Get-Temp $Path), $Text)
    Move-Replace (Get-Temp $Path) $Path
}

function Set-State([string]$Name, [string]$Value) {
    $path = Join-Path $script:Root $Name
    if ((Test-Path -LiteralPath $path -PathType Leaf) -and (Get-State $Name) -eq $Value) { return }
    Write-Replace $path "$Value`n"
}

# Get-Current: the release that current names, when its tree is complete.
function Get-Current {
    $tag = Get-State 'current'
    if (-not $tag.StartsWith('v') -or -not (Test-Version $tag.Substring(1) 3)) { return '' }
    if (-not (Test-Path -LiteralPath (Join-Parts $script:Root, 'releases', $tag, 'install.ps1') -PathType Leaf)) { return '' }
    return $tag
}

function Get-Hash([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
}

# Get-Destination NAME: what is at the copy of helper NAME (Kind missing,
# same as this release's, own or foreign, with a description for foreign).
function Get-Destination([string]$Name, [string]$Source) {
    $path = Join-Path $script:ClaudeDir "$Name.cjs"
    $item = Get-Item -LiteralPath $path -Force -ErrorAction Ignore
    if ($null -eq $item) { return [pscustomobject]@{ Path = $path; Kind = 'missing'; What = '' } }
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        return [pscustomobject]@{ Path = $path; Kind = 'foreign'; What = 'a link' }
    }
    if ($item.PSIsContainer) { return [pscustomobject]@{ Path = $path; Kind = 'foreign'; What = 'a directory' } }
    $hash = Get-Hash $path
    if ($Source -and $hash -eq (Get-Hash $Source)) { return [pscustomobject]@{ Path = $path; Kind = 'same'; What = '' } }
    foreach ($release in @(Get-ChildItem -LiteralPath (Join-Path $script:Root 'releases') -Directory -Filter 'v*' -ErrorAction Ignore)) {
        $copy = Join-Parts $release.FullName, 'helpers', "$Name.cjs"
        if ((Test-Path -LiteralPath $copy -PathType Leaf) -and (Get-Hash $copy) -eq $hash) {
            return [pscustomobject]@{ Path = $path; Kind = 'own'; What = '' }
        }
    }
    return [pscustomobject]@{ Path = $path; Kind = 'foreign'; What = "a file that is no release's copy" }
}

function Show-Commands {
    [Console]::Out.WriteLine('Claude Code helpers, as commands in settings.json:')
    foreach ($entry in @(@('statusLine:            ', 'statusline'), @('Notification hook:     ', 'notify'),
            @('PreToolUse hook (Bash):', 'check-git-hooks'))) {
        [Console]::Out.WriteLine("  $($entry[0]) node -e `"require(require('os').homedir() + '/.claude/coding-agent-kit/$($entry[1]).cjs').main()`"")
    }
}

# Invoke-Activate: this release becomes current; the run that exported it holds the lock.
function Invoke-Activate {
    $release = $script:Release
    $version = $release.Substring(1)
    $own = $version.Substring(0, $version.LastIndexOf('.'))
    $channel = if ($script:Channel) { $script:Channel } elseif (Get-State 'channel') { Get-State 'channel' } else { $own }
    if ($channel -ne $own) { Fail 1 "$release is not a release of channel $channel." }
    Lock
    $entry = Join-Path $script:Here 'install.ps1'
    $destinations = @()
    $refused = $false
    foreach ($name in $Helpers) {
        $source = Join-Parts $script:Here, 'helpers', "$name.cjs"
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            Fail 1 "$($script:Here) is not a complete release (missing helpers\$name.cjs)."
        }
        $destination = Get-Destination $name $source
        if ($destination.Kind -eq 'foreign') {
            [Console]::Error.WriteLine("install.ps1: refusing $($destination.Path): $($destination.What)")
            $refused = $true
        }
        $destinations += [pscustomobject]@{ Source = $source; Path = $destination.Path; Kind = $destination.Kind }
    }
    $item = Get-Item -LiteralPath $script:ClaudeDir -Force -ErrorAction Ignore
    if ($null -ne $item -and -not $item.PSIsContainer) {
        [Console]::Error.WriteLine("install.ps1: refusing $($script:ClaudeDir): a file, not a directory")
        $refused = $true
    }
    if ($refused) { Fail 1 'nothing changed; move those paths aside.' }
    $old = Get-Current
    $pending = @($destinations | Where-Object Kind -NE 'same')
    $entryPath = Join-Path $script:Root 'install.ps1'
    $entrySame = (Test-Path -LiteralPath $entryPath -PathType Leaf) -and (Get-Hash $entryPath) -eq (Get-Hash $entry)
    if ($old -eq $release -and $pending.Count -eq 0 -and $entrySame -and (Get-State 'channel') -eq $channel) {
        [Console]::Out.WriteLine("coding-agent-kit: $release is already current (channel $channel).")
        return
    }
    if ($old -and $old -ne $release) {
        [Console]::Out.WriteLine("coding-agent-kit: switched from $old to $release (channel $channel).")
    } else {
        [Console]::Out.WriteLine("coding-agent-kit: $release (channel $channel) in $($script:Root)")
    }
    # The channel first: an update after a crash never leaves it backwards.
    Set-State 'channel' $channel
    # All copies are staged before the renames, which then follow each other.
    if ($pending.Count -gt 0) { New-Item -ItemType Directory -Force -Path $script:ClaudeDir | Out-Null }
    foreach ($destination in $pending) { [IO.File]::Copy($destination.Source, (Get-Temp $destination.Path), $true) }
    if (-not $entrySame) { [IO.File]::Copy($entry, (Get-Temp $entryPath), $true) }
    foreach ($destination in $pending) {
        Move-Replace (Get-Temp $destination.Path) $destination.Path
        [Console]::Out.WriteLine("$($destination.Path) <- $($destination.Source)")
    }
    if (-not $entrySame) { Move-Replace (Get-Temp $entryPath) $entryPath }
    if ($old -ne $release) {
        Set-State 'current' $release
        foreach ($tree in @(Get-ChildItem -LiteralPath (Join-Path $script:Root 'releases') -Directory -Filter 'v*')) {
            if ($tree.Name -ne $release -and $tree.Name -ne $old) { Remove-Item -LiteralPath $tree.FullName -Recurse -Force }
        }
    }
    if ($pending.Count -gt 0) { Show-Commands }
}

function Invoke-Uninstall {
    if (Test-Path -LiteralPath $script:Root -PathType Container) { Lock }
    $found = $false
    foreach ($name in $Helpers) {
        $destination = Get-Destination $name ''
        if ($destination.Kind -eq 'own') {
            Remove-Item -LiteralPath $destination.Path -Force
            [Console]::Out.WriteLine("removed $($destination.Path)")
            $found = $true
        } elseif ($destination.Kind -eq 'foreign') {
            [Console]::Out.WriteLine("kept $($destination.Path): $($destination.What)")
            $found = $true
        }
    }
    $item = Get-Item -LiteralPath $script:ClaudeDir -Force -ErrorAction Ignore
    if ($found -and $null -ne $item -and $item.PSIsContainer -and
        -not ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -and
        @(Get-ChildItem -LiteralPath $script:ClaudeDir -Force).Count -eq 0) {
        Remove-Item -LiteralPath $script:ClaudeDir -Force
        [Console]::Out.WriteLine("removed $($script:ClaudeDir)")
    }
    if ($script:Purge -and (Test-Path -LiteralPath $script:Root)) {
        foreach ($name in @('repo.git', 'releases', 'current', 'channel', 'install.ps1')) {
            $path = Join-Path $script:Root $name
            if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force }
        }
        foreach ($leftover in @(Get-ChildItem -LiteralPath $script:Root -Filter '.tmp.*' -Force)) {
            Remove-Item -LiteralPath $leftover.FullName -Recurse -Force
        }
        Unlock
        Remove-Item -LiteralPath (Join-Path $script:Root '.lock') -Force -ErrorAction Ignore
        if (@(Get-ChildItem -LiteralPath $script:Root -Force).Count -eq 0) {
            Remove-Item -LiteralPath $script:Root -Force
            [Console]::Out.WriteLine("removed $($script:Root)")
        } else {
            [Console]::Out.WriteLine("kept $($script:Root): a directory that holds files install.ps1 did not make")
        }
    } elseif (-not $found) {
        [Console]::Out.WriteLine('coding-agent-kit: no managed install copies to remove.')
    }
}

function Get-Releases([string]$Url) {
    $result = Invoke-Git ls-remote --tags $Url
    if ($result.Code -ne 0) {
        $first = if ($result.Output.Count -gt 0) { $result.Output[0] } else { "git exited with $($result.Code)" }
        Fail 1 "cannot list the releases at ${Url}: $first"
    }
    $versions = @(foreach ($line in $result.Output) {
            $fields = $line -split '\s+'
            if ($fields.Count -eq 2 -and $fields[1].StartsWith('refs/tags/v') -and (Test-Version $fields[1].Substring(11) 3)) {
                $fields[1].Substring(11)
            }
        })
    if ($versions.Count -eq 0) { return }
    return $versions | Sort-Object { [version]$_ }
}

function Invoke-Install {
    $current = Get-Current
    if ($script:Update -and -not $current) {
        Fail 1 "no managed install in $($script:Root) to update; install one first with -Channel MAJOR.MINOR."
    }
    New-Item -ItemType Directory -Force -Path $script:Root | Out-Null
    Lock
    $current = Get-Current
    $url = $env:CODING_AGENT_KIT_REPO_URL
    $repo = Join-Path $script:Root 'repo.git'
    if (-not $url -and (Test-Path -LiteralPath $repo -PathType Container)) {
        $stored = Invoke-Git -C $repo config --get remote.origin.url
        if ($stored.Code -eq 0 -and $stored.Output.Count -gt 0) { $url = $stored.Output[0] }
    }
    if (-not $url) { $url = $DefaultUrl }
    if ($url.StartsWith('-')) { Fail 1 "the repository URL $url starts with -." }
    $versions = @(Get-Releases $url)
    if ($versions.Count -eq 0) { Fail 1 "$url has no vMAJOR.MINOR.PATCH release." }
    $newest = $versions[-1]
    $channel = if ($script:Channel) { $script:Channel } elseif (Get-State 'channel') { Get-State 'channel' } else {
        $newest.Substring(0, $newest.LastIndexOf('.'))
    }
    if (-not (Test-Version $channel 2)) { Fail 1 "the remembered channel $channel is not MAJOR.MINOR." }
    if (-not (Test-Managed $channel)) { Fail 1 "channel $channel predates managed installs; the first channel with them is 0.6." }
    $best = @($versions | Where-Object { $_.StartsWith("$channel.") })
    if ($best.Count -eq 0) { Fail 1 "$url has no release in channel $channel." }
    $target = "v$($best[-1])"
    # Within the channel only a newer patch moves current.
    if ($current -and $current -ne $target -and $current.StartsWith("v$channel.") -and
        [version]$current.Substring(1) -gt [version]$target.Substring(1)) {
        $target = $current
    }
    $tree = Join-Parts $script:Root, 'releases', $target
    if ($script:Reinstall -or -not (Test-Path -LiteralPath (Join-Path $tree 'install.ps1') -PathType Leaf)) {
        Export-Release $url $target
    }
    & (Join-Path $tree 'install.ps1') -Activate -Channel $channel
    exit $LASTEXITCODE
}

# Export-Release URL TAG: export TAG into releases\, from a fresh clone with
# -Reinstall; the old clone and tree are replaced only once that worked.
function Export-Release([string]$Url, [string]$Tag) {
    $repo = Join-Path $script:Root 'repo.git'
    $fresh = $script:Reinstall -or -not (Test-Path -LiteralPath $repo -PathType Container)
    if ($fresh) {
        $repo = Join-Path $script:Root ".tmp.repo.$PID"
        $result = Invoke-Git clone --bare --quiet -- $Url $repo
        if ($result.Code -ne 0) { Fail 1 "cannot clone ${Url}: $($result.Output -join ' ')" }
    }
    $result = Invoke-Git -C $repo rev-parse --quiet --verify "refs/tags/$Tag^{commit}"
    if ($result.Code -ne 0) {
        $result = Invoke-Git -C $repo fetch --quiet --no-tags -- $Url "refs/tags/${Tag}:refs/tags/$Tag"
        if ($result.Code -ne 0) { Fail 1 "cannot fetch $Tag from ${Url}: $($result.Output -join ' ')" }
    }
    New-Item -ItemType Directory -Force -Path (Join-Path $script:Root 'releases') | Out-Null
    $temp = Join-Parts $script:Root, 'releases', ".tmp.$Tag.$PID"
    $result = Invoke-Git -C $repo archive --format=zip -o "$temp.zip" "refs/tags/$Tag"
    if ($result.Code -ne 0) { Fail 1 "cannot export ${Tag}: $($result.Output -join ' ')" }
    Expand-Archive -LiteralPath "$temp.zip" -DestinationPath $temp
    Remove-Item -LiteralPath "$temp.zip" -Force
    if (-not (Test-Path -LiteralPath (Join-Path $temp 'install.ps1') -PathType Leaf)) {
        Fail 1 "$Tag is not a complete release (missing install.ps1)."
    }
    if ($fresh) {
        $kept = Join-Path $script:Root 'repo.git'
        if (Test-Path -LiteralPath $kept) { Remove-Item -LiteralPath $kept -Recurse -Force }
        [IO.Directory]::Move($repo, $kept)
    }
    $tree = Join-Parts $script:Root, 'releases', $Tag
    $old = Join-Parts $script:Root, 'releases', ".tmp.old.$Tag.$PID"
    if (Test-Path -LiteralPath $tree) { [IO.Directory]::Move($tree, $old) }
    [IO.Directory]::Move($temp, $tree)
    if (Test-Path -LiteralPath $old) { Remove-Item -LiteralPath $old -Recurse -Force }
    if ($script:Reinstall) { [Console]::Out.WriteLine("coding-agent-kit: exported $Tag again from $Url.") }
}

function Invoke-Main([string[]]$Arguments) {
    $script:Channel = ''
    $script:Update = $false
    $script:Uninstall = $false
    $script:Purge = $false
    $script:Reinstall = $false
    $activate = $false
    for ($index = 0; $index -lt $Arguments.Count; $index++) {
        switch ($Arguments[$index].ToLowerInvariant()) {
            { $_ -in '-channel', '--channel' } {
                $value = if ($index + 1 -lt $Arguments.Count) { $Arguments[$index + 1] } else { '' }
                if (-not (Test-Version $value 2)) { Fail 2 '-Channel needs MAJOR.MINOR, for example 0.6.' }
                if (-not (Test-Managed $value)) {
                    Fail 2 "channel $value predates managed installs; the first channel with them is 0.6."
                }
                $script:Channel = $value
                $index++
                break
            }
            { $_ -in '-update', '--update' } { $script:Update = $true; break }
            { $_ -in '-uninstall', '--uninstall' } { $script:Uninstall = $true; break }
            { $_ -in '-purge', '--purge' } { $script:Purge = $true; break }
            { $_ -in '-managed', '--managed' } { break }
            { $_ -in '-activate', '--activate' } { $activate = $true; break }
            { $_ -in '-reinstall', '--reinstall' } { $script:Reinstall = $true; break }
            { $_ -in '-help', '--help' } { Show-Usage 0 }
            default { Show-Usage 2 }
        }
    }
    if ($script:Uninstall) {
        if ($script:Channel -or $script:Update -or $script:Reinstall -or $activate) { Fail 2 '-Uninstall takes only -Purge.' }
    } elseif ($script:Purge) {
        Fail 2 '-Purge needs -Uninstall.'
    } elseif (@($script:Update, $script:Reinstall, $activate | Where-Object { $_ }).Count -gt 1) {
        Fail 2 '-Update, -Activate and -Reinstall exclude each other.'
    }
    $homeName = if ($IsWindows) { 'USERPROFILE' } else { 'HOME' }
    $userHome = [Environment]::GetEnvironmentVariable($homeName)
    if (-not $userHome -or -not [IO.Path]::IsPathFullyQualified($userHome)) {
        Fail 1 "$homeName must be set to an absolute path."
    }
    $data = $env:LOCALAPPDATA
    if (-not $data -or -not [IO.Path]::IsPathFullyQualified($data)) { Fail 1 'LOCALAPPDATA must be set to an absolute path.' }
    $script:Root = Join-Path $data 'coding-agent-kit'
    $script:ClaudeDir = Join-Path (Join-Path $userHome '.claude') 'coding-agent-kit'
    $script:Here = Split-Path -Parent $PSCommandPath
    $script:Release = ''
    $name = Split-Path -Leaf $script:Here
    if ((Test-SamePath (Split-Path -Parent $script:Here) (Join-Path $script:Root 'releases')) -and
        $name.StartsWith('v') -and (Test-Version $name.Substring(1) 3)) {
        $script:Release = $name
    }
    $entry = Test-SamePath $script:Here $script:Root
    if ($activate) {
        if (-not $script:Release) { Fail 1 '-Activate works only in a release of the managed root.' }
        Invoke-Activate
        return
    }
    $entryPath = Join-Path $script:Root 'install.ps1'
    if (-not $script:Release -and -not $entry -and -not $script:Reinstall -and (Test-Path -LiteralPath $entryPath -PathType Leaf)) {
        & $entryPath @Arguments
        exit $LASTEXITCODE
    }
    if ($script:Uninstall) { Invoke-Uninstall; return }
    if (-not (Get-Command git -CommandType Application -ErrorAction Ignore)) {
        Fail 1 'the managed install needs git, which is not on PATH.'
    }
    # ssh gives up on a host that does not answer, unless git is told how to run ssh.
    if (-not $env:GIT_SSH_COMMAND -and -not $env:GIT_SSH -and (Invoke-Git config --get core.sshCommand).Code -ne 0) {
        $env:GIT_SSH_COMMAND = 'ssh -o BatchMode=yes -o ConnectTimeout=30 -o ServerAliveInterval=15 -o ServerAliveCountMax=4'
    }
    Invoke-Install
}

$script:OwnsLock = $false
$savedPrompt = $env:GIT_TERMINAL_PROMPT
$savedSsh = $env:GIT_SSH_COMMAND
$env:GIT_TERMINAL_PROMPT = '0'
try {
    Invoke-Main @($args)
    exit 0
} catch {
    [Console]::Error.WriteLine("install.ps1: $((Get-Inner $_.Exception).Message)")
    exit 1
} finally {
    Unlock
    $env:GIT_TERMINAL_PROMPT = $savedPrompt
    $env:GIT_SSH_COMMAND = $savedSsh
}
