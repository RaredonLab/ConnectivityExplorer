/**
 * useEdges — fetch directed edge data from the backend.
 *
 * Splits work into two independent effects:
 *
 *   Structural effect  (deps: viewport, density, enabled, minStrength)
 *     POSTs to /query-grouped — heavy fetch that returns edge positions,
 *     metadata, lrm_count, and score_sum (unfiltered total score).
 *     Only re-runs when the viewport or density changes, NOT on LRM filter changes.
 *
 *   Score effect  (deps: viewport, hiddenLrms, lrmCatalogue)
 *     POSTs to /query-scores — lightweight fetch that returns only
 *     {edge, visible_lrm_count, visible_score_sum} for the current LRM filter.
 *     Automatically chooses the faster query strategy:
 *       • included_lrms (WHERE lrm IN …) when the visible set is smaller —
 *         DuckDB reads only the matching fraction of the parquet.
 *       • excluded_lrms (CASE WHEN NOT IN …) when the excluded set is smaller.
 *     Short-circuits with no backend call when hiddenLrms is empty (show all).
 *
 * The two results are merged client-side: structural edges get their
 * visible_lrm_count / visible_score_sum fields overlaid from the score map.
 * When no filter is active, score_sum from the structural fetch is used directly.
 *
 * Returns { edges, loading }.
 */
import { useState, useEffect, useRef, useMemo } from "react";

const DEBOUNCE_MS = 400;

/**
 * Normalise a store filter into the request-body shape the backend expects
 * (issue #45), or undefined when nothing is constrained.
 */
function filterBody(filter) {
  if (!filter?.field) return undefined;
  const hasValues = Array.isArray(filter.values) && filter.values.length > 0;
  if (!hasValues && filter.min == null && filter.max == null) return undefined;
  return {
    field: filter.field,
    values: hasValues ? filter.values : null,
    min: filter.min ?? null,
    max: filter.max ?? null,
    include_missing: !!filter.includeMissing,
  };
}

export function useEdges(
  apiBase, dataset, viewport, imageSize, enabled,
  minStrength, hiddenLrms, lrmCatalogue, density = 1.0,
  edgeFile = "edges.parquet", cellFilter = null, edgeFilter = null
) {
  // Which edge-source parquet to query; appended to every /edges request.
  const efParam = `?edge_file=${encodeURIComponent(edgeFile)}`;

  // Serialised so the structural effect can depend on filter *content* rather
  // than object identity, which changes on every render.
  const cellFilterBody = filterBody(cellFilter);
  const edgeFilterBody = filterBody(edgeFilter);
  const filterKey = JSON.stringify([cellFilterBody ?? null, edgeFilterBody ?? null]);
  // ── Structural state ──────────────────────────────────────────────────────
  const [structuralEdges, setStructuralEdges] = useState([]);
  const [loadingStructural, setLoadingStructural] = useState(false);
  const structTimerRef = useRef(null);
  const structAbortRef = useRef(null);

  // ── Score state ───────────────────────────────────────────────────────────
  // Map<edge_id, {visible_lrm_count, visible_score_sum}> | null
  // null  = no filter active; merge falls back to score_sum from structural
  // Map   = active filter; may be empty if all edges have 0 visible LRMs
  const [edgeScores, setEdgeScores] = useState(null);
  const [loadingScores, setLoadingScores] = useState(false);
  const scoreTimerRef = useRef(null);
  const scoreAbortRef = useRef(null);

  // ── Effect 1: structural fetch ─────────────────────────────────────────────
  // Depends on viewport / density / enabled / minStrength only.
  // hiddenLrms is intentionally excluded — LRM changes don't re-fetch positions.
  useEffect(() => {
    if (!enabled || !viewport || !imageSize?.w) {
      setStructuralEdges([]);
      setLoadingStructural(false);
      return;
    }

    clearTimeout(structTimerRef.current);
    structTimerRef.current = setTimeout(async () => {
      if (structAbortRef.current) structAbortRef.current.abort();
      const ctrl = new AbortController();
      structAbortRef.current = ctrl;

      setLoadingStructural(true);
      const { xmin, ymin, xmax, ymax } = viewport;
      const body = { xmin, ymin, xmax, ymax, density: Math.max(0.01, Math.min(1.0, density)) };
      if (minStrength != null && minStrength > 0) body.min_strength = minStrength;
      // Metadata filters go to the server so they apply before the density
      // sample; filtering the response instead would sample first and leave a
      // fraction of the subset.
      if (cellFilterBody) body.cell_filter = cellFilterBody;
      if (edgeFilterBody) body.edge_filter = edgeFilterBody;

      try {
        const res = await fetch(`${apiBase}/edges/${dataset}/query-grouped${efParam}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
          signal: ctrl.signal,
        });
        setStructuralEdges(res.ok ? await res.json() : []);
      } catch (e) {
        if (e.name !== "AbortError") setStructuralEdges([]);
      } finally {
        if (structAbortRef.current === ctrl) setLoadingStructural(false);
      }
    }, DEBOUNCE_MS);

    return () => clearTimeout(structTimerRef.current);
  }, [apiBase, dataset, viewport, imageSize, enabled, minStrength, density, efParam, filterKey]); // eslint-disable-line

  // ── Effect 2: score fetch ──────────────────────────────────────────────────
  // Runs when viewport OR hiddenLrms changes.
  // Short-circuits when hiddenLrms is empty (no filter → use score_sum from structural).
  useEffect(() => {
    if (!enabled || !viewport || !imageSize?.w) {
      setEdgeScores(null);
      setLoadingScores(false);
      return;
    }

    const hiddenCount = hiddenLrms?.size ?? 0;
    const totalLrms   = lrmCatalogue?.length ?? 0;

    // No filter active: clear any stale score map so merge uses structural score_sum
    if (hiddenCount === 0 || totalLrms === 0) {
      clearTimeout(scoreTimerRef.current);
      if (scoreAbortRef.current) { scoreAbortRef.current.abort(); scoreAbortRef.current = null; }
      setEdgeScores(null);
      setLoadingScores(false);
      return;
    }

    // All LRMs hidden: every edge has visible_lrm_count = 0; no fetch needed
    const visibleCount = totalLrms - hiddenCount;
    if (visibleCount <= 0) {
      clearTimeout(scoreTimerRef.current);
      if (scoreAbortRef.current) { scoreAbortRef.current.abort(); scoreAbortRef.current = null; }
      setEdgeScores(new Map()); // empty Map signals "filter active, all hidden"
      setLoadingScores(false);
      return;
    }

    clearTimeout(scoreTimerRef.current);
    scoreTimerRef.current = setTimeout(async () => {
      if (scoreAbortRef.current) scoreAbortRef.current.abort();
      const ctrl = new AbortController();
      scoreAbortRef.current = ctrl;

      setLoadingScores(true);
      const { xmin, ymin, xmax, ymax } = viewport;
      const body = { xmin, ymin, xmax, ymax };

      // Choose the smaller set to minimise the IN-list and maximise DuckDB pruning.
      // included_lrms → WHERE lrm IN (...): reads only matching rows (~visibleCount/totalLrms fraction).
      // excluded_lrms → CASE WHEN NOT IN (...): full scan, but exclusion list is short.
      if (visibleCount <= hiddenCount) {
        body.included_lrms = lrmCatalogue
          .filter(l => !hiddenLrms.has(l.lrm))
          .map(l => l.lrm);
      } else {
        body.excluded_lrms = [...hiddenLrms];
      }

      try {
        const res = await fetch(`${apiBase}/edges/${dataset}/query-scores${efParam}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
          signal: ctrl.signal,
        });
        if (res.ok) {
          const arr = await res.json();
          setEdgeScores(new Map(arr.map(s => [
            s.edge,
            { visible_lrm_count: s.visible_lrm_count ?? 0,
              visible_score_sum:  s.visible_score_sum  ?? 0 },
          ])));
        } else {
          setEdgeScores(null); // fallback: treat as no filter
        }
      } catch (e) {
        if (e.name !== "AbortError") setEdgeScores(null);
      } finally {
        if (scoreAbortRef.current === ctrl) setLoadingScores(false);
      }
    }, DEBOUNCE_MS);

    return () => clearTimeout(scoreTimerRef.current);
  }, [apiBase, dataset, viewport, imageSize, enabled, hiddenLrms, lrmCatalogue, efParam]); // eslint-disable-line

  // ── Merge: overlay scores onto structural edges ───────────────────────────
  // edgeScores === null → no filter; use score_sum from structural as visible_score_sum
  // edgeScores is a Map → filter active; look up each edge, default to 0 if absent
  const edges = useMemo(() => {
    if (structuralEdges.length === 0) return [];
    if (edgeScores === null) {
      // No LRM filter — visible_* fields equal the unfiltered totals
      return structuralEdges.map(e => ({
        ...e,
        visible_lrm_count: e.lrm_count ?? 1,
        visible_score_sum: e.score_sum  ?? null,
      }));
    }
    // LRM filter active — overlay scores; missing entries → 0 visible LRMs
    return structuralEdges.map(e => {
      const s = edgeScores.get(e.edge);
      return {
        ...e,
        visible_lrm_count: s?.visible_lrm_count ?? 0,
        visible_score_sum:  s?.visible_score_sum  ?? 0,
      };
    });
  }, [structuralEdges, edgeScores]);

  // ── Abort on unmount ──────────────────────────────────────────────────────
  useEffect(() => {
    return () => {
      clearTimeout(structTimerRef.current);
      clearTimeout(scoreTimerRef.current);
      if (structAbortRef.current) structAbortRef.current.abort();
      if (scoreAbortRef.current)  scoreAbortRef.current.abort();
    };
  }, []);

  return { edges, loading: loadingStructural || loadingScores };
}
