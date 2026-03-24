# Research Plan: Agent Frameworks & Tools

**Created:** 2026-03-24
**Updated:** 2026-03-24
**Status:** PHASE 1 COMPLETE — Top 10 tools identified and prioritized

---

## Top 10 Tools (Prioritized by Impact/Effort)

| Rank | Tool | Stars | Effort | Impact | Priority |
|------|------|-------|--------|--------|----------|
| 1 | **Instructor** | 11K | LOW | LLM output validation | Immediate (installed, needs integration) |
| 2 | **PydanticAI** | 15.4K | MEDIUM | Agent orchestration with DI | Near-term |
| 3 | **FedFred** | Small | LOW | Async FRED + rate limiting | Immediate |
| 4 | **LiteLLM** | 38.6K | LOW-MED | Unified LLM gateway (100+ providers) | Near-term |
| 5 | **OpenBB** | 63.4K | MED-HIGH | Multi-provider economic data | Strategic |
| 6 | **LangGraph** | 24.8K | HIGH | Stateful agent orchestration | Strategic (already partial) |
| 7 | **DSPy** | 23K | MED-HIGH | Programmatic prompt optimization | Strategic |
| 8 | **Agno** | 38.7K | HIGH | Full agent runtime | Evaluate |
| 9 | **OpenAI Agents SDK** | Active | MEDIUM | Multi-agent workflows | Evaluate |
| 10 | **Mirascope** | 1.4K | LOW-MED | Lightweight LLM interaction | Optional |

---

## Recommended Integration Order

### Phase A: Quick Wins (1-2 cycles each)
1. **Instructor** — replace manual JSON parsing with Pydantic response_model validation
2. **FedFred** — replace custom FRED httpx calls with async client + rate limiting

### Phase B: LLM Layer (2-3 cycles)
3. **LiteLLM** — replace custom LLM provider abstraction with unified gateway

### Phase C: Agent Architecture (5+ cycles)
4. **PydanticAI** — restructure query pipeline into typed agent definitions
5. **OpenBB** — evaluate as data aggregation layer for 4-5 providers

### Phase D: Advanced (future)
6. **DSPy** — optimize prompts programmatically
7. **LangGraph** checkpoint persistence

---

## Progress Log

| Cycle | Date | Activity | Status |
|-------|------|----------|--------|
| 55 | 2026-03-24 | Phase 1: Research complete. Top 10 identified. | ✅ DONE |
| - | - | Phase 2: Debate (3 agents evaluate top 3) | NEXT |
| - | - | Phase A: Instructor integration | PLANNED |
| - | - | Phase A: FedFred integration | PLANNED |
