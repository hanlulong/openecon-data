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
| Catalog concepts (historical) | 95% | Good for common queries, but rule-based as final authority |
| **Retrieval candidates + LLM adjudication** | **target path** | **Current architecture** |

### Decision
Use retrieval to assemble provider-local candidate evidence from the 330K catalog
and let LLM adjudication make the semantic decision. Exact provider-native codes
and exact provider-native titles remain mechanical passthrough; catalog/concept
shortcuts do not act as final authority.

## Pipeline (IndicatorSelector)

```
User Query: "female youth unemployment in Nigeria"
         │
         ▼
Stage 1: PROVIDER-LOCAL CANDIDATE RETRIEVAL
  → FTS5 + embedding retrieval returns WorldBank candidate evidence
  → Candidate names/codes/metadata are shown to the LLM selector
  → LLM picks: SL.UEM.1524.FE.ZS ✅
         │
         ▼ (if LLM rejects all candidates)
Stage 2: BOUNDED ALTERNATE SEARCH
  → LLM returns REJECT + SEARCH terms
  → Retrieval retries once with the alternate search phrase
  → LLM picks, asks, or rejects again
         │
         ▼ (if still unresolved)
Stage 3: NO DECISION / CLARIFICATION / PROVIDER FAIL-CLOSED
  → No top-candidate fallback is allowed
```

## Key Files

| File | Purpose | Lines |
|------|---------|-------|
| `backend/services/indicator_selector.py` | Retrieval + LLM adjudication service | ~350 |
| `backend/services/embedding_retrieval.py` | OpenAI embedding search | ~215 |
| `backend/services/indicator_resolution.py` | Fetch-time authority/provenance gate | ~1,900 |
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
| FTS5 retrieval | Free (local) |
| Embedding search | ~$0.0001 when remote embeddings are used |
| LLM selection | ~$0.001 (1 API call) |
| **Total** | **~$0.001** |

## Providers Covered

All 10 providers: FRED (139K), IMF (115K), WorldBank (29K), CoinGecko (19K), Comtrade (8K), Eurostat (8K), StatsCan (8K), OECD (3K), BIS (61), ExchangeRate (49).

## Integration Status

**UPDATED** (2026-05): `IndicatorSelector` is the semantic selector. The retired
resolver/translator shortcut modules have been removed from runtime and from the
codebase; unresolved selector output now fails closed or asks for clarification
instead of falling back to catalog/translator rules.

```python
# In indicator_resolution.resolve_indicator_for_fetch():
selection = await IndicatorSelector().select(indicator_query, provider)
if selection.code:
    return selection.code  # Retrieval → LLM picked the indicator
# else: keep provider-neutral text / clarification; no shortcut fallback
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
| indicator_selector.py | ACTIVE retrieval + LLM selector | ~350 |
| embedding_retrieval.py | ACTIVE | 215 |
| retired resolver/translator shortcut modules | REMOVED | -2,900+ |
| Old catalog/translator final-authority path | REPLACED with fail-closed selector contract | - |
| semantic_provider_router.py | DEPRECATED (still in `backend/routing/`) | 473 |
| provider_router.py | REMOVED | was 988 |
| keyword_matcher.py | REMOVED | was 520 |
| unified_router.py | ACTIVE (current routing) | ~460 |
