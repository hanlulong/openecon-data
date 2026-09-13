"""Index identity/shape regressions using tiny fixed vectors and no network."""

import copy
import json
import sqlite3
from types import SimpleNamespace

import numpy as np
import pytest

from backend.services import embedding_retrieval as retrieval
from scripts import append_missing_rows_to_name_index as append_rows
from scripts import build_weighted_synonym_index as blend_rows


@pytest.fixture
def index(tmp_path, monkeypatch):
    for attr, filename in (
        ("INDEX_NPY", "index.npy"), ("INDEX_FILE", "index.npz"),
        ("META_FILE", "meta.json"),
    ):
        monkeypatch.setattr(retrieval, attr, tmp_path / filename)
    metadata = {"codes": ["A", "B"], "names": ["First", "Tail"],
                "providers": ["FRED", "CoinGecko"]}
    matrix = np.zeros((2, retrieval.EMBEDDING_DIM), dtype=np.float32)
    matrix[0, 0] = matrix[1, 1] = 1
    retrieval.META_FILE.write_text(json.dumps(metadata))
    np.save(retrieval.INDEX_NPY, matrix)
    np.savez(retrieval.INDEX_FILE, embeddings=matrix)
    return metadata, matrix


def test_tail_result_keeps_name_code_provider_identity(index, monkeypatch):
    _, matrix = index
    service = retrieval.EmbeddingRetrieval()
    monkeypatch.setattr(service, "_embed_query", lambda query: matrix[1])
    assert service.search("fixed tail", top_k=1) == [
        {"code": "B", "name": "Tail", "provider": "CoinGecko", "score": 1.0}
    ]
    assert isinstance(service._embeddings, np.memmap)
    assert not service._embeddings.flags.writeable


@pytest.mark.parametrize("bad", [
    None, [], {}, {"codes": ["A"], "names": [], "providers": ["FRED"]},
    {"codes": ["A"], "names": ["First"], "providers": []},
    {"codes": "A", "names": ["First"], "providers": ["FRED"]},
    {"codes": ["A"], "names": [None], "providers": ["FRED"]},
    {"codes": ["A"], "names": [" "], "providers": ["FRED"]},
    {"codes": [1], "names": ["First"], "providers": ["FRED"]},
])
def test_bad_metadata_fails_before_loading_either_matrix(index, monkeypatch, bad):
    retrieval.META_FILE.write_text(json.dumps(bad))
    loads = []
    monkeypatch.setattr(retrieval.np, "load", lambda *a, **kw: loads.append(a))
    service = retrieval.EmbeddingRetrieval()
    assert service._load_index() is False
    assert loads == []
    assert not service._loaded and service._embeddings is None
    assert service._codes == service._names == service._providers == []


@pytest.mark.parametrize("shape,dtype", [
    ((2,), "float32"), ((2, 3), "float32"), ((1, 1536), "float32"),
    ((2, 1536, 1), "float32"), ((2, 1536), "float64"),
])
def test_invalid_existing_mmap_never_loads_large_fallback(index, monkeypatch, shape, dtype):
    np.save(retrieval.INDEX_NPY, np.zeros(shape, dtype=dtype))
    original = np.load
    loads = []

    def observed(path, *args, **kwargs):
        loads.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(retrieval.np, "load", observed)
    service = retrieval.EmbeddingRetrieval()
    assert service._load_index() is False
    assert loads == [retrieval.INDEX_NPY]
    assert service._embeddings is None and not service._loaded
    assert service._codes == service._names == service._providers == []


def test_absent_mmap_uses_valid_npz(index):
    retrieval.INDEX_NPY.unlink()
    service = retrieval.EmbeddingRetrieval()
    assert service._load_index() is True
    np.testing.assert_array_equal(service._embeddings, index[1])


def test_invalid_npz_cannot_publish_loaded_state(index):
    retrieval.INDEX_NPY.unlink()
    np.savez(retrieval.INDEX_FILE, embeddings=np.ones((2, 3), dtype=np.float32))
    service = retrieval.EmbeddingRetrieval()
    assert service._load_index() is False
    assert not service._loaded and service._embeddings is None
    assert service._codes == service._names == service._providers == []


@pytest.fixture
def append_base(tmp_path, monkeypatch):
    data = tmp_path / "backend" / "data"
    base = data / "openai_embeddings" / "backup_pre_synonyms"
    base.mkdir(parents=True)
    meta = {"codes": ["A"], "names": ["Existing"], "providers": ["FRED"]}
    matrix = np.zeros((1, retrieval.EMBEDDING_DIM), dtype=np.float32)
    matrix[0, 0] = 1
    (base / "indicator_metadata.json").write_text(json.dumps(meta))
    np.savez(base / "indicator_embeddings.npz", embeddings=matrix)
    with sqlite3.connect(data / "indicators.db") as conn:
        conn.execute("CREATE TABLE indicators(provider TEXT, code TEXT, name TEXT)")
        conn.executemany("INSERT INTO indicators VALUES (?,?,?)", [
            ("FRED", "A", "Existing"), ("CoinGecko", "B", "Tail"),
            ("ChinaMacro", "C", "Another tail"),
        ])
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        # Deliberately reverse API response order: index identifies each input.
        return SimpleNamespace(data=[
            SimpleNamespace(index=i, embedding=np.eye(1, retrieval.EMBEDDING_DIM, i + 1)[0].tolist())
            for i in reversed(range(len(kwargs["input"])))
        ])

    monkeypatch.setattr(append_rows, "ROOT", tmp_path)
    monkeypatch.setattr(append_rows, "BASE", base)
    monkeypatch.setattr(append_rows, "IDX_DIR", base.parent)
    monkeypatch.setattr(retrieval.EmbeddingRetrieval, "_get_client", lambda self: SimpleNamespace(
        embeddings=SimpleNamespace(create=create)))
    return base, copy.deepcopy(meta), matrix, calls


def test_append_preserves_prefix_and_appends_all_fields_in_response_index_order(append_base):
    base, old_meta, old_matrix, calls = append_base
    assert append_rows.main() == 0
    meta = json.loads((base / "indicator_metadata.json").read_text())
    for key in old_meta:
        assert meta[key][:1] == old_meta[key]
        assert len(meta[key]) == 3
    assert meta["names"] == ["Existing", "Tail", "Another tail"]
    with np.load(base / "indicator_embeddings.npz") as data:
        matrix = data["embeddings"]
        np.testing.assert_array_equal(matrix[:1], old_matrix)
        assert matrix[1, 1] == matrix[2, 2] == 1
    assert append_rows.main() == 0
    assert len(calls) == 1


def test_invalid_append_base_rejected_before_paid_calls_or_writes(append_base):
    base, meta, _, calls = append_base
    meta["names"] = []
    path = base / "indicator_metadata.json"
    path.write_text(json.dumps(meta))
    before = {p.name: p.read_bytes() for p in base.iterdir()}
    with pytest.raises(ValueError):
        append_rows.main()
    assert calls == []
    assert {p.name: p.read_bytes() for p in base.iterdir()} == before


@pytest.mark.parametrize("bad", [
    "count", "duplicate_index", "bool_index", "float_index", "dimensions", "nonfinite", "zero", "overflow_norm",
])
def test_invalid_embedding_response_cannot_publish_any_artifact(append_base, monkeypatch, bad):
    base, _, _, _ = append_base
    before = {p.name: p.read_bytes() for p in base.iterdir()}
    vector = np.eye(1, retrieval.EMBEDDING_DIM)[0].tolist()
    data = [SimpleNamespace(index=i, embedding=vector.copy()) for i in range(2)]
    if bad == "count":
        data.pop()
    elif bad == "duplicate_index":
        data[1].index = 0
    elif bad == "bool_index":
        data[0].index = False
    elif bad == "float_index":
        data[0].index = 0.0
    elif bad == "dimensions":
        data[0].embedding = [1.0]
    elif bad == "nonfinite":
        data[0].embedding[0] = float("nan")
    elif bad == "overflow_norm":
        data[0].embedding = [float(np.finfo(np.float32).max)] * retrieval.EMBEDDING_DIM
    else:
        data[0].embedding = [0.0] * retrieval.EMBEDDING_DIM
    client = SimpleNamespace(embeddings=SimpleNamespace(create=lambda **kw: SimpleNamespace(data=data)))
    monkeypatch.setattr(retrieval.EmbeddingRetrieval, "_get_client", lambda self: client)
    with pytest.raises(ValueError):
        append_rows.main()
    assert {p.name: p.read_bytes() for p in base.iterdir()} == before
    assert list(base.parent.iterdir()) == [base]


def test_weighted_builder_rejects_corrupt_base_before_paid_calls(append_base, monkeypatch):
    base, meta, _, calls = append_base
    meta["names"] = []
    (base / "indicator_metadata.json").write_text(json.dumps(meta))
    monkeypatch.setattr(blend_rows, "BACKUP", base)
    monkeypatch.setattr("sys.argv", ["blend"])
    with pytest.raises(ValueError):
        blend_rows.main()
    assert calls == []


def test_weighted_builder_preserves_identity_with_reordered_responses(append_base, monkeypatch):
    base, _, _, _ = append_base
    assert append_rows.main() == 0
    with sqlite3.connect(append_rows.ROOT / "backend/data/indicators.db") as conn:
        conn.execute("ALTER TABLE indicators ADD COLUMN synonyms TEXT")
        conn.execute("UPDATE indicators SET synonyms='test synonym phrase' WHERE code IN ('B','C')")
    monkeypatch.setattr(blend_rows, "BACKUP", base)
    monkeypatch.setattr(blend_rows, "ROOT", append_rows.ROOT)
    monkeypatch.setattr(blend_rows, "NPZ", base.parent / "result.npz")
    monkeypatch.setattr(blend_rows, "META", base.parent / "result.json")
    monkeypatch.setattr(blend_rows, "PROBES", [("fixed query", "FRED")])
    monkeypatch.setattr(blend_rows, "_write_normalized_npy_atomic", lambda matrix: None)
    monkeypatch.setattr("sys.argv", ["blend"])
    assert blend_rows.main() == 0
    assert json.loads(blend_rows.META.read_text()) == json.loads((base / "indicator_metadata.json").read_text())
    with np.load(blend_rows.NPZ) as result, np.load(base / "indicator_embeddings.npz") as original:
        np.testing.assert_array_equal(result["embeddings"], original["embeddings"])
