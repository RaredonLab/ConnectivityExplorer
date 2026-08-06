import { useState, useEffect, useRef } from "react";

/**
 * Fetches cell boundary vertices and groups them into polygon objects.
 * Returns { cells, total, effectiveFraction, loading, error }.
 *
 * In-flight requests are aborted when a newer fetch supersedes them, so stale
 * responses from prior viewport positions never overwrite current data.
 *
 * fraction param:
 *   null   → auto mode: hook targets ~TARGET_CELLS rendered cells, adapting the
 *            fraction to the actual density seen in the last fetch.
 *   number → user override: send exactly this fraction (0–1).
 *
 * prevTotalRef seeds to a conservative estimate (50k) so the very first probe
 * is 10% rather than 100%.  After the first response the estimate self-corrects.
 */

const TARGET_CELLS = 5_000;
const SEED_TOTAL   = 50_000; // conservative first-probe estimate

/**
 * Serialise a metadata filter (issue #45) into query params.
 *
 * Returns "" when there is nothing to constrain, so the URL is byte-identical to
 * the pre-filter one and no cached response is missed. The filter is sent to the
 * server rather than applied to the response because sampling happens server-side:
 * filtering afterwards would leave a fraction of a fraction on screen.
 */
function filterParams(filter) {
  if (!filter?.field) return "";
  const p = new URLSearchParams();
  const hasValues = Array.isArray(filter.values) && filter.values.length > 0;
  if (!hasValues && filter.min == null && filter.max == null) return "";
  p.set("filter_field", filter.field);
  if (hasValues) for (const v of filter.values) p.append("filter_values", v);
  if (filter.min != null) p.set("filter_min", filter.min);
  if (filter.max != null) p.set("filter_max", filter.max);
  if (filter.includeMissing) p.set("filter_missing", "true");
  return `&${p.toString()}`;
}

export function useCellBoundaries(
  apiBase, dataset, viewport, imageSize, enabled = true, fraction = null,
  filter = null
) {
  const [cells, setCells]                     = useState([]);
  const [total, setTotal]                     = useState(0);
  const [effectiveFraction, setEffective]     = useState(TARGET_CELLS / SEED_TOTAL);
  const [loading, setLoading]                 = useState(false);
  const [error, setError]                     = useState(null);
  const timerRef    = useRef(null);
  const abortRef    = useRef(null);
  const prevTotalRef = useRef(SEED_TOTAL);   // running estimate of cells in viewport

  // Serialised once so it can be both spliced into the URL and used as an effect
  // dependency — the filter arrives as an object whose identity changes on every
  // render, which would otherwise refetch continuously.
  const filterQS = filterParams(filter);

  // One-shot recalibration.
  //
  // In auto mode the fraction is picked from `prevTotalRef`, the total the *last*
  // fetch saw. Applying a metadata filter (or switching dataset) changes that
  // total out from under the estimate, and nothing else would trigger another
  // fetch — so the layer would sit showing a tenth of an already-small subset
  // until the user happened to pan. Bumping this counter re-runs the fetch once
  // with the corrected fraction; `calibratedRef` keys it to the current request
  // so it can converge rather than oscillate.
  const [recalibrate, setRecalibrate] = useState(0);
  const calibratedRef = useRef(null);

  useEffect(() => {
    if (!enabled || !dataset) {
      setLoading(false);
      return;
    }

    clearTimeout(timerRef.current);
    timerRef.current = setTimeout(async () => {
      // Cancel any in-flight request before starting a new one
      if (abortRef.current) abortRef.current.abort();
      const ctrl = new AbortController();
      abortRef.current = ctrl;

      // Compute the fraction we'll actually send this round
      const autoFrac = Math.min(1.0, TARGET_CELLS / Math.max(1, prevTotalRef.current));
      const eff = fraction !== null
        ? Math.max(0.0001, Math.min(1.0, fraction))
        : autoFrac;
      setEffective(eff);

      setLoading(true);
      setError(null);
      try {
        let url = `${apiBase}/spatial/${dataset}/cell-boundaries`;
        const fracParam = `fraction=${eff}`;
        if (viewport && imageSize?.w) {
          const { xmin, ymin, xmax, ymax } = viewport;
          url += `?xmin=${xmin}&ymin=${ymin}&xmax=${xmax}&ymax=${ymax}&${fracParam}`;
        } else {
          url += `?${fracParam}`;
        }
        url += filterQS;
        const res = await fetch(url, { signal: ctrl.signal });
        if (!res.ok) { setCells([]); setTotal(0); return; }
        const data = await res.json();

        // Expect { boundaries: [...], total: N }
        const rows = Array.isArray(data) ? data : (data.boundaries ?? []);
        const totalCells = typeof data.total === "number" ? data.total : rows.length;

        // Update the running estimate so the next auto fraction is better calibrated
        if (totalCells > 0) prevTotalRef.current = totalCells;
        setTotal(totalCells);

        // If that estimate was badly wrong, correct it now rather than waiting
        // for the user to pan. Only in auto mode — an explicit slider value is
        // the user's decision, not an estimate. Once per request key.
        // The 1.2 threshold is what makes the panel's sample readout honest: the
        // panel derives its percentage from the *current* total, so anything
        // looser leaves it advertising a fraction the canvas is not drawing at.
        // Total is a pre-sample count for a fixed bbox and filter, so the second
        // fetch computes the same fraction and the loop settles after one pass.
        if (fraction === null && totalCells > 0) {
          const better = Math.min(1.0, TARGET_CELLS / totalCells);
          const key = `${url}|${totalCells}`;
          if (better > eff * 1.2 && calibratedRef.current !== key) {
            calibratedRef.current = key;
            setRecalibrate((c) => c + 1);
          }
        }

        if (!Array.isArray(rows)) { setCells([]); return; }

        // Group flat vertex list by cell_id → polygon arrays
        const byCell = new Map();
        for (const row of rows) {
          if (!byCell.has(row.cell_id)) byCell.set(row.cell_id, []);
          byCell.get(row.cell_id).push([row.vertex_x, row.vertex_y]);
        }
        setCells(
          Array.from(byCell.entries()).map(([cell_id, polygon]) => ({ cell_id, polygon }))
        );
      } catch (e) {
        if (e.name === "AbortError") return; // silently ignore — a newer fetch is in flight
        setError(e.message);
      } finally {
        if (abortRef.current === ctrl) setLoading(false);
      }
    }, 200);

    return () => clearTimeout(timerRef.current);
  }, [apiBase, dataset, viewport?.xmin, viewport?.ymin, viewport?.xmax, viewport?.ymax, enabled, fraction, filterQS, recalibrate]);

  // Abort in-flight request on unmount
  useEffect(() => {
    return () => {
      clearTimeout(timerRef.current);
      if (abortRef.current) abortRef.current.abort();
    };
  }, []);

  return { cells, total, effectiveFraction, loading, error };
}
