# Research Plan: Agent Frameworks & Tools

**Created:** 2026-03-24
**Status:** PLANNED (not yet started)
**Owner:** hanlulong

---

## User Request

Conduct a deep research of all relevant information and resources in GitHub, LangChain. List top 50 repos. Investigate each of them. Find useful tools and agent frameworks. Evaluate and have multiple agents to debate before incorporating to our existing approach. This is a long term project, conduct a detailed plan and implement in multiple cycles.

---

## Research Areas

### 1. Agent Frameworks
- LangGraph (already integrated)
- CrewAI
- AutoGen / Microsoft Agent Framework
- PydanticAI
- Haystack
- LlamaIndex agents
- Semantic Kernel

### 2. Structured Output / LLM Tools
- instructor (installed, integration pending)
- Outlines (grammar-guided generation)
- guidance (llguidance)
- LMQL

### 3. Embedding & Search
- BAAI/bge models (downloaded, migration pending)
- Nomic Embed v2
- Cross-encoder rerankers
- ColBERT / late interaction models

### 4. Economic Data Tools
- OpenBB Platform
- fedfred (async FRED client)
- weo-reader (IMF WEO)
- TAM MCP Server

### 5. Memory & State
- Mem0
- Letta
- Zep / Graphiti
- LangGraph checkpoint persistence

### 6. Monitoring & Observability
- LangSmith
- Langfuse
- Phoenix (Arize)

---

## Implementation Plan

### Phase 1: Research (2-3 cycles)
- Web search for top 50 repos in each category
- Read documentation, check GitHub stars, last commit date
- Create comparison matrix

### Phase 2: Evaluation (2-3 cycles)
- Spawn 3+ agents to debate each tool's fit
- Evaluate against OpenEcon's specific needs
- Consider: API compatibility, performance, maintenance burden

### Phase 3: Integration (5+ cycles)
- Start with lowest-risk, highest-impact tools
- Implement with proper migration path
- Test against 100+ benchmarks

---

## Progress Log

| Cycle | Date | Activity | Status |
|-------|------|----------|--------|
| - | - | Not yet started | PLANNED |
