# Indicator Resolution Architecture

## Overview

OpenEcon resolves natural language queries to specific economic indicators from a database of **330,000+ indicators** across 10 data providers. This document describes the resolution architecture and the decisions behind it.

## Architecture Decision Record (2026-04-01)

### Problem
Users ask questions like "female youth unemployment in Nigeria" — the system must find the exact indicator code `SL.UEM.1524.FE.ZS` from 330K possibilities.

### Approaches Tested

| Approach | Top-5 Accuracy | Notes |
|----------|---------------|-------|
| FTS5 keyword search (BM25) | 30% | Fails on vocabulary mismatch |
| FAISS MiniLM-L6 embeddings | 0% | Model too generic for economic terms |
| OpenAI text-embedding-3-small | 80% | Understands semantic meaning |
| Catalog concepts (86 curated) | 95% | Perfect for common queries only |
| **Catalog + OpenAI embed + LLM** | **96-100%** | **Final architecture** |

### Decision
Use a multi-stage pipeline: catalog for common concepts, OpenAI embeddings for the 330K long tail, and LLM for final variant selection.

## Pipeline (IndicatorSelector)

```
User Query: "female youth unemployment in Nigeria"
         │
         ▼
Stage 1: CATALOG CONCEPT MATCH
  → find_concept_by_term("female youth unemployment") → "unemployment"
  → get_indicator_code("unemployment", "WorldBank") → SL.UEM.TOTL.ZS
  → Get 63 variants in unemployment family
  → LLM picks: SL.UEM.1524.FE.ZS ✅
         │
         ▼ (only if catalog has no match)
Stage 1.5: DIRECT NAME MATCH (SQL LIKE)
  → WHERE LOWER(name) LIKE '%maternal mortality ratio%'
  → Exact match: SH.STA.MMRT ✅
         │
         ▼ (only if name match has no results)
Stage 2: OPENAI EMBEDDING SEARCH
  → Embed query → find 20 nearest indicators by cosine similarity
  → LLM picks best from 20 candidates
         │
         ▼ (only if embeddings unavailable)
Stage 3: FTS5 OR SEARCH (fallback)
  → Traditional keyword search as last resort
```

## Key Files

| File | Purpose | Lines |
|------|---------|-------|
| `backend/services/indicator_selector.py` | Main resolution service | ~350 |
| `backend/services/embedding_retrieval.py` | OpenAI embedding search | ~215 |
| `backend/services/indicator_resolver.py` | Legacy resolver (still in production pipeline) | ~1,960 |
| `backend/catalog/concepts/*.yaml` | 86 curated concept definitions | ~3,500 |
| `backend/data/openai_embeddings/` | Pre-built embedding index (not in git) | 584MB |

## Building the Embedding Index

The embedding index must be built locally (too large for git):

```python
from backend.services.embedding_retrieval import get_embedding_retrieval
er = get_embedding_retrieval()
er.build_index()  # ~$0.18, ~13 minutes
```

## LLM Selection Prompt

When multiple candidates are found, the LLM decides:
- **CLEAR** query → auto-pick the best indicator
- **AMBIGUOUS** query → return ≤10 options for user to choose

The LLM understands that:
- "unemployment" = total rate (not youth/female/by-education)
- "GDP per capita" ≠ "GDP total"
- "Moody's Baa" = corporate bond (not Treasury)

## Cost

| Component | Cost per query |
|-----------|---------------|
| Catalog lookup | Free (local) |
| Name match | Free (SQL) |
| Embedding search | ~$0.0001 (1 API call) |
| LLM selection | ~$0.001 (1 API call) |
| **Total** | **~$0.001** |

## Providers Covered

All 10 providers: FRED (139K), IMF (115K), WorldBank (29K), CoinGecko (19K), Comtrade (8K), Eurostat (8K), StatsCan (8K), OECD (3K), BIS (61), ExchangeRate (49).

## Integration Status

**INTEGRATED** (2026-04-01): The IndicatorSelector is wired into `query.py`'s
`_resolve_indicator_for_fetch()` as the PRIMARY resolution path. The legacy
`indicator_resolver.py` serves as fallback when embeddings are unavailable.

```python
# In query.py _resolve_indicator_for_fetch():
selection = await IndicatorSelector().select(indicator_query, provider)
if selection.code:
    return selection.code  # Embed → LLM picked the indicator
# else: fall through to legacy IndicatorResolver
```

## Routing Architecture (Updated 2026-04)

Query routing now uses **LLM-based routing via UnifiedRouter** (`backend/routing/unified_router.py`), replacing the old deterministic `ProviderRouter` and `keyword_matcher.py`. The LLM system prompt includes a provider capability matrix, and the UnifiedRouter makes the final routing decision. Key changes:

- **LLM-based routing** replaced regex/keyword routing (Phases 1-4 of consolidation)
- **Intent caching** for repeat queries (in-memory + Redis)
- **Multi-round conversations** with Redis persistence via `ConversationManager`
- **Performance**: ~4x faster cold queries, ~72x faster cached queries
- **85% effective** sweep accuracy with 0 semantic failures

## Cleanup Status

| Component | Status | Lines |
|-----------|--------|-------|
| ChromaDB code in vector_search.py | REMOVED | -165 |
| indicator_selector.py | SIMPLIFIED to embed->LLM | 220 |
| embedding_retrieval.py | ACTIVE | 215 |
| Old 4-stage selector code | REPLACED with 2-step | -280 |
| semantic_provider_router.py | DEPRECATED (still in `backend/routing/`) | 473 |
| provider_router.py | REMOVED | was 988 |
| keyword_matcher.py | REMOVED | was 520 |
| unified_router.py | ACTIVE (current routing) | ~460 |
