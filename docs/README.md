# OpenEcon Data Documentation

OpenEcon Data is a one-stop MCP + API layer for economic data across 10+ providers. This index provides quick access to setup, MCP integration, and technical references.

## Quick Links

- **[Public Quick Start](../README.md)** - 2 simple ways to use OpenEcon data (website or MCP)
- **[Getting Started](guides/getting-started.md)** - First-time setup and basic usage
- **[Cross-Platform Setup](guides/cross-platform-setup.md)** - Setup for Ubuntu/Linux, macOS, and Windows
- **[MCP Setup (Claude Code + Codex)](mcp/setup.md)** - Add `openecon-data` as an MCP server
- **[Developer & Contributor Guide](development/DEVELOPER_CONTRIBUTOR_GUIDE.md)** - Technical setup, architecture, testing, deployment, contribution
- **[API Quick Reference](providers/API_QUICK_REFERENCE.md)** - Quick reference for all supported APIs
- **[Security Policy](../.github/SECURITY.md)** - Security features and best practices
- **Hosted data app:** [https://data.openecon.ai/chat](https://data.openecon.ai/chat)

---

## Table of Contents

1. [User Guides](#user-guides)
2. [Data Providers](#data-providers)
3. [API Reference](#api-reference)
4. [Development](#development)
5. [Deployment](#deployment)
6. [Architecture](#architecture)
7. [MCP Quick Start](#mcp-quick-start)
8. [Troubleshooting](#troubleshooting)

---

## User Guides

Guides to help you get started and use OpenEcon Data effectively.

| Guide | Description |
|-------|-------------|
| [Getting Started](guides/getting-started.md) | First-time setup and basic usage |
| [Cross-Platform Setup](guides/cross-platform-setup.md) | Platform-specific installation (Linux, macOS, Windows) |
| [Testing Guide](guides/testing.md) | How to run and write tests |
| [Complex Query Testing](guides/COMPLEX_QUERY_TESTING.md) | Testing multi-provider and complex queries |

---

## Data Providers

OpenEcon Data integrates with 10+ economic data providers. Each provider has specific capabilities and data coverage.

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

Full endpoint documentation: **[API Reference](reference/api.md)** -- request/response schemas, authentication, conversation flow, rate limiting, circuit breaker behavior.

**Core Endpoints:**

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/api/health` | GET | No | Health check with service status |
| `/api/query` | POST | Optional | Process natural language query |
| `/api/query/stream` | POST | Optional | Streaming query (SSE) with real-time progress |
| `/api/query/pro` | POST | Optional | Pro Mode (AI-generated code execution) |
| `/api/query/pro/stream` | POST | Optional | Pro Mode with streaming |
| `/api/export` | POST | No | Export data as CSV/JSON/Stata |
| `/api/feedback` | POST | No | Submit user feedback |
| `/api/auth/register` | POST | No | User registration |
| `/api/auth/login` | POST | No | User login |
| `/api/auth/me` | GET | Yes | Current user profile |
| `/api/user/history` | GET | Yes | Query history |
| `/api/user/history` | DELETE | Yes | Clear query history |
| `/api/session/history` | GET | No | Anonymous session history |
| `/api/cache/stats` | GET | Yes | Cache statistics |
| `/api/cache/clear` | POST | Yes | Clear cache |
| `/api/performance/metrics` | GET | No | Performance metrics |
| `/api/performance/status` | GET | No | System status |

### MCP Server

OpenEcon Data exposes a Model Context Protocol (MCP) server for AI assistants:
- **Endpoint:** `/mcp`
- **Documentation:** [MCP Setup Guide](mcp/setup.md)

---

## MCP Quick Start

Use one of these endpoints:
- Hosted: `https://data.openecon.ai/mcp`
- Local: `http://localhost:3001/mcp`

Add to Codex:
```bash
codex mcp add openecon-data --url https://data.openecon.ai/mcp
codex mcp get openecon-data
```

Add to Claude Code:
```bash
claude mcp add --transport sse openecon-data https://data.openecon.ai/mcp --scope user
claude mcp get openecon-data
```

Example prompt:
- `Use query_data to compare US and Canada GDP growth since 2015.`

---

## Development

Guides for developers contributing to OpenEcon Data.

### Architecture & Design

| Document | Description |
|----------|-------------|
| [Developer & Contributor Guide](development/DEVELOPER_CONTRIBUTOR_GUIDE.md) | Technical setup and contribution workflow |
| [LLM Abstraction](development/LLM_ABSTRACTION.md) | LLM provider abstraction layer |
| [Metadata System](development/METADATA_SYSTEM_IMPROVEMENTS.md) | RAG-based metadata search |
| [FAISS vs ChromaDB](development/FAISS_VS_CHROMADB_DECISION.md) | Vector search architecture decision |
| [Indicator Resolution](INDICATOR_RESOLUTION.md) | Indicator resolution pipeline (current) |
| [Routing Improvements](development/ROUTING_IMPROVEMENTS.md) | Query routing logic (superseded by UnifiedRouter) |
| [Prompt Architecture](PROMPT_ARCHITECTURE_IMPROVEMENTS.md) | LLM prompt design (superseded by UnifiedRouter) |

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

Guides for deploying OpenEcon Data to production.

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

OpenEcon Data consists of:
1. **Backend** (Python/FastAPI) - API server, LLM integration, data providers
2. **Frontend** (React/TypeScript) - Chat interface, data visualization
3. **Supabase** - Authentication and query history storage

### Data Flow

```
User Query → LLM Parser → UnifiedRouter → Indicator Selector → Data Provider → Normalizer → Response
                ↓                                                                    ↓
     Conversation Context (Redis)                                          Intent Cache (Redis)
```

LLM-based routing replaced the old deterministic `ProviderRouter` and `keyword_matcher.py` (Phases 1-4 of routing consolidation). The LLM prompt includes a provider capability matrix, and `UnifiedRouter` makes the final routing decision. Repeat queries hit an intent cache (~72x speedup).

### Key Components

| Component | Location | Description |
|-----------|----------|-------------|
| Query Service | `backend/services/query.py` | Main orchestration layer |
| UnifiedRouter | `backend/routing/unified_router.py` | LLM-assisted provider routing |
| OpenRouter Service | `backend/services/openrouter.py` | LLM integration |
| Indicator Selector | `backend/services/indicator_selector.py` | Catalog + embedding + LLM indicator resolution |
| Providers | `backend/providers/` | Data source integrations (FRED, WorldBank, IMF, etc.) |
| Conversation Manager | `backend/services/conversation.py` | Multi-round context with Redis persistence |
| Cache | `backend/services/cache.py`, `redis_cache.py` | In-memory + Redis distributed caching |

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

- **Issues:** [GitHub Issues](https://github.com/hanlulong/openecon-data/issues)
- **Documentation:** This index
- **Code Reference:** [CLAUDE.md](../CLAUDE.md)
