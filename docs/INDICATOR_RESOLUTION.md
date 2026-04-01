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

## Migration Status

The IndicatorSelector is built and tested but **not yet integrated** into the main query pipeline (`query.py`). The current production system uses `indicator_resolver.py`. Integration is the next step.
