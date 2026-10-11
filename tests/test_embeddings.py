import math
import os
import subprocess
import sys

import pytest

from test_context_graph.embeddings import (
    BGE_QUERY_INSTRUCTION,
    DEFAULT_MODEL,
    FastEmbedEmbedder,
    HashEmbedder,
    default_embedder,
    tokenize,
)


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))  # the vectors are unit length


def norm(vector: list[float]) -> float:
    return math.sqrt(sum(x * x for x in vector))


# ---- tokenize -----------------------------------------------------------------------------


def test_tokenize_splits_camel_case_and_keeps_the_whole_identifier():
    assert tokenize("getOwnerById") == ["get", "owner", "by", "id", "getownerbyid"]


def test_tokenize_splits_snake_case_digits_acronyms_and_punctuation():
    tokens = tokenize("owner_repository HTTPServer2Config /api/owners/{ownerId}")
    assert tokens[:2] == ["owner", "repository"]
    assert ["http", "server", "2", "config", "httpserver2config"] == tokens[2:7]
    assert tokens[7:] == ["api", "owners", "owner", "id", "ownerid"]


def test_tokenize_keeps_non_ascii_words_whole_and_drops_punctuation_only_text():
    assert tokenize("Größe der Haustiere") == ["größe", "der", "haustiere"]
    assert tokenize("-- ;; {} ()") == []


# ---- HashEmbedder -------------------------------------------------------------------------


def test_hash_embedder_names_itself_after_its_dimension():
    embedder = HashEmbedder(64)
    assert embedder.dim == 64
    assert embedder.model_name == "hash-64"
    assert HashEmbedder().dim == 256


def test_hash_embedder_rejects_a_non_positive_dimension():
    with pytest.raises(ValueError):
        HashEmbedder(0)


def test_hash_embedder_is_deterministic():
    text = "OwnerRepository.findByLastName(String lastName)"
    first = HashEmbedder().embed_query(text)
    assert first == HashEmbedder().embed_query(text)
    assert HashEmbedder().embed_documents([text, text]) == [first, first]


def test_hash_embedder_is_stable_across_processes():
    """Python's hash() of a str changes per process (PYTHONHASHSEED); the vectors must not."""
    text = "OwnerRepository.findByLastName(String lastName)"
    script = (
        "from test_context_graph.embeddings import HashEmbedder; "
        f"print(repr(HashEmbedder(32).embed_query({text!r})))"
    )
    env = {**os.environ, "PYTHONHASHSEED": "12345"}
    out = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, check=True
    ).stdout
    assert eval(out) == HashEmbedder(32).embed_query(text)


@pytest.mark.parametrize(
    "text", ["", "   ", "{}();", "x", "public class Owner extends Person {}", "owner " * 500]
)
def test_hash_embedder_vectors_are_unit_length(text):
    vector = HashEmbedder().embed_query(text)
    assert len(vector) == 256
    assert all(isinstance(x, float) for x in vector)
    assert norm(vector) == pytest.approx(1.0)


def test_hash_embedder_scores_texts_sharing_words_above_unrelated_ones():
    embedder = HashEmbedder()
    query = embedder.embed_query("find owners by last name")
    related, partly, unrelated = embedder.embed_documents(
        [
            "Collection<Owner> findByLastName(String lastName) throws DataAccessException",
            "Owner findById(int id)",
            "PetTypeFormatter prints the pet type for a locale",
        ]
    )
    assert cosine(query, related) > cosine(query, partly) > cosine(query, unrelated)
    assert cosine(query, query) == pytest.approx(1.0)


def test_hash_embedder_embeds_no_documents_to_no_vectors():
    assert HashEmbedder().embed_documents([]) == []


# ---- FastEmbedEmbedder, without the model -------------------------------------------------


class PlainFakeModel:
    """Stands in for a fastembed model with only embed(): records calls, returns unnormalised
    vectors."""

    embedding_size = 3

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], dict]] = []

    def embed(self, texts, **kwargs):
        self.calls.append(("embed", list(texts), kwargs))
        return iter([[1.0, 1.0, 0.0] for _ in texts])


class FakeModel(PlainFakeModel):
    """Like fastembed.TextEmbedding: passage and query variants too."""

    def passage_embed(self, texts, **kwargs):
        self.calls.append(("passage_embed", list(texts), kwargs))
        return iter([[3.0, 4.0, 0.0] for _ in texts])

    def query_embed(self, query, **kwargs):
        self.calls.append(("query_embed", [query], kwargs))
        return iter([[0.0, 0.0, 2.0]])


def fake_embedder(model: PlainFakeModel, name: str = DEFAULT_MODEL) -> FastEmbedEmbedder:
    embedder = FastEmbedEmbedder(name, batch_size=7)
    embedder._model = model  # what _load() would have created
    return embedder


def test_fastembed_embedder_loads_nothing_until_used():
    embedder = FastEmbedEmbedder()
    assert embedder.model_name == "BAAI/bge-small-en-v1.5"
    assert embedder._model is None


def test_importing_embeddings_does_not_import_fastembed():
    script = (
        "import sys; from test_context_graph.embeddings import FastEmbedEmbedder, default_embedder; "
        "FastEmbedEmbedder(); default_embedder('fastembed'); print('fastembed' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


def test_fastembed_embedder_embeds_documents_as_passages_in_batches_normalised():
    model = FakeModel()
    vectors = fake_embedder(model).embed_documents(["a", "b"])
    assert vectors == [[0.6, 0.8, 0.0], [0.6, 0.8, 0.0]]
    assert model.calls == [("passage_embed", ["a", "b"], {"batch_size": 7})]


def test_fastembed_embedder_adds_the_bge_instruction_to_queries_only():
    model = FakeModel()
    embedder = fake_embedder(model)
    assert embedder.embed_query("owner by id") == [0.0, 0.0, 1.0]
    assert model.calls == [("query_embed", [BGE_QUERY_INSTRUCTION + "owner by id"], {})]
    embedder.embed_documents(["owner by id"])
    assert model.calls[-1][1] == ["owner by id"]


@pytest.mark.parametrize(
    ("name", "prefix"),
    [
        ("BAAI/bge-small-en-v1.5", BGE_QUERY_INSTRUCTION),
        ("BAAI/bge-base-en-v1.5", BGE_QUERY_INSTRUCTION),
        ("BAAI/bge-small-zh-v1.5", ""),
        ("sentence-transformers/all-MiniLM-L6-v2", ""),
    ],
)
def test_fastembed_embedder_query_prefix_depends_on_the_model(name, prefix):
    assert FastEmbedEmbedder(name).query_prefix == prefix


def test_fastembed_embedder_falls_back_to_embed_without_passage_and_query_variants():
    model = PlainFakeModel()
    embedder = fake_embedder(model, "some/other-model")
    half = 1 / math.sqrt(2)
    assert embedder.embed_documents(["a"]) == [pytest.approx([half, half, 0.0])]
    assert embedder.embed_query("q") == pytest.approx([half, half, 0.0])
    assert [call[0] for call in model.calls] == ["embed", "embed"]
    assert model.calls[1][1] == ["q"]


def test_fastembed_embedder_dim_comes_from_the_model():
    assert fake_embedder(FakeModel()).dim == 3


def test_fastembed_embedder_embeds_no_documents_without_loading_the_model():
    embedder = FastEmbedEmbedder()
    assert embedder.embed_documents([]) == []
    assert embedder._model is None


# ---- default_embedder ---------------------------------------------------------------------


def test_default_embedder_is_the_local_model(monkeypatch):
    monkeypatch.delenv("TCG_EMBEDDER", raising=False)
    embedder = default_embedder()
    assert isinstance(embedder, FastEmbedEmbedder)
    assert embedder._model is None


@pytest.mark.parametrize("value", ["hash", "HASH", " hash "])
def test_default_embedder_is_the_hash_one_when_the_env_says_so(monkeypatch, value):
    monkeypatch.setenv("TCG_EMBEDDER", value)
    assert isinstance(default_embedder(), HashEmbedder)


def test_default_embedder_name_overrides_the_env(monkeypatch):
    monkeypatch.setenv("TCG_EMBEDDER", "hash")
    assert isinstance(default_embedder("fastembed"), FastEmbedEmbedder)
    monkeypatch.setenv("TCG_EMBEDDER", "fastembed")
    assert isinstance(default_embedder("hash"), HashEmbedder)


def test_default_embedder_rejects_an_unknown_name(monkeypatch):
    monkeypatch.setenv("TCG_EMBEDDER", "hsah")
    with pytest.raises(ValueError, match="hsah"):
        default_embedder()


# ---- the real model (opt-in: TCG_TEST_FASTEMBED=1, needs the model in the local cache) ------


@pytest.mark.skipif(
    os.environ.get("TCG_TEST_FASTEMBED") != "1", reason="set TCG_TEST_FASTEMBED=1 to load the model"
)
def test_fastembed_embedder_with_the_real_model():
    embedder = FastEmbedEmbedder()
    assert embedder.dim == 384
    related, unrelated = embedder.embed_documents(
        [
            "public Owner findById(int id) { return ownerRepository.findById(id); }",
            "Running the app locally: ./mvnw spring-boot:run",
        ]
    )
    query = embedder.embed_query("find an owner by id")
    assert len(query) == len(related) == 384
    assert norm(query) == pytest.approx(1.0, abs=1e-5)
    assert norm(related) == pytest.approx(1.0, abs=1e-5)
    assert cosine(query, related) > cosine(query, unrelated)
