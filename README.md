# econ-data-mcp

**One-stop MCP + API layer for economic data across 10+ sources**

`econ-data-mcp` unifies FRED, World Bank, IMF, UN Comtrade, Statistics Canada, BIS, Eurostat, OECD, FX, and crypto data behind a single interface. Query in plain English and get structured results, charts, exports, and source provenance.

**For users (no setup):** use **[OpenEcon.ai](https://openecon.ai)**, then open the live data app at **[data.openecon.io/chat](https://data.openecon.io/chat)**.  
**For developers (self-host/customize):** clone this repo and run locally.

## 🎯 Why econ-data-mcp

- **One-stop data access** - Multiple providers behind one MCP tool and one API
- **Natural language to data** - Ask economics questions in plain English
- **Traceable outputs** - Source metadata and API provenance included
- **Agent-ready** - Works with MCP clients like Claude Code and Codex

## ✨ Features

- **Natural Language Queries** - Ask questions in plain English, get structured data responses
- **10+ Data Sources** - FRED, World Bank, UN Comtrade, Statistics Canada, IMF, BIS, Eurostat, OECD, ExchangeRate-API, CoinGecko
- **Interactive Charts** - Visualize data with line, bar, and scatter charts
- **Data Export** - Download results as CSV or JSON
- **Conversation Memory** - Follow-up questions that build on previous context
- **Pro Mode** - Execute Python code for custom analysis (sandbox environment)

## 🚀 Quick Start

### Prerequisites

- **Node.js** 18+ and npm 9+
- **Python** 3.9 or newer
- **API Keys**: OpenRouter (required), FRED and UN Comtrade (recommended)

### Installation

**Ubuntu/Linux & macOS:**
```bash
git clone https://github.com/hanlulong/econ-data-mcp.git
cd econ-data-mcp
./scripts/setup.sh
```

**Windows (PowerShell):**
```powershell
git clone https://github.com/hanlulong/econ-data-mcp.git
cd econ-data-mcp
.\scripts\setup.ps1
```

**Windows (Command Prompt):**
```cmd
git clone https://github.com/hanlulong/econ-data-mcp.git
cd econ-data-mcp
scripts\setup.bat
```

### Configuration

Edit `.env` and add your API keys:

```bash
# Required
LLM_PROVIDER=openrouter           # Recommended default for first-time setup
LLM_MODEL=openai/gpt-4o-mini
OPENROUTER_API_KEY=sk-or-...  # Get from https://openrouter.ai/keys
JWT_SECRET=<random-string>     # Generate with: openssl rand -hex 32

# Recommended
FRED_API_KEY=...               # Get from https://fred.stlouisfed.org/
COMTRADE_API_KEY=...           # Get from https://comtradedeveloper.un.org/
```

Local development is clone-ready:
- No manual database bootstrap needed (`backend/data/indicators.db` is created if missing)
- No manual vector index bootstrap needed (`backend/data/faiss_index` is created/rebuilt on demand when vector search is enabled)
- Supabase is optional in development (mock auth is used when Supabase is not configured)

### Running

```bash
# Activate Python virtual environment
source backend/.venv/bin/activate  # Linux/macOS
# backend\.venv\Scripts\Activate.ps1  # Windows PowerShell
# backend\.venv\Scripts\activate.bat  # Windows CMD

# Start both frontend and backend
npm run dev
```

Visit **http://localhost:5173** to use the application.

## 🔌 MCP Setup (Claude Code + Codex)

Use either endpoint:

- **Hosted MCP endpoint:** `https://data.openecon.io/mcp`
- **Local MCP endpoint:** `http://localhost:3001/mcp`

### Add to Codex

```bash
# Hosted
codex mcp add econ-data-mcp --url https://data.openecon.io/mcp

# Or local
codex mcp add econ-data-mcp-local --url http://localhost:3001/mcp

# Verify
codex mcp list
codex mcp get econ-data-mcp
```

### Add to Claude Code

```bash
# Hosted
claude mcp add --transport sse econ-data-mcp https://data.openecon.io/mcp --scope user

# Or local
claude mcp add --transport sse econ-data-mcp-local http://localhost:3001/mcp --scope user

# Verify
claude mcp get econ-data-mcp
```

### Example Prompts for Claude Code / Codex

- `Use query_data to compare US, UK, and Japan inflation from 2015 to 2025. Return a compact table and key trend summary.`
- `Use query_data to fetch China exports to the United States from 2020 to 2024 and provide CSV-ready output.`
- `Use query_data to show US unemployment rate and CPI together since 2010, then explain major turning points.`
- `Use query_data to retrieve EUR/USD exchange rate history for the last 24 months and highlight volatility periods.`

## 📖 Documentation

- **[Complete Documentation](docs/README.md)** - Full documentation index
- **[Getting Started Guide](docs/guides/getting-started.md)** - Detailed setup and usage
- **[Cross-Platform Setup](docs/guides/cross-platform-setup.md)** - Platform-specific instructions
- **[MCP Client Setup (Claude Code + Codex)](docs/mcp/setup.md)** - Add this server to MCP clients
- **[API Reference](backend/README.md)** - Backend API endpoints
- **[Security Policy](.github/SECURITY.md)** - Security features and best practices

## 🏗️ Architecture

```
econ-data-mcp/
├── backend/              # FastAPI service (Python)
│   ├── main.py           # API endpoints and routing
│   ├── models.py         # Pydantic models
│   ├── providers/        # Data source integrations (FRED, World Bank, etc.)
│   └── services/         # Auth, query orchestration, caching
├── packages/
│   └── frontend/         # React + TypeScript + Vite SPA
│       ├── src/
│       │   ├── components/  # UI components
│       │   └── services/    # API client
└── docs/                 # Documentation
```

**Tech Stack:**
- **Backend**: Python 3.9+, FastAPI, Uvicorn, Redis caching
- **Frontend**: React 18, TypeScript, Vite, Tailwind CSS v4, Recharts
- **LLM Integration**: OpenRouter API (GPT-4o-mini), Grok for Pro Mode
- **Database**: Supabase (auth + query history)
- **Search**: FAISS vector embeddings for metadata

## 🔧 Development

### Start Servers Individually

```bash
# Terminal 1 - Backend
source backend/.venv/bin/activate
npm run dev:backend

# Terminal 2 - Frontend
npm run dev:frontend
```

### Available Scripts

| Script | Description |
|--------|-------------|
| `npm run dev` | Start both servers concurrently |
| `npm run dev:backend` | Start FastAPI backend only |
| `npm run dev:frontend` | Start Vite dev server only |
| `npm run build` | Build frontend for production |
| `npm run test` | Run tests |
| `npm run format` | Format code with Prettier |
| `npm run clean` | Clean build artifacts |

### Testing

```bash
# Backend tests
cd backend
source .venv/bin/activate
python -m unittest discover -s backend/tests

# Frontend tests
npm run test --workspace=packages/frontend
```

See [Testing Guide](docs/guides/testing.md) for details.

## 🌍 Data Sources

econ-data-mcp integrates with 10+ economic data providers:

| Provider | Coverage | API Key |
|----------|----------|---------|
| **FRED** | US economic data | Recommended |
| **World Bank** | Global development indicators | None |
| **UN Comtrade** | International trade data | Recommended |
| **Statistics Canada** | Canadian economic data | None |
| **IMF** | International financial statistics | None |
| **BIS** | Central bank data | None |
| **Eurostat** | European statistics | None |
| **OECD** | OECD member countries | None |
| **ExchangeRate-API** | Currency exchange rates | Optional |
| **CoinGecko** | Cryptocurrency data | Optional |

See [Trade Data Reference](docs/reference/trade-data.md) for UN Comtrade usage details.

## 🔐 Security

- JWT-based authentication with bcrypt password hashing
- Minimum 12-character passwords with complexity requirements
- Sandboxed code execution for Pro Mode
- CORS protection with configurable origins
- No sensitive data in version control

See [SECURITY.md](.github/SECURITY.md) for complete security documentation.

## 🚢 Deployment

econ-data-mcp is production-ready and can be deployed to:

- **Frontend**: Vercel, Netlify, or any static host
- **Backend**: Railway, Render, AWS, or self-hosted

For self-hosted deployment (Apache2 + Uvicorn), see [Production Deployment](CLAUDE.md#production-deployment).

**Production checklist:**
1. Generate secure `JWT_SECRET`
2. Configure `ALLOWED_ORIGINS` for your domain
3. Set up SSL certificates (Let's Encrypt recommended)
4. Build frontend: `npm run build:frontend`
5. Start backend with production settings
6. Configure reverse proxy (Apache/Nginx)

## 📊 Example Queries

Try these queries in the chat interface:

- "Show me US GDP for the last 5 years"
- "Compare unemployment rates between US and Canada"
- "What is the trade balance between China and US?"
- "Show me Germany's inflation rate from 2020 to 2024"
- "Get World Bank population data for India"

## 🤝 Contributing

Contributions are welcome! We appreciate all contributions, from bug reports to new features.

**Quick Start:**
1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Make your changes and test thoroughly
4. Commit your changes (`git commit -m 'Add amazing feature'`)
5. Push to your branch (`git push origin feature/amazing-feature`)
6. Open a Pull Request

**Detailed Guidelines:**
- See [CONTRIBUTORS.md](.github/CONTRIBUTORS.md) for complete contribution guidelines
- Follow our code style (Python: PEP 8, TypeScript: Prettier)
- Update documentation for significant changes
- Add tests for new features

**Development Workflow:**
This project uses OpenSpec specification-driven development. See `openspec/AGENTS.md` for workflow details.

## 📄 License

MIT License - see [LICENSE](LICENSE) file for details.

## 🆘 Support

- **Documentation**: [docs/README.md](docs/README.md)
- **Issues**: [GitHub Issues](https://github.com/hanlulong/econ-data-mcp/issues)
- **Security**: See [SECURITY.md](.github/SECURITY.md)

## 🙏 Acknowledgments

Built with:
- [FastAPI](https://fastapi.tiangolo.com/) - Modern Python web framework
- [React](https://react.dev/) - UI library
- [Vite](https://vitejs.dev/) - Frontend tooling
- [Recharts](https://recharts.org/) - Data visualization
- [OpenRouter](https://openrouter.ai/) - LLM API gateway

---

**Made with ❤️ by the econ-data-mcp team**
