@echo off
setlocal enabledelayedexpansion
title Hatcut Video Editor

REM Ensure working directory is the script's root folder:
cd /d "%~dp0"

echo ===================================================
echo               Hatcut Video Editor
echo ===================================================
echo.

REM Detect Python executable (python, py -3, or py):
set "PYTHON_CMD="
where python >nul 2>nul
if !ERRORLEVEL! equ 0 (
    set "PYTHON_CMD=python"
) else (
    where py >nul 2>nul
    if !ERRORLEVEL! equ 0 (
        set "PYTHON_CMD=py -3"
    )
)

if "%PYTHON_CMD%"=="" (
    echo [ERROR] Python was not found in your PATH.
    echo Please install Python 3.10+ and ensure "Add python.exe to PATH" is checked during installation.
    echo.
    pause
    exit /b 1
)

REM Check for ffmpeg:
where ffmpeg >nul 2>nul
if !ERRORLEVEL! neq 0 (
    echo [WARNING] ffmpeg was not found in your PATH.
    echo Hatcut uses ffmpeg for media decoding, waveforms, proxies, and rendering.
    echo Please ensure ffmpeg is installed and added to PATH for full functionality.
    echo.
)

REM If arguments were passed, forward them directly:
if not "%~1"=="" (
    echo Opening project: %*
    %PYTHON_CMD% -m hatcut serve %*
    goto finish
)

REM If no project argument was given and demo project is missing, build demo:
if not exist "examples\demo.hatcut" (
    if exist "examples\make_demo.py" (
        echo Generating initial demo project...
        %PYTHON_CMD% examples\make_demo.py
    )
)

if exist "examples\demo.hatcut" (
    echo Starting Hatcut GUI on http://127.0.0.1:8777/ [Demo Project]...
    %PYTHON_CMD% -m hatcut serve "examples\demo.hatcut"
) else (
    echo Starting Hatcut GUI on http://127.0.0.1:8777/...
    %PYTHON_CMD% -m hatcut serve
)

:finish
if !ERRORLEVEL! neq 0 (
    echo.
    echo [ERROR] Editor stopped with exit code !ERRORLEVEL!.
) else (
    echo.
    echo Hatcut session closed.
)
pause
