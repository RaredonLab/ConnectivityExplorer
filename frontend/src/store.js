import { create } from "zustand";

const API = import.meta.env.VITE_API_URL ?? "/api";

/**
 * One panel's dataset-bound state.
 *
 * `dataset: null` on init; DatasetPicker fills it from /spatial/datasets.
 * Everything else is derived from whichever dataset is loaded, which is exactly
 * why it cannot live at the top level once two panels can show two datasets:
 * image dimensions, pixel size, capabilities, gene panel, LRM vocabulary and
 * value ranges all differ between them.
 */
function makePanel() {
  return {
    dataset: null,
    activeImage: null,
    imageSize: { w: null, h: null },     // from the DZI descriptor, when OSD opens
    platformCapabilities: null,          // /spatial/{ds}/info -> has_transcripts, unit_label, …
    pixelSize: 1.0,                      // µm per image pixel; drives measurement + zoom match
    edgeFile: "edges.parquet",
    lrmCatalogue: [],
    allGenes: [],
    genesLoaded: false,
    cellColorRange: { vmin: null, vmax: null },
    edgeColorRange: { vmin: null, vmax: null },
    cellColorType: "continuous",
    cellColorCategories: [],
    transcriptStats: { shown: 0, total: 0 },
    cellBoundaryStats: { shown: 0, total: 0 },
  };
}

export const useStore = create((set, get) => ({
  apiBase: API,

  // ══ Per-panel state ═══════════════════════════════════════════════════════
  //
  // Everything here is bound to *a dataset*, so with two panels showing two
  // datasets it cannot be global. Style and choice settings (layer opacity,
  // palettes, filters, colour-by) stay global for now: one sidebar drives both
  // panels, which is what makes a side-by-side comparison comparable. Phase 2
  // splits those per panel behind sidebar tabs.
  //
  // Read as `panels[panelIndex]`. Panel 1 exists even in single mode so nothing
  // has to guard on panelCount.
  panels: [makePanel(), makePanel()],

  // Generic shallow patch. Specific transitions that must reset dependent state
  // (setPanelDataset, setPanelEdgeFile) are separate below.
  patchPanel: (i, patch) => set((s) => {
    const next = [...s.panels];
    next[i] = { ...next[i], ...patch };
    return { panels: next };
  }),

  // Switching a panel's dataset resets everything derived from the old one.
  // activeImage is cleared because image names are platform-specific
  // ("morphology" on Xenium, "Roi1_DAPI" on seqFISH), and the picker only learns
  // the new list asynchronously — without this, OSD spends that window asking
  // the new dataset for the old dataset's image and logging 404s.
  setPanelDataset: (i, dataset) => set((s) => {
    const next = [...s.panels];
    next[i] = { ...makePanel(), dataset };
    // Selections belong to a dataset; drop any that pointed at the old one.
    const sel = s.selection && s.selection.panelIndex === i ? null : s.selection;
    return {
      panels: next,
      selection: sel,
      // The shared settings that name a *column, gene or mechanism* are reset by
      // any panel's dataset change, even though they are shared. They have to be:
      // a filter naming a column the new dataset lacks 400s on every viewport
      // change, and a gene allowlist from a different panel is meaningless.
      //
      // The cost is that switching one panel's dataset clears the other panel's
      // filter too. That is the honest consequence of one sidebar driving both,
      // and it goes away in Phase 2 when these become per-panel.
      selectedGenes: null,
      hiddenLrms: new Set(),
      categoricalOverrides: {},
      cellFilter: null,
      edgeFilter: null,
      categoryColorOverrides: {},
      transcriptColorOverrides: {},
      colorBy: { mode: "off", field: null },
      cellColorClamp: { low: null, high: null },
      edgeColorClamp: { low: null, high: null },
    };
  }),

  // The LRM catalogue, colour range and edge filter are all specific to one
  // edges.parquet and must be re-derived when the source changes.
  setPanelEdgeFile: (i, edgeFile) => set((s) => {
    const next = [...s.panels];
    next[i] = {
      ...next[i], edgeFile, lrmCatalogue: [],
      edgeColorRange: { vmin: null, vmax: null },
    };
    const sel = s.selection && s.selection.panelIndex === i && s.selection.kind === "edge"
      ? null : s.selection;
    return { panels: next, selection: sel, hiddenLrms: new Set(), edgeFilter: null };
  }),

  // ── Selection ─────────────────────────────────────────────────────────────
  // Carries the panel it came from, so the info panels know which dataset to
  // query. Without that they would resolve a panel-1 click against panel 0's
  // dataset and show the wrong cell.
  //   { panelIndex, kind: "cell" | "edge", cell? , edge? }
  selection: null,
  setSelectedCell: (cell, panelIndex = 0) =>
    set({ selection: cell ? { panelIndex, kind: "cell", cell } : null }),
  setSelectedEdge: (edge, panelIndex = 0) =>
    set({ selection: edge ? { panelIndex, kind: "edge", edge } : null }),
  clearSelection: () => set({ selection: null }),

  // ── Categorical / continuous override (issue #35) ─────────────────────────
  // Keyed "cell::<field>" / "edge::<field>" → true | false. Absent means
  // auto-detect, which is what the backend does when `categorical` is null.
  // Seurat writes cluster IDs as integers, so dtype alone routes them to a
  // viridis gradient; this is how the user says "these are twenty categories".
  categoricalOverrides: {},
  setCategoricalOverride: (scope, field, value) => set((s) => {
    const next = { ...s.categoricalOverrides };
    if (value === null || value === undefined) delete next[`${scope}::${field}`];
    else next[`${scope}::${field}`] = value;
    return { categoricalOverrides: next };
  }),

  // ── Metadata subsetting (issue #45) ───────────────────────────────────────
  // A filter is { field, values: string[] | null, min, max, includeMissing }.
  // null means no filter. `values` is a categorical allowlist; min/max an
  // inclusive numeric range. Applied server-side before sampling, so narrowing
  // to a rare cluster shows all of it rather than a sample of a sample.
  //
  // cellFilter also governs edges: an edge is drawn only when BOTH endpoints
  // survive it. edgeFilter is independent and applies to the edge table itself.

  // ── Shared colour scale across panels ─────────────────────────────────────
  // On by default, and this is a figure-integrity setting rather than a
  // preference: two viridis panels that each auto-ranged to their own data look
  // comparable and are not. Panel A's yellow might be 40 counts and panel B's
  // 4,000. With this on, both panels map through one range computed across both,
  // so the single legend describes everything on screen.
  //
  // Unlock it when one panel's range is so much narrower that shared scaling
  // flattens it — then the panels are individually readable but not comparable,
  // which is the trade you are making knowingly.
  linkColorScale: true,
  setLinkColorScale: (v) => set({ linkColorScale: v }),

  cellFilter: null,
  setCellFilter: (f) => set({ cellFilter: f }),
  edgeFilter: null,
  setEdgeFilter: (f) => set({ edgeFilter: f }),

  // ── Viewport (image pixel coords, kept in sync with OpenSeadragon) ────────
  // One entry per panel; panel 1 is only used in split-screen mode.
  // viewports        — expanded bbox used by data-fetching hooks (may be larger than
  //                    the true visible area when the panel is rotated, to ensure all
  //                    visible corners are covered).
  // viewportActual   — un-expanded OSD bounds (true visible area); used only by the
  //                    ⇔ Match zoom feature so it matches the real viewport width.
  viewports: [null, null],
  setViewport: (viewport, panelIndex = 0) => set((s) => {
    const next = [...s.viewports];
    next[panelIndex] = viewport;
    return { viewports: next };
  }),
  viewportActual: [null, null],
  setViewportActual: (viewport, panelIndex = 0) => set((s) => {
    const next = [...s.viewportActual];
    next[panelIndex] = viewport;
    return { viewportActual: next };
  }),

  // ── Split-screen ──────────────────────────────────────────────────────────
  panelCount: 1,
  setPanelCount: (n) => set({ panelCount: n }),

  // Zoom-match request: set to { fromPanel } to tell the OTHER panel to adopt
  // the same zoom level (visible image area) while keeping its own center.
  // Consumed and cleared by the target ViewerPanel's useEffect.
  pendingZoomMatch: null,
  requestZoomMatch: (fromPanel) => set({ pendingZoomMatch: { fromPanel } }),
  clearZoomMatch: () => set({ pendingZoomMatch: null }),

  // ── Per-panel rotation ────────────────────────────────────────────────────
  // Rotation angle in degrees (0–359) for each panel.
  // Applied to OSD tile display (setRotation) and deck.gl layer modelMatrix.
  panelRotations: [0, 0],
  setPanelRotation: (panelIndex, angle) => set((s) => {
    const next = [...s.panelRotations];
    next[panelIndex] = ((Math.round(angle) % 360) + 360) % 360;
    return { panelRotations: next };
  }),

  // ── Layer visibility ───────────────────────────────────────────────────────
  layers: {
    morphology:   { visible: false, opacity: 1.0 },
    transcripts:  { visible: false, opacity: 0.8 },
    cellSegments: { visible: true,  opacity: 1.0, outlineOpacity: 0.0 },
    tissueGraph:  { visible: true,  opacity: 0.05 },
    edges:        { visible: false, opacity: 0.25 },
  },

  // cellBoundaryFraction: fraction of cells in viewport to fetch.
  // null = auto (hook targets ~5k cells, adapts per viewport density).
  // number = user override (0–1, set by slider).
  cellBoundaryFraction: null,
  setCellBoundaryFraction: (v) => set({
    cellBoundaryFraction: v !== null ? Math.max(0.0001, Math.min(1.0, v)) : null,
  }),

  // ── Color clamp / squish (oob::squish): values outside [low,high] map to palette ends) ──
  cellColorClamp: { low: null, high: null },
  setCellColorClamp: (low, high) => set({ cellColorClamp: { low, high } }),
  edgeColorClamp: { low: null, high: null },
  setEdgeColorClamp: (low, high) => set({ edgeColorClamp: { low, high } }),

  // ── Edge style ────────────────────────────────────────────────────────────
  edgeWidth: 2,
  setEdgeWidth: (v) => set({ edgeWidth: v }),
  showArrowheads: true,
  setShowArrowheads: (v) => set({ showArrowheads: v }),
  // arrowStyle: "full" = filled chevron both sides; "half" = harpoon (outer barb only)
  arrowStyle: "half",
  setArrowStyle: (v) => set({ arrowStyle: v }),
  // arrowheadScale: multiplier on base arrowLen (edgeWidth * 4)
  arrowheadScale: 1.0,
  setArrowheadScale: (v) => set({ arrowheadScale: v }),

  // ── Edge filter + color state ─────────────────────────────────────────────
  // edgeDensity: fraction of available viewport edges to show (0.01–1.0)
  edgeDensity: 0.1,
  setEdgeDensity: (v) => set({ edgeDensity: v }),
  edgeMinStrength: 0,
  setEdgeMinStrength: (v) => set({ edgeMinStrength: v }),

  // mode: 'default' | 'lrm_set' | 'metadata'
  // field: for metadata = column name; unused for other modes
  edgeColorBy: { mode: "lrm_set", field: null },
  setEdgeColorBy: (mode, field) => set({ edgeColorBy: { mode, field } }),

  // Edge palette (for continuous metadata coloring)
  edgeColorPalette: "viridis",
  setEdgeColorPalette: (p) => set({ edgeColorPalette: p }),

  // Directional rendering: show perpendicular offset so A→B ≠ B→A visually
  edgeDirectional: true,
  setEdgeDirectional: (v) => set({ edgeDirectional: v }),
  // edgeOffset: perpendicular separation in image-pixels between A→B and B→A
  edgeOffset: 0,
  setEdgeOffset: (v) => set({ edgeOffset: v }),

  // Show autocrine self-loop rings
  showAutocrine: false,
  setShowAutocrine: (v) => set({ showAutocrine: v }),
  // Autocrine circle geometry — independent from directed-edge line width
  autocrineRadius: 14,
  setAutocrineRadius: (v) => set({ autocrineRadius: v }),
  autocrineLineWidth: 2,
  setAutocrineLineWidth: (v) => set({ autocrineLineWidth: v }),

  // ── LRM mechanism filter ───────────────────────────────────────────────────
  // Shared across panels and keyed on the "ligand|receptor" string, so a
  // mechanism present in both datasets is one checkbox governing both — which is
  // the point of a comparison. The catalogue it is checked against is per panel
  // (panels[i].lrmCatalogue); the sidebar shows the union.
  hiddenLrms: new Set(),
  toggleLrm: (lrm) =>
    set((s) => {
      const next = new Set(s.hiddenLrms);
      if (next.has(lrm)) next.delete(lrm); else next.add(lrm);
      return { hiddenLrms: next };
    }),
  setAllLrmsVisible: () => set({ hiddenLrms: new Set() }),
  // The union across panels: hiddenLrms is shared, so "none" has to cover every
  // mechanism visible in either panel or one side keeps drawing.
  hideAllLrms: () =>
    set((s) => ({
      hiddenLrms: new Set(
        s.panels.flatMap((p) => p.lrmCatalogue)
          .map((e) => e.lrm ?? `${e.ligand}|${e.receptor}`)
      ),
    })),
  setLayerProp: (id, prop, value) =>
    set((s) => ({
      layers: { ...s.layers, [id]: { ...s.layers[id], [prop]: value } },
    })),

  // ── Cell color ────────────────────────────────────────────────────────────
  // cellColorEnabled: drives the color-by layer on/off
  // colorBy.mode: 'off' | 'gene_set' | 'metadata'
  // colorBy.field: metadata column name (only used in metadata mode)
  // cellColorPalette: palette for continuous metadata (viridis/plasma/magma/inferno)
  cellColorEnabled: false,
  setCellColorEnabled: (v) => set({ cellColorEnabled: v }),
  colorBy: { mode: "off", field: null },
  setColorBy: (mode, field) => set({ colorBy: { mode, field } }),
  cellColorPalette: "viridis",
  setCellColorPalette: (p) => set({ cellColorPalette: p }),

  // transcriptFraction: fraction of viewport transcripts to request (0–1).
  // transcriptStats: live shown/total counts for the status display (panel 0).
  transcriptFraction: 0.1,
  setTranscriptFraction: (f) => set({ transcriptFraction: Math.max(0.0001, Math.min(1.0, f)) }),

  // categoryColorOverrides: user-chosen colors for categorical metadata columns.
  // keyed by `${field}::${category}` → [r, g, b, 255].  Reset on dataset change.
  categoryColorOverrides: {},
  setCategoryColorOverride: (field, cat, rgba) => set((s) => ({
    categoryColorOverrides: { ...s.categoryColorOverrides, [`${field}::${cat}`]: rgba },
  })),
  // Bulk-set: merges supplied map on top of existing overrides (used for CSV import).
  mergeCategoryColorOverrides: (map) => set((s) => ({
    categoryColorOverrides: { ...s.categoryColorOverrides, ...map },
  })),
  resetCategoryColorOverrides: () => set({ categoryColorOverrides: {} }),

  // transcriptColorOverrides: user-chosen colors for transcript species.
  // keyed by gene name → [r, g, b, 255].  Reset on dataset change.
  transcriptColorOverrides: {},
  setTranscriptColorOverride: (gene, rgba) => set((s) => ({
    transcriptColorOverrides: { ...s.transcriptColorOverrides, [gene]: rgba },
  })),
  mergeTranscriptColorOverrides: (map) => set((s) => ({
    transcriptColorOverrides: { ...s.transcriptColorOverrides, ...map },
  })),
  resetTranscriptColorOverrides: () => set({ transcriptColorOverrides: {} }),

  // ── Annotations ───────────────────────────────────────────────────────────
  // annotationMode: current interaction mode
  annotationMode: "pan", // "pan" | "region" | "measure"
  setAnnotationMode: (mode) => set({ annotationMode: mode }),

  // activeRegion: vertices of the polygon currently being drawn (image px)
  activeRegion: [],
  addRegionPoint: (pt) => set((s) => ({ activeRegion: [...s.activeRegion, pt] })),
  cancelActiveRegion: () => set({ activeRegion: [] }),

  // regions: completed annotation polygons
  // each: { id, points [[x,y],...], selectedCellIds [str,...], color [r,g,b] }
  regions: [],
  commitRegion: (region) =>
    set((s) => ({ regions: [...s.regions, region], activeRegion: [] })),
  removeRegion: (id) =>
    set((s) => ({ regions: s.regions.filter((r) => r.id !== id) })),

  // measurements: [{id, p1:[x,y], p2:[x,y], distPx}]
  measurements: [],
  addMeasurement: (m) => set((s) => ({ measurements: [...s.measurements, m] })),
  removeMeasurement: (id) =>
    set((s) => ({ measurements: s.measurements.filter((m) => m.id !== id) })),

  clearAnnotations: () =>
    set({ activeRegion: [], regions: [], measurements: [] }),

  // ── Rendering / loading state ─────────────────────────────────────────────
  // loadingKeys: Set of string keys currently in flight (one entry per panel).
  // The status badge is visible whenever loadingKeys.size > 0.
  loadingKeys: new Set(),
  setLoadingKey: (key, loading) => set((s) => {
    const next = new Set(s.loadingKeys);
    if (loading) next.add(key); else next.delete(key);
    return { loadingKeys: next };
  }),

  // ── Transcript species filter ──────────────────────────────────────────────
  // selectedGenes: null = no filter (show all); Set<string> = allowlist (show only these).
  // The selection is dataset-scoped and persists across pan/zoom.
  selectedGenes: null,
  setSelectedGenes: (genes) => set({ selectedGenes: genes }),
  toggleSelectedGene: (gene) =>
    set((s) => {
      // The universe of genes is the union across the visible panels — the same
      // list the picker renders.
      const all = [...new Set(
        s.panels.slice(0, s.panelCount).flatMap((p) => p.allGenes ?? [])
      )];

      if (s.selectedGenes === null) {
        // "Show all" renders EVERY checkbox ticked, so a click here means
        // "uncheck this one" — exclude it and keep the rest.
        //
        // This used to start an allowlist containing only the clicked gene, the
        // exact opposite of what the click meant. On a 480-gene Xenium panel
        // that silently narrowed the transcript layer from 200,000 dots to ~360,
        // which reads as "transcripts are broken" rather than "you filtered to
        // one gene". The checkbox said checked; the click has to mean uncheck.
        if (all.length === 0) return {};          // list not loaded yet — ignore
        return { selectedGenes: new Set(all.filter((g) => g !== gene)) };
      }

      const next = new Set(s.selectedGenes);
      if (next.has(gene)) next.delete(gene); else next.add(gene);
      // Back to everything selected is the same as no filter. Collapsing keeps
      // the semantics single-valued and keeps hundreds of gene names out of the
      // request URL.
      if (all.length > 0 && next.size === all.length) return { selectedGenes: null };
      return { selectedGenes: next };
    }),
}));

// Dev-only handle for debugging from the browser console.
if (typeof window !== "undefined" && import.meta.env?.DEV) window.__tpStore = useStore;
