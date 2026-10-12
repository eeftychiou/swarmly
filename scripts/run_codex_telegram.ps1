[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Config,
    [string]$EnvFile = "",
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"
$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path | Split-Path -Parent

if (-not (Test-Path -LiteralPath $Config)) {
    throw "Codex Telegram config not found: $Config"
}
if (-not $PythonExe) {
    $PythonExe = Join-Path $RepoDir ".venv\Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $PythonExe)) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand) {
        $PythonExe = $pythonCommand.Source
    }
}
if (-not $PythonExe -or -not (Test-Path -LiteralPath $PythonExe)) {
    throw "Python executable not found. Create .venv or pass -PythonExe explicitly."
}

if ($EnvFile -and (Test-Path -LiteralPath $EnvFile)) {
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
}

# The daemon spawns the Codex CLI by name (the `codex` and `codex_thread` adapters). Service
# and task environments often carry a minimal PATH, and then every message fails with
# `[WinError 2]`. Resolve the CLI on this machine, pin its directory for the child process,
# and print what was chosen instead of failing silently later.
$codexCommand = Get-Command codex -ErrorAction SilentlyContinue
if (-not $codexCommand -and $env:LOCALAPPDATA) {
    $codexBinRoot = Join-Path $env:LOCALAPPDATA "OpenAI\Codex\bin"
    if (Test-Path -LiteralPath $codexBinRoot) {
        $codexCandidate = Get-ChildItem -LiteralPath $codexBinRoot -Recurse -Filter "codex.exe" `
            -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending |
            Select-Object -First 1
        if ($codexCandidate) { $codexCommand = $codexCandidate }
    }
}
if ($codexCommand) {
    $codexPath = if ($codexCommand -is [System.Management.Automation.CommandInfo]) {
        $codexCommand.Source
    } else {
        $codexCommand.FullName
    }
    $codexDir = Split-Path -Parent $codexPath
    if ($codexDir) { $env:PATH = "$codexDir;$env:PATH" }
    $codexVersion = (& $codexPath --version 2>$null | Select-Object -First 1)
    if (-not $codexVersion) {
        Write-Warning "codex CLI at $codexPath did not report a version - check the install."
    }
    Write-Host "codex CLI       : $codexPath $codexVersion"
} else {
    Write-Warning ("codex CLI not found on PATH - codex/codex_thread adapters cannot run. " +
        "Install the Codex CLI or put its directory on PATH before starting the transport.")
}

& $PythonExe -m swarmly.telegram.cli --config $Config check
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
& $PythonExe -m swarmly.telegram.cli --config $Config run
exit $LASTEXITCODE
