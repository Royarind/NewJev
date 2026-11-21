@echo off
title NewJev Agent
color 0B

echo.
echo   =========================================
echo     NewJev Computer Use Agent
echo   =========================================
echo.

:: Check if .env exists
if not exist ".env" (
    echo   [WARNING] .env not found!
    echo   Copying .env.example to .env ...
    copy ".env.example" ".env" >nul 2>&1
    echo   Please edit .env and add your API keys.
    echo   Then run start.bat again.
    echo.
    pause
    exit /b 1
)

:: Check if .venv exists, create if not
if not exist ".venv" (
    echo   Setting up virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo   [ERROR] Failed to create virtual environment.
        echo   Make sure Python 3.11+ is installed.
        pause
        exit /b 1
    )
    echo   Installing dependencies...
    .venv\Scripts\pip install -e . --quiet
    if errorlevel 1 (
        echo   [ERROR] Failed to install dependencies.
        pause
        exit /b 1
    )
    echo   Installing Playwright browsers...
    .venv\Scripts\playwright install chromium
    echo.
)

:: Newer features need a few extra packages: install them once if they are missing
.venv\Scripts\python -c "import openpyxl, pypdf, reportlab" 2>nul || (
    echo   Installing new dependencies...
    .venv\Scripts\pip install -e . --quiet
)

:: Activate and run
call .venv\Scripts\activate.bat

if "%1"=="test" goto run_tests
if "%1"=="--test" goto run_tests

python run.py %*
goto end

:run_tests
shift
pytest tests\test_tools.py tests\test_memory.py tests\test_goal_understanding.py %1 %2 %3 %4 %5 %6 %7 %8 %9
goto end

:end
if errorlevel 1 (
    echo.
    echo   [ERROR] Agent exited with an error.
    pause
)

