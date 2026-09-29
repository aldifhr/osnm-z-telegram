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
  powershell -ExecutionPolicy Bypass -File .\setup.ps1 -OsnmZPath C:\src\osnm-z
  powershell -ExecutionPolicy Bypass -File .\setup.ps1 -OsnmZPath C:\src\osnm-z -SkipTask
  powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    # Path to the osnm-z checkout. The bot files are copied into its bot\
    # directory, so this is the directory that holds src\ and uv.lock.
    # Not mandatory: -Uninstall only removes the scheduled task.
    [string] $OsnmZPath,

    # Register the scheduled task that starts the bot at logon.
    [switch] $Task,
    # Register the task to start the bot when the machine boots, hidden.
    [switch] $TaskAtStartup,
    # Skip the scheduled task entirely.
    [switch] $SkipTask,
    # Remove the scheduled task.
    [switch] $Uninstall
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$TaskName  = 'osnm-z-telegram-bot'
$CurrentUser = "$env:USERDOMAIN\$env:USERNAME"

function Info($Message)  { Write-Host $Message -ForegroundColor Cyan }
function Ok($Message)    { Write-Host $Message -ForegroundColor Green }
function Warn($Message)  { Write-Host $Message -ForegroundColor Yellow }
function Die($Message)   { Write-Host "[!] $Message" -ForegroundColor Red; exit 1 }

# ── uninstall ────────────────────────────────────────────────────────────
if ($Uninstall) {
    # -Uninstall only removes the scheduled task, so it must work without
    # -OsnmZPath; a Mandatory parameter here would block the one command that
    # does not need a checkout.
    if (Get-Command Get-ScheduledTask -ErrorAction SilentlyContinue) {
        if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
            Ok "removed scheduled task: $TaskName"
        } else { Info "no scheduled task named $TaskName" }
    } else {
        # Task Scheduler cmdlets are Windows-only; without this the raw
        # CommandNotFoundException surfaces instead of a usable message.
        Warn 'Get-ScheduledTask is unavailable here, so no task was removed.'
    }
    exit 0
}

if (-not $OsnmZPath) {
    Die 'pass -OsnmZPath <checkout>. Example: .\setup.ps1 -OsnmZPath C:\src\osnm-z'
}
$OsnmZPath = (Resolve-Path -LiteralPath $OsnmZPath).Path
if (-not (Test-Path (Join-Path $OsnmZPath 'src\osnm_z') -PathType Container)) {
    Die "$OsnmZPath does not look like an osnm-z checkout (no src\osnm_z). Pass -OsnmZPath <checkout>."
}
if (-not (Test-Path (Join-Path $OsnmZPath 'uv.lock') -PathType Leaf)) {
    Die "$OsnmZPath has no uv.lock. Run 'uv sync --frozen' there first."
}
Info "osnm-z   : $OsnmZPath"

# ── 1. dependencies ──────────────────────────────────────────────────────
$Uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $Uv) {
    Warn 'uv not found. Install it with:'
    Warn '  powershell -c "irm https://astral.sh/uv/install.ps1 | iex"'
    Die  'uv is required.'
}

Push-Location $OsnmZPath
try {
    if (-not (Test-Path (Join-Path $OsnmZPath '.venv'))) {
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

# ── 2. assemble the install: copy the bot into <osnm-z>\bot ─────────────
# An upstream clone has no bot\ directory, and bot.py has to sit at
# <osnm-z>\bot\bot.py with osnmzbot\ beside it so that ../src resolves the
# upstream package. Copying here is what makes the rest of this script and
# run-bot.ps1 work; without it the launcher fails on a missing src\ directory.
$BotDir = Join-Path $OsnmZPath 'bot'
if (-not (Test-Path $BotDir -PathType Container)) {
    New-Item -ItemType Directory -Path $BotDir | Out-Null
    Info "created $BotDir"
}

Info 'copying the bot into the checkout'
foreach ($name in 'bot.py', 'supply.py', 'test_bot.py', 'test_status.py',
                'test_simulate.py', 'test_simulate_ui.py',
                'run-bot.sh', 'run-bot.ps1', 'run-bot.cmd') {
    $source = Join-Path $ScriptDir $name
    if (Test-Path $source -PathType Leaf) {
        Copy-Item $source (Join-Path $BotDir $name) -Force
    }
}
Copy-Item (Join-Path $ScriptDir 'osnmzbot') $BotDir -Recurse -Force
Ok "bot files in $BotDir"

# .env: create the app config from the example, never overwrite an existing one
$AppEnv = Join-Path $OsnmZPath '.env'
$AppExample = Join-Path $ScriptDir 'app.env.example'
if (-not (Test-Path $AppEnv -PathType Leaf)) {
    if (Test-Path $AppExample -PathType Leaf) {
        Copy-Item $AppExample $AppEnv
        Warn "created .env from app.env.example - set WALLET_KEY and RPC_URL"
    } else { Die 'no .env and no app.env.example to copy from' }
}
$BotEnv = Join-Path $BotDir '.env'
$BotExample = Join-Path $ScriptDir 'bot.env.example'
if (-not (Test-Path $BotEnv -PathType Leaf)) {
    if (Test-Path $BotExample -PathType Leaf) {
        Copy-Item $BotExample $BotEnv
        Warn 'created bot\.env from bot.env.example - set TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_CHAT_ID'
    } else { Die 'no bot\.env and no bot.env.example to copy from' }
}


# ── 3. ACLs: the Linux 0600 equivalent ────────────────────────────────────
# icacls is used rather than the ACL cmdlets because it is available on every
# Windows edition, including Home, where Get-Acl/Set-Acl behave inconsistently.
# It is a Windows tool, so on anything else the step is skipped with a warning
# rather than aborting the install.
if (-not (Get-Command icacls.exe -ErrorAction SilentlyContinue)) {
    Warn 'icacls.exe is unavailable (not Windows), so the .env files were not ACL-locked.'
    Warn 'On Windows, re-run this script to apply the lock-down.'
} else {
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
# The env files now live in the checkout, not next to this script.
Lock-Down $BotEnv -IsDirectory:$false | Out-Null
}

# ── 4. self-check ────────────────────────────────────────────────────────
# Check the launcher as it will actually run: from <osnm-z>\bot, against that
# checkout's .env. Checking the copy next to this script would validate the
# wrong directory and report a confusing config error.
$launcher = Join-Path $BotDir 'run-bot.ps1'
if (-not (Test-Path $launcher -PathType Leaf)) {
    Die "missing $launcher; the install is incomplete"
}
$check = $null
$shell = Get-Command powershell -ErrorAction SilentlyContinue
if (-not $shell) {
    # No Windows PowerShell host (this script is also linted on other
    # platforms); report and let the operator run the check themselves.
    Warn 'powershell was not found, so the self-check was skipped.'
    Warn "Run it manually:  pwsh -File `"$(Join-Path $BotDir 'run-bot.ps1')`" -Check"
} else {
    $check = & $shell.Source -NoProfile -ExecutionPolicy Bypass -File $launcher -Check 2>&1
    $check | ForEach-Object { Write-Host "  $_" }
    if ($LASTEXITCODE -ne 0) { Die 'run-bot.ps1 -Check failed' }
    Ok 'configuration validated'
}

# ── 5. autostart ─────────────────────────────────────────────────────────
if ($SkipTask) {
    Info 'skipping the scheduled task (-SkipTask). Start it with:'
    Info '  powershell -ExecutionPolicy Bypass -File .\run-bot.ps1'
    exit 0
}

if (-not (Get-Command Register-ScheduledTask -ErrorAction SilentlyContinue)) {
    # The ScheduledTasks module is Windows-only. Everything else is installed;
    # only the autostart entry is missing, so say so instead of crashing.
    Warn 'Register-ScheduledTask is unavailable (not Windows), so no autostart was set up.'
    Info 'Start the bot manually, or re-run this script on Windows.'
    exit 0
}

$Action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument ('-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f (Join-Path $BotDir 'run-bot.ps1')) `
    -WorkingDirectory $OsnmZPath

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
Info "  logs     : $(Join-Path $BotDir 'logs\bot.log')"
Info '  foreground for debugging:'
Info "    powershell -ExecutionPolicy Bypass -File .\run-bot.ps1"
