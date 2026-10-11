"""The index folder: nodes.json, edges.json, manifest.json, and the chunks in LanceDB.

JSON files are written sorted, so an unchanged graph gives identical files, and atomically
(temp file + rename), so a reader such as a running MCP server never sees half a file.
Nothing is created until the first write: lancedb.connect() creates its folder, so reads
check that the folder exists before connecting. lancedb and pyarrow are imported on first use;
lancedb alone takes most of a second to import.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .models import SCHEMA_VERSION, Chunk, Edge, Graph, Manifest, Node, from_dict, to_dict

if TYPE_CHECKING:
    import pyarrow as pa
    from lancedb.db import DBConnection
    from lancedb.table import Table

TABLE = "chunks"
# Every Chunk field, in table order; the table adds "vector".
COLUMNS = ("id", "node_id", "kind", "path", "start_line", "end_line", "text", "embed_text",
           "content_hash")
_IDS_PER_FILTER = 1000  # ids per `id IN (...)` clause


class IndexStore:
    """One index folder. Reads before the first write find nothing and create nothing."""

    def __init__(self, index_dir: str | Path) -> None:
        self.index_dir = Path(index_dir)
        self.nodes_path = self.index_dir / "nodes.json"
        self.edges_path = self.index_dir / "edges.json"
        self.manifest_path = self.index_dir / "manifest.json"
        self.lancedb_path = self.index_dir / "lancedb"
        self._db: DBConnection | None = None

    # ---- the graph ------------------------------------------------------------------------

    def write_graph(self, graph: Graph, meta: dict[str, Any] | None = None) -> None:
        nodes = sorted(graph.nodes, key=lambda node: node.id)
        edges = sorted(graph.edges, key=_edge_key)
        _write_json(
            self.nodes_path,
            {"schema_version": SCHEMA_VERSION, "meta": meta or {}, "nodes": to_dict(nodes)},
        )
        _write_json(self.edges_path, {"schema_version": SCHEMA_VERSION, "edges": to_dict(edges)})

    def read_graph(self) -> Graph:
        nodes = self._read_index_file(self.nodes_path)["nodes"]
        edges = self._read_index_file(self.edges_path)["edges"]
        return Graph(
            nodes=[from_dict(Node, node) for node in nodes],
            edges=[from_dict(Edge, edge) for edge in edges],
        )

    def read_meta(self) -> dict[str, Any]:
        """The `meta` given to write_graph."""
        return dict(self._read_index_file(self.nodes_path).get("meta") or {})

    def _read_index_file(self, path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise FileNotFoundError(
                f"no index at {self.index_dir} ({path.name} is missing): run `tcg index <repo>`"
            )
        data = _read_json(path)
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(
                f"{path} has schema version {data.get('schema_version')}, this version of tcg "
                f"reads {SCHEMA_VERSION}: run `tcg index <repo> --full`"
            )
        return data

    # ---- the manifest ---------------------------------------------------------------------

    def write_manifest(self, manifest: Manifest) -> None:
        data = to_dict(manifest)
        for key in ("files", "chunks", "counts"):
            data[key] = dict(sorted(data[key].items()))
        _write_json(self.manifest_path, data)

    def read_manifest(self) -> Manifest | None:
        """None if there's none or it can't be read: the indexer then re-indexes everything,
        which rewrites it (raising would block that). schema_version is the caller's to check."""
        if not self.manifest_path.is_file():
            return None
        try:
            return from_dict(Manifest, _read_json(self.manifest_path))
        except (ValueError, TypeError, AttributeError):  # bad JSON, or not a manifest's shape
            return None

    # ---- the chunk table ------------------------------------------------------------------

    def upsert_chunks(self, chunks: list[Chunk], vectors: Sequence[Sequence[float]]) -> None:
        """Insert chunks, or replace those whose id is already stored. A repeated id in one call
        keeps its last chunk (lancedb would store both copies, or refuse the merge)."""
        if len(chunks) != len(vectors):
            raise ValueError(f"{len(chunks)} chunks but {len(vectors)} vectors")
        if not chunks:
            return
        rows = {chunk.id: (chunk, vector) for chunk, vector in zip(chunks, vectors)}
        lengths = sorted({len(vector) for _, vector in rows.values()})
        if len(lengths) != 1 or lengths[0] == 0:
            raise ValueError(f"vectors must all have the same non-zero length, got {lengths}")
        dim = lengths[0]
        data = _arrow_table(list(rows.values()), dim)
        table = self._table()
        if table is None:
            self._connect(create=True).create_table(TABLE, data=data, schema=data.schema)
            return
        _check_dim(table, dim)
        merge = table.merge_insert("id").when_matched_update_all().when_not_matched_insert_all()
        merge.execute(data)

    def delete_chunks(self, ids: Iterable[str]) -> None:
        unique = sorted(set(ids))
        table = self._table() if unique else None
        if table is None:
            return
        for start in range(0, len(unique), _IDS_PER_FILTER):
            table.delete(f"id IN ({_sql_list(unique[start : start + _IDS_PER_FILTER])})")

    def reset_chunks(self) -> None:
        """Drop the table, e.g. when the embedding model (and so the vectors) changed."""
        db = self._connect()
        if db is not None:
            db.drop_table(TABLE, ignore_missing=True)

    def chunk_hashes(self) -> dict[str, str]:
        """chunk id -> content_hash of everything stored: what plan_chunks compares against."""
        table = self._table()
        if table is None:
            return {}
        rows = table.search().select(["id", "content_hash"]).to_arrow()
        return dict(zip(rows.column("id").to_pylist(), rows.column("content_hash").to_pylist()))

    def get_chunks(self, ids: Iterable[str]) -> list[Chunk]:
        """The stored chunks with these ids, in the order asked; unknown ids are left out."""
        wanted = list(dict.fromkeys(ids))
        table = self._table() if wanted else None
        if table is None:
            return []
        found: dict[str, Chunk] = {}
        for start in range(0, len(wanted), _IDS_PER_FILTER):
            batch = wanted[start : start + _IDS_PER_FILTER]
            query = table.search().where(f"id IN ({_sql_list(batch)})").select(list(COLUMNS))
            for row in query.to_list():
                found[row["id"]] = _chunk(row)
        return [found[chunk_id] for chunk_id in wanted if chunk_id in found]

    def count_chunks(self) -> int:
        table = self._table()
        return 0 if table is None else table.count_rows()

    def search(
        self,
        vector: Sequence[float],
        limit: int = 10,
        kinds: list[str] | None = None,
        path_prefix: str | None = None,
    ) -> list[tuple[Chunk, float]]:
        """The `limit` chunks nearest to `vector` by cosine, as (chunk, similarity), best first.
        Filters run before the search, so a narrow filter still returns `limit` results.
        `kinds` None or empty means every kind; `path_prefix` is a literal prefix of the path."""
        table = self._table() if limit > 0 else None
        if table is None:
            return []
        _check_dim(table, len(vector))
        query = (
            table.search([float(x) for x in vector], vector_column_name="vector")
            .distance_type("cosine")
            .select([*COLUMNS, "_distance"])
            .limit(limit)
        )
        clauses = []
        if kinds:
            clauses.append(f"kind IN ({_sql_list(kinds)})")
        if path_prefix:
            clauses.append(f"starts_with(path, {_sql_str(path_prefix)})")
        if clauses:
            query = query.where(" AND ".join(clauses), prefilter=True)
        results = [(_chunk(row), 1.0 - float(row["_distance"])) for row in query.to_list()]
        results.sort(key=lambda result: (-result[1], result[0].id))
        return results

    def _connect(self, create: bool = False) -> DBConnection | None:
        """The connection, or None when only reading and there's no table folder yet."""
        if self._db is None:
            if not create and not self.lancedb_path.is_dir():
                return None
            import lancedb

            self._db = lancedb.connect(self.lancedb_path)
        return self._db

    def _table(self) -> Table | None:
        db = self._connect()
        if db is None or TABLE not in db:
            return None
        return db.open_table(TABLE)


# ---- helpers ------------------------------------------------------------------------------


def _edge_key(edge: Edge) -> tuple[str, str, str, str]:
    # attrs break ties (two CALLS edges on different lines), so the order never depends on input
    return edge.kind, edge.source, edge.target, json.dumps(edge.attrs, sort_keys=True, default=str)


def _write_json(path: Path, data: Any) -> None:
    """Write a temp file next to `path`, then rename it over `path` (atomic on one filesystem)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "x", encoding="utf-8") as f:  # open(), unlike mkstemp, honours the umask
            json.dump(data, f, indent=1, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:  # also UnicodeDecodeError
        raise ValueError(f"{path} is not valid JSON ({e}): run `tcg index <repo> --full`") from e
    if not isinstance(data, dict):
        raise ValueError(f"{path} doesn't hold a JSON object: run `tcg index <repo> --full`")
    return data


def _schema(dim: int) -> pa.Schema:
    import pyarrow as pa

    return pa.schema(
        [
            pa.field("id", pa.string(), nullable=False),
            pa.field("node_id", pa.string(), nullable=False),
            pa.field("kind", pa.string(), nullable=False),
            pa.field("path", pa.string()),
            pa.field("start_line", pa.int32()),
            pa.field("end_line", pa.int32()),
            pa.field("text", pa.string(), nullable=False),
            pa.field("embed_text", pa.string(), nullable=False),
            pa.field("content_hash", pa.string(), nullable=False),
            pa.field("vector", pa.list_(pa.float32(), dim), nullable=False),
        ]
    )


def _arrow_table(rows: list[tuple[Chunk, Sequence[float]]], dim: int) -> pa.Table:
    import numpy as np
    import pyarrow as pa

    schema = _schema(dim)
    columns = [
        pa.array([getattr(chunk, name) for chunk, _ in rows], type=schema.field(name).type)
        for name in COLUMNS
    ]
    flat = np.asarray([vector for _, vector in rows], dtype=np.float32).reshape(-1)
    columns.append(pa.FixedSizeListArray.from_arrays(pa.array(flat, type=pa.float32()), dim))
    return pa.Table.from_arrays(columns, schema=schema)


def _check_dim(table: Table, dim: int) -> None:
    stored = table.schema.field("vector").type.list_size
    if dim != stored:
        raise ValueError(
            f"the chunk table holds {stored}-dim vectors, not {dim}: the embedding model changed "
            "since the index was built; re-index with `tcg index <repo> --full` (reset_chunks)"
        )


def _chunk(row: dict[str, Any]) -> Chunk:
    return Chunk(**{name: row[name] for name in COLUMNS})


def _sql_str(value: str) -> str:
    """A SQL string literal: quotes doubled. Backslashes are literal in lancedb's SQL."""
    return "'" + value.replace("'", "''") + "'"


def _sql_list(values: Iterable[str]) -> str:
    return ", ".join(_sql_str(value) for value in values)
