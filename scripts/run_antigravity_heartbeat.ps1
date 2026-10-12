[CmdletBinding()]
param(
    [string]$Config = "config.yaml",
    [string]$EnvFile = "",
    [string]$PythonExe = ""
)

# The Antigravity liveness watchdog (#68) -- the Windows-Task-Scheduler stand-in
# for the systemd timer the repo's POSIX docs assume.
#
# `heartbeat-check` reads the canonical beat written by `bus-poll --once` and exits
# 0 while the beat is fresh, 3 when it is stale. `--alert` additionally posts a
# channel alert on staleness, which is the dead-man switch: silence from this host
# stops refreshing the beat, and the next check turns that silence into a message
# the swarm can see.
#
# Cadence (15 min) is intentionally shorter than the staleness threshold
# (factor 3 x the 30 min poll interval = 90 min), so a single skipped run is not an
# alarm; only a genuinely dead host trips it. Exit codes pass through unchanged so
# Task Scheduler's "Last Result" column stays meaningful (3 = stale, alerted).
#
# Invocation detail that matters: the CLI logs to stderr, and with
# `$ErrorActionPreference = "Stop"` a native command's stderr becomes a TERMINATING
# error in PowerShell 5.1 -- a plain `& ... *>> $log` call would abort this wrapper
# at its first log line, reporting failure for a run that never really failed. The
# redirection is therefore done by cmd.exe and the wait is process-scoped, matching
# the transport launcher.

$ErrorActionPreference = "Stop"

$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path | Split-Path -Parent
Set-Location -LiteralPath $RepoDir

$StateDir = Join-Path $RepoDir ".agents\state"
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null

Write-Host "[$(Get-Date -Format s)] heartbeat wrapper invoked: config=$Config"

# --- environment ---------------------------------------------------------------
# This matters MOST here. The dead-man watch only needs a token on the STALE path,
# where it must post an alert -- i.e. exactly when something has already gone wrong.
# A watchdog that works in testing (fresh beat => no token needed) and fails
# silently in the one case it exists for is the worst possible shape.
#
# In practice the CLI self-loads .env from CWD (swarmly/telegram/config.py:239) and
# this script pins CWD to the repo root, so the alert DOES send even with an empty
# task environment -- verified by execution with the token stripped. The explicit
# load below removes the reliance on that coincidence.
if (-not $EnvFile) {
    $candidate = Join-Path $RepoDir ".env"
    if (Test-Path -LiteralPath $candidate) { $EnvFile = $candidate }
}
if ($EnvFile) {
    if (-not (Test-Path -LiteralPath $EnvFile)) {
        Write-Host "FATAL: -EnvFile '$EnvFile' does not exist"
        exit 2
    }
    Get-Content -LiteralPath $EnvFile | ForEach-Object {
        if ($_ -match '^\s*([^#=\s]+)\s*=\s*(.*)$') {
            $envName = $matches[1]
            $envValue = $matches[2].Trim()
            if (($envValue.Length -ge 2) -and
                (($envValue.StartsWith('"') -and $envValue.EndsWith('"')) -or
                 ($envValue.StartsWith("'") -and $envValue.EndsWith("'")))) {
                $envValue = $envValue.Substring(1, $envValue.Length - 2)
            }
            Set-Item -Path "Env:$envName" -Value $envValue
        }
    }
    Write-Host "loaded environment from $EnvFile"
}

# --- pre-flight: fail here, with a reason, not in python -----------------------
if (-not (Test-Path -LiteralPath $Config)) {
    $resolved = if ([System.IO.Path]::IsPathRooted($Config)) { $Config } else { Join-Path $RepoDir $Config }
    Write-Host "FATAL: config not found: $resolved"
    exit 2
}

if (-not $PythonExe) {
    $PythonExe = Join-Path $RepoDir ".venv\Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $PythonExe)) {
    Write-Host "FATAL: python not found at '$PythonExe'; pass -PythonExe explicitly."
    exit 2
}

$outLog = Join-Path $StateDir "antigravity.heartbeat-check.out.log"
$errLog = Join-Path $StateDir "antigravity.heartbeat-check.err.log"
Write-Host "heartbeat-check --alert -> $errLog"

# Same shape as the transport launcher: cmd performs the redirection and the wait
# is process-scoped, so a leaked child cannot wedge this wrapper, and the child's
# exit code (0 fresh, 3 stale-and-alerted) passes through honestly.
$cmdLine = '/c ""{0}" -m swarmly.telegram.cli --config "{1}" heartbeat-check --alert 1>"{2}" 2>"{3}""' -f `
    $PythonExe, $Config, $outLog, $errLog

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $env:ComSpec
$psi.Arguments = $cmdLine
$psi.WorkingDirectory = $RepoDir
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true

$proc = New-Object System.Diagnostics.Process
$proc.StartInfo = $psi
[void]$proc.Start()
$proc.WaitForExit()

# Never `exit` a negative status: PowerShell 5.1 refuses it and raises
# "System error.", leaving the task's "Last Result" at -1.
$rc = $proc.ExitCode
if ($rc -lt 0) {
    Write-Host ("heartbeat-check was terminated (status 0x{0:X8})." -f ($rc -band 0xFFFFFFFF))
    exit 1
}

Write-Host "heartbeat-check exited rc=$rc"
exit $rc
