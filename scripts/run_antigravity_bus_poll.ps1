[CmdletBinding()]
param(
    [string]$Config = "config.yaml",
    [string]$EnvFile = "",
    [string]$PythonExe = "",
    [int]$LockTtlMinutes = 10
)

# One durable-bus poll cycle for the Antigravity host (#68).
#
# `bus-poll --once` is the documented per-host scheduler entry point; it needs no
# Telegram token and no running transport. `--quiet` suppresses the human summary
# so the task's only output is the exit code.
#
# This cycle is what writes .agents/state/antigravity.heartbeat.json, which is the
# input the a2a-agy-heartbeat watchdog reads. Cadence (30 min) is deliberately half
# the heartbeat staleness threshold, so one missed cycle is not an alarm.
#
# Invocation detail that matters: the CLI logs to stderr, and with
# `$ErrorActionPreference = "Stop"` a native command's stderr becomes a TERMINATING
# error in PowerShell 5.1, so a plain `& ... *>> $log` call would abort this wrapper
# at the cycle's first log line. The redirection is therefore done by cmd.exe and the
# wait is Process.WaitForExit() -- process-scoped. Do NOT use
# `Start-Process -Wait -RedirectStandardOutput/-Error`: that returns only when the
# redirected HANDLES close, and an `agy` child left over from an injection can hold
# the write end, wedging this wrapper while the cycle is already finished.

$ErrorActionPreference = "Stop"

$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path | Split-Path -Parent
Set-Location -LiteralPath $RepoDir

$StateDir = Join-Path $RepoDir ".agents\state"
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null

Write-Host "[$(Get-Date -Format s)] bus-poll wrapper invoked: config=$Config"

# --- environment ---------------------------------------------------------------
# Same reasoning as the transport wrapper: the CLI self-loads .env from CWD
# (swarmly/telegram/config.py:239), which happens to work because CWD is the repo root
# -- but state the dependency instead of relying on it.
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
# A missing config makes the CLI exit 2, and a scheduler's "Last Result" shows
# only "2" -- the reason is unreadable afterwards.
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

# --- overlap guard -------------------------------------------------------------
# A cycle that outlives its interval must not stack. The TTL is a parameter rather
# than a magic number, and is documented as needing to be >= any plausible cycle
# while staying under the task cadence (30 min) so a crashed cycle can never block
# the next beat -- the failure mode that would matter most, since the beat is what
# the dead-man watch reads.
$lock = Join-Path $StateDir "antigravity.buspoll.lock"
if (Test-Path -LiteralPath $lock) {
    $age = (Get-Date) - (Get-Item -LiteralPath $lock).LastWriteTime
    if ($age.TotalMinutes -lt $LockTtlMinutes) {
        Write-Host "bus-poll: another cycle is in flight (lock age $([int]$age.TotalSeconds)s); no-op."
        exit 0
    }
    Write-Host "bus-poll: taking over a stale lock ($([int]$age.TotalMinutes) min > ${LockTtlMinutes} min TTL)."
}
Set-Content -LiteralPath $lock -Value (Get-Date -Format s)

$outLog = Join-Path $StateDir "antigravity.bus-poll.out.log"
$errLog = Join-Path $StateDir "antigravity.bus-poll.err.log"
Write-Host "bus-poll --once --quiet -> $errLog"

# cmd performs the redirection and the wait is Process.WaitForExit(), which is
# process-scoped. NOT Start-Process -Wait: that returns only when the redirected
# HANDLES close, and a cycle that spawns `agy` leaves a child holding the write end,
# wedging this wrapper after the cycle is already over.
$cmdLine = '/c ""{0}" -m swarmly.telegram.cli --config "{1}" bus-poll --once --quiet 1>"{2}" 2>"{3}""' -f `
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

Remove-Item -LiteralPath $lock -Force -ErrorAction SilentlyContinue

# A force-killed cycle reports a negative status (0xFFFFFFFF renders as -1), and
# PowerShell 5.1 refuses to `exit` a negative code -- it raises "System error." and
# the task's "Last Result" reads as -1, as if this WRAPPER had crashed. Be explicit.
$rc = $proc.ExitCode
if ($rc -lt 0) {
    Write-Host ("bus-poll was terminated (status 0x{0:X8})." -f ($rc -band 0xFFFFFFFF))
    exit 1
}

Write-Host "bus-poll exited rc=$rc"
exit $rc
