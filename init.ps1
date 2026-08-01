#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Point your PowerShell profile at *this checkout* - for hacking on the tool.

.DESCRIPTION
    Commands run straight out of the working tree, so an edit takes effect with
    no reinstall, and updates come from `git pull`.

    For normal use install a managed copy instead, which can self-update:
      irm https://raw.githubusercontent.com/hairbui76/cx-switch/main/install.ps1 | iex
#>

$ErrorActionPreference = 'Stop'

$dir = $PSScriptRoot
$begin = '# >>> codex-switch >>>'
$end = '# <<< codex-switch <<<'

# --- sanity check -----------------------------------------------------------
# Candidates must be *probed*, not just found on PATH: on Windows `python3` is
# usually the Microsoft Store stub, which exists but is not an interpreter.
function Resolve-Python {
    foreach ($name in @($env:CODEX_SWITCH_PYTHON, 'python3', 'python', 'py')) {
        if (-not $name) { continue }
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        & $cmd.Source -c 'import sys; sys.exit(0 if sys.version_info >= (3, 7) else 1)' 2>$null
        if ($LASTEXITCODE -eq 0) { return $cmd.Source }
    }
    return $null
}

$py = Resolve-Python
if (-not $py) {
    Write-Host '[xx] Python 3.7+ not found.' -ForegroundColor Red
    Write-Host '     Install from https://python.org or the Microsoft Store, then re-run.'
    exit 1
}
$version = (& $py -c 'import sys; print("%d.%d" % sys.version_info[:2])').Trim()
Write-Host "[ok] Python $version at $py" -ForegroundColor Green

# --- write the profile block ------------------------------------------------
$switch = Join-Path $dir 'bin\codex-switch.ps1'

$block = @"
$begin
# Managed by init.ps1 (dev checkout at $dir) - edit the repo, not this block.
function cx { & '$switch' @args }
$end
"@

$profilePath = $PROFILE.CurrentUserAllHosts
$profileDir = Split-Path $profilePath -Parent
if (-not (Test-Path $profileDir)) { New-Item -ItemType Directory -Path $profileDir -Force | Out-Null }

$existing = if (Test-Path $profilePath) { Get-Content $profilePath -Raw } else { '' }

# Drop any previously installed block so re-running stays idempotent.
$pattern = [regex]::Escape($begin) + '.*?' + [regex]::Escape($end)
$cleaned = ([regex]::Replace($existing, $pattern, '', 'Singleline')).TrimEnd()

$updated = if ($cleaned) { "$cleaned`r`n`r`n$block`r`n" } else { "$block`r`n" }
Set-Content -Path $profilePath -Value $updated -Encoding UTF8

Write-Host "[ok] Installed into $profilePath" -ForegroundColor Green
Write-Host ''
Write-Host 'Run this to activate now:' -ForegroundColor Cyan
Write-Host "  . `$PROFILE.CurrentUserAllHosts"
Write-Host '  (or just open a new terminal)'
Write-Host ''
Write-Host 'Commands:'
Write-Host '  cx add           Log in as a new account'
Write-Host '  cx save <name>   Save the account you are logged in as'
Write-Host '  cx <name>        Switch to an account'
Write-Host '  cx list          List saved accounts'
Write-Host '  cx status        Show the current account'
Write-Host '  cx next          Switch to the next account'
Write-Host '  cx usage         Usage for every account'
Write-Host '  cx doctor        Diagnose setup problems'
Write-Host ''
Write-Host 'This is a dev checkout: update it with `git pull`.'
Write-Host '`cx update` only works on a managed install.'
Write-Host ''
Write-Host 'Coming from the v1 script? Run: cx migrate' -ForegroundColor Yellow
