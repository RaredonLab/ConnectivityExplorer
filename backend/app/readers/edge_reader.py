"""
Reads edge-list Parquet files.

Required columns: x1, y1, x2, y2  (native coordinate space, e.g. µm)
All other columns are open metadata (layer type, strength, p-value, etc.)

Coordinates are returned in image pixel space (divided by pixel_size).
BBox query parameters are also expected in image pixel space.

Uses DuckDB for all parquet queries so large files (1+ GB) are processed in
streaming chunks without loading the full dataset into RAM.
"""
import math
import os
from pathlib import Path
from typing import Optional
import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from app.readers import duck, metadata_filter, supplemental
from app.readers.metadata_filter import MetadataFilter



_UNSET = object()


# The pipeline, in one place:
#
#     all edges in viewport
#       → density filter          (spatially random, this predicate)
#       → EDGESET A               → tissue-graph layer
#       → sending filter
#       → receiving filter
#       → edge-table filters
#       → EDGESET B               → edge-data layer
#
# So B ⊆ A always: edge data can never be drawn where the graph beneath it has
# been sampled away.
#
# The two layers are fetched by separate queries, and the order above is only
# guaranteed because this predicate is *deterministic per edge*. A given edge
# gets the same verdict in every query regardless of what else ran, so the
# predicate commutes with the filters and "density then filter" and "filter then
# density" select the identical set.
#
# `USING SAMPLE` cannot do this. Two independent bernoulli draws at 10% give the
# graph one random tenth and the edge layer a different one — an overlap of ~1%,
# with edge data floating free of the structure it is supposed to sit on.
#
# A deterministic hash of the edge identity fixes that. The same edge gets the
# same verdict in every query, whatever filters ran, so the edge layer is always
# a subset of the graph:
#
#     graph      = { e : keep(e) }
#     edge data  = { e : passes_filters(e) and keep(e) }  ⊆ graph
#
# It is also spatially uniform (the hash ignores position) and stable across
# re-fetches, which matters because the alternative flickers on every pan.
#
# Unlike USING SAMPLE this is an ordinary predicate, so it composes with the
# filters by AND and cannot be reordered below them — the property the
# subquery-wrapping was there to guarantee.
_DENSITY_MODULUS = 1_000_000


def density_predicate(density: float, key_sql: str = '"edge"') -> str:
    """SQL keeping a deterministic, spatially uniform fraction of edges."""
    if density is None or density >= 1.0:
        return ""
    cut = max(1, int(density * _DENSITY_MODULUS))
    return f"(hash(CAST({key_sql} AS VARCHAR)) % {_DENSITY_MODULUS}) < {cut}"


class EdgeReader:
    def __init__(self, path: Path, pixel_size: float = 1.0):
        self.path = path
        # SQL-safe path string (escape single quotes)
        self._sql_path = str(path).replace("'", "''")
        self._schema_cache = None
        self._lrm_catalogue_cache = None
        self._pixel_size = pixel_size
        self._supp_cache = _UNSET

    # ── Supplemental edge metadata ────────────────────────────────────────────
    #
    # Mirrors the cell-metadata/ convention exactly, one level up: the user drops
    # CSV/parquet keyed on `edge` into an edge-metadata/ folder beside the dataset,
    # and the columns appear in the edge color-by dropdown without regenerating
    # edges.parquet from R.
    #
    # The folder lives next to the *dataset*, not next to the edge file, so a single
    # set of annotations applies across every edge source in a dataset (see the
    # multiple-edge-files feature). Annotations describe cell pairs, which are a
    # property of the tissue rather than of one scoring run.

    @property
    def _dataset_dir(self) -> Path:
        # self.path is either <dataset>/edges.parquet or <dataset>/edges/<name>.parquet.
        parent = self.path.parent
        return parent.parent if parent.name == "edges" else parent

    def _supplemental(self):
        """User edge annotations keyed on `edge`, or None. Cached per instance."""
        if self._supp_cache is not _UNSET:
            return self._supp_cache
        files = supplemental.collect_files(self._dataset_dir / "edge-metadata")
        self._supp_cache = supplemental.load_supplemental(
            files, key="edge", log_prefix="edge_reader"
        )
        return self._supp_cache

    def _supplemental_columns(self) -> dict:
        """{column: dtype_str} for supplemental columns, excluding the join key."""
        df = self._supplemental()
        if df is None:
            return {}
        return {c: str(df[c].dtype) for c in df.columns if c != "edge"}

    @property
    def pixel_size(self) -> float:
        return self._pixel_size

    def _parquet_schema(self):
        if self._schema_cache is None:
            self._schema_cache = pq.read_schema(self.path)
        return self._schema_cache

    def _from(self) -> str:
        return f"read_parquet('{self._sql_path}')"

    def _conn(self):
        # Shared with the spatial readers via duck.connect(), which also gives a
        # fresh isolated connection per call (duckdb's default connection is not
        # thread-safe and returns empty or corrupt results under FastAPI's
        # threadpool rather than raising).
        #
        # This used to build its own connection with
        # `os.getenv("DUCKDB_MEMORY_LIMIT", "8GB")`. That default only applies
        # when the variable is *absent*: set-but-empty returns "", so
        # `SET memory_limit=''` raised a ParserException and every /edges
        # endpoint 500'd. Compose sets it empty on purpose now, to let the limit
        # be sized from available memory — so the two disagreed and edges stopped
        # loading everywhere. Going through duck.connect() means there is one
        # defaulting rule instead of two, and EdgeReader also picks up the
        # temp_directory it never had, so a large edge query can spill instead of
        # failing outright.
        return duck.connect()

    def schema(self) -> dict:
        """Column names and dtypes, parquet columns plus any supplemental ones.

        The frontend builds the edge color-by dropdown straight from this, so
        including supplemental columns here is all it takes for them to appear —
        no frontend change required.
        """
        schema = self._parquet_schema()
        columns = {name: str(schema.field(name).type) for name in schema.names}
        # Parquet wins a name collision: it is the authoritative source, and a
        # supplemental column shadowing a real one would be confusing to debug.
        for col, dtype in self._supplemental_columns().items():
            columns.setdefault(col, dtype)
        return {"columns": columns}

    def lrm_catalogue(self) -> list[dict]:
        """Return unique LRM rows sorted by lrm_id. Includes string 'lrm' field if present."""
        if self._lrm_catalogue_cache is not None:
            return self._lrm_catalogue_cache
        want = [c for c in ("lrm_id", "lrm", "ligand", "receptor")
                if c in self._parquet_schema().names]
        if not want:
            self._lrm_catalogue_cache = []
            return []
        cols = ", ".join(f'"{c}"' for c in want)
        order = "ORDER BY lrm_id" if "lrm_id" in want else ""
        with self._conn() as conn:
            df = conn.execute(
                f"SELECT DISTINCT {cols} FROM {self._from()} WHERE lrm IS NOT NULL {order}"
                if "lrm" in want else
                f"SELECT DISTINCT {cols} FROM {self._from()} WHERE ligand IS NOT NULL AND receptor IS NOT NULL {order}"
            ).df()
        if "lrm" not in df.columns and "ligand" in df.columns and "receptor" in df.columns:
            df["lrm"] = df["ligand"] + "|" + df["receptor"]
        df = df.dropna(subset=["lrm"] if "lrm" in df.columns else [])
        self._lrm_catalogue_cache = df.to_dict(orient="records")
        return self._lrm_catalogue_cache

    def edge_color_values(self, mode: str, lrms: list[str] | None = None,
                          field: str | None = None,
                          categorical: bool | None = None) -> dict:
        """
        Return per-directed-edge color values.

        mode='lrm_set'  — sum score across requested LRMs per edge; continuous
        mode='metadata' — group by edge, take first value of `field` per edge;
                          categorical vs continuous auto-detected unless the caller
                          overrides it with `categorical` (issue #35)
        """
        col_names = set(self._parquet_schema().names)

        if mode == "lrm_set":
            if not all(c in col_names for c in ("edge", "lrm", "score")):
                return {"type": "continuous", "values": {}, "min": 0, "max": 0}
            with self._conn() as conn:
                if lrms:
                    placeholders = ", ".join(["?" for _ in lrms])
                    sql = (f"SELECT edge, SUM(score) AS total FROM {self._from()} "
                           f"WHERE lrm IN ({placeholders}) GROUP BY edge")
                    df = conn.execute(sql, lrms).df()
                else:
                    df = conn.execute(
                        f"SELECT edge, SUM(score) AS total FROM {self._from()} GROUP BY edge"
                    ).df()
            if df.empty:
                return {"type": "continuous", "values": {}, "min": 0, "max": 0}
            grouped = df.set_index("edge")["total"]
            return {
                "type": "continuous",
                "values": grouped.to_dict(),
                "min": float(grouped.min()),
                "max": float(grouped.max()),
            }

        if mode == "metadata":
            if not field:
                return {"type": "continuous", "values": {}, "min": 0, "max": 0}

            if field in col_names:
                with self._conn() as conn:
                    df = conn.execute(
                        f'SELECT edge, FIRST("{field}") AS val FROM {self._from()} GROUP BY edge'
                    ).df()
                col = df.set_index("edge")["val"]
            else:
                # Supplemental column from edge-metadata/. Already one row per edge,
                # so there is nothing to aggregate.
                supp = self._supplemental()
                if supp is None or field not in supp.columns:
                    return {"type": "continuous", "values": {}, "min": 0, "max": 0}
                col = supp.set_index("edge")[field].dropna()
                if col.empty:
                    return {"type": "continuous", "values": {}, "min": 0, "max": 0}
            # Same typing rule as the cell side, from the shared module, so the
            # two color panels can never disagree about what is categorical.
            if metadata_filter.is_categorical(col, categorical):
                labels = col.dropna().astype(str)
                return {
                    "type": "categorical",
                    "values": labels.to_dict(),
                    "categories": metadata_filter.sort_categories(labels.unique()),
                }
            numeric = pd.to_numeric(col, errors="coerce").dropna()
            if numeric.empty:
                return {"type": "continuous", "values": {}, "min": 0, "max": 0}
            return {
                "type": "continuous",
                "values": numeric.to_dict(),
                "min": float(numeric.min()),
                "max": float(numeric.max()),
            }

        return {"type": "continuous", "values": {}, "min": 0, "max": 0}

    # ── Metadata filtering (issue #45) ────────────────────────────────────────

    def edge_filter_sql(self, spec: Optional[MetadataFilter], conn) -> tuple[str, list]:
        """WHERE fragment restricting the query to edges matching `spec`.

        Two paths, because edge metadata has two sources:

        * A column in ``edges.parquet`` becomes an ordinary SQL predicate, which
          DuckDB can push down and use for row-group pruning.
        * A column from ``edge-metadata/`` only exists in pandas, so the matching
          edge ids are resolved there and registered as a relation to semi-join
          against — the same trick the cell filter uses, and for the same reason:
          the id list is far too long to bind as parameters.

        Raises ValueError for an unknown column rather than quietly returning the
        unfiltered view, which would look like the filter had failed.
        """
        if spec is None:
            return "", []
        schema = self._parquet_schema()
        parquet_cols = set(schema.names)
        if spec.field in parquet_cols:
            quoted = f'"{spec.field}"'
            if spec.values is not None:
                # Compare as text so one code path covers int, float and string
                # columns. Booleans need lowering: the categories the panel offers
                # come from pandas, which writes "True", while DuckDB's cast writes
                # "true", so a literal comparison would never match.
                cast = f"CAST({quoted} AS VARCHAR)"
                vals = list(spec.values)
                if pa.types.is_boolean(schema.field(spec.field).type):
                    cast = f"lower({cast})"
                    vals = [v.lower() for v in vals]
                ph = ", ".join("?" for _ in vals)
                sql = f"{cast} IN ({ph})"
                params = vals
            else:
                parts, params = [], []
                if spec.vmin is not None:
                    parts.append(f"{quoted} >= ?"); params.append(spec.vmin)
                if spec.vmax is not None:
                    parts.append(f"{quoted} <= ?"); params.append(spec.vmax)
                sql = " AND ".join(parts) if parts else ""
            if spec.include_missing and sql:
                sql = f"({sql} OR {quoted} IS NULL)"
            return sql, params

        supp = self._supplemental()
        if supp is None or spec.field not in supp.columns:
            raise ValueError(f"unknown edge metadata column '{spec.field}'")
        keep = supp.loc[spec.mask(supp[spec.field]), "edge"].astype(str)
        if keep.empty:
            return "FALSE", []
        pred = duck.register_ids(conn, keep.tolist(), name="tp_edge_filter", col="edge")
        return f'CAST("edge" AS VARCHAR) {pred}', []

    @staticmethod
    def endpoint_filter_sql(
        sending_ids: Optional[set],
        receiving_ids: Optional[set],
        conn,
    ) -> tuple[str, list]:
        """WHERE fragment constraining each endpoint of an edge independently.

        Either side may be None, which leaves that end unconstrained — so setting
        only `sending_ids` answers "everything sent *from* these cells, to
        anywhere". With both set the result is the intersection: an edge is kept
        when its sender is in one set and its receiver in the other.

        This replaces a single set applied to both ends. That older rule tied edge
        visibility to the *cell* filter, which the lab wants independent: filtering
        cells is one action, filtering edges another, and an edge may now terminate
        on a cell that is not drawn. See docs/edge_filter_independence.md.

        An empty set means "nothing matches" and short-circuits to FALSE. It must
        not fall through to no-predicate, or a filter matching no cells would
        return every edge — and `register_ids` refuses an empty frame anyway.

        Both predicates are ordinary semi-joins in the WHERE clause, so they run
        before the GROUP BY and before the density sample.
        """
        conds: list[str] = []
        for ids, col, name in (
            (sending_ids,   "sending_cell",   "tp_send_filter"),
            (receiving_ids, "receiving_cell", "tp_recv_filter"),
        ):
            if ids is None:
                continue
            if not ids:
                return "FALSE", []
            pred = duck.register_ids(conn, ids, name=name)
            conds.append(f'CAST("{col}" AS VARCHAR) {pred}')
        return (" AND ".join(conds), []) if conds else ("", [])

    def edge_detail(self, edge_id: str) -> dict | None:
        """Return all LRM rows for a single directed edge, structured for the info panel."""
        with self._conn() as conn:
            df = conn.execute(
                f"SELECT * FROM {self._from()} WHERE edge = ?", [edge_id]
            ).df()
        if df.empty:
            return None
        first = df.iloc[0]
        lrm_rows = []
        for _, r in df.iterrows():
            entry: dict = {}
            for c in ("lrm", "lrm_id", "ligand", "receptor", "score", "score_norm"):
                if c in r.index:
                    v = r[c]
                    entry[c] = None if (isinstance(v, float) and not math.isfinite(v)) else v
            lrm_rows.append(entry)
        lrm_rows.sort(key=lambda x: x.get("score") or 0, reverse=True)
        result: dict = {"edge": edge_id, "lrms": lrm_rows}
        for c in ("sending_cell", "receiving_cell", "sending_type", "receiving_type"):
            if c in first.index:
                result[c] = first[c]
        if "is_autocrine" in first.index:
            result["is_autocrine"] = bool(first["is_autocrine"])

        # Attach user annotations under their own key so the info panel can present
        # them separately from the platform's own fields.
        supp = self._supplemental()
        if supp is not None:
            row = supp[supp["edge"].astype(str) == str(edge_id)]
            if not row.empty:
                extras = {}
                for c, v in row.iloc[0].items():
                    if c == "edge" or pd.isna(v):
                        continue
                    extras[c] = (None if isinstance(v, float) and not math.isfinite(v)
                                 else (v.item() if hasattr(v, "item") else v))
                if extras:
                    result["metadata"] = extras
        return result

    def column_summary(self, column: str) -> dict:
        col_names = set(self._parquet_schema().names)
        if column not in col_names:
            return {"type": "categorical", "values": [], "count": 0}
        quoted = f'"{column}"'
        try:
            with self._conn() as conn:
                row = conn.execute(
                    f"SELECT MIN({quoted}), MAX({quoted}), AVG({quoted}) "
                    f"FROM {self._from()}"
                ).fetchone()
            vmin, vmax, vmean = row
            if isinstance(vmin, (int, float)) and not isinstance(vmin, bool):
                return {
                    "type": "numeric",
                    "min": float(vmin),
                    "max": float(vmax),
                    "mean": float(vmean),
                }
        except Exception:
            pass
        with self._conn() as conn:
            vals = conn.execute(
                f"SELECT DISTINCT {quoted} FROM {self._from()} WHERE {quoted} IS NOT NULL"
            ).df().iloc[:, 0].tolist()
        return {"type": "categorical", "values": vals, "count": len(vals)}

    def query_structure(
        self,
        bbox: Optional[tuple] = None,
        density: float = 1.0,
        max_limit: int = 500_000,
    ) -> list[dict]:
        """Every distinct edge in the viewport, with no filters of any kind.

        This backs the tissue-graph layer, which is *ground truth*: the total set
        of edges, shown or hidden, never subset. It deliberately takes no filter
        arguments at all — not "filters default to none", but no way to pass one,
        so the layer cannot be narrowed by a future caller wiring one through.

        It is a separate query from `query_grouped`, not a flag on it, because the
        two want opposite orderings of the same pipeline. The edge-data layer must
        filter *then* sample, so a rare subset draws at full density; the graph
        must not filter at all. Returning every edge with a `passes_filter` column
        would sample the rare subset away before the flag was ever read.

        The projection is deliberately lean — `edge` and the four coordinates.
        Scores, types and LRM counts are most of `query_grouped`'s payload and the
        structural layer draws none of them; `edge` is kept only because the layer
        is pickable and the info panel resolves by id.
        """
        cols = set(self._parquet_schema().names)
        if not {"x1", "y1", "x2", "y2"} <= cols:
            return []
        ps = self.pixel_size

        where, params = "", []
        if bbox and None not in bbox:
            xmin, ymin, xmax, ymax = (v * ps for v in bbox)
            where = ("WHERE ((x1 >= ? AND x1 <= ? AND y1 >= ? AND y1 <= ?) OR "
                     "(x2 >= ? AND x2 <= ? AND y2 >= ? AND y2 <= ?))")
            params = [xmin, xmax, ymin, ymax, xmin, xmax, ymin, ymax]

        has_edge = "edge" in cols
        key = "edge" if has_edge else "x1, y1, x2, y2"
        sel = ("edge, " if has_edge else "") + \
              "FIRST(x1) AS x1, FIRST(y1) AS y1, FIRST(x2) AS x2, FIRST(y2) AS y2"
        # The *same* predicate query_grouped uses, on the same key, so the two
        # layers select an identical subset and edge data is never drawn where
        # the graph beneath it has been sampled away.
        dens = density_predicate(
            density, '"edge"' if has_edge else "concat_ws('|',x1,y1,x2,y2)")
        sample = f"WHERE {dens}" if dens else ""

        with self._conn() as conn:
            df = conn.execute(f"""
                SELECT * FROM (
                    SELECT {sel} FROM {self._from()} {where} GROUP BY {key}
                ) {sample}
                LIMIT {max_limit}
            """, params).df()

        for c in ("x1", "y1", "x2", "y2"):
            df[c] = df[c] / ps
        return df.to_dict("records")

    def query_grouped(
        self,
        bbox: Optional[tuple] = None,
        min_lrm_count: int = 1,
        density: float = 1.0,
        max_limit: int = 500_000,
        sending_ids: Optional[set] = None,
        receiving_ids: Optional[set] = None,
        edge_filters: Optional[list] = None,
    ) -> list[dict]:
        """
        Return one row per directed edge (GROUP BY edge), pre-aggregated.
        ~500x fewer rows than query() for typical LRM-rich parquet files.

        Returns structural columns (positions, metadata, lrm_count) plus
        score_sum = SUM(score) over ALL LRMs — the unfiltered total used as
        the default when no LRM filter is active.

        LRM-filter-aware scores (visible_lrm_count / visible_score_sum) are
        served separately by query_scores() so that LRM selection changes do
        not require re-fetching the heavy structural data.

        density=1.0 returns all edges in the viewport (up to max_limit).
        density<1.0 uses bernoulli sampling so each edge is independently
        included with probability `density` — spatially uniform.

        `sending_ids` / `receiving_ids` constrain the two endpoints independently
        (issue #59); `edge_filters` is a list of MetadataFilter and-ed together,
        which is the composition #45 deferred. All of them go into the WHERE
        clause, so they run before the GROUP BY and before the density sample:
        narrowing to a rare subset keeps it at full density rather than sampling
        it away. Density is last, and deliberately so — it is a rendering-volume
        control, not a selection criterion.
        """
        ps = self.pixel_size
        schema_names = set(self._parquet_schema().names)
        has_lrm   = "lrm"   in schema_names
        has_score = "score" in schema_names

        where_conditions: list[str] = []
        where_params: list = []

        if bbox:
            xmin, ymin, xmax, ymax = bbox
            if None not in (xmin, ymin, xmax, ymax):
                xmin_u, ymin_u = xmin * ps, ymin * ps
                xmax_u, ymax_u = xmax * ps, ymax * ps
                where_conditions.append(
                    "((x1 >= ? AND x1 <= ? AND y1 >= ? AND y1 <= ?) OR "
                    "(x2 >= ? AND x2 <= ? AND y2 >= ? AND y2 <= ?))"
                )
                where_params.extend([xmin_u, xmax_u, ymin_u, ymax_u,
                                      xmin_u, xmax_u, ymin_u, ymax_u])

        # Build SELECT columns
        agg_cols = ["edge"]
        for col in ("sending_cell", "receiving_cell", "is_autocrine",
                    "sending_type", "receiving_type"):
            if col in schema_names:
                agg_cols.append(f'FIRST("{col}") AS "{col}"')
        for coord in ("x1", "y1", "x2", "y2"):
            if coord in schema_names:
                agg_cols.append(f'FIRST("{coord}") AS "{coord}"')

        # lrm_count  = total LRMs for this edge (tissue-graph structural layer)
        # score_sum  = SUM(all scores) — default visible_score_sum when no LRM filter
        if has_lrm:
            agg_cols.append("COUNT(*) AS lrm_count")
        else:
            agg_cols.append("1 AS lrm_count")
        if has_score:
            agg_cols.append("SUM(score) AS score_sum")

        select = ", ".join(agg_cols)

        # Deterministic, shared with query_structure so the edge layer is always a
        # subset of the tissue graph at the same density. See density_predicate.
        dens = density_predicate(density)
        sample_clause = f"WHERE {dens}" if dens else ""

        # The connection is opened before the WHERE clause is finalised because the
        # metadata filters may need to register a relation on it to semi-join
        # against. Filter conditions are appended after the bbox so the parameter
        # order still matches the order the placeholders appear in the SQL text —
        # DuckDB binds positionally by text order, not by clause.
        # An edge file without endpoint columns cannot be filtered by cell; the
        # tissue graph still draws, it just ignores the cell subset.
        if not {"sending_cell", "receiving_cell"} <= schema_names:
            sending_ids = receiving_ids = None

        with self._conn() as conn:
            fragments = [self.endpoint_filter_sql(sending_ids, receiving_ids, conn)]
            fragments += [self.edge_filter_sql(f, conn) for f in (edge_filters or [])]
            for cond, prm in fragments:
                if cond:
                    where_conditions.append(cond)
                    where_params.extend(prm)
            where = f"WHERE {' AND '.join(where_conditions)}" if where_conditions else ""

            sql = f"""
                SELECT * FROM (
                    SELECT {select}
                    FROM {self._from()}
                    {where}
                    GROUP BY edge
                    HAVING lrm_count >= 1
                ) {sample_clause}
                LIMIT {max_limit}
            """
            df = conn.execute(sql, where_params).df()

        for col in ("x1", "y1", "x2", "y2"):
            if col in df.columns:
                df[col] = df[col] / ps

        if "is_autocrine" in df.columns:
            df["is_autocrine"] = df["is_autocrine"].astype(bool)

        return [
            {c: (None if isinstance(v, float) and not math.isfinite(v) else v)
             for c, v in row.items()}
            for row in df.to_dict(orient="records")
        ]

    def query_scores(
        self,
        bbox: Optional[tuple] = None,
        included_lrms: Optional[list] = None,
        excluded_lrms: Optional[list] = None,
        max_limit: int = 500_000,
    ) -> list[dict]:
        """
        Return per-edge LRM visibility scores: visible_lrm_count + visible_score_sum.
        Much lighter than query_grouped — no coordinates or metadata columns.

        Two query strategies, chosen by the caller based on set sizes:

        included_lrms (preferred when visible set is small):
            WHERE lrm IN (included_lrms) — DuckDB only reads matching rows,
            giving roughly a (visible / total) fraction of the scan cost.
            Edges absent from the result have visible_lrm_count = 0.

        excluded_lrms (preferred when excluded set is small):
            CASE WHEN lrm NOT IN (excluded_lrms) — full scan with per-row mask.
            All bbox edges appear in the result.

        The frontend picks whichever produces the smaller IN-list.
        No density sampling — returns scores for all bbox edges so the
        density-sampled structural edges always find their matching scores.
        """
        ps = self.pixel_size
        schema_names = set(self._parquet_schema().names)
        has_lrm   = "lrm"   in schema_names
        has_score = "score" in schema_names

        if not has_lrm:
            return []

        where_conditions: list[str] = []
        where_params: list = []

        if bbox:
            xmin, ymin, xmax, ymax = bbox
            if None not in (xmin, ymin, xmax, ymax):
                xmin_u, ymin_u = xmin * ps, ymin * ps
                xmax_u, ymax_u = xmax * ps, ymax * ps
                where_conditions.append(
                    "((x1 >= ? AND x1 <= ? AND y1 >= ? AND y1 <= ?) OR "
                    "(x2 >= ? AND x2 <= ? AND y2 >= ? AND y2 <= ?))"
                )
                where_params.extend([xmin_u, xmax_u, ymin_u, ymax_u,
                                      xmin_u, xmax_u, ymin_u, ymax_u])

        where = f"WHERE {' AND '.join(where_conditions)}" if where_conditions else ""

        if included_lrms is not None:
            # Fast path: filter to visible rows only, then aggregate.
            # Edges with 0 visible LRMs simply don't appear — the frontend
            # treats missing entries as visible_lrm_count = 0.
            ph = ", ".join("?" for _ in included_lrms)
            lrm_filter = f" AND lrm IN ({ph})" if included_lrms else " AND FALSE"
            full_where = (
                f"WHERE {' AND '.join(where_conditions)}{lrm_filter}"
                if where_conditions
                else f"WHERE lrm IN ({ph})" if included_lrms else "WHERE FALSE"
            )
            score_col = "SUM(score) AS visible_score_sum" if has_score else "0 AS visible_score_sum"
            sql = f"""
                SELECT edge,
                       COUNT(*) AS visible_lrm_count,
                       {score_col}
                FROM {self._from()}
                {full_where}
                GROUP BY edge
                LIMIT {max_limit}
            """
            params = where_params + list(included_lrms)

        else:
            # Standard path: full scan with CASE WHEN exclusion mask.
            excl = excluded_lrms or []
            ph = ", ".join("?" for _ in excl)
            vis_count = (
                f"SUM(CASE WHEN lrm NOT IN ({ph}) THEN 1 ELSE 0 END)"
                if excl else "COUNT(*)"
            )
            vis_score = ""
            if has_score:
                vis_score = (
                    f", SUM(CASE WHEN lrm NOT IN ({ph}) THEN score ELSE 0 END) AS visible_score_sum"
                    if excl else ", SUM(score) AS visible_score_sum"
                )
            # CASE WHEN placeholders come before WHERE placeholders in bind order
            excl_params = list(excl) * (2 if (excl and has_score) else (1 if excl else 0))
            sql = f"""
                SELECT edge,
                       {vis_count} AS visible_lrm_count
                       {vis_score}
                FROM {self._from()}
                {where}
                GROUP BY edge
                LIMIT {max_limit}
            """
            params = excl_params + where_params

        with self._conn() as conn:
            df = conn.execute(sql, params).df()

        if df.empty:
            return []

        return [
            {c: (None if isinstance(v, float) and not math.isfinite(v) else v)
             for c, v in row.items()}
            for row in df.to_dict(orient="records")
        ]

    def query(
        self,
        bbox: Optional[tuple] = None,
        filters: Optional[dict] = None,
        min_strength: Optional[float] = None,
        limit: int = 200_000,
    ) -> list[dict]:
        ps = self.pixel_size
        schema_names = set(self._parquet_schema().names)

        conditions: list[str] = []
        params: list = []

        if bbox:
            xmin, ymin, xmax, ymax = bbox
            if None not in (xmin, ymin, xmax, ymax):
                xmin_u, ymin_u = xmin * ps, ymin * ps
                xmax_u, ymax_u = xmax * ps, ymax * ps
                conditions.append(
                    "((x1 >= ? AND x1 <= ? AND y1 >= ? AND y1 <= ?) OR "
                    "(x2 >= ? AND x2 <= ? AND y2 >= ? AND y2 <= ?))"
                )
                params.extend([xmin_u, xmax_u, ymin_u, ymax_u,
                                xmin_u, xmax_u, ymin_u, ymax_u])

        if filters:
            for col, val in filters.items():
                if col not in schema_names:
                    continue
                if isinstance(val, list):
                    placeholders = ", ".join(["?" for _ in val])
                    conditions.append(f'"{col}" IN ({placeholders})')
                    params.extend(val)
                else:
                    conditions.append(f'"{col}" = ?')
                    params.append(val)

        if min_strength is not None and "strength" in schema_names:
            conditions.append("strength >= ?")
            params.append(min_strength)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        can_stratify = "sending_cell" in schema_names

        with self._conn() as conn:
            if can_stratify:
                # Two-stage cell-stratified sampling:
                #   1. Reservoir-sample the bbox-filtered rows down to PRE_LIMIT (fast,
                #      single pass over parquet; USING SAMPLE must go on the inner subquery
                #      so the WHERE filter runs first).
                #   2. Apply per-cell ROW_NUMBER window on the small pre-sample, keeping
                #      at most K edges per cell, then shuffle and cap at limit.
                # This distributes the budget evenly across all visible cells rather than
                # over-representing high-degree hub cells that appear first in the file.
                PRE_LIMIT = min(limit * 5, 500_000)
                K = max(1, limit // 50)  # per-cell cap (assumes ≥50 cells in view)
                df = conn.execute(
                    f"""
                    SELECT * EXCLUDE (_rn) FROM (
                        SELECT *,
                            ROW_NUMBER() OVER (
                                PARTITION BY sending_cell ORDER BY RANDOM()
                            ) AS _rn
                        FROM (
                            SELECT * FROM (
                                SELECT * FROM {self._from()} {where}
                            ) USING SAMPLE reservoir({PRE_LIMIT} ROWS)
                        )
                    ) WHERE _rn <= {K}
                    ORDER BY RANDOM()
                    LIMIT {limit}
                    """,
                    params,
                ).df()
            else:
                df = conn.execute(
                    f"SELECT * FROM {self._from()} {where} LIMIT {limit}", params
                ).df()

        for col in ("x1", "y1", "x2", "y2"):
            if col in df.columns:
                df[col] = df[col] / ps

        return [
            {c: (None if isinstance(v, float) and not math.isfinite(v) else v)
             for c, v in row.items()}
            for row in df.to_dict(orient="records")
        ]
