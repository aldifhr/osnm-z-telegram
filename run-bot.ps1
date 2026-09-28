<#
.SYNOPSIS
  osnm-z Telegram bot launcher (Windows).

.DESCRIPTION
  Same two-env-file contract as run-bot.sh: osnm_z.config._validate_known_settings
  rejects any key it does not recognise, so Telegram secrets must NOT live in the
  app .env next to WALLET_KEY. This script reads bot\.env into the process
  environment and never passes it to the mint library.

  Windows has no mode 0600. NTFS ACLs are applied instead by setup.ps1, and
  bot.py degrades gracefully: the O_CREAT 0600 flag is ignored by the Win32 CRT
  and os.chmod is a near-no-op, so the key file is only ever as private as the
  directory holding it. Keep the checkout out of shared and OneDrive paths.

.EXAMPLE
  .\run-bot.ps1
  .\run-bot.ps1 -Check      # validate config and exit
#>
[CmdletBinding()]
param(
    # Validate configuration, print the resolved paths, and exit without starting.
    [switch] $Check
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppDir    = Split-Path -Parent $ScriptDir

function Fail($Message) {
    Write-Host "[!] $Message" -ForegroundColor Red
    exit 1
}

# --- locate the upstream checkout -----------------------------------------
# bot.py inserts <checkout>/src onto sys.path, so the layout must be
# <checkout>/bot/run-bot.ps1 with <checkout>/src alongside it.
$SrcDir = Join-Path $AppDir 'src'
if (-not (Test-Path $SrcDir -PathType Container)) {
    Fail "missing $SrcDir - bot.py must live at <osnm-z checkout>\bot\ (it imports ../src)"
}

# --- env files ------------------------------------------------------------
$AppEnv = Join-Path $AppDir '.env'
$BotEnv = Join-Path $ScriptDir '.env'

if (-not (Test-Path $AppEnv -PathType Leaf)) { Fail "missing $AppEnv (app config, holds WALLET_KEY)" }
if (-not (Test-Path $BotEnv -PathType Leaf)) { Fail "missing $BotEnv (telegram config)" }

# --- parse the Telegram env file ourselves --------------------------------
# Get-Content | ForEach over KEY=VALUE. No bash, no coreutils, no quoting rules.
# Later duplicates win, matching how the POSIX `set -a; . file; set +a` behaves.
function Read-EnvFile([string] $Path) {
    $map = @{}
    foreach ($line in [System.IO.File]::ReadAllLines($Path)) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith('#')) { continue }
        $eq = $trimmed.IndexOf('=')
        if ($eq -lt 1) { continue }
        $key = $trimmed.Substring(0, $eq).Trim()
        $value = $trimmed.Substring($eq + 1).Trim()
        if (($value.StartsWith('"') -and $value.EndsWith('"') -and $value.Length -gt 1) -or
            ($value.StartsWith("'") -and $value.EndsWith("'") -and $value.Length -gt 1)) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        $map[$key] = $value
    }
    return $map
}

$BotConfig = Read-EnvFile $BotEnv
foreach ($required in 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_ALLOWED_CHAT_ID') {
    if (-not $BotConfig.ContainsKey($required) -or -not $BotConfig[$required]) {
        Fail "$required not set in $BotEnv"
    }
    Set-Item -Path "env:$required" -Value $BotConfig[$required]
}

# --- uv -------------------------------------------------------------------
$Uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $Uv) {
    foreach ($candidate in @(
        "$env:USERPROFILE\.local\bin\uv.exe",
        "$env:USERPROFILE\.cargo\bin\uv.exe",
        "$env:LOCALAPPDATA\Programs\uv\uv.exe"
    )) {
        if (Test-Path $candidate -PathType Leaf) { $Uv = $candidate; break }
    }
}
if (-not $Uv) { Fail 'uv not found. Install: powershell -c "irm https://astral.sh/uv/install.ps1 | iex"' }

# Keep caches inside the app tree: the default %LOCALAPPDATA% location can be
# unwritable for a service account, and a self-contained cache makes the
# checkout relocatable.
$UvCache = Join-Path $AppDir '.uv-cache'
$TmpDir  = Join-Path $AppDir '.tmp'
New-Item -ItemType Directory -Force -Path $UvCache, $TmpDir | Out-Null
$env:UV_CACHE_DIR = $UvCache
$env:TMPDIR       = $TmpDir
$env:TEMP         = $TmpDir

Push-Location $AppDir
try {
    if ($Check) {
        Write-Host "app dir    : $AppDir" -ForegroundColor Cyan
        Write-Host "app env    : $AppEnv"
        Write-Host "bot env    : $BotEnv"
        Write-Host "uv         : $Uv"
        # Never print the token. A short or malformed value would make
        # Substring throw and take the whole -Check with it, so bound the slice.
        $token = [string] $BotConfig['TELEGRAM_BOT_TOKEN']
        $head = if ($token.Length -gt 6) { $token.Substring(0, 6) } else { '<set>' }
        Write-Host "token      : $head... (set, not printed in full)"
        Write-Host "chat id    : $($BotConfig['TELEGRAM_ALLOWED_CHAT_ID'])"
        # uv creates bin\ on POSIX and Scripts\ on Windows, so accept either.
        $venvPython = @('.venv\Scripts\python.exe', '.venv\bin\python') |
            ForEach-Object { Join-Path $AppDir $_ } |
            Where-Object { Test-Path $_ } |
            Select-Object -First 1
        if ($venvPython) {
            Write-Host "venv       : $venvPython"
        } else {
            Write-Host "venv       : MISSING - run: uv sync --frozen --python 3.12" -ForegroundColor Yellow
        }
        Write-Host "`nconfig OK" -ForegroundColor Green
        return
    }

    & $Uv run --frozen --no-sync python bot/bot.py
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
