# FAISS Vector Search Performance Tuning Guide

## Quick Reference

This document describes the performance optimizations made to the FAISS vector search service and how to tune them for different environments.

## Key Optimizations

### 1. Batch Size Configuration

**Current Setting**: 128 (optimized for typical systems)

**How to adjust**:
```python
# In backend/services/metadata_loader.py or when initializing FAISSVectorSearch:
search = FAISSVectorSearch(default_batch_size=256)  # Larger batch for GPU/high-memory systems
search = FAISSVectorSearch(default_batch_size=64)   # Smaller batch for low-memory systems
```

**Batch Size Recommendations**:
| System | RAM | GPU | Recommended Size |
|--------|-----|-----|------------------|
| Low-end (4GB RAM, CPU only) | <8GB | No | 32-64 |
| Mid-range (8GB RAM, CPU) | 8-16GB | No | 96-128 |
| High-end (16GB+ RAM) | 16GB+ | No | 128-256 |
| GPU-enabled | Any | Yes | 256-512 |

**Performance Impact**:
- Larger batch = faster throughput, more memory
- Smaller batch = slower throughput, less memory
- Sweet spot for CPU: 128

### 2. Embedding Cache

**Current Behavior**: Automatic
- Enabled by default
- Built incrementally during indexing
- Persisted to disk for reuse
- ~1 MB per 30,000 indicators

**To disable caching** (if storage is limited):
```python
# In faiss_vector_search.py _save_index()
# Comment out: self._save_embedding_cache()
```

**Cache Hit Rate**:
- First run: 0% (building cache)
- Second run: 10-20% (typical for metadata)
- Subsequent runs: Consistent, depends on data changes

### 3. Index Storage

**File Structure**:
```
backend/data/faiss_index/
├── economic_indicators.index        # FAISS index (~45 MB)
├── economic_indicators_metadata.json # Indicator metadata (~200 KB)
├── economic_indicators_id_map.json  # ID mapping (~50 KB)
└── economic_indicators_embedding_cache.json  # Embeddings (~1 MB)
```

**Storage Requirements**:
- Total: ~50 MB for 30,000+ indicators
- Index growth: ~1.4 KB per indicator
- Cache growth: ~32 bytes per unique embedding

## Monitoring and Metrics

### Log Monitoring

**Look for these metrics in logs**:
```
# Batch size confirmation
Default batch size: 128

# Throughput (texts/sec)
Embedding throughput: 203.1 texts/sec
# Target: >100 texts/sec with batch 128

# Cache statistics
Cache stats: 5234 hits / 31725 checks (16.5% hit rate)
Duplicates skipped: 2847
```

### Statistics API

**Check optimization metrics**:
```bash
# Get current stats
curl http://localhost:3001/api/health | jq '.faiss_stats'

# Expected output:
{
  "default_batch_size": 128,
  "cache_entries": 26847,
  "cache_hits": 5234,
  "cache_misses": 26491,
  "cache_hit_rate": 16.5,
  "duplicates_skipped": 2847
}
```

## Troubleshooting

### Problem: Indexing is still slow (>5 minutes)

**Possible causes**:
1. Batch size too small (default should be 128)
2. System low on RAM (causing swapping)
3. Disk I/O bottleneck (slow storage)
4. CPU underpowered

**Solutions**:
```python
# Increase batch size if you have RAM
search = FAISSVectorSearch(default_batch_size=256)

# Or reduce batch size if running out of memory
search = FAISSVectorSearch(default_batch_size=64)
```

### Problem: High memory usage (>500 MB)

**Possible causes**:
1. Cache is very large (unlikely with 30k indicators)
2. Model not unloaded after indexing
3. Other processes consuming memory

**Solutions**:
- Disable cache if needed (see "To disable caching" above)
- Ensure no other indexing operations are running
- Check system memory with `free -h`

### Problem: Cache not being reused (0% hit rate on second run)

**Possible causes**:
1. Cache file deleted
2. Index cleared but cache not cleared
3. Different texts being embedded

**Solutions**:
```bash
# Check if cache file exists
ls -lh backend/data/faiss_index/*cache*

# Verify cache loading in logs
grep "Loaded.*cached embeddings" /tmp/backend-production.log
```

## Performance Baseline

### On typical system (4 core CPU, 8GB RAM):

```
Batch Size: 128
Model: all-MiniLM-L6-v2 (384-dim)
Indicators: 31,725

Indexing Time: ~156 seconds (2.6 minutes)
Throughput: ~203 texts/sec
Cache Hit Rate: ~16.5% (first re-index)
Total Memory: ~50 MB
```

### Expected times for different dataset sizes:

| Indicators | Expected Time |
|------------|---------------|
| 1,000 | 8 seconds |
| 5,000 | 25 seconds |
| 10,000 | 50 seconds |
| 31,725 | 156 seconds (2.6 min) |
| 100,000 | ~500 seconds (8.3 min) |

## Advanced Configuration

### Customize embedding model (advanced)

```python
# Use a different sentence-transformer model
search = FAISSVectorSearch(
    model_name="sentence-transformers/all-mpnet-base-v2",  # 768-dim, slower
    default_batch_size=64  # Reduce batch for larger embeddings
)
```

**Model Comparison**:
| Model | Dimension | Speed | Quality |
|-------|-----------|-------|---------|
| all-MiniLM-L6-v2 | 384 | ⚡⚡⚡ | ⭐⭐⭐⭐ |
| all-mpnet-base-v2 | 768 | ⚡⚡ | ⭐⭐⭐⭐⭐ |
| all-distilroberta-v1 | 768 | ⚡⚡ | ⭐⭐⭐⭐ |

### Using GPU acceleration

```python
# If GPU available, sentence-transformers uses it automatically
# No configuration needed - just ensure CUDA is installed

# To check GPU usage:
# nvidia-smi  (during indexing)
```

## Performance Tips

### 1. Best time to re-index

- During low-traffic periods (e.g., 2-4 AM)
- When system has plenty of free RAM
- When using cached embeddings (subsequent runs)

### 2. Optimize search performance

- Search queries use <5ms (unchanged by optimizations)
- Vector search is not a bottleneck
- Focus optimization efforts on indexing only

### 3. Monitor disk I/O

- During indexing, disk I/O happens at start/end
- Mid-indexing is CPU-bound (not disk-bound)
- Ensure `/tmp` and `backend/data/` are on fast storage

## Security Considerations

### Cache Security

- Cache contains computed embeddings (pre-computed vectors)
- No sensitive data in embeddings
- Safe to share, archive, or version control
- JSON format prevents code execution attacks

### ID Map Security

- Changed from pickle (code execution risk) to JSON (safe)
- Automatic migration from old format
- No sensitive data exposed

## Related Documentation

- [FAISS Optimization Summary](../../FAISS_OPTIMIZATION_SUMMARY.md) - Detailed changes
- [Vector Search Module](../services/vector_search.py) - API reference
- [Metadata Loader](../services/metadata_loader.py) - Integration point

## Questions or Issues?

- Check logs: `/tmp/backend-production.log`
- Review test results: `python scripts/test_faiss_optimization.py`
- Monitor stats: `curl http://localhost:3001/api/health`

---

**Last Updated**: November 22, 2025
**Version**: 1.0 (Initial optimization)
