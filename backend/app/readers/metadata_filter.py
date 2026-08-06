"""
Metadata typing and subsetting, shared by every reader.

Two related problems live here because they are the same problem seen twice:

*   **Is this column categorical or continuous?** (issue #35) Seurat writes cluster
    IDs as integers, so dtype alone routes them to a viridis gradient when the user
    wants twenty distinct colours. The auto-rule below guesses, and an explicit
    caller override wins over the guess.

*   **Which units does the user want to see?** (issue #45) The same column, read the
    same way, also drives "show me only clusters 4 and 7" — a categorical allowlist
    or a numeric range that restricts what gets rendered.

Both are expressed against a single pandas column so cells and edges behave
identically; `base_reader` uses this for the cells table and `edge_reader` for the
edge table.
"""
from dataclasses import dataclass
from typing import Optional

import pandas as pd

# Above this many distinct integers, a column is assumed to be a measurement rather
# than a code. Cluster IDs, phenotype codes and bin assignments sit well below it;
# transcript counts sit well above. Users past the threshold reach for the explicit
# "treat as categorical" toggle, which is exactly what issue #35 asked for.
CATEGORICAL_MAX_UNIQUE = 30


def is_categorical(col: pd.Series, forced: Optional[bool] = None) -> bool:
    """Decide how a metadata column should be coloured.

    ``forced`` is the user's explicit choice from the color panel:
      * ``None``  — auto-detect (the historical behaviour)
      * ``True``  — treat as categorical whatever the dtype
      * ``False`` — treat as continuous, but only if the values are actually
        numeric; a text column has no gradient to draw, so the request is
        ignored rather than silently rendering every cell the same colour.
    """
    numeric = pd.api.types.is_numeric_dtype(col) and not pd.api.types.is_bool_dtype(col)
    if forced is True:
        return True
    if forced is False:
        return not numeric
    return (
        pd.api.types.is_string_dtype(col)
        or pd.api.types.is_object_dtype(col)
        or pd.api.types.is_bool_dtype(col)
        or isinstance(col.dtype, pd.CategoricalDtype)
        or (pd.api.types.is_integer_dtype(col) and col.nunique() <= CATEGORICAL_MAX_UNIQUE)
    )


def sort_categories(labels) -> list[str]:
    """Order category labels, numerically when they are all numbers.

    Labels reach the legend as strings, so a plain sort puts cluster 10 between 1
    and 2. Issue #35 asks for the numeric order to survive, and it costs one parse
    attempt: if every label is a number the sort key is that number, otherwise it
    falls back to the lexicographic order used before.
    """
    labels = [str(v) for v in labels]
    try:
        return sorted(labels, key=lambda s: (float(s), s))
    except (TypeError, ValueError):
        return sorted(labels)


@dataclass(frozen=True)
class MetadataFilter:
    """A restriction of the view to a subset of units (issue #45).

    Exactly one of the two forms is meaningful:

      * ``values`` — a categorical allowlist, compared against ``str(value)`` so it
        works regardless of whether the column arrived as int, float or text.
      * ``vmin`` / ``vmax`` — an inclusive numeric range; either end may be open.

    ``include_missing`` decides what happens to rows where the column is NaN. It
    defaults to False: a cell with no cluster call is not part of "cluster 4".
    """

    field: str
    values: Optional[tuple] = None
    vmin: Optional[float] = None
    vmax: Optional[float] = None
    include_missing: bool = False

    @classmethod
    def build(cls, field: Optional[str], values=None, vmin=None, vmax=None,
              include_missing: bool = False) -> Optional["MetadataFilter"]:
        """Construct from loose router input, or None when nothing is constrained.

        A field name with no values and no bounds is not a filter — it is a column
        the user has selected but not yet narrowed — so it returns None and the
        caller renders everything.
        """
        if not field:
            return None
        vals = tuple(str(v) for v in values) if values else None
        if vals is None and vmin is None and vmax is None:
            return None
        return cls(
            field=field,
            values=vals,
            vmin=None if vmin is None else float(vmin),
            vmax=None if vmax is None else float(vmax),
            include_missing=bool(include_missing),
        )

    def mask(self, col: pd.Series) -> pd.Series:
        """Boolean mask over ``col`` selecting the rows this filter keeps."""
        present = col.notna()
        if self.values is not None:
            keep = col.astype(str).isin(set(self.values)) & present
        else:
            numeric = pd.to_numeric(col, errors="coerce")
            keep = numeric.notna()
            if self.vmin is not None:
                keep &= numeric >= self.vmin
            if self.vmax is not None:
                keep &= numeric <= self.vmax
        if self.include_missing:
            keep = keep | ~present
        return keep
