@echo off
setlocal
cd /d "%~dp0"
where uv >nul 2>nul
if errorlevel 1 (
  echo Music Video Producer requires uv: https://docs.astral.sh/uv/
  pause
  exit /b 1
)
uv sync
if errorlevel 1 pause & exit /b 1
start "Music Video Producer" http://127.0.0.1:8765
uv run python run.py
