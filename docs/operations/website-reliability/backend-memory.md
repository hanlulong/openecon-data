# Backend memory management

The API runs two workers. Its OpenAI indicator matrix already uses a read-only memory map shared with other workers and MCP. Process RSS counts these shared pages more than once; use proportional set size (PSS), private memory, service memory accounting, and swap when comparing deployments.

The September 2026 memory changes address separate allocations:

- Load the legacy FAISS embedding build cache only when batch embedding or index construction needs it. Ordinary search and statistics use the existing index without loading the JSON build cache. Saving an index must preserve an unloaded cache; incompatible index configuration must not resurrect old cached embeddings.
- Share the wrapper's embedding model/client with its FAISS backend. Preserve the wrapper's raw outputs and the backend's existing normalization. Serialize first initialization and reuse an already loaded model when backend initialization is retried.
- Bound each result-cache instance by entry count and estimated retained payload bytes, with a smaller entry admission limit. Own defensive copies at cache boundaries so callers cannot mutate stored payloads. Preserve result freshness, Redis mirrors, and best-effort stale fallback until eviction.

Cache byte estimates are operating limits for supported Python payloads, not RSS limits. They exclude temporary request/response copies, allocator overhead, native libraries, and other application caches. Each API worker and MCP process can have a global cache and a Redis fallback cache. Disk backups consume storage but do not reduce live memory when deleted.

`CacheService(max_entries=10000, max_bytes=128 * 1024 * 1024, max_entry_bytes=16 * 1024 * 1024)` configures those defaults per instance. Capacity eviction removes the oldest write. Oversized, unsupported, or uncopyable writes are skipped without replacing an existing entry; read-copy failures return a miss. Statistics include `estimated_bytes`, `max_bytes`, `max_entry_bytes`, `evictions`, `rejections`, and `read_copy_failures`. Existing TTL and best-effort stale reads remain in place; eviction can remove a fallback entry.

The offline comparison used the unchanged 41,767-vector FAISS index, cached MiniLM model, and six fixed queries under a 2,300 MiB/no-swap/one-CPU process limit. Ordered top-ten results matched, with scores and embedding norms within 0.000001. The original process retained 40,987 build-cache entries; serving with the updated process retained none and shared the wrapper/backend model.

After those queries and garbage collection, private memory fell from 1,188.3 MiB to 575.0 MiB (613.3 MiB less); proportional memory fell from 1,373.9 MiB to 750.4 MiB. Neither process used swap. First lazy search took 8.035 versus 0.311 seconds in this single trial with cached filesystem data; imports took 6.536 versus 6.458 seconds. These figures describe this component and workload, not the entire backend or guaranteed production latency.

Build-cache file replacement is atomic. On an invalidated index, an empty cache is published before replacement index files, and populated vectors follow successful index/manifest publication. A failed write therefore cannot publish new-model cached vectors ahead of the old index. The existing multi-file index build is not a transaction; model/index migrations still require a coordinated build and rollback. Serving this deployment does not rewrite index or model artifacts.

Cache accounting has a CPU cost. In a separate three-iteration illustration using 10,000 data points, median write time was 0.066 seconds before and 0.255 seconds after, and read time was 0.054 versus 0.094 seconds. Estimated retained payload was 5.24 MiB. This is a cache-operation example, not a throughput test or an application response-time guarantee.

Validation must compare identical queries, models, and index files in separate offline processes under identical resource limits. Report this controlled comparison separately from a production snapshot taken after a restart: old request caches and allocator state are also cleared by restarting. Keep both API workers and existing service memory limits; monitor cache eviction, hit rate, latency, and memory after deployment.

The local website monitor remains the availability check. Deployment verification also requires API/MCP health and representative queries. No GitHub workflows or new dependencies are needed.
