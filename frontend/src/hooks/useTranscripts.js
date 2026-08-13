import { useState, useEffect, useRef } from "react";

/**
 * Fetches transcripts from the backend, filtered by viewport bbox.
 * Debounced so rapid pan/zoom doesn't hammer the API.
 * In-flight requests are aborted when a newer fetch supersedes them, so stale
 * responses from intermediate viewport positions never overwrite current data.
 *
 * @param fraction      0–1 fraction of viewport transcripts to request.
 * @param selectedGenes null = all species; Set<string> = only those genes.
 *                      Passed to the backend so total reflects selected species only.
 * @param allGenes      the dataset's full gene panel, used only to decide whether
 *                      to state the filter as an allowlist or as its complement.
 *
 * Returns { transcripts, total, loading, error }.
 *   total — pre-sample count in the viewport after gene filtering (from backend).
 */
export function useTranscripts(apiBase, dataset, viewport, imageSize, enabled = true, fraction = 1.0, selectedGenes = null, allGenes = null) {
  const [transcripts, setTranscripts] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const timerRef = useRef(null);
  const abortRef = useRef(null);

  // Stable string representation of the gene set for use as a dep.
  const genesKey = selectedGenes === null ? "" : [...selectedGenes].sort().join(",");

  useEffect(() => {
    if (!enabled || !dataset) {
      setTranscripts([]);
      setTotal(0);
      setLoading(false);
      return;
    }

    // An empty allowlist means "show no species" — distinct from null, which
    // means "no filter". Without this the request omits the gene parameter
    // entirely, so the backend applies no filter and returns the full 200K-row
    // cap; Viewer's client-side filter then discards every row. Nothing draws,
    // which looks right, but a ~20 MB response is fetched and thrown away on
    // every pan and the layer's shown/total badge reports the unfiltered count
    // while the canvas is empty.
    if (selectedGenes !== null && selectedGenes.size === 0) {
      setTranscripts([]);
      setTotal(0);
      setLoading(false);
      return;
    }

    clearTimeout(timerRef.current);
    timerRef.current = setTimeout(async () => {
      // Cancel any in-flight request before starting a new one
      if (abortRef.current) abortRef.current.abort();
      const ctrl = new AbortController();
      abortRef.current = ctrl;

      setLoading(true);
      setError(null);
      try {
        let url = `${apiBase}/spatial/${dataset}/transcripts?fraction=${fraction}`;

        // Send the selected gene filter so the backend samples within those
        // species only — stated as an allowlist or as its complement, whichever
        // is shorter.
        //
        // This is a GET, so the list travels in the URL and long ones are fatal:
        // measured against this stack, 479 of 480 genes is a 7,780-byte URL, only
        // ~220 bytes under nginx's 8 KB request-line limit, and 600 genes returns
        // 414 outright. Deselecting a handful of genes from a large panel is an
        // ordinary thing to do and produces exactly that shape, so the naive
        // allowlist breaks on any panel much past 500 genes — a Xenium Prime 5K
        // run would fail on the first click.
        //
        // The complement is small precisely when the allowlist is not, so sending
        // the smaller of the two bounds the URL at roughly half the panel.
        if (selectedGenes !== null && selectedGenes.size > 0) {
          const total = Array.isArray(allGenes) ? allGenes.length : 0;
          const excluded = total > 0
            ? allGenes.filter((g) => !selectedGenes.has(g))
            : [];
          if (total > 0 && excluded.length < selectedGenes.size) {
            for (const g of excluded) url += `&exclude_genes=${encodeURIComponent(g)}`;
          } else {
            for (const g of selectedGenes) url += `&genes=${encodeURIComponent(g)}`;
          }
        }

        // Always send bbox when available so the backend can sample uniformly
        // within the viewport. No zoom-out skip — the backend cap handles volume.
        if (viewport && imageSize?.w) {
          const { xmin, ymin, xmax, ymax } = viewport;
          url += `&xmin=${xmin}&ymin=${ymin}&xmax=${xmax}&ymax=${ymax}`;
        }

        const res = await fetch(url, { signal: ctrl.signal });
        if (!res.ok) { setTranscripts([]); setTotal(0); return; }
        const data = await res.json();
        // Response is { transcripts: [...], total: N }
        const arr = Array.isArray(data) ? data : (data.transcripts ?? []);
        const tot = typeof data.total === "number" ? data.total : arr.length;
        setTranscripts(arr);
        setTotal(tot);
      } catch (e) {
        if (e.name === "AbortError") return; // silently ignore — a newer fetch is in flight
        setError(e.message);
        setTotal(0);
      } finally {
        if (abortRef.current === ctrl) setLoading(false);
      }
    }, 200);

    return () => clearTimeout(timerRef.current);
  // allGenes.length is a dep because it decides the URL *form*: before the panel
  // loads there is no complement to compute, so an early fetch would fall back to
  // the long allowlist and could 414. Re-running once it arrives fixes that.
  }, [apiBase, dataset, viewport?.xmin, viewport?.ymin, viewport?.xmax, viewport?.ymax, enabled, fraction, genesKey, allGenes?.length]); // eslint-disable-line

  // Abort in-flight request on unmount
  useEffect(() => {
    return () => {
      clearTimeout(timerRef.current);
      if (abortRef.current) abortRef.current.abort();
    };
  }, []);

  return { transcripts, total, loading, error };
}
