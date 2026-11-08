#!/bin/bash
# openecon-data Setup Script for Unix/Linux/macOS
set -e

echo "🚀 openecon-data Setup for Unix/Linux/macOS"
echo "========================================"

# Check for required tools
echo ""
echo "📋 Checking prerequisites..."

# Check Node.js
if ! command -v node &> /dev/null; then
    echo "❌ Node.js is not installed. Please install Node.js >= 18.0.0"
    echo "   Download from: https://nodejs.org/"
    exit 1
fi

NODE_VERSION=$(node -v | cut -d'v' -f2 | cut -d'.' -f1)
if [ "$NODE_VERSION" -lt 18 ]; then
    echo "❌ Node.js version must be >= 18.0.0 (current: $(node -v))"
    exit 1
fi
echo "✅ Node.js $(node -v)"

# Check npm
if ! command -v npm &> /dev/null; then
    echo "❌ npm is not installed"
    exit 1
fi
echo "✅ npm $(npm -v)"

# Check Python (the backend uses `X | None` syntax and needs Python >= 3.10)
if ! command -v python3 &> /dev/null || ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2> /dev/null; then
    echo "❌ Python >= 3.10 is required (python3 is missing or older)"
    echo "   Download from: https://www.python.org/downloads/"
    exit 1
fi
echo "✅ Python $(python3 --version | cut -d' ' -f2)"

# Install Node dependencies
echo ""
echo "📦 Installing Node.js dependencies..."
npm install

# Create Python virtual environment
echo ""
echo "🐍 Creating Python virtual environment..."
python3 -m venv backend/.venv

# Activate virtual environment and install Python dependencies
echo ""
echo "📦 Installing Python dependencies..."
if [ -f "backend/.venv/bin/activate" ]; then
    source backend/.venv/bin/activate
    pip install --upgrade pip
    pip install -r backend/requirements.txt
    deactivate
else
    echo "❌ Failed to create virtual environment"
    exit 1
fi

# Create .env file if it doesn't exist
echo ""
if [ ! -f ".env" ]; then
    echo "📝 Creating .env file from template..."
    cp .env.example .env
    echo "⚠️  IMPORTANT: Edit .env file and add your API keys:"
    echo "   - OPENROUTER_API_KEY (required)"
    echo "   - JWT_SECRET (required - generate with: openssl rand -hex 32)"
    echo "   - FRED_API_KEY (recommended)"
    echo "   - COMTRADE_API_KEY (recommended)"
else
    echo "✅ .env file already exists"
fi

echo ""
echo "✅ Setup complete!"
echo ""
echo "📚 Next steps:"
echo "   1. Edit .env file with your API keys"
echo "   2. Activate Python virtual environment:"
echo "      source backend/.venv/bin/activate"
echo "   3. Start development servers:"
echo "      npm run dev"
echo ""
echo "   Or start servers individually:"
echo "   - Backend: npm run dev:backend"
echo "   - Frontend: npm run dev:frontend"
echo ""
