# Cross-Platform Setup Guide

This guide provides platform-specific instructions for setting up econ-data-mcp on Ubuntu/Linux, macOS, and Windows.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Quick Setup](#quick-setup)
- [Platform-Specific Instructions](#platform-specific-instructions)
  - [Ubuntu/Linux](#ubuntulinux)
  - [macOS](#macos)
  - [Windows](#windows)
- [Manual Setup](#manual-setup)
- [Common Issues](#common-issues)

## Prerequisites

All platforms require:

- **Node.js** >= 18.0.0
- **npm** >= 9.0.0
- **Python** >= 3.8
- **Git** (for cloning the repository)

## Quick Setup

### Ubuntu/Linux & macOS

```bash
# Clone the repository
git clone https://github.com/yourusername/econ-data-mcp.git
cd econ-data-mcp

# Run setup script
./scripts/setup.sh

# Edit .env file with your API keys
nano .env  # or use your preferred editor

# Activate virtual environment
source backend/.venv/bin/activate

# Start development servers
npm run dev
```

### Windows (PowerShell)

```powershell
# Clone the repository
git clone https://github.com/yourusername/econ-data-mcp.git
cd econ-data-mcp

# Allow script execution (run PowerShell as Administrator)
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser

# Run setup script
.\scripts\setup.ps1

# Edit .env file with your API keys
notepad .env

# Activate virtual environment
backend\.venv\Scripts\Activate.ps1

# Start development servers
npm run dev
```

### Windows (Command Prompt)

```cmd
REM Clone the repository
git clone https://github.com/yourusername/econ-data-mcp.git
cd econ-data-mcp

REM Run setup script
scripts\setup.bat

REM Edit .env file with your API keys
notepad .env

REM Activate virtual environment
backend\.venv\Scripts\activate.bat

REM Start development servers
npm run dev
```

## Platform-Specific Instructions

### Ubuntu/Linux

#### Installing Prerequisites

**Ubuntu/Debian:**
```bash
# Update package list
sudo apt update

# Install Node.js (via NodeSource)
curl -fsSL https://deb.nodesource.com/setup_18.x | sudo -E bash -
sudo apt install -y nodejs

# Install Python
sudo apt install -y python3 python3-venv python3-pip

# Install Git
sudo apt install -y git
```

**Fedora/RHEL:**
```bash
# Install Node.js
sudo dnf install nodejs

# Install Python
sudo dnf install python3 python3-pip

# Install Git
sudo dnf install git
```

#### Development Workflow

```bash
# Activate virtual environment
source backend/.venv/bin/activate

# Start both servers
npm run dev

# Or start individually:
# Terminal 1 - Backend
npm run dev:backend

# Terminal 2 - Frontend
npm run dev:frontend

# Deactivate virtual environment when done
deactivate
```

### macOS

#### Installing Prerequisites

**Using Homebrew (recommended):**
```bash
# Install Homebrew if not already installed
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Install Node.js
brew install node

# Install Python (macOS comes with Python, but brew version is recommended)
brew install python@3.11

# Git is included with Xcode Command Line Tools
xcode-select --install
```

**Using Node Version Manager (nvm):**
```bash
# Install nvm
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.0/install.sh | bash

# Install Node.js
nvm install 18
nvm use 18
```

#### Development Workflow

Same as Ubuntu/Linux (see above).

#### macOS-Specific Notes

- If you encounter permission issues, avoid using `sudo` with npm. Use nvm instead.
- macOS uses `zsh` by default. The setup script works with both `bash` and `zsh`.

### Windows

#### Installing Prerequisites

**Node.js:**
1. Download from [nodejs.org](https://nodejs.org/)
2. Run the installer
3. Ensure "Add to PATH" is checked

**Python:**
1. Download from [python.org](https://www.python.org/downloads/)
2. Run the installer
3. **IMPORTANT:** Check "Add Python to PATH" during installation

**Git:**
1. Download from [git-scm.com](https://git-scm.com/)
2. Run the installer with default settings

#### Development Workflow

**PowerShell:**
```powershell
# Activate virtual environment
backend\.venv\Scripts\Activate.ps1

# Start both servers
npm run dev

# Or start individually in separate terminals
npm run dev:backend  # Terminal 1
npm run dev:frontend  # Terminal 2

# Deactivate virtual environment when done
deactivate
```

**Command Prompt:**
```cmd
REM Activate virtual environment
backend\.venv\Scripts\activate.bat

REM Start both servers
npm run dev

REM Deactivate when done
deactivate
```

#### Windows-Specific Notes

- **PowerShell Execution Policy:** You may need to allow script execution:
  ```powershell
  Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
  ```
- **Path Length Limit:** If you encounter issues with long file paths, enable long path support:
  ```powershell
  # Run as Administrator
  New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" -Name "LongPathsEnabled" -Value 1 -PropertyType DWORD -Force
  ```
- **Line Endings:** Git may convert line endings. This is usually fine, but if you have issues:
  ```bash
  git config --global core.autocrlf true
  ```

## Manual Setup

If the automated scripts don't work, follow these manual steps:

### 1. Install Node.js Dependencies

```bash
npm install
```

### 2. Create Python Virtual Environment

**Unix/Linux/macOS:**
```bash
python3 -m venv backend/.venv
```

**Windows:**
```cmd
python -m venv backend\.venv
```

### 3. Activate Virtual Environment

**Unix/Linux/macOS:**
```bash
source backend/.venv/bin/activate
```

**Windows PowerShell:**
```powershell
backend\.venv\Scripts\Activate.ps1
```

**Windows Command Prompt:**
```cmd
backend\.venv\Scripts\activate.bat
```

### 4. Install Python Dependencies

```bash
pip install --upgrade pip
pip install -r backend/requirements.txt
```

### 5. Configure Environment Variables

```bash
# Copy template
cp .env.example .env  # Unix/Linux/macOS
copy .env.example .env  # Windows

# Edit .env and add your API keys
```

### 6. Start Development Servers

```bash
npm run dev
```

## Common Issues

### Issue: `uvicorn: command not found`

**Cause:** Virtual environment not activated or uvicorn not installed.

**Solution:**
```bash
# Activate virtual environment
source backend/.venv/bin/activate  # Unix/macOS
backend\.venv\Scripts\Activate.ps1  # Windows PowerShell

# Install dependencies
pip install -r backend/requirements.txt
```

### Issue: `npm run dev` fails with "concurrently not found"

**Cause:** Node dependencies not installed.

**Solution:**
```bash
npm install
```

### Issue: Python virtual environment activation fails on Windows

**Cause:** PowerShell execution policy or incorrect command.

**Solution:**
```powershell
# For PowerShell, allow script execution
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser

# Then activate
backend\.venv\Scripts\Activate.ps1
```

### Issue: Port 3001 or 5173 already in use

**Cause:** Another process is using the port.

**Solution:**

**Unix/Linux/macOS:**
```bash
# Find process using port
lsof -i :3001
# or
lsof -i :5173

# Kill the process
kill -9 <PID>
```

**Windows:**
```cmd
REM Find process
netstat -ano | findstr :3001

REM Kill process
taskkill /PID <PID> /F
```

### Issue: `ModuleNotFoundError` in Python

**Cause:** Dependencies not installed in virtual environment.

**Solution:**
```bash
# Make sure virtual environment is activated
# Then reinstall dependencies
pip install -r backend/requirements.txt
```

### Issue: CORS errors in browser

**Cause:** Backend not running or CORS configuration issue.

**Solution:**
1. Ensure backend is running on port 3001
2. Check `.env` file has correct `ALLOWED_ORIGINS`
3. Clear browser cache and hard refresh (Ctrl+Shift+R)

### Issue: Frontend shows blank page

**Cause:** Build errors or port conflicts.

**Solution:**
```bash
# Check console for errors
# Rebuild frontend
npm run build:frontend

# Try starting frontend only
npm run dev:frontend
```

## Environment Variables

Required environment variables (edit `.env`):

```bash
# REQUIRED
OPENROUTER_API_KEY=sk-or-...  # Get from https://openrouter.ai/keys
JWT_SECRET=<random-hex-string>  # Generate with: openssl rand -hex 32

# RECOMMENDED
FRED_API_KEY=...  # https://fred.stlouisfed.org/docs/api/api_key.html
COMTRADE_API_KEY=...  # Contact UN Comtrade

# OPTIONAL
ALLOWED_ORIGINS=http://localhost:5173,http://localhost:3000
NODE_ENV=development
```

## Next Steps

After setup is complete:

1. Visit http://localhost:5173 to see the frontend
2. Backend API is available at http://localhost:3001
3. Test the health endpoint: http://localhost:3001/api/health
4. Read the [Getting Started Guide](./getting-started.md)
5. Check [Testing Guide](./testing.md)

## Additional Resources

- [Project Documentation](../../README.md)
- [Claude Code Instructions](../../CLAUDE.md)
- [Security Policy](../../.github/SECURITY.md)
- [Agent Integration Notes](../development/agents.md)
