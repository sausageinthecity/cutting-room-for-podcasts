# run.ps1 - the one file Task Scheduler runs (Module 4.6).
#
# Works from any starting folder: Task Scheduler starts in C:\Windows\System32.
#   1. Goes to the project folder (wherever this file lives).
#   2. Re-reads PATH from Windows' own settings, so ffmpeg and claude are found even in
#      a bare session that loaded no profile.
#   3. Checks that the project's Python, ffmpeg and claude are there. A missing tool is
#      one clear line here, and run.py also puts it in the log and the morning sheet.
#   4. Runs run.py with the project's own Python in .venv, and passes back its exit code:
#      0 = all good, 1 = something is listed under Problems in logs\morning.txt.
#
# Run it by hand with:
#   powershell -NoProfile -ExecutionPolicy Bypass -File "C:\path\to\run.ps1"

$ErrorActionPreference = 'Stop'
$project = $PSScriptRoot
Set-Location -LiteralPath $project

# PATH as Windows has it stored (machine + user), plus anything this session already had.
$stored = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
          [Environment]::GetEnvironmentVariable('Path', 'User')
$env:Path = $stored + ';' + $env:Path

$python = Join-Path $project '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    $msg = "The project's Python is missing ($python). Recreate .venv, then run: .venv\Scripts\pip install -r requirements.txt"
    Write-Output "PROBLEM: $msg"
    # run.py can't start, so it can't write the morning sheet. Write it here instead.
    $logs = Join-Path $project 'logs'
    New-Item -ItemType Directory -Force -Path $logs | Out-Null
    $sheet = "MORNING SHEET  $(Get-Date -Format 'ddd MMM dd'), the line did NOT run at $(Get-Date -Format 'HH:mm')`r`n`r`nNEEDS YOU`r`n  $msg`r`n"
    Set-Content -LiteralPath (Join-Path $logs 'morning.txt') -Value $sheet -Encoding UTF8
    exit 1
}

foreach ($tool in 'ffmpeg', 'ffprobe', 'claude') {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Write-Output "PROBLEM: $tool wasn't found, so parts of the line will fail. Reinstall it, then restart the computer."
    }
}

# Python's own output in UTF-8, so show titles with odd characters survive.
$env:PYTHONIOENCODING = 'utf-8'
$sheetPath = Join-Path $project 'logs\morning.txt'
$startedAt = Get-Date
# 'Continue' here: if this script's output is ever redirected, PowerShell 5.1 treats
# Python's progress bars (on stderr) as errors, and 'Stop' would end the run over them.
$ErrorActionPreference = 'Continue'
& $python (Join-Path $project 'run.py')
$code = $LASTEXITCODE

# Backstop: run.py always writes the sheet (or a busy run leaves it alone and exits 0).
# If it exited with a problem and the sheet wasn't touched since it started, run.py died
# before it could write anything, so write the sheet here.
$touched = (Test-Path -LiteralPath $sheetPath) -and ((Get-Item -LiteralPath $sheetPath).LastWriteTime -ge $startedAt.AddSeconds(-2))
if ($code -ne 0 -and -not $touched) {
    New-Item -ItemType Directory -Force -Path (Split-Path $sheetPath) | Out-Null
    $sheet = "MORNING SHEET  $(Get-Date -Format 'ddd MMM dd'), $(Get-Date -Format 'HH:mm'): the line crashed before it could write its own sheet (exit code $code).`r`n`r`nNEEDS YOU`r`n  -> Run run.ps1 by hand in PowerShell to see the error.`r`n"
    Set-Content -LiteralPath $sheetPath -Value $sheet -Encoding UTF8
}
exit $code
