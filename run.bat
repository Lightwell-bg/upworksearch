@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [upwork-scout] .venv not found. See README_RU.md, section "Installation".
  exit /b 1
)
".venv\Scripts\python.exe" -m upwork_scout %*
