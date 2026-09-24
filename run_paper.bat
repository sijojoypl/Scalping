@echo off
rem Start the Reverse RSI scalper in PAPER mode (simulated fills, no real orders).
cd /d "%~dp0"
python -m scalper paper %*
pause
