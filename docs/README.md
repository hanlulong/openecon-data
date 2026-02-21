# econ-data-mcp Documentation

Welcome to the econ-data-mcp documentation. This index provides quick access to all documentation organized by topic.

## Quick Links

- **[Getting Started](guides/getting-started.md)** - First-time setup and basic usage
- **[Cross-Platform Setup](guides/cross-platform-setup.md)** - Setup for Ubuntu/Linux, macOS, and Windows
- **[API Quick Reference](providers/API_QUICK_REFERENCE.md)** - Quick reference for all supported APIs
- **[Security Policy](../.github/SECURITY.md)** - Security features and best practices

---

## Table of Contents

1. [User Guides](#user-guides)
2. [Data Providers](#data-providers)
3. [API Reference](#api-reference)
4. [Development](#development)
5. [Deployment](#deployment)
6. [Architecture](#architecture)
7. [Troubleshooting](#troubleshooting)

---

## User Guides

Guides to help you get started and use econ-data-mcp effectively.

| Guide | Description |
|-------|-------------|
| [Getting Started](guides/getting-started.md) | First-time setup and basic usage |
| [Cross-Platform Setup](guides/cross-platform-setup.md) | Platform-specific installation (Linux, macOS, Windows) |
| [Testing Guide](guides/testing.md) | How to run and write tests |
| [Complex Query Testing](guides/COMPLEX_QUERY_TESTING.md) | Testing multi-provider and complex queries |

---

## Data Providers

econ-data-mcp integrates with 10+ economic data providers. Each provider has specific capabilities and data coverage.

### Provider Documentation

| Provider | Description | Documentation |
|----------|-------------|---------------|
| **FRED** | Federal Reserve Economic Data (US) | [API Reference](providers/FRED_API_REFERENCE.md) |
| **World Bank** | Global development indicators | [API Quick Reference](providers/API_QUICK_REFERENCE.md) |
| **UN Comtrade** | International trade flows | [Trade Data Guide](reference/trade-data.md) |
| **Statistics Canada** | Canadian economic data | [Categorical Data](features/statscan-categorical-data.md) |
| **IMF** | International financial statistics | [Regional Queries](fixes/IMF_REGIONAL_QUERY_QUICK_REFERENCE.md) |
| **BIS** | Bank for International Settlements | [Provider Fixes](fixes/BIS_PROVIDER_FIX_2025-11-26.md) |
| **Eurostat** | European Union statistics | [Complete Guide](reference/EUROSTAT_API_COMPLETE_GUIDE.md) |
| **OECD** | OECD member countries data | [Dynamic Discovery](reference/oecd_dynamic_discovery.md) |
| **ExchangeRate-API** | Currency exchange rates | [Quick Reference](providers/API_QUICK_REFERENCE.md) |
| **CoinGecko** | Cryptocurrency prices | [Quick Reference](providers/API_QUICK_REFERENCE.md) |

### Provider Technical Reference

| Document | Description |
|----------|-------------|
| [SDMX API Research](reference/SDMX_API_RESEARCH.md) | SDMX protocol for IMF, BIS, Eurostat, OECD |
| [Trade Data Reference](reference/trade-data.md) | UN Comtrade usage and HS codes |
| [Eurostat Research](reference/EUROSTAT_RESEARCH_SUMMARY.md) | Eurostat API technical details |

---

## API Reference

### Backend API Endpoints

The econ-data-mcp backend exposes a REST API at `/api/*`. See [backend/README.md](../backend/README.md) for the full endpoint list.

**Core Endpoints:**

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/health` | GET | Health check with service status |
| `/api/query` | POST | Process natural language query |
| `/api/query/stream` | POST | Streaming query with real-time updates |
| `/api/query/pro` | POST | Pro Mode (AI-generated code execution) |
| `/api/export` | POST | Export data as CSV/JSON |
| `/api/auth/register` | POST | User registration |
| `/api/auth/login` | POST | User login |

### MCP Server

econ-data-mcp exposes a Model Context Protocol (MCP) server for AI assistants:
- **Endpoint:** `/mcp`
- **Documentation:** [MCP Setup Guide](mcp/setup.md)

---

## Development

Guides for developers contributing to econ-data-mcp.

### Architecture & Design

| Document | Description |
|----------|-------------|
| [LLM Abstraction](development/LLM_ABSTRACTION.md) | LLM provider abstraction layer |
| [Metadata System](development/METADATA_SYSTEM_IMPROVEMENTS.md) | RAG-based metadata search |
| [FAISS vs ChromaDB](development/FAISS_VS_CHROMADB_DECISION.md) | Vector search architecture decision |
| [Routing Improvements](development/ROUTING_IMPROVEMENTS.md) | Query routing logic |
| [Prompt Architecture](PROMPT_ARCHITECTURE_IMPROVEMENTS.md) | LLM prompt design |

### Performance & Optimization

| Document | Description |
|----------|-------------|
| [FAISS Performance](development/FAISS_PERFORMANCE_TUNING.md) | Vector search optimization |
| [FAISS Deployment](development/FAISS_DEPLOYMENT_REPORT.md) | Production deployment notes |
| [Accuracy Improvements](development/ACCURACY_IMPROVEMENT_REPORT.md) | Data accuracy analysis |

### Agent & AI Integration

| Document | Description |
|----------|-------------|
| [Agent Instructions](development/agents.md) | AI agent integration guide |
| [LLM Improvements](development/LLM_IMPROVEMENTS.md) | LLM system enhancements |

### Provider Development

| Document | Description |
|----------|-------------|
| [Provider Analysis](development/PROVIDER_ANALYSIS_AND_FIXES.md) | Provider implementation analysis |
| [StatsCan Improvements](development/STATSCAN_95_IMPROVEMENT_REPORT.md) | Statistics Canada 95% accuracy report |
| [Default Time Periods](development/DEFAULT_TIME_PERIODS.md) | Time period handling |

---

## Deployment

Guides for deploying econ-data-mcp to production.

| Document | Description |
|----------|-------------|
| [Deployment Summary](DEPLOYMENT_SUMMARY.md) | Production deployment overview |
| [Apache Pro Mode Setup](deployment/apache-promode-setup.md) | Apache2 configuration for Pro Mode |

### Environment Configuration

See the main [CLAUDE.md](../CLAUDE.md) file for:
- Required environment variables
- Production deployment checklist
- Apache2 configuration details
- Backend/frontend server management

---

## Architecture

### System Overview

econ-data-mcp consists of:
1. **Backend** (Python/FastAPI) - API server, LLM integration, data providers
2. **Frontend** (React/TypeScript) - Chat interface, data visualization
3. **Supabase** - Authentication and query history storage

### Data Flow

```
User Query → LLM Parser → Provider Router → Data Provider → Normalizer → Response
                ↓
         Conversation Context
```

### Key Components

| Component | Location | Description |
|-----------|----------|-------------|
| Query Service | `backend/services/query.py` | Main orchestration layer |
| OpenRouter Service | `backend/services/openrouter.py` | LLM integration |
| Providers | `backend/providers/` | Data source integrations |
| Metadata Search | `backend/services/metadata_search.py` | RAG-based indicator discovery |
| Cache | `backend/services/cache.py` | In-memory caching |

---

## Troubleshooting

### Common Issues

**Query returns no data:**
1. Check if the indicator exists in the provider
2. Verify date range is valid
3. Check provider-specific limitations

**Authentication errors:**
1. Verify Supabase credentials in `.env`
2. Check token expiration
3. Clear browser localStorage and retry

**Provider-specific issues:**
- [HTTP 500 Provider Fixes](fixes/HTTP_500_PROVIDER_FIXES.md)
- [BIS Provider Fix](fixes/BIS_PROVIDER_FIX_2025-11-26.md)
- [IMF Regional Query Fix](fixes/IMF_REGIONAL_QUERY_FIX.md)
- [World Bank/ExchangeRate Fix](fixes/worldbank-exchangerate-fix-2025-11-20.md)

### Debug Logs

```bash
# Backend logs
tail -f /tmp/backend-dev.log

# Check health endpoint
curl http://localhost:3001/api/health
```

---

## Recent Improvements

| Document | Description |
|----------|-------------|
| [Provider Testing Summary](improvements/provider_testing_summary_2025-11-21.md) | Latest test results |
| [Eurostat Fixes](improvements/eurostat_fixes_summary.md) | Eurostat provider improvements |
| [OECD Improvements](improvements/oecd_improvements_report.md) | OECD provider enhancements |
| [IMF Provider Improvements](improvements/IMF_PROVIDER_IMPROVEMENTS.md) | IMF data quality fixes |
| [OECD Rate Limit Fix](improvements/OECD_RATE_LIMIT_FIX_TECHNICAL_ANALYSIS.md) | Rate limiting implementation |

---

## Archive

Historical documentation and development logs are available in the [archive/](archive/) directory.

---

## Contributing

1. Read the [Getting Started](guides/getting-started.md) guide
2. Review the [Security Policy](../.github/SECURITY.md)
3. Follow coding standards in [CLAUDE.md](../CLAUDE.md)
4. Submit pull requests to the `main` branch

---

## Need Help?

- **Issues:** [GitHub Issues](https://github.com/hanlulong/econ-data-mcp/issues)
- **Documentation:** This index
- **Code Reference:** [CLAUDE.md](../CLAUDE.md)
