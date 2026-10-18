<#
.SYNOPSIS
    One-command setup and start for the Copilot Gateway on Windows PowerShell 5.1 or PowerShell 7.

.DESCRIPTION
    Creates .venv, installs the gateway and its dependencies, installs Playwright Chromium,
    creates .env from .env.example, then starts the gateway in the foreground. Re-running is
    safe: a working .venv is reused and an existing .env is left alone.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\setup.ps1

.EXAMPLE
    .\setup.ps1 -NoStart -NoBrowser
#>
param(
    [switch]$NoStart,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Write-Step([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Warn([string]$Message) { Write-Host "WARNING: $Message" -ForegroundColor Yellow }
function Stop-Setup([string]$Message) { Write-Host "ERROR: $Message" -ForegroundColor Red; exit 1 }

# Runs a command with its output hidden and returns the exit code. Native stderr is not
# allowed to stop the script, because a missing interpreter is an expected probe result.
function Invoke-Quiet([string[]]$Command) {
    $exe = $Command[0]
    $rest = @()
    if ($Command.Count -gt 1) { $rest = $Command[1..($Command.Count - 1)] }
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $exe @rest 2>&1 | Out-Null
        return $LASTEXITCODE
    } catch {
        return 1
    } finally {
        $ErrorActionPreference = $previous
    }
}

# --- Python 3.11+ -------------------------------------------------------------
$versionCheck = 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'
if ($env:PYTHON) {
    # PYTHON is an explicit choice, so there is no fallback.
    $candidates = @($env:PYTHON)
} else {
    # 'py' is the Windows Python launcher.
    $candidates = @('py -3.13', 'py -3.12', 'py -3.11', 'py -3', 'python', 'python3')
}

$python = $null
foreach ($candidate in $candidates) {
    $parts = [string[]]($candidate.Split(' ', [System.StringSplitOptions]::RemoveEmptyEntries))
    if ((Invoke-Quiet ($parts + @('-c', $versionCheck))) -eq 0) {
        $python = $parts
        break
    }
}
if (-not $python) {
    Stop-Setup "Python 3.11 or newer was not found. Install it with: winget install Python.Python.3.12 (then open a new PowerShell window)."
}
Write-Step ("Using Python: " + ($python -join ' '))

# --- Virtualenv ---------------------------------------------------------------
# Windows puts the interpreter in .venv\Scripts; macOS and Linux (PowerShell 7) use .venv/bin.
$isWindowsHost = [System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT
if ($isWindowsHost) {
    $venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
} else {
    $venvPython = Join-Path $PSScriptRoot '.venv/bin/python'
}

if ((Test-Path $venvPython) -and ((Invoke-Quiet @($venvPython, '-c', 'import sys')) -eq 0)) {
    Write-Step "Using existing .venv"
} else {
    if (Test-Path '.venv') {
        Write-Step "Existing .venv does not work on this system (created on another OS?). Recreating it."
        Remove-Item -Recurse -Force '.venv'
    }
    Write-Step "Creating virtualenv in .venv"
    $pyExe = $python[0]
    $pyRest = @()
    if ($python.Count -gt 1) { $pyRest = $python[1..($python.Count - 1)] }
    & $pyExe @pyRest -m venv .venv
    if ($LASTEXITCODE -ne 0) { Stop-Setup "Could not create .venv. On Debian or Ubuntu, install python3-venv." }
    if (-not (Test-Path $venvPython)) { Stop-Setup "The new .venv has no python at $venvPython. Delete .venv and run setup again." }
}

# --- Dependencies -------------------------------------------------------------
Write-Step "Installing the gateway and its dependencies"
& $venvPython -m pip install --quiet --upgrade pip
if ($LASTEXITCODE -ne 0) { Stop-Setup "pip could not upgrade itself." }
& $venvPython -m pip install --quiet -e '.[dev]'
if ($LASTEXITCODE -ne 0) { Stop-Setup "pip install failed. See the messages above." }

# --- Playwright Chromium ------------------------------------------------------
# Microsoft sign-in opens a real browser window, so Chromium is needed.
if (-not $NoBrowser) {
    Write-Step "Installing Playwright Chromium"
    & $venvPython -m playwright install chromium
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "Playwright could not install Chromium, and sign-in needs it."
        Write-Warn "Retry: $venvPython -m playwright install chromium"
        Write-Warn "Or set CHROMIUM_PATH in .env to an existing Chrome or Chromium."
    }
} else {
    Write-Step "Skipping Playwright Chromium (-NoBrowser)"
}

# --- Local settings -----------------------------------------------------------
if (Test-Path '.env') {
    Write-Step ".env already exists, leaving it alone"
} else {
    if (-not (Test-Path '.env.example')) { Stop-Setup ".env.example is missing from this checkout." }
    Copy-Item '.env.example' '.env'
    Write-Step "Created .env from .env.example"
}

# --- Start --------------------------------------------------------------------
if (-not $NoStart) {
    Write-Step "Starting the gateway (Ctrl+C to stop)"
    & $venvPython -m copilot_gateway
    exit $LASTEXITCODE
}
Write-Step "Setup complete. Start the gateway with: $venvPython -m copilot_gateway"
