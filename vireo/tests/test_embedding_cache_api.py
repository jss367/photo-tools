"""Settings must account for reusable species labels and clear them too."""

import numpy as np


def test_embedding_cache_size_and_clear_include_individual_labels(app_and_db, tmp_path, monkeypatch):
    import classifier
    from embedding_cache import LabelEmbeddingCache

    app, _db = app_and_db
    cache_dir = tmp_path / "embedding-cache"
    monkeypatch.setattr(classifier, "CACHE_DIR", str(cache_dir))
    labels = LabelEmbeddingCache(cache_dir, {"model_runtime": "test", "labels": ["bird"]}, 4)
    labels.resolve("bird", lambda: np.ones(4, dtype=np.float32))
    expected_size = sum(p.stat().st_size for p in cache_dir.rglob("*.npy"))
    client = app.test_client()
    info = client.get("/api/embedding-cache").get_json()
    assert info["total_size"] == expected_size > 0
    assert info["entries"] == [{"file": "Reusable species labels", "size": expected_size, "label_count": 1}]
    assert client.delete("/api/embedding-cache").status_code == 200
    assert labels.read("bird") is None
    assert client.get("/api/embedding-cache").get_json()["total_size"] == 0
