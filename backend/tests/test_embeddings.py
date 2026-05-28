"""Unit tests for the Embedder factory + FakeEmbedder.

No real model is loaded here (that's the slow integration test). These cover
the factory dispatch, the FakeEmbedder's determinism + cosine-ordering
invariant (which the /search tests rely on), and the stub NotImplementedError
contract.
"""

import math

import pytest

from app.embeddings import FakeEmbedder, get_embedder
from app.embeddings.sentence_transformers import SentenceTransformersEmbedder
from app.embeddings.stubs import OllamaEmbedder, OpenAIEmbedder, VoyageEmbedder


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb)


# --- Factory --------------------------------------------------------------


def test_factory_bge_small_is_default_dim_384() -> None:
    embedder = get_embedder("bge-small")
    assert isinstance(embedder, SentenceTransformersEmbedder)
    assert embedder.name == "bge-small-en-v1.5"
    assert embedder.dimension == 384


def test_factory_bge_base_dim_768() -> None:
    embedder = get_embedder("bge-base")
    assert embedder.name == "bge-base-en-v1.5"
    assert embedder.dimension == 768


def test_factory_mock_returns_fake() -> None:
    assert isinstance(get_embedder("mock"), FakeEmbedder)


def test_factory_unknown_provider_raises() -> None:
    with pytest.raises(ValueError, match="unknown EMBEDDING_PROVIDER"):
        get_embedder("does-not-exist")


def test_factory_is_cached() -> None:
    # Same provider id returns the same instance (so the model loads once).
    assert get_embedder("bge-small") is get_embedder("bge-small")


def test_stub_embedders_raise_not_implemented() -> None:
    for cls in (OpenAIEmbedder, VoyageEmbedder, OllamaEmbedder):
        inst = cls()
        # name/dimension are introspectable...
        assert isinstance(inst.name, str)
        assert inst.dimension > 0
        # ...but embedding raises.
        with pytest.raises(NotImplementedError):
            inst.embed_one("x")
        with pytest.raises(NotImplementedError):
            inst.embed_batch(["x"])


# --- FakeEmbedder ---------------------------------------------------------


def test_fake_dimension_and_unit_length() -> None:
    fake = FakeEmbedder(dim=384)
    assert fake.dimension == 384
    vec = fake.embed_one("def main(): pass")
    assert len(vec) == 384
    assert math.isclose(math.sqrt(sum(x * x for x in vec)), 1.0, rel_tol=1e-6)


def test_fake_is_deterministic() -> None:
    fake = FakeEmbedder()
    assert fake.embed_one("hello world") == fake.embed_one("hello world")


def test_fake_empty_text_is_nonzero_unit_vector() -> None:
    fake = FakeEmbedder()
    vec = fake.embed_one("")
    assert math.isclose(math.sqrt(sum(x * x for x in vec)), 1.0, rel_tol=1e-6)


def test_fake_batch_matches_one() -> None:
    fake = FakeEmbedder()
    texts = ["alpha beta", "gamma delta"]
    batch = fake.embed_batch(texts)
    assert batch == [fake.embed_one(t) for t in texts]


def test_fake_cosine_ordering_invariant() -> None:
    """A chunk that shares tokens with the query must score higher than one
    that doesn't. This is the property the /search ranking tests depend on.
    """
    fake = FakeEmbedder()
    query = fake.embed_one("async database connection pool")
    related = fake.embed_one(
        "def get_connection(): return await database.connection_pool.acquire()"
    )
    unrelated = fake.embed_one("the quick brown fox jumps over the lazy dog")
    assert _cosine(query, related) > _cosine(query, unrelated)
