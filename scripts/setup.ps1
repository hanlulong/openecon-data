# econ-data-mcp Setup Script for Windows (PowerShell)

Write-Host "🚀 econ-data-mcp Setup for Windows (PowerShell)" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""

Write-Host "📋 Checking prerequisites..." -ForegroundColor Yellow
Write-Host ""

# Check Node.js
try {
    $nodeVersion = node -v
    Write-Host "✅ Node.js $nodeVersion" -ForegroundColor Green

    $majorVersion = [int]($nodeVersion -replace 'v(\d+)\..*', '$1')
    if ($majorVersion -lt 18) {
        Write-Host "❌ Node.js version must be >= 18.0.0 (current: $nodeVersion)" -ForegroundColor Red
        exit 1
    }
} catch {
    Write-Host "❌ Node.js is not installed. Please install Node.js >= 18.0.0" -ForegroundColor Red
    Write-Host "   Download from: https://nodejs.org/" -ForegroundColor Yellow
    exit 1
}

# Check npm
try {
    $npmVersion = npm -v
    Write-Host "✅ npm $npmVersion" -ForegroundColor Green
} catch {
    Write-Host "❌ npm is not installed" -ForegroundColor Red
    exit 1
}

# Check Python
try {
    $pythonVersion = python --version
    Write-Host "✅ $pythonVersion" -ForegroundColor Green
} catch {
    Write-Host "❌ Python is not installed. Please install Python >= 3.8" -ForegroundColor Red
    Write-Host "   Download from: https://www.python.org/downloads/" -ForegroundColor Yellow
    Write-Host "   Make sure to check 'Add Python to PATH' during installation" -ForegroundColor Yellow
    exit 1
}

# Install Node dependencies
Write-Host ""
Write-Host "📦 Installing Node.js dependencies..." -ForegroundColor Yellow
npm install
if ($LASTEXITCODE -ne 0) {
    Write-Host "❌ Failed to install Node.js dependencies" -ForegroundColor Red
    exit 1
}

# Create Python virtual environment
Write-Host ""
Write-Host "🐍 Creating Python virtual environment..." -ForegroundColor Yellow
python -m venv backend\.venv
if ($LASTEXITCODE -ne 0) {
    Write-Host "❌ Failed to create virtual environment" -ForegroundColor Red
    exit 1
}

# Install Python dependencies
Write-Host ""
Write-Host "📦 Installing Python dependencies..." -ForegroundColor Yellow
& backend\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r backend\requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host "❌ Failed to install Python dependencies" -ForegroundColor Red
    deactivate
    exit 1
}
deactivate

# Create .env file if it doesn't exist
Write-Host ""
if (-not (Test-Path ".env")) {
    Write-Host "📝 Creating .env file from template..." -ForegroundColor Yellow
    Copy-Item .env.example .env
    Write-Host ""
    Write-Host "⚠️  IMPORTANT: Edit .env file and add your API keys:" -ForegroundColor Yellow
    Write-Host "   - OPENROUTER_API_KEY (required)" -ForegroundColor Cyan
    Write-Host "   - JWT_SECRET (required - generate with: openssl rand -hex 32)" -ForegroundColor Cyan
    Write-Host "   - FRED_API_KEY (recommended)" -ForegroundColor Cyan
    Write-Host "   - COMTRADE_API_KEY (recommended)" -ForegroundColor Cyan
} else {
    Write-Host "✅ .env file already exists" -ForegroundColor Green
}

Write-Host ""
Write-Host "✅ Setup complete!" -ForegroundColor Green
Write-Host ""
Write-Host "📚 Next steps:" -ForegroundColor Cyan
Write-Host "   1. Edit .env file with your API keys"
Write-Host "   2. Activate Python virtual environment:"
Write-Host "      backend\.venv\Scripts\Activate.ps1" -ForegroundColor Yellow
Write-Host "   3. Start development servers:"
Write-Host "      npm run dev" -ForegroundColor Yellow
Write-Host ""
Write-Host "   Or start servers individually:"
Write-Host "   - Backend: npm run dev:backend" -ForegroundColor Yellow
Write-Host "   - Frontend: npm run dev:frontend" -ForegroundColor Yellow
Write-Host ""
