"""Explicit HTTP mocks validate the adapter; no hosted model inference is claimed."""
import json
import math

import httpx
import pytest

from invoiceiq import retrieval


@pytest.fixture
def hosted(monkeypatch, repo):
    monkeypatch.setenv("INVOICEIQ_DB", repo.path)
    monkeypatch.setenv("EMBEDDING_BACKEND", "remote")
    monkeypatch.setenv("EMBEDDING_MODEL", "BAAI/bge-m3")
    monkeypatch.setenv("EMBEDDING_API_URL", "https://embedding.invalid/v1/embeddings")
    monkeypatch.setenv("EMBEDDING_API_KEY", "injected-test-token")
    def forbidden(*args, **kwargs):
        raise AssertionError("Remote mode must never load a local model")
    monkeypatch.setattr(retrieval, "embedding_model", forbidden)
    return repo


def mock_http(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr(retrieval.httpx, "Client",
                        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))


def response_for(request):
    batch = json.loads(request.content)["input"]
    return httpx.Response(200, json={"data": [
        {"index": i, "embedding": [3., 4.] + [0.] * 1022}
        for i in reversed(range(len(batch)))]})


def test_remote_cache_and_endpoint_isolation(hosted, monkeypatch):
    calls = []
    def handler(request):
        assert request.headers["authorization"] == "Bearer injected-test-token"
        calls.append(json.loads(request.content)["input"])
        return response_for(request)
    mock_http(monkeypatch, handler)
    vectors = retrieval.encoded(["a", "a", "b"])
    assert vectors[0][:2] == [.6, .8]
    assert math.isclose(sum(x*x for x in vectors[0]), 1.)
    retrieval.encoded(["a", "c"])
    assert calls == [["a", "b"], ["c"]]
    monkeypatch.setenv("EMBEDDING_API_URL", "https://other.invalid/v1/embeddings")
    retrieval.encoded(["a"])
    assert calls[-1] == ["a"]


def test_remote_batch_limit(hosted, monkeypatch):
    batches = []
    def handler(request):
        batches.append(len(json.loads(request.content)["input"]))
        return response_for(request)
    mock_http(monkeypatch, handler)
    assert len(retrieval.encoded([str(i) for i in range(65)])) == 65
    assert batches == [32, 32, 1]


@pytest.mark.parametrize("rows", [
    [],
    [{"index": 1, "embedding": [1.] * 1024}],
    [{"index": 0, "embedding": [1.]}],
    [{"index": 0, "embedding": [0.] * 1024}],
    [{"index": 0, "embedding": ["invalid"] * 1024}],
])
def test_invalid_vectors_not_cached(hosted, monkeypatch, rows):
    mock_http(monkeypatch, lambda request: httpx.Response(200, json={"data": rows}))
    with pytest.raises(ValueError):
        retrieval.encoded(["a"])
    with hosted.connect() as connection:
        assert connection.execute("SELECT count(*) FROM vector_cache").fetchone()[0] == 0


def test_hosted_failure_has_no_local_fallback(hosted, monkeypatch):
    mock_http(monkeypatch, lambda request: httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        retrieval.encoded(["a"])


def test_missing_endpoint_fails_without_loading_local(hosted, monkeypatch):
    monkeypatch.setenv("EMBEDDING_API_URL", "")
    with pytest.raises(RuntimeError, match="EMBEDDING_API_URL"):
        retrieval.encoded(["a"])
