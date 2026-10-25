[CmdletBinding()]
param(
    [string]$RepoDir = "",
    [string]$Config = "config.yaml",
    [string]$EnvFile = "",
    [string]$PythonExe = "",
    [switch]$Status,
    [switch]$Stop,
    [switch]$Restart,
    [switch]$DryRun,
    [switch]$Force,
    [int]$WaitSeconds = 60
)

# =============================================================================
# start_antigravity.ps1 -- ONE command to start, stop and inspect the A2A
# processes on a Windows host (issue #68).
#
#   .\scripts\start_antigravity.ps1             start everything, then verify it
#   .\scripts\start_antigravity.ps1 -Status     what is registered and running
#   .\scripts\start_antigravity.ps1 -Stop       stop everything
#   .\scripts\start_antigravity.ps1 -Restart    -Stop then start
#   .\scripts\start_antigravity.ps1 -DryRun     print the plan, change nothing
#
# WHY THIS EXISTS. Supervision on Windows is three scheduled tasks over three
# wrapper scripts, and the recipes for creating them used to live only as shell
# snippets in docs\telegram\SETUP-ANTIGRAVITY.md (section 6a). Rebuilding that by
# hand -- or reconstructing it by reading the codebase -- is slow and easy to get
# subtly wrong (the four traps in section 6a each cost a real debugging session).
# This script is the single, discoverable entry point: the task table below is the
# source of truth it implements, and the doc table is the same contract in prose.
# tests\test_start_script_antigravity.py fails if the two ever disagree.
#
# IT IS IDEMPOTENT, AND IT DOES NOT SILENTLY REWRITE YOUR HOST. An existing task
# is left exactly as it is unless a BEHAVIOURAL field differs from this table
# (same command, same arguments, same cadence, same time limit, same battery and
# multi-instance settings, same principal). Purely cosmetic differences -- XML
# element order, a RegistrationInfo date, `StopAtDurationEnd` on a repetition with
# no duration -- do not trigger a re-registration. Pass -Force to re-register
# regardless.
#
# A task action goes through scripts\run_hidden.vbs, not directly to powershell.
# `wscript.exe` is GUI-subsystem and so allocates no console of its own; that is
# what stops a window flashing on every tick. It also means the arguments are
# parsed by powershell.exe's CRT parser, so paths are quoted with the canonical
# CreateProcess rules (mirroring QuoteArg in run_hidden.vbs) rather than naively
# wrapped -- see `A path with a space` handling in New-TaskXml below.
#
# EXIT CODES
#   0  succeeded (started and observed, or -Stop / -Status / -DryRun completed)
#   2  pre-flight failed -- nothing was touched. The reason is printed.
#   3  tasks were registered and enabled but the transport was not observed
#      running within -WaitSeconds. Log paths are printed; nothing is claimed.
#
# Reversal:  .\scripts\start_antigravity.ps1 -Stop
# =============================================================================

$ErrorActionPreference = "Stop"

# -----------------------------------------------------------------------------
# The contract. Cadences, time limits and wrappers, in start order.
#
# Order matters: transport first, then the poller that writes the heartbeat, then
# the watchdog that reads it. Starting the watchdog first would let it read a
# stale (or absent) beat and fire --alert on a system that is merely still coming
# up -- an alert about nothing, which is how a dead-man switch gets ignored.
#
# ExecutionTimeLimit: the transport is a foreground long-running process, so its
# task must never be capped (PT0S = no limit); the two one-shot wrappers get
# PT10M, comfortably above any real cycle.
# -----------------------------------------------------------------------------
$TaskTable = @(
    [pscustomobject]@{ Name = 'a2a-agy-transport'; Wrapper = 'run_antigravity_telegram.ps1'; Minutes = 30; TimeLimit = 'PT0S';  Role = 'restart-on-failure tick for the long-running transport' },
    [pscustomobject]@{ Name = 'a2a-agy-bus-poll';  Wrapper = 'run_antigravity_bus_poll.ps1';  Minutes = 30; TimeLimit = 'PT10M'; Role = 'durable bus poll; writes the heartbeat the watchdog reads' },
    [pscustomobject]@{ Name = 'a2a-agy-heartbeat'; Wrapper = 'run_antigravity_heartbeat.ps1'; Minutes = 15; TimeLimit = 'PT10M'; Role = 'dead-man watch; rc 3 = stale and alerted' }
)

$TransportTask = 'a2a-agy-transport'
$VbsShim = 'run_hidden.vbs'

# The transport's argv tail, exactly the pattern its own wrapper uses to decide
# "already running". It is anchored to the END of the command line: a bare
# `\brun\b` also matches unrelated arguments such as `--config C:\run\cfg.yaml`.
$TransportPattern = '(?:a2a|swarmly)\.telegram\.cli\b.*[\s"]run(\s+--?[^\s]+)*["'']?\s*$'

# Markers identifying a leftover launcher from THIS stack, never a stray process
# that merely happens to be called wscript.exe or powershell.exe.
$LauncherPattern = 'run_hidden\.vbs|run_antigravity_(telegram|bus_poll|heartbeat)\.ps1'

# -----------------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------------

function Resolve-Under {
    # Resolve a possibly-relative path against $Base, without touching the
    # filesystem (so it works for a path that does not exist yet, which is
    # exactly the case we want to report clearly).
    param([string]$Path, [string]$Base)
    if ([System.IO.Path]::IsPathRooted($Path)) { return [System.IO.Path]::GetFullPath($Path) }
    return [System.IO.Path]::GetFullPath((Join-Path $Base $Path))
}

function Quote-CrtArg {
    # Canonical CreateProcess (CRT) quoting, mirroring QuoteArg() in run_hidden.vbs.
    #
    # WScript.Shell.Run does no parsing of its own -- it hands the string to
    # CreateProcess, and powershell.exe's CRT parser reads it. That parser treats a
    # backslash before a quote as an escape, so naive wrapping corrupts a path that
    # ends in a backslash:
    #     -Path "C:\dir with space\"    arrives as  C:\dir with space"
    #     -Path "C:\dir with space\\"   arrives as  C:\dir with space\
    # A backslash run doubles only when a quote (or the closing quote) follows, and
    # an embedded quote is escaped with 2n+1 backslashes.
    #
    # Quoting unconditionally (rather than only when the value contains whitespace)
    # is deliberate: it is always correct under these rules, and it reproduces the
    # documented task action -- `//B "<shim>" "<wrapper>"` -- exactly, so a host that
    # was built by hand from SETUP-ANTIGRAVITY.md 6a is recognised as already correct
    # instead of being re-registered for a cosmetic difference.
    param([string]$Value)

    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append('"')
    $backslashes = 0
    foreach ($raw in $Value.ToCharArray()) {
        $ch = [string]$raw
        if ($ch -eq '\') {
            $backslashes++
        } elseif ($ch -eq '"') {
            [void]$sb.Append(('\') * ($backslashes * 2 + 1))
            [void]$sb.Append('"')
            $backslashes = 0
        } else {
            if ($backslashes -gt 0) {
                [void]$sb.Append(('\') * $backslashes)
                $backslashes = 0
            }
            [void]$sb.Append($ch)
        }
    }
    if ($backslashes -gt 0) { [void]$sb.Append(('\') * ($backslashes * 2)) }
    [void]$sb.Append('"')
    return $sb.ToString()
}

function Format-Bool {
    # Task Scheduler hands these back as real booleans, so a plain "$value" renders
    # them as PowerShell's `False`, which never compares equal to the XML spelling
    # `false`. Normalise both sides to the XML spelling.
    param($Value)
    if ($Value -is [string]) { return $Value.ToLowerInvariant() }
    if ([bool]$Value) { return 'true' }
    return 'false'
}

function Get-AccountSid {
    # A task's principal is a SID in its XML but an account NAME in the CIM object
    # model, so comparing the two directly always "differs" and would re-register a
    # perfectly good task on every run. Resolve to a SID before comparing.
    param([string]$Identity)
    if (-not $Identity) { return '' }
    if ($Identity -match '^S-\d-(\d+-)+\d+$') { return $Identity }
    try {
        return ([System.Security.Principal.NTAccount]$Identity).Translate(
            [System.Security.Principal.SecurityIdentifier]).Value
    } catch {
        return $Identity
    }
}

function New-TaskXml {
    param(
        [string]$Name,
        [string]$Arguments,
        [int]$Minutes,
        [string]$TimeLimit,
        [string]$UserId,
        [string]$StartBoundary
    )
    $author = "$env:USERDOMAIN\$env:USERNAME"
    $interval = "PT${Minutes}M"
    $xmlArgs = $Arguments.Replace('&', '&amp;').Replace('<', '&lt;').Replace('>', '&gt;').Replace('"', '&quot;')

    return @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>$author</Author>
    <URI>\$Name</URI>
  </RegistrationInfo>
  <Principals>
    <Principal id="Author">
      <UserId>$UserId</UserId>
      <LogonType>InteractiveToken</LogonType>
    </Principal>
  </Principals>
  <Settings>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <Enabled>true</Enabled>
    <ExecutionTimeLimit>$TimeLimit</ExecutionTimeLimit>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <StartWhenAvailable>true</StartWhenAvailable>
    <IdleSettings>
      <Duration>PT10M</Duration>
      <WaitTimeout>PT1H</WaitTimeout>
      <StopOnIdleEnd>true</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
  </Settings>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>$StartBoundary</StartBoundary>
      <Repetition>
        <Interval>$interval</Interval>
      </Repetition>
    </TimeTrigger>
  </Triggers>
  <Actions Context="Author">
    <Exec>
      <Command>wscript.exe</Command>
      <Arguments>$xmlArgs</Arguments>
    </Exec>
  </Actions>
</Task>
"@
}

function Get-TaskDefinition {
    # The behavioural fingerprint of a registered task. Only these fields are
    # compared: everything else in a task's XML is cosmetic and must NOT cause a
    # re-registration of a working host.
    param([string]$Name, [string]$Arguments, [int]$Minutes, [string]$TimeLimit, [string]$UserId)
    return [pscustomobject]@{
        Command    = 'wscript.exe'
        Arguments  = $Arguments
        Interval   = "PT${Minutes}M"
        TimeLimit  = $TimeLimit
        MultiInst  = 'IgnoreNew'
        OnBatteries = 'DisallowStartIfOnBatteries=false;StopIfGoingOnBatteries=false'
        StartWhenAvailable = $true
        RunLevel   = 'Limited'
        UserId     = $UserId
        LogonType  = 'InteractiveToken'
    }
}

function Get-LiveDefinition {
    param([string]$Name)
    $task = Get-ScheduledTask -TaskName $Name -ErrorAction Stop
    $action = @($task.Actions)[0]
    $trigger = @($task.Triggers)[0]
    $interval = ''
    if ($trigger -and $trigger.Repetition -and $trigger.Repetition.Interval) {
        $interval = "$($trigger.Repetition.Interval)"
    }
    $settings = $task.Settings
    $principal = $task.Principal

    # Task Scheduler reports `LogonType` as a short enum name through the CIM object
    # model (`Interactive`) where its own XML says `InteractiveToken`. Both mean the
    # same principal, so map the short form back before comparing.
    $logonType = "$($principal.LogonType)"
    if ($logonType -eq 'Interactive') { $logonType = 'InteractiveToken' }

    return [pscustomobject]@{
        Command    = "$($action.Execute)"
        Arguments  = "$($action.Arguments)"
        Interval   = $interval
        TimeLimit  = "$($settings.ExecutionTimeLimit)"
        MultiInst  = "$($settings.MultipleInstances)"
        OnBatteries = "DisallowStartIfOnBatteries=$(Format-Bool $settings.DisallowStartIfOnBatteries);StopIfGoingOnBatteries=$(Format-Bool $settings.StopIfGoingOnBatteries)"
        StartWhenAvailable = [bool]$settings.StartWhenAvailable
        RunLevel   = "$($principal.RunLevel)"
        UserId     = (Get-AccountSid "$($principal.UserId)")
        LogonType  = $logonType
    }
}

function Compare-Definition {
    # Returns the list of fields that differ (empty == matches).
    param($Expected, $Live)
    $diffs = New-Object System.Collections.Generic.List[string]
    foreach ($field in 'Command', 'Arguments', 'Interval', 'TimeLimit', 'MultiInst', 'OnBatteries', 'StartWhenAvailable', 'RunLevel', 'UserId', 'LogonType') {
        $want = "$($Expected.$field)"
        $have = "$($Live.$field)"
        # Case-INSENSITIVE on purpose. Windows paths, image names and Task
        # Scheduler's own enum spellings are all case-insensitive, and none of
        # these fields has a case-significant value. A case-sensitive compare
        # would call a hand-built task using `WScript.exe` or a differently-cased
        # path "drifted" and rewrite it -- needless churn on precisely the hosts
        # this comparison exists to leave alone.
        if ($want -ine $have) { $diffs.Add("$field`: expected '$want', found '$have'") }
    }
    return $diffs
}

function Get-SelfProcessTree {
    # This process and every ancestor of it.
    #
    # Needed because the transport matcher looks for a *python* whose argv contains
    # `swarmly.telegram.cli ... run` -- and an agent session driving this repo runs
    # exactly that command line. Without this, running `-Stop` from inside such a
    # session would match the caller and kill it. The launcher matcher has the same
    # hazard one level up: a parent powershell.exe that was invoked with this
    # script's path matches $LauncherPattern.
    #
    # Walking the ancestry is deliberate rather than comparing `$PID` only: the
    # script may be run by a wrapper shell whose parent is the process that would
    # match, and taskkill /T kills descendants of the pid it is given.
    $tree = New-Object System.Collections.Generic.HashSet[int]
    try {
        $all = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Select-Object ProcessId, ParentProcessId
        $byId = @{}
        foreach ($p in $all) { $byId[[int]$p.ProcessId] = [int]$p.ParentProcessId }
        $cursor = $PID
        $guard = 0
        while ($cursor -and $cursor -ne 0 -and $guard -lt 64) {
            [void]$tree.Add($cursor)
            if (-not $byId.ContainsKey($cursor)) { break }
            $cursor = $byId[$cursor]
            $guard++
        }
    } catch {
        [void]$tree.Add($PID)
    }
    return $tree
}

#: Ids that must never be killed: this process and its ancestors (see above).
$SelfTree = Get-SelfProcessTree

function Get-TransportProcesses {
    @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and
            $_.CommandLine -match $TransportPattern -and
            -not $SelfTree.Contains([int]$_.ProcessId)
        })
}

function Get-LauncherProcesses {
    @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Name -in @('wscript.exe', 'powershell.exe') -and
            $_.CommandLine -and $_.CommandLine -match $LauncherPattern -and
            -not $SelfTree.Contains([int]$_.ProcessId)
        })
}

function Get-EnvValue {
    # Minimal reader for `key: value` under a top-level section, used only to
    # report the resolved bus root. Best-effort by design: a miss falls back to
    # the repo root, which is what the config defaults mean anyway.
    param([string]$Text, [string]$Section, [string]$Key)
    $inSection = $false
    foreach ($line in ($Text -split "`r?`n")) {
        if ($line -match '^([A-Za-z0-9_]+):\s*$') { $inSection = ($matches[1] -eq $Section); continue }
        if ($line -notmatch '^\s') { $inSection = $false; continue }
        if ($inSection -and $line -match ('^\s+' + [regex]::Escape($Key) + ':\s*(\S.*?)\s*$')) {
            return $matches[1].Trim("'", '"')
        }
    }
    return $null
}

function Show-Header {
    param([string]$Text)
    Write-Host ""
    Write-Host "== $Text"
}

# -----------------------------------------------------------------------------
# mode selection
# -----------------------------------------------------------------------------

$modes = @()
if ($Status) { $modes += '-Status' }
if ($Stop) { $modes += '-Stop' }
if ($Restart) { $modes += '-Restart' }
if ($modes.Count -gt 1) {
    Write-Host "FATAL: -Status, -Stop and -Restart are mutually exclusive (got $($modes -join ', '))"
    exit 2
}
$mode = if ($modes.Count -eq 1) { $modes[0] } else { 'Start' }

# -----------------------------------------------------------------------------
# Whether the caller named these explicitly.
#
# Captured HERE, at script scope, because `$PSBoundParameters` is scoped to the
# current function: inside Invoke-Preflight or Start-Stack it describes THAT
# function's (empty) parameter set, not this script's. Reading it there silently
# answers "no" for every flag, so -Config / -EnvFile / -PythonExe would be
# resolved and printed but never forwarded to the wrappers -- the operator sees
# their path echoed back and the task keeps using the default. Verified on this
# host: `-Config` + `-PythonExe` printed "wrapper args: (none)".
#
# The distinction matters because these three are only *appended* to the task
# action when explicitly given; when they are not, the action stays byte-identical
# to the documented contract, which is what makes "definition matches, leave it
# alone" meaningful.
# -----------------------------------------------------------------------------
$ConfigPassed = $PSBoundParameters.ContainsKey('Config')
$EnvFilePassed = $PSBoundParameters.ContainsKey('EnvFile')
$PythonExePassed = $PSBoundParameters.ContainsKey('PythonExe')

# -----------------------------------------------------------------------------
# resolve the checkout and the three paths the wrappers take
# -----------------------------------------------------------------------------

if (-not $RepoDir) {
    $RepoDir = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
} else {
    $RepoDir = [System.IO.Path]::GetFullPath($RepoDir)
}

$ConfigPath = Resolve-Under -Path $Config -Base $RepoDir

$EnvPath = ''
if ($EnvFilePassed -and $EnvFile) {
    $EnvPath = Resolve-Under -Path $EnvFile -Base $RepoDir
} else {
    $candidate = Join-Path $RepoDir '.env'
    if (Test-Path -LiteralPath $candidate) { $EnvPath = $candidate }
}

$PythonPath = ''
if ($PythonExePassed -and $PythonExe) {
    $PythonPath = Resolve-Under -Path $PythonExe -Base $RepoDir
} else {
    $PythonPath = Join-Path $RepoDir '.venv\Scripts\python.exe'
}

$ShimPath = Join-Path $RepoDir "scripts\$VbsShim"
$StateDir = Join-Path $RepoDir '.agents\state'

# -----------------------------------------------------------------------------
# pre-flight -- fail here, with a reason, before touching any task
#
# A scheduled task's failure is recorded as a bare number in "Last Result", so a
# misconfiguration discovered at task-run time is nearly unreadable afterwards.
# Check the cheap things up front instead, and only for the modes that need them:
# -Stop and -Status must still work on a host that is broken.
# -----------------------------------------------------------------------------

function Invoke-Preflight {
    $problems = New-Object System.Collections.Generic.List[string]

    if (-not (Test-Path -LiteralPath $RepoDir -PathType Container)) {
        $problems.Add("repository root not found: $RepoDir (pass -RepoDir)")
    }
    if (-not (Test-Path -LiteralPath $ShimPath -PathType Leaf)) {
        $problems.Add("launcher shim not found: $ShimPath")
    }
    foreach ($task in $TaskTable) {
        $wrapper = Join-Path $RepoDir "scripts\$($task.Wrapper)"
        if (-not (Test-Path -LiteralPath $wrapper -PathType Leaf)) {
            $problems.Add("wrapper not found for $($task.Name): $wrapper")
        }
    }
    if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
        $problems.Add("config not found: $ConfigPath (pass -Config)")
    }
    if ($EnvFilePassed -and $EnvFile -and -not (Test-Path -LiteralPath $EnvPath -PathType Leaf)) {
        $problems.Add("-EnvFile '$EnvPath' does not exist")
    }
    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
        $problems.Add("python not found: $PythonPath (pass -PythonExe)")
    }
    return $problems
}

# -----------------------------------------------------------------------------
# -Status
# -----------------------------------------------------------------------------

function Show-Status {
    Show-Header "resolved paths"
    Write-Host "  repo       : $RepoDir"
    Write-Host "  config     : $ConfigPath$(if (Test-Path -LiteralPath $ConfigPath -PathType Leaf) { '' } else { '   (MISSING)' })"
    Write-Host "  env file   : $(if ($EnvPath) { $EnvPath } else { '(none found)' })"
    Write-Host "  python     : $PythonPath$(if (Test-Path -LiteralPath $PythonPath -PathType Leaf) { '' } else { '   (MISSING)' })"
    Write-Host "  state dir  : $StateDir"

    # The heartbeat lives under the *bus* root, which is `bus.repo` /
    # `fallback.gitbus_root` when set and the checkout itself otherwise.
    $busRoot = $null
    if (Test-Path -LiteralPath $ConfigPath -PathType Leaf) {
        $text = Get-Content -LiteralPath $ConfigPath -Raw
        $configured = Get-EnvValue -Text $text -Section 'bus' -Key 'repo'
        if (-not $configured) { $configured = Get-EnvValue -Text $text -Section 'fallback' -Key 'gitbus_root' }
        if ($configured) { $busRoot = Resolve-Under -Path $configured -Base $RepoDir }
    }
    Write-Host "  bus root   : $(if ($busRoot) { $busRoot } else { '(none: bus.repo is unset, so the bus is not polled)' })"

    Show-Header "scheduled tasks"
    foreach ($task in $TaskTable) {
        $live = Get-ScheduledTask -TaskName $task.Name -ErrorAction SilentlyContinue
        if ($null -eq $live) {
            Write-Host ("  {0,-18} MISSING  (run this script with no arguments to create it)" -f $task.Name)
            continue
        }
        $info = Get-ScheduledTaskInfo -TaskName $task.Name -ErrorAction SilentlyContinue
        $lastRun = if ($info -and $info.LastRunTime) { "$($info.LastRunTime)" } else { 'never' }
        $nextRun = if ($info -and $info.NextRunTime) { "$($info.NextRunTime)" } else { '-' }
        $lastRc = if ($info) { "$($info.LastTaskResult)" } else { '-' }
        Write-Host ("  {0,-18} {1,-9} enabled={2,-6} last={3} rc={4} next={5}" -f `
            $task.Name, "$($live.State)", "$($live.Settings.Enabled)", $lastRun, $lastRc, $nextRun)
        Write-Host ("  {0,-18}   cadence={1} min, wrapper={2}" -f '', $task.Minutes, $task.Wrapper)
    }

    Show-Header "live transport"
    $procs = Get-TransportProcesses
    if ($procs.Count -eq 0) {
        Write-Host "  no `python -m swarmly.telegram.cli ... run` process found"
    } else {
        # The venv python is a trampoline, so one transport normally shows two
        # pids (shim + interpreter). That is expected, not a double run.
        Write-Host "  $($procs.Count) matching process(es); pid(s) $(($procs | ForEach-Object { $_.ProcessId }) -join ', ')"
        foreach ($p in $procs) { Write-Host "    $($p.ProcessId): $($p.CommandLine)" }
    }

    $runLock = Join-Path $StateDir 'antigravity.run.lock'
    if (Test-Path -LiteralPath $runLock) {
        Write-Host "  run lock   : $runLock -> $(Get-Content -LiteralPath $runLock -Raw)"
    }

    Show-Header "heartbeat"
    $found = @()
    foreach ($root in @($busRoot, $RepoDir) | Where-Object { $_ } | Select-Object -Unique) {
        $found += @(Get-ChildItem -LiteralPath (Join-Path $root '.agents\state') -Filter '*.heartbeat.json' -ErrorAction SilentlyContinue)
    }
    if ($found.Count -eq 0) {
        Write-Host "  no *.heartbeat.json under $(if ($busRoot) { $busRoot } else { $RepoDir })\.agents\state"
    } else {
        foreach ($file in $found) {
            $age = (Get-Date) - $file.LastWriteTime
            Write-Host ("  {0}  age {1:N1} min  ({2})" -f $file.FullName, $age.TotalMinutes, $file.LastWriteTime)
        }
        Write-Host "  (fresh = the poller cycled recently; the watchdog trips at 3x the poll interval)"
    }

    Show-Header "logs"
    foreach ($name in 'antigravity.transport.host.log', 'antigravity.transport.out.log', 'antigravity.transport.err.log',
                      'antigravity.bus-poll.out.log', 'antigravity.bus-poll.err.log',
                      'antigravity.heartbeat-check.out.log', 'antigravity.heartbeat-check.err.log') {
        $path = Join-Path $StateDir $name
        if (Test-Path -LiteralPath $path) {
            Write-Host ("  {0}  ({1:N1} min ago)" -f $path, ((Get-Date) - (Get-Item -LiteralPath $path).LastWriteTime).TotalMinutes)
        }
    }
}

# -----------------------------------------------------------------------------
# -Stop
# -----------------------------------------------------------------------------

function Invoke-Stop {
    Show-Header "disabling tasks"
    # Disable BEFORE stopping: a 30-minute repetition that fires between the two
    # steps would start the transport again behind us.
    foreach ($task in $TaskTable) {
        $live = Get-ScheduledTask -TaskName $task.Name -ErrorAction SilentlyContinue
        if ($null -eq $live) { Write-Host "  $($task.Name): not registered, nothing to do"; continue }
        if ($DryRun) { Write-Host "  $($task.Name): would disable and stop (state=$($live.State))"; continue }
        try {
            if ($live.Settings.Enabled) {
                Disable-ScheduledTask -TaskName $task.Name | Out-Null
                Write-Host "  $($task.Name): disabled"
            } else {
                Write-Host "  $($task.Name): already disabled"
            }
        } catch {
            Write-Host "  $($task.Name): disable failed -- $($_.Exception.Message)"
        }
        if ($live.State -eq 'Running' -or (Get-ScheduledTask -TaskName $task.Name).State -eq 'Running') {
            if ($DryRun) { Write-Host "  $($task.Name): would stop (running)" }
            else {
                try {
                    Stop-ScheduledTask -TaskName $task.Name
                    Write-Host "  $($task.Name): stopped"
                } catch {
                    Write-Host "  $($task.Name): stop failed -- $($_.Exception.Message)"
                }
            }
        }
    }

    Show-Header "killing anything the scheduler left behind"
    # Stopping a task ends its process tree, but a launcher whose child was already
    # reparented can survive. Sweep by argv, not by image name, so nothing
    # unrelated is ever killed.
    $victims = @(Get-TransportProcesses) + @(Get-LauncherProcesses)
    if ($victims.Count -eq 0) { Write-Host "  nothing left running" }
    foreach ($p in $victims) {
        if ($DryRun) { Write-Host "  would kill pid $($p.ProcessId) ($($p.Name))"; continue }
        & taskkill.exe /PID $p.ProcessId /T /F 2>&1 | Out-Null
        Write-Host "  killed pid $($p.ProcessId) ($($p.Name))"
    }
    if (-not $DryRun) {
        # Wait for the tree to actually drain rather than sleeping a guessed
        # interval. The transport holds `.agents\state\<alias>.run.lock` and the
        # Telegram getUpdates connection; a new instance started while the old one
        # is still dying exits 3 on the lock or races it for the update offset.
        $deadline = (Get-Date).AddSeconds(20)
        while ((Get-Date) -lt $deadline) {
            $still = @(Get-TransportProcesses) + @(Get-LauncherProcesses)
            if ($still.Count -eq 0) { break }
            Start-Sleep -Milliseconds 500
        }
        $still = @(Get-TransportProcesses) + @(Get-LauncherProcesses)
        if ($still.Count -gt 0) {
            Write-Host "  WARNING: still alive after 20s: pid(s) $(($still | ForEach-Object { $_.ProcessId }) -join ', ')"
            Write-Host "  a start now would race the run lock; investigate before restarting."
        } else {
            Write-Host "  verified: no A2A process remains"
        }
    }
}

# -----------------------------------------------------------------------------
# start
# -----------------------------------------------------------------------------

function Start-Stack {
    $problems = Invoke-Preflight
    if ($problems.Count -gt 0) {
        Write-Host "FATAL: pre-flight failed; no task has been touched."
        foreach ($p in $problems) { Write-Host "  - $p" }
        exit 2
    }

    $userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $startBoundary = (Get-Date).ToString("yyyy-MM-ddTHH:mm:sszzz")

    # Extra wrapper arguments are appended ONLY when the caller named them. The
    # unparameterised action is then byte-identical to the documented contract and
    # to the live tasks, which is what makes "matches, leave alone" meaningful.
    $extra = @()
    if ($ConfigPassed) { $extra += @('-Config', $ConfigPath) }
    if ($EnvFilePassed -and $EnvPath) { $extra += @('-EnvFile', $EnvPath) }
    if ($PythonExePassed) { $extra += @('-PythonExe', $PythonPath) }

    Show-Header "plan"
    Write-Host "  repo       : $RepoDir"
    Write-Host "  config     : $ConfigPath"
    Write-Host "  env file   : $(if ($EnvPath) { $EnvPath } else { '(none -- the wrapper will fall back to its default)' })"
    Write-Host "  python     : $PythonPath"
    Write-Host "  principal  : $userId (InteractiveToken, Limited)"
    Write-Host "  order      : $(($TaskTable | ForEach-Object { $_.Name }) -join ' -> ')"
    if ($extra.Count -gt 0) {
        Write-Host "  wrapper args: $($extra -join ' ')  (explicitly requested)"
    } else {
        Write-Host "  wrapper args: (none -- the wrappers' own defaults)"
    }

    foreach ($task in $TaskTable) {
        $wrapper = "scripts\$($task.Wrapper)"
        $arguments = "//B $(Quote-CrtArg $ShimPath) $(Quote-CrtArg (Join-Path $RepoDir $wrapper))"
        foreach ($item in $extra) { $arguments += " $(Quote-CrtArg $item)" }

        $expected = Get-TaskDefinition -Name $task.Name -Arguments $arguments -Minutes $task.Minutes `
                                       -TimeLimit $task.TimeLimit -UserId $userId

        Show-Header "$($task.Name)  [$($task.Role)]"
        Write-Host "  cadence $($task.Minutes) min, time limit $($task.TimeLimit)"
        Write-Host "  action  wscript.exe $arguments"

        if ($DryRun) {
            $existing = Get-ScheduledTask -TaskName $task.Name -ErrorAction SilentlyContinue
            if ($null -eq $existing) { Write-Host "  -> would register, enable and start" }
            else {
                # `-gt 0` rather than truthiness: a one-element list's `.Count`
                # property-enumerates to a single bool through PowerShell's member
                # enumeration, so a bare `if ($diffs.Count -eq 0)` is not worth
                # relying on when the cost of being wrong is a skipped drift check.
                $diffs = @(Compare-Definition -Expected $expected -Live (Get-LiveDefinition -Name $task.Name))
                if ($diffs.Count -eq 0) { Write-Host "  -> definition already matches: would leave it alone, enable, start" }
                else { Write-Host "  -> would re-register: $($diffs -join ' | ')" }
            }
            continue
        }

        $existing = Get-ScheduledTask -TaskName $task.Name -ErrorAction SilentlyContinue
        $needsRegister = $false
        if ($null -eq $existing) {
            $needsRegister = $true
            Write-Host "  registering (not present)"
        } elseif ($Force) {
            $needsRegister = $true
            Write-Host "  registering (-Force)"
        } else {
            $diffs = @(Compare-Definition -Expected $expected -Live (Get-LiveDefinition -Name $task.Name))
            if ($diffs.Count -eq 0) {
                Write-Host "  definition matches; leaving it as it is"
            } else {
                $needsRegister = $true
                Write-Host "  definition differs -- re-registering:"
                foreach ($d in $diffs) { Write-Host "    $d" }
            }
        }

        if ($needsRegister) {
            try {
                $xml = New-TaskXml -Name $task.Name -Arguments $arguments -Minutes $task.Minutes `
                                   -TimeLimit $task.TimeLimit -UserId $userId -StartBoundary $startBoundary
                Register-ScheduledTask -TaskName $task.Name -Xml $xml -Force | Out-Null
                Write-Host "  registered"
            } catch {
                Write-Host "  FATAL: registration failed: $($_.Exception.Message)"
                Write-Host "  (registering a task for the current user should not need elevation;"
                Write-Host "   if this is a policy block, create the task by hand per SETUP-ANTIGRAVITY.md 6a.)"
                exit 2
            }
        }

        try {
            if (-not (Get-ScheduledTask -TaskName $task.Name).Settings.Enabled) {
                Enable-ScheduledTask -TaskName $task.Name | Out-Null
                Write-Host "  enabled"
            }
        } catch {
            Write-Host "  enable failed -- $($_.Exception.Message)"
        }

        $state = (Get-ScheduledTask -TaskName $task.Name).State
        if ($state -eq 'Running') {
            Write-Host "  already running (no-op)"
        } else {
            try {
                Start-ScheduledTask -TaskName $task.Name
                Write-Host "  start requested"
            } catch {
                # StartWhenAvailable can make the repetition fire the moment the
                # task is enabled, so "already running" here is a normal race.
                Write-Host "  start returned: $($_.Exception.Message)"
            }
        }
    }
}

# -----------------------------------------------------------------------------
# verify the start, then report -- never claim a success that was not observed
# -----------------------------------------------------------------------------

function Confirm-Started {
    Show-Header "verify"

    $deadline = (Get-Date).AddSeconds($WaitSeconds)
    $procs = @()
    while ((Get-Date) -lt $deadline) {
        $procs = Get-TransportProcesses
        if ($procs.Count -gt 0) { break }
        Start-Sleep -Seconds 2
    }
    $taskState = (Get-ScheduledTask -TaskName $TransportTask).State
    $transportUp = ($procs.Count -gt 0)
    $taskRunning = ($taskState -eq 'Running')

    if ($transportUp -and $taskRunning) {
        Write-Host "  transport: RUNNING (task state Running, pid(s) $(($procs | ForEach-Object { $_.ProcessId }) -join ', '))"
        Write-Host "  this is the foreground design working: the task stays Running for as long as the transport lives"
    } elseif ($taskRunning) {
        Write-Host "  transport: task is Running but no matching python process was seen within ${WaitSeconds}s"
    } else {
        Write-Host "  transport: task state is '$taskState' and no matching python process was seen within ${WaitSeconds}s"
    }

    foreach ($task in $TaskTable) {
        $live = Get-ScheduledTask -TaskName $task.Name -ErrorAction SilentlyContinue
        if ($null -eq $live) { Write-Host "  $($task.Name): MISSING"; continue }
        Write-Host "  $($task.Name): $($live.State), enabled=$($live.Settings.Enabled)"
    }

    Show-Header "if you need to look"
    Write-Host "  $StateDir\antigravity.transport.host.log     <- the launcher's own transcript"
    Write-Host "  $StateDir\antigravity.transport.out.log      <- transport stdout"
    Write-Host "  $StateDir\antigravity.transport.err.log      <- transport stderr (the CLI logs here)"
    Write-Host "  $StateDir\antigravity.bus-poll.err.log       <- one line per poll cycle"
    Write-Host "  $StateDir\antigravity.heartbeat-check.err.log"
    Write-Host "  reverse with: .\scripts\start_antigravity.ps1 -Stop"

    if (-not ($transportUp -and $taskRunning)) {
        Write-Host ""
        Write-Host "NOT STARTED (rc 3). Read $StateDir\antigravity.transport.host.log first:"
        Write-Host "  'python not found' / 'config not found' there means the task is running with a"
        Write-Host "  different working directory or PATH than this shell."
        exit 3
    }
    Write-Host ""
    Write-Host "started and verified."
}

# -----------------------------------------------------------------------------
# main
# -----------------------------------------------------------------------------

Write-Host "start_antigravity.ps1 -- mode: $mode$(if ($DryRun) { ' (dry run)' })"

switch ($mode) {
    '-Status' { Show-Status }
    '-Stop'   { Invoke-Stop }
    '-Restart' {
        Invoke-Stop
        # Invoke-Stop already waited for the process tree to drain; starting now
        # cannot race the run lock.
        Start-Stack
        if (-not $DryRun) { Confirm-Started }
    }
    default {
        Start-Stack
        if ($DryRun) {
            Show-Header "dry run"
            Write-Host "  nothing was registered, enabled, started or stopped."
        } else {
            Confirm-Started
        }
    }
}
