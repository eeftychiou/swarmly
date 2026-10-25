[CmdletBinding()]
param(
    [string]$Config = "config.yaml",
    [string]$EnvFile = "",
    [string]$PythonExe = ""
)

# Starts the Antigravity Telegram transport on a Windows host with no systemd
# (issue #68), designed to be run BY a scheduled task.
#
# Four hard-won details, each one paid for by a failed run:
#
# 1. The transport runs in the FOREGROUND: this script does not return until the
#    transport exits. That is deliberate. A `Start-Process -PassThru` child that
#    the launcher ABANDONS does not outlive its spawning shell -- verified on this
#    host: the child connected, published its online notice, then died the moment
#    the invoking process went away. Hosting it here makes the caller's lifetime
#    the transport's lifetime, which is exactly what supervision needs.
#
# 2. DO NOT invoke it as `& $PythonExe ... *>> $log` with
#    `$ErrorActionPreference = "Stop"` set. The transport writes its logging to
#    stderr; PowerShell 5.1 turns a native command's stderr into a
#    NativeCommandError, and with EAP=Stop that is a TERMINATING error -- the
#    launcher kills the transport at its very first log line ("connected as
#    @example_agy_bot"), the task reports 1, and the transport log stays empty. That
#    failure was observed twice before the transcript below made it visible.
#    Start-Process -Wait hands the child its own file handles instead, so its
#    stderr never enters PowerShell's error stream at all.
#
# 3. `run` has no single-instance guard of its own (issue #77), so two pollers on
#    one token fail silently and confusingly. The liveness pre-check below makes
#    "start the transport" idempotent.
#
# 4. A scheduled task DOES get a console when the action is `powershell -File`
#    with LogonType=Interactive; that console is the window users see flash. An
#    earlier version of this note claimed the opposite ("a scheduled task has no
#    console"), which was wrong on this host and hid the cause for a while. The
#    window is not suppressed by Task Scheduler's `Hidden` flag either: that flag
#    only hides the task from the Task Scheduler UI. What actually silences it is
#    not being a console process at all -- the tasks therefore start this script
#    through scripts\run_hidden.vbs (wscript.exe, a GUI-subsystem binary), which
#    is what docs\telegram\SETUP-ANTIGRAVITY.md now instructs.
#
#    Either way the console is not a log: its output is discarded when the task
#    ends, so the Start-Transcript below remains the durable record. Read it
#    before guessing why a run did nothing.
#
# 5. Do NOT go back to `Start-Process -Wait -RedirectStandardOutput/-Error`.
#    That form returns only when the redirected handles are CLOSED, not when the
#    child exits -- and the transport's own adapter spawns `agy` children that
#    INHERIT those handles (swarmly/telegram/adapters/antigravity.py:235
#    create_subprocess_exec, and the :630/:658 subprocess.run calls for agentapi).
#    When a reply is in flight the transport dies (or is killed) while an agy
#    child lives on still holding the write end, and the launcher then blocks
#    INSIDE Start-Process for as long as that child survives. The task stays
#    Running the whole time, so IgnoreNew refuses every subsequent start
#    (0x800710E0) and the transport is never restarted -- unsupervised, while
#    "Last Result" still reads success. Observed live: launcher wedged from
#    09:07 with no python process at all, until it was killed by hand.
#
# Combined with a task whose repetition is set to skip-while-running (Task
# Scheduler's default IgnoreNew), this doubles as restart-on-failure: while the
# transport runs, each repetition is skipped; when it dies, the next starts it.

$ErrorActionPreference = "Stop"

$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path | Split-Path -Parent
Set-Location -LiteralPath $RepoDir

$StateDir = Join-Path $RepoDir ".agents\state"
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null

Start-Transcript -Path (Join-Path $StateDir "antigravity.transport.host.log") -Append | Out-Null

Write-Host "[$(Get-Date -Format s)] launcher invoked: config=$Config envfile=$EnvFile"

# --- environment ---------------------------------------------------------------
# The config may name a token via `bot_token_env` (config.yaml does:
# A2A_TELEGRAM_BOT_TOKEN_ANTIGRAVITY). AgentConfig.from_dict self-loads .env from
# the config's own directory and then from CWD (swarmly/telegram/config.py:239), and
# this script pins CWD to the repo root -- which is why the transport starts from
# a task whose environment carries no token. That works, but it is a HIDDEN
# dependency: it breaks the moment someone runs this wrapper from elsewhere. Load
# the env file explicitly (same pattern as run_codex_telegram.ps1 / SETUP-CODEX 6)
# so the dependency is stated rather than incidental.
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
# A missing config makes the CLI exit 2. A scheduler's "Last Result" shows only
# "2", so the reason is invisible after the fact. Check cheaply and say why.
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

# --- liveness pre-check (issue #77) ------------------------------------------
# Best-effort only (#77 remains the real single-instance guard): this is a
# snapshot of the process table, so it can race a transport that is starting or
# exiting. It is deliberately here because a false positive is cheap (skip a
# tick, try again in a minute) while a false negative is not (two pollers on one
# token fail silently).
#
# The match is anchored to the ARGV TAIL (F13, PR #83 review): a bare `\brun\b`
# also matches unrelated arguments, e.g. `--config C:\run\cfg.yaml`. The guard
# would then report "already running" and the transport would NEVER be started
# -- a silent no-supervision failure, which is the opposite of what this script
# exists to do. Requiring `cli` ... `run` as the final argument removes that.
$runPattern = '(?:a2a|swarmly)\.telegram\.cli\b.*[\s"]run(\s+--?[^\s]+)*["'']?\s*$'

$running = @(
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match $runPattern }
)

if ($running.Count -gt 0) {
    $pids = ($running | ForEach-Object { $_.ProcessId }) -join ','
    # The venv python is a trampoline: parent and interpreter child both carry the
    # argv, so this normally reports two pids for one transport. Harmless for a
    # `-gt 0` test, but the message says process(es) so it is not read as a double-run.
    Write-Host "a2a transport already running (process(es) pid $pids); no-op."
    exit 0
}

# --- run in the foreground (see notes 2 and 5 above) --------------------------
$outLog = Join-Path $StateDir "antigravity.transport.out.log"
$errLog = Join-Path $StateDir "antigravity.transport.err.log"

Write-Host "starting a2a transport -> $outLog (stderr: $errLog)"

# cmd performs the redirection, NOT Start-Process. See note 5: Start-Process -Wait
# returns only once the redirected handles are CLOSED, which an orphaned agy child
# can hold open indefinitely, wedging this launcher while the transport is already
# dead. A .NET Process with UseShellExecute=$false waits for the CHILD only, and
# cmd /c propagates the child's exit code so the task's Last Result stays honest.
$cmdLine = '/c ""{0}" -m swarmly.telegram.cli --config "{1}" run 1>"{2}" 2>"{3}""' -f `
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

# A transport that was KILLED (by an operator, by Task Scheduler, or by a crash)
# reports a negative status here -- 0xFFFFFFFF renders as -1. Never hand that to
# `exit`: PowerShell 5.1 refuses a negative exit code, raises "System error." in the
# task transcript, and the task's "Last Result" then shows -1 -- which reads as if
# the LAUNCHER crashed rather than the transport having been killed. Say what
# happened instead.
$rc = $proc.ExitCode
if ($rc -lt 0) {
    Write-Host ("transport was terminated (status 0x{0:X8}); the task's repetition will start it again." -f ($rc -band 0xFFFFFFFF))
    exit 1
}

Write-Host "transport exited rc=$rc"
exit $rc
