import { useState, useEffect, useRef, useMemo } from "react";
import { valueToColor, QUAL_PALETTE } from "../utils/colormap";

/**
 * Computes per-directed-edge color values.
 *
 * lrm_set mode: computed synchronously client-side via useMemo from
 *   visible_score_sum in the edges array (no server call).
 *   Clamp and palette changes update the canvas immediately.
 *
 * metadata mode: fetched from the backend (requires server-side GROUP BY).
 *   Uses the same two-effect split as useCellColors:
 *     Effect 1 — fetch raw values (palette/clamp NOT in deps; they don't
 *                affect the server response). Debounced + abortable.
 *     Effect 2 — apply clamp + palette to rawMeta synchronously (no debounce)
 *                so "reset range" and slider drags update the canvas instantly.
 *
 * Returns:
 *   colorValues   Map<edge_id, [r,g,b,a]> or null when mode is "default"
 *   type          "continuous" | "categorical"
 *   vmin, vmax    for continuous legend
 *   p95           95th-percentile value (used for auto-calibration)
 *   categories    string[] for categorical legend
 *   categoryColors Map<label, [r,g,b,a]> for categorical legend
 *   loading
 */
export function useEdgeColors(
  apiBase, dataset, edgeColorBy, hiddenLrms, lrmCatalogue,
  palette, enabled, clamp,
  edges,  // array from useEdges — used for client-side lrm_set coloring
  edgeFile = "edges.parquet",  // which edge-source parquet the metadata fetch reads
  categorical = null  // issue #35 override: null = auto-detect, true/false = forced
) {
  // Appended to the metadata edge-color-values request (lrm_set is client-side only).
  const efParam = `?edge_file=${encodeURIComponent(edgeFile)}`;
  const [result, setResult] = useState({
    colorValues: null, type: "continuous", vmin: 0, vmax: 0, p95: null,
    categories: [], categoryColors: new Map(),
  });
  const [loading, setLoading] = useState(false);
  const timerRef = useRef(null);
  const abortRef = useRef(null);

  // rawMeta: last continuous metadata server response { values, min, max, p95 }
  // Cached so clamp/palette remapping never needs a re-fetch.
  const [rawMeta, setRawMeta] = useState(null);

  // ── lrm_set: compute synchronously from edges.visible_score_sum ──────────
  //
  // Autocrine edges are included. They used to be filtered out here, which left
  // the autocrine rings permanently on their default orange no matter what the
  // colour control said — the one edge type that ignored "colour by LRM set".
  // A shared scale is safe: measured across the bundled datasets, autocrine
  // score_sum medians run 1.00–1.33× the directed medians, so they neither
  // dominate the range nor need a scale of their own. One scale, one legend,
  // and every edge on screen is described by it.
  const lrmSetResult = useMemo(() => {
    if (!enabled || edgeColorBy?.mode !== "lrm_set" || !edges || edges.length === 0) {
      return null;
    }
    const entries = edges.map((e) => [e.edge, e.visible_score_sum ?? 0]);
    const scores = entries.map(([, v]) => v);
    let min = Infinity, max = -Infinity;
    for (const v of scores) { if (v < min) min = v; if (v > max) max = v; }
    if (!isFinite(min)) min = 0;
    if (!isFinite(max)) max = 0;
    const sorted = [...scores].sort((a, b) => a - b);
    const p95 = sorted[Math.floor(sorted.length * 0.95)] ?? max;
    const lo = clamp?.low  ?? min;
    const hi = clamp?.high ?? max;  // use max (not p95) so reset matches the legend
    const colorMap = new Map(
      entries.map(([id, v]) => [id, valueToColor(v, lo, hi, palette)])
    );
    return { colorValues: colorMap, type: "continuous", vmin: min, vmax: max, p95, categories: [], categoryColors: new Map() };
  }, [enabled, edgeColorBy?.mode, edges, palette, clamp?.low, clamp?.high]);

  // ── Effect 1: fetch raw metadata values (no clamp, no palette) ───────────
  // palette and clamp deliberately excluded — they don't change the server response.
  useEffect(() => {
    if (!enabled || !edgeColorBy || edgeColorBy.mode !== "metadata") {
      if (edgeColorBy?.mode !== "lrm_set") {
        setResult({ colorValues: null, type: "continuous", vmin: 0, vmax: 0, p95: null, categories: [], categoryColors: new Map() });
      }
      setRawMeta(null);
      setLoading(false);
      return;
    }

    const { field } = edgeColorBy;
    if (!field) {
      setResult({ colorValues: null, type: "continuous", vmin: 0, vmax: 0, p95: null, categories: [], categoryColors: new Map() });
      setRawMeta(null);
      setLoading(false);
      return;
    }

    clearTimeout(timerRef.current);
    timerRef.current = setTimeout(async () => {
      if (abortRef.current) abortRef.current.abort();
      const ctrl = new AbortController();
      abortRef.current = ctrl;

      setLoading(true);
      try {
        const res = await fetch(`${apiBase}/edges/${dataset}/edge-color-values${efParam}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ mode: "metadata", field, categorical }),
          signal: ctrl.signal,
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();

        if (data.type === "categorical") {
          // Categorical: map colors immediately (no clamp applies)
          const { categories } = data;
          const colorMap = new Map(
            categories.map((cat, i) => [cat, QUAL_PALETTE[i % QUAL_PALETTE.length]])
          );
          const edgeColors = new Map(
            Object.entries(data.values).map(([id, label]) => [id, colorMap.get(label) ?? [128, 128, 128, 255]])
          );
          setRawMeta(null);
          setResult({ colorValues: edgeColors, type: "categorical", vmin: 0, vmax: 0, p95: null, categories, categoryColors: colorMap });
        } else {
          // Continuous: cache raw values; Effect 2 applies clamp + palette
          const sorted = Object.values(data.values).sort((a, b) => a - b);
          const p95 = sorted.length > 0 ? sorted[Math.floor(sorted.length * 0.95)] : data.max;
          setRawMeta({ values: data.values, min: data.min, max: data.max, p95 });
        }
      } catch (e) {
        if (e.name === "AbortError") return;
        setRawMeta(null);
        setResult({ colorValues: null, type: "continuous", vmin: 0, vmax: 0, p95: null, categories: [], categoryColors: new Map() });
      } finally {
        if (abortRef.current === ctrl) setLoading(false);
      }
    }, 400);
    return () => clearTimeout(timerRef.current);
  }, [apiBase, dataset, edgeColorBy?.mode, edgeColorBy?.field, enabled, efParam, categorical]); // eslint-disable-line

  // ── Effect 2: apply clamp + palette to continuous metadata (no fetch, no debounce) ──
  useEffect(() => {
    if (!rawMeta) return;
    const { values, min, max, p95 } = rawMeta;
    const lo = clamp?.low  ?? min;
    const hi = clamp?.high ?? p95;
    const colorMap = new Map(
      Object.entries(values).map(([id, v]) => [id, valueToColor(v, lo, hi, palette)])
    );
    setResult({ colorValues: colorMap, type: "continuous", vmin: min, vmax: max, p95, categories: [], categoryColors: new Map() });
  }, [rawMeta, clamp?.low, clamp?.high, palette]); // eslint-disable-line

  // ── Abort in-flight request on unmount ────────────────────────────────────
  useEffect(() => {
    return () => {
      clearTimeout(timerRef.current);
      if (abortRef.current) abortRef.current.abort();
    };
  }, []);

  // Return lrm_set result immediately (synchronous), or the fetched metadata result
  if (edgeColorBy?.mode === "lrm_set") {
    return { ...(lrmSetResult ?? result), loading: false };
  }
  return { ...result, loading };
}
