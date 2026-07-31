@echo off
rem =====================================================
rem  Link Extractor - Launcher
rem  Double-click to start. All messages come from
rem  start.py (UTF-8 safe). This bat stays pure ASCII
rem  to avoid cmd code-page issues.
rem =====================================================
setlocal

cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    where py >nul 2>nul
    if errorlevel 1 (
        echo [ERROR] Python not found in PATH.
        echo Please install Python 3.10+ and check "Add to PATH".
        pause
        exit /b 1
    )
    py -3 start.py %*
) else (
    python start.py %*
)
