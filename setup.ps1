<#
.SYNOPSIS
  Windows setup for the osnm-z Telegram bot: dependencies, ACLs, and autostart.

.DESCRIPTION
  Run once in an elevated PowerShell, then again non-elevated if you want the
  autostart task to run as your user rather than SYSTEM.

  The ACL step is the important one. Linux keeps the private key at mode 0600;
  Windows has no equivalent bit, so the key file inherits whatever the parent
  directory allows. This locks <checkout>\.env, <checkout>\bot\.env, and their
  directories down to the current user and SYSTEM only. Without it, any local
  account can read the wallet key.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\setup.ps1
  powershell -ExecutionPolicy Bypass -File .\setup.ps1 -SkipTask
  powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    # Register the scheduled task that starts the bot at logon.
    [switch] $Task,
    # Register the task to start the bot when the machine boots, hidden.
    [switch] $TaskAtStartup,
    # Skip the scheduled task entirely.
    [switch] $SkipTask,
    # Remove the scheduled task and the ACL entries added by this script.
    [switch] $Uninstall
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppDir    = Split-Path -Parent $ScriptDir
$TaskName  = 'osnm-z-telegram-bot'
$CurrentUser = "$env:USERDOMAIN\$env:USERNAME"

function Info($Message)  { Write-Host $Message -ForegroundColor Cyan }
function Ok($Message)    { Write-Host $Message -ForegroundColor Green }
function Warn($Message)  { Write-Host $Message -ForegroundColor Yellow }
function Die($Message)   { Write-Host "[!] $Message" -ForegroundColor Red; exit 1 }

# ── uninstall ────────────────────────────────────────────────────────────
if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Ok "removed scheduled task: $TaskName"
    } else { Info "no scheduled task named $TaskName" }
    exit 0
}

Info "app dir : $AppDir"

# ── 1. dependencies ──────────────────────────────────────────────────────
$Uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $Uv) {
    Warn 'uv not found. Install it with:'
    Warn '  powershell -c "irm https://astral.sh/uv/install.ps1 | iex"'
    Die  'uv is required.'
}

Push-Location $AppDir
try {
    if (-not (Test-Path (Join-Path $AppDir '.venv'))) {
        Info 'creating the virtualenv (first run, downloads the toolchain)'
        & $Uv sync --frozen --python 3.12
        if ($LASTEXITCODE -ne 0) { Die "uv sync failed" }
    }
    # The Telegram layer is not in the upstream lockfile.
    $req = Join-Path $ScriptDir 'requirements-bot.txt'
    if (Test-Path $req -PathType Leaf) {
        Info 'installing the telegram layer'
        & $Uv pip install -r $req
        if ($LASTEXITCODE -ne 0) { Die 'installing requirements-bot.txt failed' }
    }
    Ok 'dependencies present'
}
finally { Pop-Location }

# ── 2. env files ─────────────────────────────────────────────────────────
$AppEnv = Join-Path $AppDir '.env'
$BotEnv = Join-Path $ScriptDir '.env'
$AppExample = Join-Path $ScriptDir 'app.env.example'
$BotExample = Join-Path $ScriptDir 'bot.env.example'

if (-not (Test-Path $AppEnv -PathType Leaf)) {
    if (Test-Path $AppExample -PathType Leaf) {
        Copy-Item $AppExample $AppEnv
        Warn "created $AppEnv from app.env.example - set WALLET_KEY and RPC_URL"
    } else { Die "missing $AppEnv and no app.env.example to copy from" }
}
if (-not (Test-Path $BotEnv -PathType Leaf)) {
    if (Test-Path $BotExample -PathType Leaf) {
        Copy-Item $BotExample $BotEnv
        Warn "created $BotEnv from bot.env.example - set TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_CHAT_ID"
    } else { Die "missing $BotEnv and no bot.env.example to copy from" }
}

# ── 3. ACLs: the Linux 0600 equivalent ────────────────────────────────────
# icacls is used rather than the ACL cmdlets because it is available on every
# Windows edition, including Home, where Get-Acl/Set-Acl behave inconsistently.
function Lock-Down([string] $Path, [switch] $IsDirectory) {
    if (-not (Test-Path $Path)) { return }
    $target = if ($IsDirectory) { "$Path\*" } else { $Path }
    $inherit = if ($IsDirectory) { '(OI)(CI)' } else { '' }
    # Drop inheritance, then grant only this user and SYSTEM.
    & icacls.exe $Path /inheritance:r | Out-Null
    & icacls.exe $target "/reset" 2>$null | Out-Null
    # ${CurrentUser}: PowerShell reads the ':' as a scope qualifier, so the
    # variable name must be delimited with braces before the (F) permission.
    & icacls.exe $target "/grant:r" "${CurrentUser}:(F)" "SYSTEM:(F)" "$inherit" | Out-Null
    & icacls.exe $target "/inheritance:r" 2>$null | Out-Null
}

Info 'locking down the env files (the 0600 equivalent)'
foreach ($secret in @($AppEnv, $BotEnv)) {
    if (Test-Path $secret -PathType Leaf) {
        Lock-Down $secret
        $acl = (& icacls.exe $secret) -join "`n"
        if ($acl -match 'BUILTIN\\Users') {
            Die "$secret still grants access to BUILTIN\Users - refusing to continue"
        }
        # ${name} is required: PowerShell would read "$secret -" as a variable name.
        Ok "  ${secret} -> ${CurrentUser} + SYSTEM only"
    }
}
# The venv and the checkout tree can hold the caches; lock the .env parents too.
Lock-Down (Join-Path $ScriptDir '.env') -IsDirectory:$false 2>$null | Out-Null

# ── 4. self-check ────────────────────────────────────────────────────────
$check = & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ScriptDir 'run-bot.ps1') -Check 2>&1
$check | ForEach-Object { Write-Host "  $_" }
if ($LASTEXITCODE -ne 0) { Die 'run-bot.ps1 -Check failed' }
Ok 'configuration validated'

# ── 5. autostart ─────────────────────────────────────────────────────────
if ($SkipTask) {
    Info 'skipping the scheduled task (-SkipTask). Start it with:'
    Info '  powershell -ExecutionPolicy Bypass -File .\run-bot.ps1'
    exit 0
}

$Action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument ('-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f (Join-Path $ScriptDir 'run-bot.ps1')) `
    -WorkingDirectory $AppDir

if ($TaskAtStartup) {
    # Runs as SYSTEM at boot, so logon triggers and the user profile do not exist.
    $Trigger    = New-ScheduledTaskTrigger -AtStartup
    $Principal  = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    Info 'registering an at-startup task as SYSTEM'
} else {
    # Logon trigger runs in the user context, which is where uv and the venv live.
    $Trigger    = New-ScheduledTaskTrigger -AtLogOn -User $CurrentUser
    $Principal  = New-ScheduledTaskPrincipal -UserId $CurrentUser -LogonType Interactive -RunLevel Limited
    Info 'registering an at-logon task'
}

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Info "replaced the existing task: $TaskName"
}
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings | Out-Null
Ok "scheduled task registered: $TaskName"

Info ''
Info 'next steps:'
Info "  status   : Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo"
Info "  start    : Start-ScheduledTask -TaskName $TaskName"
Info "  logs     : \$env:APPDATA\osnm-z-telegram-bot\bot.log"
Info '  foreground for debugging:'
Info "    powershell -ExecutionPolicy Bypass -File .\run-bot.ps1"
