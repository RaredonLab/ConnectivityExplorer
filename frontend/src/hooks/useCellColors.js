import { useState, useEffect, useRef } from "react";
import { valueToColor, QUAL_PALETTE } from "../utils/colormap";
import { geneColor } from "../utils/geneColor";

/**
 * Fetches per-cell color values and maps them to RGBA.
 *
 * Three-effect design keeps server fetches and client-side remapping separate:
 *
 *   Effect 1 — fetch raw server data
 *     Deps: apiBase, dataset, colorBy, allGenes, selectedGenes, enabled
 *     Clamp and palette are intentionally excluded — they don't affect the
 *     server response, only how the returned values are mapped to colors.
 *     Debounced at 400 ms; in-flight requests are aborted when superseded.
 *
 *   Effect 2 — apply clamp + palette to continuous raw data (no fetch)
 *     Deps: rawCont, clamp, palette
 *     Fires synchronously (no debounce) so slider drags and "reset range"
 *     update the canvas in the same render cycle as the slider itself.
 *
 *   Effect 3 — remap categorical colors (no fetch)
 *     Deps: rawCat, categoryColorOverrides, colorBy.field
 *     Unchanged from the previous design.
 *
 * Modes:
 *   gene_set  — POST with selected genes; returns continuous sum
 *   metadata  — POST with field; backend auto-detects continuous vs. categorical
 *               unless `categorical` overrides it (issue #35)
 *
 * Returns:
 *   colorValues   Map<cell_id, [r,g,b,a]> or null when disabled
 *   type          "continuous" | "categorical"
 *   vmin, vmax    for continuous legend
 *   categories    string[] for categorical legend
 *   categoryColors Map<label, [r,g,b,a]> for categorical legend
 *   loading
 */
export function useCellColors(apiBase, dataset, colorBy, allGenes, selectedGenes, palette, enabled, clamp, categoryColorOverrides, categorical = null) {
  const [result, setResult] = useState({
    colorValues: null, type: "continuous", vmin: 0, vmax: 0,
    categories: [], categoryColors: new Map(),
  });
  const [loading, setLoading] = useState(false);
  const timerRef = useRef(null);
  const abortRef = useRef(null);

  // rawCat:  last categorical server response { categories, values }
  // rawCont: last continuous server response  { values, min, max }
  // Stored separately so palette/clamp changes recompute colors without re-fetching.
  const [rawCat,  setRawCat]  = useState(null);
  const [rawCont, setRawCont] = useState(null);

  // ── Effect 1: fetch raw server data ──────────────────────────────────────
  // palette and clamp deliberately excluded — they don't change the server response.
  useEffect(() => {
    if (!enabled || !colorBy || colorBy.mode === "off") {
      setResult({ colorValues: null, type: "continuous", vmin: 0, vmax: 0, categories: [], categoryColors: new Map() });
      setRawCat(null);
      setRawCont(null);
      setLoading(false);
      return;
    }

    const { mode, field } = colorBy;

    const genesToSend = mode === "gene_set"
      ? (selectedGenes === null ? allGenes : [...selectedGenes])
      : null;
    if (mode === "gene_set" && (!genesToSend || genesToSend.length === 0)) {
      setResult({ colorValues: null, type: "continuous", vmin: 0, vmax: 0, categories: [], categoryColors: new Map() });
      setRawCat(null);
      setRawCont(null);
      setLoading(false);
      return;
    }
    if (mode === "metadata" && !field) {
      setResult({ colorValues: null, type: "continuous", vmin: 0, vmax: 0, categories: [], categoryColors: new Map() });
      setRawCat(null);
      setRawCont(null);
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
        const body = mode === "gene_set"
          ? { mode: "gene_set", genes: genesToSend }
          : { mode: "metadata", field, categorical };

        const res = await fetch(`${apiBase}/spatial/${dataset}/color-values`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
          signal: ctrl.signal,
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (!data?.values) throw new Error("color-values response missing 'values'");

        if (data.type === "categorical") {
          setRawCat({ categories: data.categories, values: data.values });
          setRawCont(null);
        } else {
          setRawCont({ values: data.values, min: data.min, max: data.max });
          setRawCat(null);
        }
      } catch (e) {
        if (e.name === "AbortError") return;
        setRawCat(null);
        setRawCont(null);
        setResult({ colorValues: null, type: "continuous", vmin: 0, vmax: 0, categories: [], categoryColors: new Map() });
      } finally {
        if (abortRef.current === ctrl) setLoading(false);
      }
    }, 400);
    return () => clearTimeout(timerRef.current);
  }, [apiBase, dataset, colorBy?.mode, colorBy?.field, allGenes, selectedGenes, enabled, categorical]); // eslint-disable-line

  // ── Effect 2: apply clamp + palette to continuous data (no fetch, no debounce) ──
  // Fires immediately when rawCont, clamp, or palette changes so slider drags
  // and "reset range" update the canvas in the same render cycle as the slider.
  useEffect(() => {
    if (!rawCont) return;
    const { values, min, max } = rawCont;
    const lo = clamp?.low  ?? min;
    const hi = clamp?.high ?? max;
    const colorMap = new Map(
      Object.entries(values).map(([id, v]) => [id, valueToColor(v, lo, hi, palette)])
    );
    setResult({ colorValues: colorMap, type: "continuous", vmin: min, vmax: max, categories: [], categoryColors: new Map() });
  }, [rawCont, clamp?.low, clamp?.high, palette]); // eslint-disable-line

  // ── Effect 3: remap categorical colors client-side (no fetch) ────────────
  // Runs whenever raw server data or the user edits a swatch color.
  useEffect(() => {
    if (!rawCat) return;
    const { categories, values } = rawCat;
    const field = colorBy?.field;
    const colorMap = new Map(
      categories.map((cat, i) => {
        const key = `${field}::${cat}`;
        const override = categoryColorOverrides?.[key];
        return [cat, override ?? (i < QUAL_PALETTE.length ? QUAL_PALETTE[i] : [...geneColor(cat), 255])];
      })
    );
    const cellColors = new Map(
      Object.entries(values).map(([id, label]) => [id, colorMap.get(label) ?? [128, 128, 128, 255]])
    );
    setResult({ colorValues: cellColors, type: "categorical", vmin: 0, vmax: 0, categories, categoryColors: colorMap });
  }, [rawCat, categoryColorOverrides, colorBy?.field]); // eslint-disable-line

  // ── Abort in-flight request on unmount ────────────────────────────────────
  useEffect(() => {
    return () => {
      clearTimeout(timerRef.current);
      if (abortRef.current) abortRef.current.abort();
    };
  }, []);

  return { ...result, loading };
}
