"""Text embedders: a local model for real indexes and a hashing one for tests and offline use.

Every embedder returns plain lists of floats, L2-normalised, so cosine similarity is a dot
product and the store doesn't care which embedder made a vector, as long as the dims match.
This module imports only the standard library: fastembed (onnxruntime) takes about half a
second to import, so it's loaded on first use.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from collections import Counter
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"

# BGE English models were trained with this instruction on short queries (not on passages).
# fastembed's query_embed doesn't add it, so FastEmbedEmbedder does.
BGE_QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "


class Embedder(Protocol):
    """What the indexer and the context engine need from an embedding model.

    Declared as read-only properties so a plain attribute or a lazy property both satisfy it.
    """

    @property
    def model_name(self) -> str:  # recorded in the manifest: a new model means re-embedding
        ...

    @property
    def dim(self) -> int: ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


# ---- the local model ---------------------------------------------------------------------


def default_cache_dir() -> Path:
    """Where models are kept: $TCG_MODEL_CACHE, else ~/.cache/test-context-graph/models.

    fastembed's own default is the temp folder, which an MCP client's minimal environment
    doesn't share with the shell, so the model was downloaded again on every server start.
    """
    configured = os.environ.get("TCG_MODEL_CACHE")
    return Path(configured).expanduser() if configured else Path.home() / ".cache" / "test-context-graph" / "models"


class FastEmbedEmbedder:
    """A local ONNX model via fastembed; the model files are cached on disk after one download."""

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        cache_dir: str | Path | None = None,
        batch_size: int = 32,
    ) -> None:
        self.model_name = model_name
        self.cache_dir = Path(cache_dir) if cache_dir is not None else default_cache_dir()
        self.batch_size = batch_size
        self._model: Any = None
        self._dim: int | None = None

    @property
    def query_prefix(self) -> str:
        name = self.model_name.lower()
        return BGE_QUERY_INSTRUCTION if name.startswith("baai/bge-") and "-en" in name else ""

    @property
    def dim(self) -> int:
        """Loads the model, like the embed methods: the size is whatever the model gives."""
        if self._dim is None:
            model = self._load()
            try:
                self._dim = int(model.embedding_size)
            except (AttributeError, NotImplementedError, ValueError):
                self._dim = len(self.embed_query("dimension probe"))
        return self._dim

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._load()
        embed = getattr(model, "passage_embed", None) or model.embed
        return [_unit(vector) for vector in embed(list(texts), batch_size=self.batch_size)]

    def embed_query(self, text: str) -> list[float]:
        model = self._load()
        query = self.query_prefix + text
        query_embed = getattr(model, "query_embed", None)
        vectors: Iterable[Any] = query_embed(query) if query_embed else model.embed([query])
        return _unit(next(iter(vectors)))

    def _load(self) -> Any:
        if self._model is None:
            from fastembed import TextEmbedding

            cache = str(self.cache_dir)
            try:
                # Offline first: once the model is cached, loading it needs no network at all.
                self._model = TextEmbedding(model_name=self.model_name, cache_dir=cache, local_files_only=True)
            except Exception:  # fastembed raises various errors for "not cached yet"
                self._model = TextEmbedding(model_name=self.model_name, cache_dir=cache)
        return self._model


def _unit(vector: Iterable[float]) -> list[float]:
    """The vector scaled to length 1 (a zero vector is returned as is)."""
    values = [float(x) for x in vector]
    norm = math.sqrt(sum(x * x for x in values))
    return [x / norm for x in values] if norm > 0.0 else values


# ---- the hashing embedder ----------------------------------------------------------------

_WORD = re.compile(r"[^\W_]+")  # runs of letters and digits: '_', punctuation and spaces split
_PART = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")  # camelCase, ACRONYMWord, digits
_EMPTY_TOKEN = "\x00empty"  # what a text without words hashes to, so its vector isn't zero


def tokenize(text: str) -> list[str]:
    """Lower-case word parts, with identifiers split: "getOwnerById" -> get, owner, by, id,
    plus the whole identifier ("getownerbyid") so an exact identifier match counts extra."""
    tokens: list[str] = []
    for word in _WORD.findall(text):
        parts = _PART.findall(word) if word.isascii() else [word]
        tokens.extend(part.lower() for part in parts)
        if len(parts) > 1:
            tokens.append(word.lower())
    return tokens


@lru_cache(maxsize=65536)
def _bucket(token: str, dim: int) -> tuple[int, float]:
    """A stable (index, sign) for a token: blake2b, unlike hash(), is the same in every process."""
    value = int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "little")
    return value % dim, (1.0 if value >> 63 == 0 else -1.0)


class HashEmbedder:
    """Deterministic bag-of-words vectors from hashed tokens (the "hashing trick"). No model and
    no network: for tests, and a crude fallback where the model can't be downloaded."""

    def __init__(self, dim: int = 256) -> None:
        if dim < 1:
            raise ValueError(f"dim must be positive, got {dim}")
        self.dim = dim
        self.model_name = f"hash-{dim}"

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    def _embed(self, text: str) -> list[float]:
        counts = Counter(tokenize(text)) or Counter({_EMPTY_TOKEN: 1})
        vector = [0.0] * self.dim
        for token, count in counts.items():
            index, sign = _bucket(token, self.dim)
            vector[index] += sign * (1.0 + math.log(count))  # sublinear: repeats count less
        unit = _unit(vector)
        # Tokens sharing a bucket with opposite signs can cancel out. Never return a zero vector
        # (cosine is undefined for it); a single token can't cancel, so this recursion ends.
        return unit if any(unit) else self._embed("")


# ---- choosing one ------------------------------------------------------------------------


def default_embedder(name: str | None = None) -> Embedder:
    """`name` ("fastembed" or "hash"), else env TCG_EMBEDDER, else the local model."""
    choice = (name or os.environ.get("TCG_EMBEDDER") or "fastembed").strip().lower()
    if choice == "hash":
        return HashEmbedder()
    if choice == "fastembed":
        return FastEmbedEmbedder()
    raise ValueError(f"unknown embedder {choice!r}: use 'fastembed' or 'hash'")
