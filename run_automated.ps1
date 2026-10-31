# Fully automated BTC-Edge setup and run
# Double-click this file to start everything

$ErrorActionPreference = 'Continue'
Set-Location $PSScriptRoot

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "BTC-Edge Fully Automated Setup" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# Check if .venv exists
if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment..." -ForegroundColor Yellow
    python -m venv .venv
    .\.venv\Scripts\Activate.ps1
    pip install -r requirements.txt
}

# Activate virtual environment
. .\.venv\Scripts\Activate.ps1

# Run the fully automated script
Write-Host "Starting automated setup..." -ForegroundColor Green
python scripts/run_all.py --skip-engine

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "Setup Complete!" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Your trade history has been exported to: data/trades/" -ForegroundColor Green
Write-Host ""
Write-Host "To start the trading engine, run:" -ForegroundColor Yellow
Write-Host "  python scripts/run_engine.py" -ForegroundColor Yellow
Write-Host ""
Write-Host "Or open the dashboard at: http://localhost:8080" -ForegroundColor Yellow
Write-Host ""

pause
