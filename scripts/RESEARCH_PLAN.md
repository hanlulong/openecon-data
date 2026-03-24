# Research Plan: Agent Frameworks & Tools

**Created:** 2026-03-24
**Updated:** 2026-03-24
**Status:** PHASE 1 COMPLETE — 55 repos evaluated, top 30 deep-dived

---

## Complete Research: 55 Repos Evaluated

### A. AI Agent Frameworks (20 repos)

| # | Framework | Stars | Relevance | Effort | Status |
|---|-----------|-------|-----------|--------|--------|
| 1 | AutoGen (Microsoft) | 50.4K | HIGH | MED | Merging to MS Agent Framework |
| 2 | CrewAI | 45.9K | HIGH | LOW | Role-based crews |
| 3 | **LangGraph** | 44.6K | HIGHEST | **INTEGRATED** | Already in project |
| 4 | LlamaIndex | 42K | HIGH | MED | RAG + agent workflows |
| 5 | Agno (Phidata) | 38.9K | HIGH | LOW | 5000x faster than LangGraph |
| 6 | DSPy (Stanford) | 33K | MED | MED | Compile prompts, not engineer |
| 7 | Semantic Kernel (MS) | 27.4K | MED | HIGH | C#-primary |
| 8 | Composio | 27.3K | LOW | LOW | SaaS tool integrations |
| 9 | SmolAgents (HF) | 26K | MED | LOW | Code-first agents |
| 10 | Haystack (deepset) | 24.6K | HIGH | MED | Pipeline architecture |
| 11 | Mastra | 22.3K | LOW | HIGH | TypeScript-only |
| 12 | Prefect | 21.9K | MED | MED | Workflow orchestration |
| 13 | Letta (MemGPT) | 21.6K | MED | MED | 3-tier memory agents |
| 14 | OpenAI Agents SDK | 20.2K | MED | LOW | Handoff-based |
| 15 | **PydanticAI** | 15.7K | HIGHEST | LOW | Perfect Pydantic/FastAPI fit |
| 16 | CAMEL-AI | 15.2K | LOW | HIGH | Research-focused |
| 17 | Marvin (Prefect) | 6.1K | MED | LOW | Task-centric + PydanticAI |
| 18 | BeeAI (IBM) | 3.1K | MED | MED | Dual Python/TS |
| 19 | Mirascope | 1.4K | MED | LOW | Lightweight "anti-framework" |
| 20 | ControlFlow | 1.1K | N/A | N/A | ARCHIVED → Marvin 3.0 |

### B. Data Tools & LLM Libraries (20 repos)

| # | Tool | Stars | Category | Relevance | Effort |
|---|------|-------|----------|-----------|--------|
| 1 | OpenBB | 63.4K | Economic Data | HIGH | MED-HIGH |
| 2 | **FAISS** | 39.5K | Vector Search | **INTEGRATED** | N/A |
| 3 | Qdrant | 29K | Vector DB | LOW-MED | MED |
| 4 | ChromaDB | 26K | Vector DB | LOW (removed) | N/A |
| 5 | yfinance | 22K | Market Data | MED | LOW |
| 6 | Guidance (MS) | 19K | Structured Output | MED | MED-HIGH |
| 7 | **sentence-transformers** | 16K+ | Embeddings | **INTEGRATED** | N/A |
| 8 | **Instructor** | 12.2K | Structured Output | VERY HIGH | LOW |
| 9 | Outlines | 11.9K | Structured Output | MED | MED |
| 10 | Portkey AI Gateway | 10.9K | LLM Gateway | LOW | LOW |
| 11 | Marvin (also agent) | 6.1K | Structured Output | MED | LOW |
| 12 | LMQL | 4.2K | Query Language | LOW-MED | MED-HIGH |
| 13 | pandas-datareader | 3.1K | Data Access | MED | LOW |
| 14 | Quandl/Nasdaq | 2.8K | Financial Data | LOW-MED | LOW |
| 15 | FedFred | ~200 | FRED Client | MED-HIGH | LOW |
| 16 | sdmx1 | ~35 | SDMX Client | HIGH | MED |
| 17 | weo-reader | ~37 | IMF WEO | LOW-MED | LOW |
| 18 | **LiteLLM** | 38.6K | LLM Gateway | **INTEGRATED** | N/A |
| 19 | Mem0 | 41-48K | Agent Memory | MED-HIGH | MED |
| 20 | Graphiti (Zep) | 24K | Knowledge Graph | MED | HIGH |

### C. MCP, Monitoring & Infrastructure (15 repos)

| # | Tool | Stars | Category | Relevance | Effort |
|---|------|-------|----------|-----------|--------|
| 1 | **FastAPI-MCP** | 2.5K | MCP Server | **INTEGRATED** | N/A |
| 2 | OpenBB MCP | 35K | MCP Server | HIGH | MED |
| 3 | Composio MCP | 15K | MCP Tools | MOD | LOW-MED |
| 4 | LangSmith | 400 | Monitoring | HIGH | LOW (dep installed!) |
| 5 | Langfuse | 9K | Monitoring | HIGH | LOW |
| 6 | Phoenix (Arize) | 12K | LLM Observability | HIGH | LOW-MED |
| 7 | Helicone | 3K | LLM Proxy | MOD | LOW |
| 8 | OpenLLMetry | 5K | Auto-instrumentation | HIGH | VERY LOW |
| 9 | **Tenacity** | 6.5K | Retry | **INTEGRATED** | N/A |
| 10 | pybreaker | 1.2K | Circuit Breaker | HIGH | LOW |
| 11 | **httpx** | 13.5K | HTTP Client | **INTEGRATED** | N/A |
| 12 | **Redis async** | 13K | Cache | **INTEGRATED** | N/A |
| 13 | Hypothesis | 7.5K | Property Testing | MOD-HIGH | LOW |
| 14 | Locust | 25K | Load Testing | HIGH | LOW |
| 15 | Gunicorn+Workers | 10K | Production Server | **CRITICAL** | VERY LOW |

---

## Already Integrated (8 repos)

LangGraph, FAISS, sentence-transformers, LiteLLM, FastAPI-MCP, Tenacity, httpx, Redis

---

## TOP 15 Recommendations (Priority Order)

### P0 — Critical (do immediately)
1. **Gunicorn + Uvicorn Workers** — One-line change for multi-core + crash recovery

### P1 — Quick Wins (1-2 days each)
2. **Instructor** (11K★) — Replace manual JSON parsing with Pydantic-validated LLM output
3. **FedFred** (~200★) — Modern async FRED client with rate limiting
4. **OpenLLMetry** (5K★) — One-line auto-instrumentation for LangChain+LiteLLM+FAISS
5. **pybreaker** (1.2K★) — Circuit breaker for provider APIs (fail fast when OECD is down)
6. **LangSmith activation** — Already installed, set 2 env vars

### P2 — Near-term (1-2 weeks each)
7. **PydanticAI** (15.7K★) — Typed agent orchestration, perfect FastAPI/Pydantic fit
8. **sdmx1** (~35★) — Unify 5+ SDMX providers into one client
9. **Langfuse** (9K★) — Self-hosted LLM observability
10. **Locust** (25K★) — Load testing for SSE streaming endpoint

### P3 — Strategic (2-4 weeks each)
11. **OpenBB** (63.4K★) — Unified data platform for 100+ sources
12. **LiteLLM** advanced features — Cost tracking, guardrails (already installed)
13. **Mem0** (41K★) — Persistent user memory for personalized conversations
14. **Hypothesis** (7.5K★) — Property-based testing for indicator resolution
15. **yfinance** (22K★) — Add stock/equity data as new provider

---

## Progress Log

| Cycle | Date | Activity | Status |
|-------|------|----------|--------|
| 55 | 2026-03-24 | Phase 1: Top 10 identified | ✅ |
| 56 | 2026-03-24 | Phase 1 EXPANDED: 55 repos evaluated, top 30 deep-dived | ✅ |
| - | - | Phase 2: Debate top 5 with 3 agents | NEXT |
| - | - | Phase A: Gunicorn + Instructor + FedFred | PLANNED |
