"""Hermetic memory-lifecycle regressions: fake models and FAISS, temporary JSON.

No application import, model downloads, network calls, or real index writes.
"""

import builtins
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import threading
import time
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest


class FakeModel:
    def __init__(self):
        self.calls = []
        self.embeddings = SimpleNamespace(create=self.create)

    def get_sentence_embedding_dimension(self):
        return 2

    def encode(self, texts, **kwargs):
        self.calls.append((texts, kwargs))
        return np.array([3.0, 4.0] if isinstance(texts, str) else [[3.0, 4.0] for _ in texts])

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(data=[SimpleNamespace(embedding=[3.0, 4.0]) for _ in kwargs["input"]])


class FakeIndex:
    metric_type = 0

    def __init__(self, dimensions=2, count=2):
        self.d = dimensions
        self.ntotal = count
        self.last_query = None

    def search(self, query, k):
        self.last_query = query
        return np.array([[0.75, 0.25]])[:, :k], np.array([[0, 1]])[:, :k]


@pytest.fixture
def harness(tmp_path, monkeypatch):
    state = SimpleNamespace(
        directory=tmp_path, models=[], model_attempts=0, fail_models=0,
        model_delay=0, cache_reads=0, cache_delay=0, backend_failures=0,
        index_writes=[], index_present=True, lock=threading.Lock(),
    )
    state.settings = SimpleNamespace(
        embedding_model="local-test", embedding_dimensions=2,
        openai_api_key="isolated-test", openai_base_url="https://unused.example",
        openai_org_id=None,
    )

    def model_factory(*args, **kwargs):
        with state.lock:
            state.model_attempts += 1
            fail = state.fail_models > 0
            if fail:
                state.fail_models -= 1
        time.sleep(state.model_delay)
        if fail:
            raise RuntimeError("isolated model initialization failure")
        model = FakeModel()
        with state.lock:
            state.models.append(model)
        return model

    fake_faiss = ModuleType("faiss")
    fake_faiss.METRIC_INNER_PRODUCT = 0
    fake_faiss.read_index = lambda path: FakeIndex()
    fake_faiss.write_index = lambda index, path: state.index_writes.append(path)
    fake_faiss.IndexFlatIP = lambda dimensions: FakeIndex(dimensions, count=0)
    fake_transformers = ModuleType("sentence_transformers")
    fake_transformers.SentenceTransformer = model_factory
    fake_openai = ModuleType("openai")
    fake_openai.OpenAI = model_factory
    fake_config = ModuleType("backend.config")
    fake_config.get_settings = lambda: state.settings
    for name, module in (
        ("faiss", fake_faiss), ("sentence_transformers", fake_transformers),
        ("openai", fake_openai), ("chromadb", None), ("backend.config", fake_config),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    def load(name, source):
        spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / "services" / source)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        return module

    state.faiss = load("backend.services._memory_test_faiss", "faiss_vector_search.py")
    state.vector = load("backend.services._memory_test_vector", "vector_search.py")
    monkeypatch.setitem(sys.modules, "backend.services.faiss_vector_search", state.faiss)
    state.search_class = state.faiss.FAISSVectorSearch
    state.cache_path = tmp_path / "economic_indicators_embedding_cache.json"
    state.index_path = tmp_path / "economic_indicators.index"
    state.cached_hash = hashlib.sha256(b"cached").hexdigest()
    state.cache_path.write_text(json.dumps({state.cached_hash: [0.0, 1.0]}))
    (tmp_path / "economic_indicators_metadata.json").write_text(json.dumps([
        {"code": "A", "name": "First", "provider": "FRED"},
        {"code": "B", "name": "Second", "provider": "WORLDBANK"},
    ]))
    (tmp_path / "economic_indicators_id_map.json").write_text('{"A":0,"B":1}')

    def manifest(model_name="local-test"):
        (tmp_path / "economic_indicators_manifest.json").write_text(json.dumps({
            "model_name": model_name, "embedding_dim": 2, "metric": "cosine_ip",
        }))

    state.manifest = manifest
    manifest()
    original_exists = Path.exists
    original_getsize = state.faiss.os.path.getsize
    original_open = builtins.open

    def exists(path):
        return state.index_present if path == state.index_path else original_exists(path)

    def getsize(path):
        return 1 if Path(path) == state.index_path else original_getsize(path)

    def observe_open(file, mode="r", *args, **kwargs):
        if not isinstance(file, int) and Path(file) == state.cache_path and "r" in mode:
            with state.lock:
                state.cache_reads += 1
            time.sleep(state.cache_delay)
        return original_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", exists)
    monkeypatch.setattr(state.faiss.os.path, "getsize", getsize)
    monkeypatch.setattr(builtins, "open", observe_open)

    def build(**kwargs):
        return state.search_class(
            model_name=kwargs.pop("model_name", state.settings.embedding_model),
            index_dir=str(tmp_path), embedding_dim=2, embedding_dimensions=2,
            **kwargs,
        )

    def wrapper_backend(**kwargs):
        if state.backend_failures:
            state.backend_failures -= 1
            raise RuntimeError("isolated backend initialization failure")
        kwargs["index_dir"] = str(tmp_path)
        return state.search_class(**kwargs)

    state.build = build
    monkeypatch.setattr(state.faiss, "FAISSVectorSearch", wrapper_backend)
    return state


def test_search_statistics_and_unrelated_save_never_load_or_overwrite_build_cache(harness):
    before = harness.cache_path.read_bytes()
    search = harness.build()
    assert harness.cache_reads == 0
    results = search.search("test query", limit=2)
    assert [item.code for item in results] == ["A", "B"]
    assert [item.distance for item in results] == [0.75, 0.25]
    assert np.allclose(search.index.last_query, [[0.6, 0.8]])
    assert search.get_stats()["index_size"] == 2
    search._save_index()
    assert harness.cache_reads == 0
    assert search.embedding_cache == {}
    assert harness.cache_path.read_bytes() == before
    assert harness.index_writes == [str(harness.index_path)]  # Fake call only.


def test_first_batch_loads_existing_cache_once_and_only_embeds_misses(harness):
    search = harness.build()
    assert harness.cache_reads == 0
    actual = search.embed_batch(["cached", "new", "cached"])
    assert np.allclose(actual, [[0, 1], [0.6, 0.8], [0, 1]])
    assert harness.cache_reads == 1
    assert search.model.calls[0][0] == ["new"]
    search.embed_batch(["cached", "new"])
    assert harness.cache_reads == 1
    assert len(search.model.calls) == 1


def test_concurrent_first_batches_parse_build_cache_only_once(harness):
    search = harness.build()
    assert harness.cache_reads == 0
    harness.cache_delay = 0.03
    start = threading.Barrier(6)

    def embed():
        start.wait(timeout=2)
        return search.embed_batch(["cached"])

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: embed(), range(6)))
    assert all(np.allclose(result, [[0, 1]]) for result in results)
    assert harness.cache_reads == 1
    assert search.model.calls == []


@pytest.mark.parametrize("contents", [None, "invalid json", "null", "[]"])
def test_missing_or_corrupt_build_cache_is_attempted_once_and_batches_still_work(harness, contents):
    if contents is None:
        harness.cache_path.unlink()
    else:
        harness.cache_path.write_text(contents)
    search = harness.build()
    assert harness.cache_reads == 0
    assert np.allclose(search.embed_batch(["new"]), [[0.6, 0.8]])
    reads = harness.cache_reads
    harness.cache_path.write_text(json.dumps({harness.cached_hash: [0, 1]}))
    assert np.allclose(search.embed_batch(["other"]), [[0.6, 0.8]])
    assert harness.cache_reads == reads


def test_normal_reset_keeps_same_model_build_cache_reusable(harness):
    search = harness.build()
    search.reset()
    assert search.index.ntotal == 0
    assert np.allclose(search.embed_batch(["cached"]), [[0, 1]])
    assert search.model.calls == []


def test_incompatible_index_invalidates_old_cache_across_explicit_save_and_restart(harness):
    harness.manifest("incompatible-model")
    search = harness.build()
    assert search.index.ntotal == 0
    search._save_index()
    assert json.loads(harness.cache_path.read_text()) == {}
    restarted = harness.build()
    assert np.allclose(restarted.embed_batch(["cached"]), [[0.6, 0.8]])
    assert restarted.model.calls[0][0] == ["cached"]


@pytest.mark.parametrize("missing", ["index", "metadata", "invalid_metadata"])
def test_unusable_index_never_reuses_orphaned_build_cache(harness, missing):
    metadata = harness.directory / "economic_indicators_metadata.json"
    if missing == "index":
        harness.index_present = False
    elif missing == "metadata":
        metadata.unlink()
    else:
        metadata.write_text("invalid json")
    search = harness.build()
    assert search.index.ntotal == 0
    assert np.allclose(search.embed_batch(["cached"]), [[0.6, 0.8]])
    assert harness.cache_reads == 0
    assert search.model.calls[0][0] == ["cached"]


def test_failed_cache_invalidation_save_cannot_publish_a_compatible_manifest(harness, monkeypatch):
    harness.manifest("incompatible-model")
    manifest = harness.directory / "economic_indicators_manifest.json"
    original_manifest = manifest.read_bytes()
    original_cache = harness.cache_path.read_bytes()
    search = harness.build()

    def fail_dump(value, stream, **kwargs):
        stream.write('{"incomplete":')
        raise OSError("isolated invalidation persistence failure")

    monkeypatch.setattr(harness.faiss.json, "dump", fail_dump)
    search._save_index()
    assert harness.index_writes == []
    assert manifest.read_bytes() == original_manifest
    assert harness.cache_path.read_bytes() == original_cache
    restarted = harness.build()
    assert np.allclose(restarted.embed_batch(["cached"]), [[0.6, 0.8]])
    assert harness.cache_reads == 0


@pytest.mark.parametrize("failure", ["index", "manifest"])
def test_new_model_cache_cannot_lead_old_index_when_index_write_fails(harness, monkeypatch, failure):
    manifest = harness.directory / "economic_indicators_manifest.json"
    old_manifest = manifest.read_bytes()
    migrated = harness.build(model_name="different-local-model")
    migrated.model.encode = lambda texts, **kwargs: np.array([[-4.0, 3.0] for _ in texts])
    assert np.allclose(migrated.embed_batch(["cached"]), [[-0.8, 0.6]])

    def failed_index_write(index, path):
        raise OSError("isolated index write failure")

    with monkeypatch.context() as local:
        if failure == "index":
            local.setattr(harness.faiss.faiss, "write_index", failed_index_write)
        else:
            local.setattr(migrated, "_save_manifest", lambda: False)
        migrated._save_index()
    assert manifest.read_bytes() == old_manifest
    assert json.loads(harness.cache_path.read_text()) == {}
    original_model = harness.build()
    assert np.allclose(original_model.embed_batch(["cached"]), [[0.6, 0.8]])
    assert original_model.model.calls[0][0] == ["cached"]
    # Retrying must preserve new in-memory vectors and publish them only once
    # the matching index/manifest save succeeds.
    migrated._save_index()
    assert json.loads(manifest.read_text())["model_name"] == "different-local-model"
    assert np.allclose(json.loads(harness.cache_path.read_text())[harness.cached_hash], [-0.8, 0.6])
    new_model = harness.build(model_name="different-local-model")
    assert np.allclose(new_model.embed_batch(["cached"]), [[-0.8, 0.6]])
    assert new_model.model.calls == []


def test_loaded_build_cache_save_persists_new_and_reused_entries(harness):
    search = harness.build()
    search.embed_batch(["cached", "new"])
    search._save_embedding_cache()
    saved = json.loads(harness.cache_path.read_text())
    assert saved[harness.cached_hash] == [0, 1]
    assert np.allclose(saved[hashlib.sha256(b"new").hexdigest()], [0.6, 0.8])


def test_cache_save_failure_preserves_previous_complete_file(harness, monkeypatch):
    search = harness.build()
    search.embed_batch(["new"])
    previous = harness.cache_path.read_bytes()

    def fail_dump(value, stream, **kwargs):
        stream.write('{"incomplete":')
        raise OSError("isolated write failure")

    monkeypatch.setattr(harness.faiss.json, "dump", fail_dump)
    search._save_embedding_cache()
    assert harness.cache_path.read_bytes() == previous


@pytest.mark.parametrize("model_name", ["local-test", "text-embedding-3-small"])
def test_explicit_model_is_reused_without_constructing_a_second_backend_model(harness, model_name):
    harness.manifest(model_name)
    supplied = FakeModel()
    search = harness.build(model_name=model_name, embedding_model=supplied)
    assert search.model is supplied
    assert harness.model_attempts == 0
    assert np.allclose(search.embed_text("sample"), [0.6, 0.8])


@pytest.mark.parametrize("model_name", ["local-test", "text-embedding-3-small"])
def test_standalone_backend_still_constructs_its_own_model(harness, model_name):
    harness.manifest(model_name)
    search = harness.build(model_name=model_name)
    assert harness.model_attempts == 1
    assert search.model is harness.models[0]


@pytest.mark.parametrize("model_name", ["local-test", "text-embedding-3-small"])
def test_wrapper_and_backend_share_model_preserving_raw_and_normalized_embeddings(harness, model_name):
    harness.settings.embedding_model = model_name
    harness.manifest(model_name)
    wrapper = harness.vector.VectorSearchService()
    assert np.allclose(wrapper.embed_text("sample"), [3, 4])
    assert wrapper.backend.model is wrapper.model
    assert harness.model_attempts == 1
    assert np.allclose(wrapper.backend.embed_text("sample"), [0.6, 0.8])
    assert np.allclose(wrapper.embed_batch(["sample"]), [[3, 4]])
    assert np.allclose(wrapper.backend.embed_batch(["sample"]), [[0.6, 0.8]])
    if model_name.startswith("text-"):
        assert all(call["dimensions"] == 2 and call["model"] == model_name for call in wrapper.model.calls)


def test_concurrent_wrapper_initialization_constructs_one_model_and_backend(harness):
    harness.model_delay = 0.03
    wrapper = harness.vector.VectorSearchService()
    start = threading.Barrier(6)

    def initialize():
        start.wait(timeout=2)
        wrapper._ensure_initialized()
        return wrapper.backend

    with ThreadPoolExecutor(max_workers=6) as pool:
        backends = list(pool.map(lambda _: initialize(), range(6)))
    assert len({id(backend) for backend in backends}) == 1
    assert harness.model_attempts == 1
    assert wrapper.backend.model is wrapper.model


def test_backend_initialization_failure_retries_without_reloading_successful_model(harness):
    wrapper = harness.vector.VectorSearchService()
    harness.backend_failures = 1
    with pytest.raises(RuntimeError):
        wrapper._ensure_initialized()
    assert wrapper._initialized is False
    first_model = wrapper.model
    wrapper._ensure_initialized()
    assert wrapper._initialized is True
    assert wrapper.model is first_model
    assert wrapper.backend.model is first_model
    assert harness.model_attempts == 1


def test_model_initialization_failure_can_retry_successfully(harness):
    wrapper = harness.vector.VectorSearchService()
    harness.fail_models = 1
    with pytest.raises(RuntimeError, match="isolated model"):
        wrapper._ensure_initialized()
    assert wrapper._initialized is False
    assert wrapper.model is None
    wrapper._ensure_initialized()
    assert wrapper._initialized is True
    assert harness.model_attempts == 2  # One failed attempt, one shared successful model.


def test_concurrent_wrapper_singleton_creation_returns_one_instance(harness, monkeypatch):
    created = []

    def constructor():
        time.sleep(0.03)
        instance = object()
        created.append(instance)
        return instance

    monkeypatch.setattr(harness.vector, "VectorSearchService", constructor)
    monkeypatch.setattr(harness.vector, "_vector_search_service", None)
    start = threading.Barrier(6)

    def obtain():
        start.wait(timeout=2)
        return harness.vector.get_vector_search_service()

    with ThreadPoolExecutor(max_workers=6) as pool:
        returned = list(pool.map(lambda _: obtain(), range(6)))
    assert len(created) == 1
    assert all(item is created[0] for item in returned)
