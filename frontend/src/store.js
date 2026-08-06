import { create } from "zustand";

const API = import.meta.env.VITE_API_URL ?? "/api";

export const useStore = create((set, get) => ({
  // ── Dataset ────────────────────────────────────────────────────────────────
  apiBase: API,
  dataset: null,             // initialized from /spatial/datasets on first load
  activeImage: "morphology", // which OME-TIFF is loaded as the background

  // edgeFile: which edge-source parquet the app renders. "edges.parquet" is the
  // legacy top-level default; multiple sets live under the dataset's edges/ folder
  // and are identified as "edges/<name>.parquet" (see issue #46). Applies to all
  // open viewer panels — the single sidebar drives every panel equally.
  edgeFile: "edges.parquet",

  // Switching datasets resets all edge-file-scoped state so a stale LRM catalogue,
  // filter, selection, or color range from the previous dataset never leaks through.
  // activeImage is cleared too: image names are platform-specific ("morphology" on
  // Xenium, "Roi1_DAPI" on seqFISH), and DatasetPicker only learns the new dataset's
  // image list asynchronously. Without this, OSD spends that window requesting the
  // previous dataset's image from the new one and logging 404s.
  setDataset: (dataset) => set({
    dataset, activeImage: null,
    selectedGenes: null, allGenes: [], genesLoaded: false,
    platformCapabilities: null, categoryColorOverrides: {}, transcriptColorOverrides: {},
    edgeFile: "edges.parquet", lrmCatalogue: [], hiddenLrms: new Set(),
    selectedEdge: null, edgeColorRange: { vmin: null, vmax: null },
    edgeColorClamp: { low: null, high: null },
  }),
  setActiveImage: (activeImage) => set({ activeImage }),

  // Switching the edge file resets the same edge-scoped state: the LRM catalogue,
  // hidden-LRM filter, current selection, and auto-computed color range/clamp are
  // all specific to a given edges.parquet and must be re-derived for the new file.
  setEdgeFile: (edgeFile) => set({
    edgeFile, lrmCatalogue: [], hiddenLrms: new Set(), selectedEdge: null,
    edgeColorRange: { vmin: null, vmax: null }, edgeColorClamp: { low: null, high: null },
  }),

  // ── Platform capabilities (fetched from /spatial/{dataset}/info) ──────────
  // null = not yet loaded; object = { has_morphology, has_transcripts, has_boundaries, unit_label }
  platformCapabilities: null,
  setPlatformCapabilities: (caps) => set({ platformCapabilities: caps }),

  // ── Image dimensions (from DZI descriptor, set when OSD opens) ───────────
  imageSize: { w: null, h: null },
  setImageSize: (w, h) => set({ imageSize: { w, h } }),

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
  // cellBoundaryStats: live sampled/total counts for the status display.
  cellBoundaryStats: { shown: 0, total: 0 },
  setCellBoundaryStats: (shown, total) => set({ cellBoundaryStats: { shown, total } }),

  // ── Color range cache (updated from Viewer hooks for legend display) ──────
  cellColorRange: { vmin: null, vmax: null },
  setCellColorRange: (vmin, vmax) => set({ cellColorRange: { vmin, vmax } }),
  edgeColorRange: { vmin: null, vmax: null },
  setEdgeColorRange: (vmin, vmax) => set({ edgeColorRange: { vmin, vmax } }),

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

  // Selected edge (for info panel): "SendingCell|ReceivingCell" string or null
  selectedEdge: null,
  setSelectedEdge: (edge) => set({ selectedEdge: edge }),

  // ── LRM mechanism filter ───────────────────────────────────────────────────
  // hiddenLrms: Set of "ligand|receptor" string IDs to suppress.
  // lrmCatalogue: [{lrm_id, lrm, ligand, receptor}] loaded once from the backend.
  hiddenLrms: new Set(),
  lrmCatalogue: [],
  setLrmCatalogue: (cat) => set({ lrmCatalogue: cat }),
  toggleLrm: (lrm) =>
    set((s) => {
      const next = new Set(s.hiddenLrms);
      if (next.has(lrm)) next.delete(lrm); else next.add(lrm);
      return { hiddenLrms: next };
    }),
  setAllLrmsVisible: () => set({ hiddenLrms: new Set() }),
  hideAllLrms: () =>
    set((s) => ({ hiddenLrms: new Set(s.lrmCatalogue.map((e) => e.lrm ?? `${e.ligand}|${e.receptor}`)) })),
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

  // allGenes: full gene panel, fetched once on dataset change
  allGenes: [],
  genesLoaded: false,
  setAllGenes: (genes) => set({ allGenes: genes }),
  setGenesLoaded: (loaded) => set({ genesLoaded: loaded }),

  // transcriptFraction: fraction of viewport transcripts to request (0–1).
  // transcriptStats: live shown/total counts for the status display (panel 0).
  transcriptFraction: 0.1,
  setTranscriptFraction: (f) => set({ transcriptFraction: Math.max(0.0001, Math.min(1.0, f)) }),
  transcriptStats: { shown: 0, total: 0 },
  setTranscriptStats: (shown, total) => set({ transcriptStats: { shown, total } }),

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

  // ── Selected cell ─────────────────────────────────────────────────────────
  selectedCell: null,
  setSelectedCell: (cell) => set({ selectedCell: cell }),

  // ── Annotations ───────────────────────────────────────────────────────────
  // pixelSize: µm per image pixel, fetched from /spatial/{dataset}/info
  pixelSize: 1.0,
  setPixelSize: (v) => set({ pixelSize: v }),

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
      if (s.selectedGenes === null) {
        // First selection from "show all" state: start an allowlist with just this gene.
        return { selectedGenes: new Set([gene]) };
      }
      const next = new Set(s.selectedGenes);
      if (next.has(gene)) next.delete(gene); else next.add(gene);
      return { selectedGenes: next };
    }),
}));

// Dev-only handle for debugging from the browser console.
if (typeof window !== "undefined" && import.meta.env?.DEV) window.__tpStore = useStore;
