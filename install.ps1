#!/usr/bin/env pwsh
<#
.SYNOPSIS
    One-line installer for codex-switch.

.DESCRIPTION
    irm https://raw.githubusercontent.com/hairbui76/codex-switch/main/install.ps1 | iex

    Needs PowerShell 5+ and Python 3.7+. Does not need git.
    Re-running it is also how you upgrade, though `cx update` is quicker once
    installed.

    Environment overrides:
      CODEX_SWITCH_CHANNEL  main (every push, default) | stable (releases)
      CODEX_SWITCH_HOME     install root
      CODEX_SWITCH_BIN      directory the launcher goes into
      CODEX_SWITCH_PYTHON   interpreter to use
      CODEX_SWITCH_REPO     owner/name to install from
      CODEX_SWITCH_NAME     what to call the command (default: cx)
#>

# The whole installer lives inside this script block, and it must never call
# `exit`. Piped through `iex` this text runs inside the caller's own session
# rather than as a script of its own, and there `exit` closes their PowerShell
# window - even from inside a function or a script block. `return` unwinds only
# the block. Running the file directly (`.\install.ps1`) behaves the same, so
# there is one code path rather than two.
#
# The block also keeps $ErrorActionPreference, $ProgressPreference and
# Resolve-Python scoped to the installer instead of leaking into the session
# that ran it.
& {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'

    $repo = if ($env:CODEX_SWITCH_REPO) { $env:CODEX_SWITCH_REPO } else { 'hairbui76/codex-switch' }
    $channel = if ($env:CODEX_SWITCH_CHANNEL) { $env:CODEX_SWITCH_CHANNEL } else { 'main' }

    # --- interpreter --------------------------------------------------------
    # Candidates must be *probed*, not just found on PATH: on Windows `python3`
    # is usually the Microsoft Store stub, which exists but is not an
    # interpreter.
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
        return
    }
    $pyVersion = (& $py -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])').Trim()
    Write-Host "[ok] Python $pyVersion at $py" -ForegroundColor Green

    # --- bootstrap ----------------------------------------------------------
    # Only a throwaway copy is fetched here, just enough to run `setup`. That
    # copy then resolves the requested channel and installs the real, pinned
    # build, so all the install logic lives in one place instead of being
    # duplicated in shell, PowerShell and Python.
    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("codex-switch-" + [System.Guid]::NewGuid().ToString('N').Substring(0, 8))
    New-Item -ItemType Directory -Path $tmp -Force | Out-Null

    # Unlike the preference variables above, this one is process-wide and would
    # outlive the installer, so it is put back exactly as it was found.
    $savedEncoding = $env:PYTHONIOENCODING
    try {
        Write-Host "[--] fetching $repo"
        $zip = Join-Path $tmp 'bootstrap.zip'
        # A zip rather than a tarball: Expand-Archive is built in, tar.exe is
        # not present before Windows 10 1803.
        try {
            Invoke-WebRequest -Uri "https://codeload.github.com/$repo/zip/main" -OutFile $zip -UseBasicParsing
        }
        catch {
            Write-Host "[xx] could not download $repo - $($_.Exception.Message)" -ForegroundColor Red
            Write-Host '     check the repo name and your network, then re-run.'
            return
        }
        Expand-Archive -Path $zip -DestinationPath $tmp -Force

        $boot = Get-ChildItem -Path $tmp -Directory |
            Where-Object { Test-Path (Join-Path $_.FullName 'bin\codex-accounts.py') } |
            Select-Object -First 1
        if (-not $boot) {
            Write-Host "[xx] the download does not look like $repo" -ForegroundColor Red
            return
        }

        $env:PYTHONIOENCODING = 'utf-8'
        & $py (Join-Path $boot.FullName 'bin\codex-accounts.py') setup --bootstrap --channel $channel
    }
    finally {
        $env:PYTHONIOENCODING = $savedEncoding
        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    }
}
