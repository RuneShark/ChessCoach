# ChessCoach setup for Windows: a Python virtualenv with the dependencies, plus the
# Stockfish engine. ChessCoach.bat runs this for you on first launch, or run it directly:
#
#     powershell -ExecutionPolicy Bypass -File setup.ps1
#
# Safe to re-run.
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

function Have($name) { [bool](Get-Command $name -ErrorAction SilentlyContinue) }

Write-Host "`nChessCoach setup" -ForegroundColor Cyan

# --- 1. Python ---------------------------------------------------------------
function Update-PathFromEnv {
  $m = [Environment]::GetEnvironmentVariable('Path', 'Machine')
  $u = [Environment]::GetEnvironmentVariable('Path', 'User')
  $env:Path = (@($m, $u) | Where-Object { $_ }) -join ';'
}

# Find a REAL Python 3, returning @(exe, prefix-args). Skips the Microsoft Store
# "App execution alias" — a fake python.exe on PATH that only opens the Store — by
# ignoring anything under WindowsApps and confirming the interpreter actually runs.
function Get-Python {
  foreach ($c in @(@('py', @('-3')), @('python', @()), @('python3', @()))) {
    $exe = $c[0]; $pre = $c[1]
    $cmd = Get-Command $exe -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    if ($cmd.Source -and $cmd.Source -like '*\WindowsApps\*') { continue }
    try {
      $v = & $exe @pre --version 2>$null
      if ($LASTEXITCODE -eq 0 -and "$v" -match 'Python 3') { return , @($exe, $pre) }
    } catch { }
  }
  return $null
}

$py = Get-Python
if (-not $py -and (Have 'winget')) {
  Write-Host "Python 3 not found - installing it (for your user, no admin needed)..." -ForegroundColor Yellow
  winget install -e --id Python.Python.3.12 --scope user --silent `
    --accept-package-agreements --accept-source-agreements
  Update-PathFromEnv
  $py = Get-Python
  if (-not $py) {
    # winget updates PATH in the registry but not this live session; use the fresh
    # per-user install directly so setup finishes in one run.
    $cand = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending | Select-Object -First 1
    if ($cand) { $py = , @($cand.FullName, @()) }
  }
}
if (-not $py) {
  Write-Host "Couldn't find or install Python 3." -ForegroundColor Red
  Write-Host "Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH')," -ForegroundColor Red
  Write-Host "or turn off the Store alias at Settings > Apps > Advanced app settings >" -ForegroundColor Red
  Write-Host "App execution aliases (python.exe / python3.exe), then run this again." -ForegroundColor Red
  exit 1
}
$pyCmd = $py[0]; $pyPre = $py[1]

# --- 2. venv + dependencies --------------------------------------------------
Write-Host "Creating .venv and installing dependencies..."
& $pyCmd @pyPre -m venv .venv
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
  Write-Host "Failed to create the .venv virtualenv - see the messages above." -ForegroundColor Red
  exit 1
}
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install -r requirements.txt --quiet
Write-Host "  dependencies installed." -ForegroundColor Green

# --- 3. Stockfish ------------------------------------------------------------
if ((Test-Path ".\stockfish.exe") -or (Have 'stockfish')) {
  Write-Host "Stockfish already available." -ForegroundColor Green
} else {
  Write-Host "Downloading Stockfish (~110 MB)..."
  try {
    # /releases/latest/download always points at the newest release (no GitHub API).
    $url = "https://github.com/official-stockfish/Stockfish/releases/latest/download/stockfish-windows-x86-64-sse41-popcnt.zip"
    $zip = Join-Path $env:TEMP "cc_sf.zip"
    $dir = Join-Path $env:TEMP "cc_sf"
    Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
    if (Test-Path $dir) { Remove-Item $dir -Recurse -Force }
    Expand-Archive -Path $zip -DestinationPath $dir -Force
    $bin = Get-ChildItem $dir -Recurse -Filter "stockfish-*.exe" | Select-Object -First 1
    Copy-Item $bin.FullName ".\stockfish.exe" -Force
    Remove-Item $zip, $dir -Recurse -Force
    Write-Host "  Stockfish downloaded to .\stockfish.exe" -ForegroundColor Green
  } catch {
    Write-Host "  Couldn't download Stockfish automatically. Get it from" -ForegroundColor Yellow
    Write-Host "  https://stockfishchess.org/download/, put stockfish.exe in this folder, and re-run." -ForegroundColor Yellow
  }
}

Write-Host "`nDone. Launch ChessCoach.bat, or: .\.venv\Scripts\python -m coach.web" -ForegroundColor Cyan
Write-Host "Then open http://127.0.0.1:6464`n" -ForegroundColor Cyan
