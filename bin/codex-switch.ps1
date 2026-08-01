# Locates a usable Python and hands off to bin/codex-accounts.py.
$ErrorActionPreference = 'Stop'
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path

# Candidates must be *probed*, not just found on PATH: on Windows `python3` is
# usually the Microsoft Store stub, which exists but is not an interpreter.
$candidates = @($env:CODEX_SWITCH_PYTHON, 'python', 'python3', 'py') |
    Where-Object { $_ }

foreach ($py in $candidates) {
    $cmd = Get-Command $py -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    & $py -c 'import sys; sys.exit(0 if sys.version_info >= (3, 7) else 1)' 2>$null
    if ($LASTEXITCODE -ne 0) { continue }

    $env:PYTHONIOENCODING = 'utf-8'
    & $py (Join-Path $dir 'codex-accounts.py') @args
    exit $LASTEXITCODE
}

Write-Error '[xx] Python 3.7+ not found. Install it, or set CODEX_SWITCH_PYTHON=C:\path\to\python.exe'
exit 1
