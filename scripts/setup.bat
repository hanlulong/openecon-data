@echo off
REM econ-data-mcp Setup Script for Windows (Command Prompt)
setlocal enabledelayedexpansion

echo econ-data-mcp Setup for Windows
echo ===========================
echo.

echo Checking prerequisites...
echo.

REM Check Node.js
where node >nul 2>nul
if %errorlevel% neq 0 (
    echo ERROR: Node.js is not installed. Please install Node.js ^>= 18.0.0
    echo        Download from: https://nodejs.org/
    exit /b 1
)
echo OK: Node.js is installed

REM Check npm
where npm >nul 2>nul
if %errorlevel% neq 0 (
    echo ERROR: npm is not installed
    exit /b 1
)
echo OK: npm is installed

REM Check Python
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo ERROR: Python is not installed. Please install Python ^>= 3.8
    echo        Download from: https://www.python.org/downloads/
    echo        Make sure to check "Add Python to PATH" during installation
    exit /b 1
)
echo OK: Python is installed
echo.

REM Install Node dependencies
echo Installing Node.js dependencies...
call npm install
if %errorlevel% neq 0 (
    echo ERROR: Failed to install Node.js dependencies
    exit /b 1
)
echo.

REM Create Python virtual environment
echo Creating Python virtual environment...
python -m venv backend\.venv
if %errorlevel% neq 0 (
    echo ERROR: Failed to create virtual environment
    exit /b 1
)
echo.

REM Install Python dependencies
echo Installing Python dependencies...
call backend\.venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r backend\requirements.txt
if %errorlevel% neq 0 (
    echo ERROR: Failed to install Python dependencies
    exit /b 1
)
call deactivate
echo.

REM Create .env file if it doesn't exist
if not exist ".env" (
    echo Creating .env file from template...
    copy .env.example .env
    echo.
    echo WARNING: Edit .env file and add your API keys:
    echo   - OPENROUTER_API_KEY ^(required^)
    echo   - JWT_SECRET ^(required - generate with: openssl rand -hex 32^)
    echo   - FRED_API_KEY ^(recommended^)
    echo   - COMTRADE_API_KEY ^(recommended^)
) else (
    echo OK: .env file already exists
)
echo.

echo Setup complete!
echo.
echo Next steps:
echo   1. Edit .env file with your API keys
echo   2. Activate Python virtual environment:
echo      backend\.venv\Scripts\activate.bat
echo   3. Start development servers:
echo      npm run dev
echo.
echo   Or start servers individually:
echo   - Backend: npm run dev:backend
echo   - Frontend: npm run dev:frontend
echo.
pause
