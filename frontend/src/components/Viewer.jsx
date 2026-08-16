/**
 * Main viewer component.
 * OpenSeadragon handles pan/zoom of the morphology tile pyramid.
 * A deck.gl OrthographicView canvas sits on top, coordinate-synced to OSD,
 * rendering all data layers (transcripts, cell segments, edges).
 *
 * Split-screen: Viewer renders one or two ViewerPanel instances side-by-side.
 * All layer settings are shared (global store); each panel has its own OSD
 * instance, deck.gl canvas, and viewport-bounded data fetches.
 *
 * Coordinate systems:
 *   Image pixel space   — what all data uses (x: 0–img_w, y: 0–img_h)
 *   OSD normalised      — [0,1]² image space (x / img_w, y / img_h)
 *   deck.gl             — OrthographicView in image pixel space
 *   Screen              — display pixels (handled by deck.gl internally)
 */
import React, { useEffect, useMemo, useRef, useState, useCallback } from "react";
import OpenSeadragon from "openseadragon";
import DeckGL from "@deck.gl/react";
import { OrthographicView } from "@deck.gl/core";
import { ScatterplotLayer, SolidPolygonLayer, PathLayer, LineLayer } from "@deck.gl/layers";

import { useStore } from "../store";
import { usePanelSettings, PanelIndexProvider } from "../hooks/usePanelSettings";
import { useTranscripts } from "../hooks/useTranscripts";
import { useCellBoundaries } from "../hooks/useCellBoundaries";
import { useCellColors } from "../hooks/useCellColors";
import { useEdges } from "../hooks/useEdges";
import { useEdgeColors } from "../hooks/useEdgeColors";
import { geneColor } from "../utils/geneColor";
import AnnotationToolbar from "./AnnotationToolbar";
import { DatasetPicker } from "./DatasetPicker";
import EdgeInfoPanel from "./EdgeInfoPanel";
import RenderingStatus from "./RenderingStatus";

// Default edge color when no color mode is active
const DEFAULT_EDGE_COLOR = [255, 150, 0, 160];
const DEFAULT_AUTOCRINE_COLOR = [255, 150, 0, 200];

const VIEW_ID = "main";

// Matches pyramid.BLANK_IMAGE_NAME: the placeholder canvas served for datasets
// with no morphology of their own. Not a real image, so it is not shown as one.
const BLANK_IMAGE_NAME = "__blank__";

// ── Rotation helpers (pure, module-level) ─────────────────────────────────

/**
 * Build a column-major 4×4 deck.gl modelMatrix for CW rotation by angleDeg
 * around pivot (cx, cy) in image pixel space.  Returns null at 0° (identity).
 */
function makeRotMatrix(angleDeg, cx, cy) {
  if (!angleDeg) return null;
  const theta = (angleDeg * Math.PI) / 180;
  const cos = Math.cos(theta), sin = Math.sin(theta);
  const tx = cx * (1 - cos) + cy * sin;
  const ty = cy * (1 - cos) - cx * sin;
  // prettier-ignore
  return [cos, sin, 0, 0, -sin, cos, 0, 0, 0, 0, 1, 0, tx, ty, 0, 1];
}

/**
 * Expand a viewport bbox to fully cover the corners of a rotated rectangle.
 * At 0°/180° the bbox is unchanged; at other angles the box grows outward.
 */
function rotatedBbox(cx, cy, halfW, halfH, angleDeg) {
  if (!angleDeg || angleDeg === 180) {
    return { xmin: cx - halfW, ymin: cy - halfH, xmax: cx + halfW, ymax: cy + halfH };
  }
  const theta = (angleDeg * Math.PI) / 180;
  const ac = Math.abs(Math.cos(theta)), as = Math.abs(Math.sin(theta));
  const bw = halfW * ac + halfH * as;
  const bh = halfW * as + halfH * ac;
  return { xmin: cx - bw, ymin: cy - bh, xmax: cx + bw, ymax: cy + bh };
}

/**
 * Inverse-rotate a point from rotated view space back to original image coords.
 * Undoes the same CW rotation around (cx, cy) that makeRotMatrix applies.
 */
function inverseRotate(ix, iy, cx, cy, angleDeg) {
  if (!angleDeg) return [ix, iy];
  const theta = (angleDeg * Math.PI) / 180;
  const cos = Math.cos(theta), sin = Math.sin(theta);
  const dx = ix - cx, dy = iy - cy;
  return [cx + dx * cos + dy * sin, cy - dx * sin + dy * cos];
}

/**
 * Forward-rotate a point from original image coords into rotated view space.
 * Used to compute the screen positions of annotation labels after rotation.
 */
function forwardRotate(x, y, cx, cy, angleDeg) {
  if (!angleDeg) return [x, y];
  const theta = (angleDeg * Math.PI) / 180;
  const cos = Math.cos(theta), sin = Math.sin(theta);
  const dx = x - cx, dy = y - cy;
  return [cx + dx * cos - dy * sin, cy + dx * sin + dy * cos];
}

// ── ViewerPanel ───────────────────────────────────────────────────────────────
// One instance per visible panel. Each has its own OSD + deck.gl + viewport.
// All layer/color/filter settings come from the shared Zustand store.

function ViewerPanel({ panelIndex }) {
  const containerRef = useRef(null);
  const viewerRef = useRef(null);
  const deckRef = useRef(null);
  const syncRef = useRef(null);
  const deckViewStateRef = useRef(null);
  const measureFirstRef = useRef(null);
  const clickTimerRef = useRef(null);
  const hoverTimerRef = useRef(null);
  const cellPolygonsRef = useRef([]);
  const [cursorPos, setCursorPos] = useState(null);
  const [hoveredTranscript, setHoveredTranscript] = useState(null);
  // Incremented every time this panel's OSD fires "open" — used to reliably
  // re-apply morphology visibility after each OSD (re)initialization.
  const [osdOpenCount, setOsdOpenCount] = useState(0);

  // deck.gl modelMatrix for the current rotation + viewport pivot.
  // Recomputed in syncDeckFromOSD on every viewport change and on rotation change.
  const [rotModelMatrix, setRotModelMatrix] = useState(null);

  // ── Shared settings: one sidebar drives every panel ───────────────────────
  const {
    apiBase,
    setViewport, setViewportActual,
    layers: layerState,
    cellColorEnabled, colorBy, cellColorPalette, categoryColorOverrides,
    selectedGenes, transcriptColorOverrides,
    edgeMinStrength, edgeDensity,
    edgeColorBy, edgeColorPalette, edgeDirectional, showAutocrine,
    edgeWidth, showArrowheads, arrowStyle, arrowheadScale,
    edgeOffset,
    autocrineRadius, autocrineLineWidth,
    hiddenLrms,
    cellColorClamp, edgeColorClamp, setEdgeColorClamp, linkColorScale,
    categoricalOverrides, cellFilter, edgeFilter,
    annotationMode,
    clearZoomMatch,
    activeRegion, addRegionPoint, cancelActiveRegion, commitRegion,
    removeRegion,
    addMeasurement,
    panelCount,
    transcriptFraction,
    cellBoundaryFraction,
    setLoadingKey,
    panelRotations, setPanelRotation,
    selection, setSelectedCell, setSelectedEdge,
    patchPanel,
  } = usePanelSettings(panelIndex);

  // ── This panel's dataset-bound state ──────────────────────────────────────
  // Everything derived from *which dataset this panel shows*: image dimensions,
  // pixel size, capabilities, gene panel, LRM vocabulary, value ranges.
  const panel = useStore((s) => s.panels[panelIndex]);
  const {
    dataset, activeImage, imageSize, platformCapabilities,
    pixelSize, edgeFile, lrmCatalogue, allGenes,
  } = panel;
  // Convenience: write one or more fields of *this* panel.
  const patch = useCallback((p) => patchPanel(panelIndex, p), [patchPanel, panelIndex]);

  // The selection belongs to whichever panel produced it; only that panel
  // highlights it, so a click in panel 1 does not light up panel 0.
  const selectedCell = selection?.panelIndex === panelIndex && selection.kind === "cell"
    ? selection.cell : null;
  const selectedEdge = selection?.panelIndex === panelIndex && selection.kind === "edge"
    ? selection.edge : null;

  const panelRotation = panelRotations[panelIndex] ?? 0;

  // Per-panel viewport from store
  const viewport = useStore((s) => s.viewports[panelIndex]);

  // Annotations belong to the panel that drew them. Subscribe to the raw arrays
  // so a change re-renders, then narrow — the selectors on the store are plain
  // functions and would not themselves trigger an update.
  const allRegions = useStore((s) => s.regions);
  const allMeasurements = useStore((s) => s.measurements);
  const activeRegionPanel = useStore((s) => s.activeRegionPanel);
  const regions = useMemo(
    () => allRegions.filter((r) => (r.panelIndex ?? 0) === panelIndex),
    [allRegions, panelIndex]);
  const measurements = useMemo(
    () => allMeasurements.filter((m) => (m.panelIndex ?? 0) === panelIndex),
    [allMeasurements, panelIndex]);
  // The in-progress outline is only drawn by the panel actually drawing it.
  const drawingHere = activeRegionPanel === null || activeRegionPanel === panelIndex;

  // Zoom-match signal — fired when the Match button is clicked in either panel
  const pendingZoomMatch = useStore((s) => s.pendingZoomMatch);

  // ── deck.gl view state (synced from OSD) ─────────────────────────────────
  const [deckViewState, setDeckViewState] = useState({
    target: [0, 0, 0],
    zoom: 0,
    minZoom: -10,
    maxZoom: 20,
  });

  const syncDeckFromOSD = useCallback(
    (osd) => {
      const imgW = imageSize.w;
      const imgH = imageSize.h;
      if (!imgW || !imgH || !containerRef.current) return;

      const bounds = osd.viewport.getBoundsNoRotate();
      const cW = containerRef.current.offsetWidth;

      const cx = (bounds.x + bounds.width / 2) * imgW;
      const cy = (bounds.y + bounds.height / 2) * imgW;
      const zoom = Math.log2(cW / (bounds.width * imgW));

      setDeckViewState((prev) => ({ ...prev, target: [cx, cy, 0], zoom }));

      // Store the true OSD bounds (un-expanded) for the ⇔ Match zoom feature,
      // which needs the actual visible width/height rather than the padded fetch bbox.
      const actualBbox = {
        xmin: bounds.x * imgW,
        ymin: bounds.y * imgW,
        xmax: (bounds.x + bounds.width)  * imgW,
        ymax: (bounds.y + bounds.height) * imgW,
      };
      setViewportActual(actualBbox, panelIndex);

      // Expand bbox to cover the full rotated viewport rectangle.
      // At 0° this is identical to the un-rotated bbox; at other angles
      // the box grows so all visible data is fetched.
      const halfW = (bounds.width  * imgW) / 2;
      const halfH = (bounds.height * imgW) / 2;
      setViewport(rotatedBbox(cx, cy, halfW, halfH, panelRotation), panelIndex);

      // Recompute the deck.gl layer rotation matrix around the new viewport pivot.
      setRotModelMatrix(makeRotMatrix(panelRotation, cx, cy));
    },
    [imageSize, setViewport, setViewportActual, panelIndex, panelRotation, setRotModelMatrix]
  );
  useEffect(() => { syncRef.current = syncDeckFromOSD; }, [syncDeckFromOSD]);
  useEffect(() => { deckViewStateRef.current = deckViewState; }, [deckViewState]);

  // Platform info + capabilities, per panel. This used to be panel-0-only
  // because both panels showed the same dataset; now each panel needs its own —
  // pixel_size drives measurement and zoom matching, and capabilities decide
  // which layers this panel can serve at all.
  useEffect(() => {
    if (!dataset) return;
    let cancelled = false;
    fetch(`${apiBase}/spatial/${dataset}/info`)
      .then((r) => r.ok ? r.json() : null)
      .then((info) => {
        if (cancelled || !info) return;
        patch({
          ...(info.pixel_size ? { pixelSize: parseFloat(info.pixel_size) } : {}),
          ...(info.capabilities ? { platformCapabilities: info.capabilities } : {}),
        });
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [apiBase, dataset, patch]);

  // Gene panel for this panel's dataset. Needed here, not just in the sidebar:
  // gene-set colouring sums whatever genes this panel actually measures, so a
  // shared list from the other panel would sum genes it does not have.
  useEffect(() => {
    if (!dataset) return;
    let cancelled = false;
    fetch(`${apiBase}/spatial/${dataset}/genes`)
      .then((r) => (r.ok ? r.json() : []))
      .then((g) => { if (!cancelled && Array.isArray(g)) patch({ allGenes: g, genesLoaded: true }); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [apiBase, dataset, patch]);

  // LRM catalogue for this panel's edge source. Fetched here rather than in the
  // sidebar because it is per (dataset, edge file): the sidebar's mechanism
  // checklist is the union of what the panels loaded.
  useEffect(() => {
    if (!dataset) return;
    let cancelled = false;
    fetch(`${apiBase}/edges/${dataset}/lrm-catalogue?edge_file=${encodeURIComponent(edgeFile)}`)
      .then((r) => (r.ok ? r.json() : []))
      .then((cat) => { if (!cancelled && Array.isArray(cat)) patch({ lrmCatalogue: cat }); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [apiBase, dataset, edgeFile, patch]);

  // ── OpenSeadragon init ────────────────────────────────────────────────────
  const dziUrl = `${apiBase}/tiles/${dataset}/dzi/${activeImage}.dzi`;

  useEffect(() => {
    if (!containerRef.current) return;
    if (viewerRef.current) {
      viewerRef.current.destroy();
      viewerRef.current = null;
    }
    // activeImage is null between a dataset switch and DatasetPicker resolving the
    // new dataset's image list. Opening OSD here would request "null.dzi".
    if (!dataset || !activeImage) return;

    const viewer = OpenSeadragon({
      element: containerRef.current,
      tileSources: dziUrl,
      prefixUrl: "",
      showNavigationControl: true,
      showNavigator: true,
      navigatorPosition: "BOTTOM_RIGHT",
      navigatorSizeRatio: 0.18,
      imageLoaderLimit: 8,
      maxZoomPixelRatio: 4,
      minZoomImageRatio: 0.5,
      defaultZoomLevel: 0,
      visibilityRatio: 0.3,
      immediateRender: false,
      smoothTileEdgesMinZoom: Infinity,
      placeholderFillStyle: "#1a1a1a",
      gestureSettingsMouse: {
        scrollToZoom: true,
        clickToZoom: false,
        dblClickToZoom: true,
        pinchToZoom: true,
      },
    });

    viewer.addHandler("open", () => {
      const src = viewer.world.getItemAt(0);
      if (src) {
        const sz = src.getContentSize();
        patch({ imageSize: { w: sz.x, h: sz.y } });
        setOsdOpenCount((c) => c + 1); // always triggers morphology opacity effect
      }
    });

    viewer.addHandler("open-failed", (e) =>
      console.error(`OSD panel ${panelIndex} open failed:`, e.message)
    );

    viewer.addHandler("animation", () => syncRef.current?.(viewer));
    viewer.addHandler("animation-finish", () => syncRef.current?.(viewer));

    viewerRef.current = viewer;
    if (import.meta.env.DEV) window[`__osd${panelIndex}`] = viewer;
    return () => {
      viewer.destroy();
      viewerRef.current = null;
      if (import.meta.env.DEV) delete window[`__osd${panelIndex}`];
    };
  }, [dziUrl]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (viewerRef.current && imageSize.w) {
      syncDeckFromOSD(viewerRef.current);
    }
  }, [imageSize, syncDeckFromOSD]);

  // ── Sync morphology layer visibility/opacity to OSD ──────────────────────
  const morphologyVisible = layerState.morphology?.visible ?? true;
  const morphologyOpacity = layerState.morphology?.opacity ?? 1.0;
  useEffect(() => {
    const item = viewerRef.current?.world?.getItemAt(0);
    if (!item) return;
    item.setOpacity(morphologyVisible ? morphologyOpacity : 0);
    const navItem = viewerRef.current?.navigator?.world?.getItemAt(0);
    if (navItem) navItem.setOpacity(0.35);
  }, [morphologyVisible, morphologyOpacity, osdOpenCount]);

  // ── Rotation — keep OSD tiles and deck.gl modelMatrix in sync ────────────
  // Fires when rotation angle changes OR when OSD reinitialises (osdOpenCount).
  useEffect(() => {
    viewerRef.current?.viewport?.setRotation(panelRotation);
    // Re-run syncDeckFromOSD via the stable ref so the modelMatrix pivot and
    // expanded bbox are immediately recomputed for the new angle.
    if (viewerRef.current) syncRef.current?.(viewerRef.current);
  }, [panelRotation, osdOpenCount]); // eslint-disable-line

  // ── Match zoom (from "⇔ Match" button) ───────────────────────────────────
  // The source panel sets pendingZoomMatch = { fromPanel }. The target panel
  // (fromPanel !== panelIndex) keeps its own center but adopts the source's zoom.
  useEffect(() => {
    if (!pendingZoomMatch) return;
    const { fromPanel } = pendingZoomMatch;
    if (fromPanel === panelIndex) return; // I'm the source — ignore
    if (!viewerRef.current || !imageSize.w) return;

    // Use viewportActual (un-expanded OSD bounds) so rotation-padded fetch
    // bboxes don't skew the matched zoom level.
    const srcVp = useStore.getState().viewportActual[fromPanel];
    if (!srcVp) return;

    // Match PHYSICAL scale — microns across the viewport — not fraction of image.
    //
    // This used to divide both panels' widths by the local imgW, i.e. it matched
    // "the same proportion of the picture". With one dataset in both panels that
    // was the same thing. With two it is meaningless: 20% of a 6.5 mm Visium
    // capture area and 20% of a 107 µm seqFISH ROI differ by a factor of 60, and
    // the panels would look matched while being nothing of the sort.
    //
    // Converting through each panel's own µm/px makes "match" mean what a
    // scalebar would: the same number of microns spans the same screen width.
    const srcPanel = useStore.getState().panels[fromPanel];
    const srcPixelSize = srcPanel?.pixelSize || 1;
    const srcImgW = srcPanel?.imageSize?.w;
    if (!srcImgW) return;

    // Source extent in microns, then back into *my* image pixels, then into the
    // OSD-normalised units fitBounds expects (OSD normalises by image width).
    const srcMicronsW = (srcVp.xmax - srcVp.xmin) * srcPixelSize;
    const srcMicronsH = (srcVp.ymax - srcVp.ymin) * srcPixelSize;
    const myPixelSize = pixelSize || 1;
    const imgW = imageSize.w;
    const srcW = (srcMicronsW / myPixelSize) / imgW;
    const srcH = (srcMicronsH / myPixelSize) / imgW;
    if (!isFinite(srcW) || !isFinite(srcH) || srcW <= 0) return;

    // Keep my current center, adopt the source's physical scale
    const center = viewerRef.current.viewport.getCenter(true);
    const newBounds = new OpenSeadragon.Rect(
      center.x - srcW / 2,
      center.y - srcH / 2,
      srcW,
      srcH,
    );
    viewerRef.current.viewport.fitBounds(newBounds, false); // animated
    clearZoomMatch();
  }, [pendingZoomMatch]); // eslint-disable-line

  // ── Screenshot ───────────────────────────────────────────────────────────
  const handleScreenshot = useCallback(() => {
    const osdCanvas = viewerRef.current?.drawer?.canvas;
    const deckCanvas = deckRef.current?.deck?.canvas;
    if (!osdCanvas) return;
    const w = osdCanvas.width;
    const h = osdCanvas.height;
    const out = document.createElement("canvas");
    out.width = w;
    out.height = h;
    const ctx = out.getContext("2d");
    ctx.drawImage(osdCanvas, 0, 0);
    if (deckCanvas) ctx.drawImage(deckCanvas, 0, 0);
    const link = document.createElement("a");
    link.download = `tissueplex_${panelCount > 1 ? `panel${panelIndex + 1}_` : ""}${Date.now()}.png`;
    link.href = out.toDataURL("image/png");
    link.click();
  }, [panelIndex, panelCount]);

  // ── Cell click picking ────────────────────────────────────────────────────
  const handleViewerClick = useCallback((e) => {
    if (!deckRef.current || !containerRef.current) return;
    const rect = containerRef.current.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;

    if (e.shiftKey) {
      const edgeInfo = deckRef.current.pickObject({ x, y, radius: 8, layerIds: ["edges-directed", "edges-autocrine", "tissue-graph"] });
      if (edgeInfo?.object) {
        setSelectedEdge(edgeInfo.object.edge, panelIndex);
        return;
      }
      const cellInfo = deckRef.current.pickObject({ x, y, radius: 6, layerIds: ["cell-segments-fill"] });
      if (cellInfo?.object) {
        setSelectedCell(cellInfo.object, panelIndex);
      } else {
        setSelectedCell(null, panelIndex);
      }
      return;
    }

    const cellInfo = deckRef.current.pickObject({ x, y, radius: 6, layerIds: ["cell-segments-fill"] });
    if (cellInfo?.object) {
      setSelectedCell(cellInfo.object, panelIndex);
      return;
    }
    setSelectedCell(null, panelIndex);

    const edgeInfo = deckRef.current.pickObject({ x, y, radius: 8, layerIds: ["edges-directed", "edges-autocrine", "tissue-graph"] });
    if (edgeInfo?.object) {
      setSelectedEdge(edgeInfo.object.edge, panelIndex);
    } else {
      setSelectedEdge(null, panelIndex);
    }
  }, [setSelectedCell, setSelectedEdge]);

  // ── Transcript hover tooltip ──────────────────────────────────────────────
  const handleTranscriptHoverMove = useCallback((e) => {
    if (!deckRef.current || !containerRef.current) return;
    const rect = containerRef.current.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    clearTimeout(hoverTimerRef.current);
    hoverTimerRef.current = setTimeout(() => {
      const info = deckRef.current.pickObject({ x, y, radius: 6, layerIds: ["transcripts"] });
      if (info?.object?.feature_name) {
        setHoveredTranscript({ gene: info.object.feature_name, qv: info.object.qv, x, y });
      } else {
        setHoveredTranscript(null);
      }
    }, 30);
  }, []);

  const handleTranscriptHoverLeave = useCallback(() => {
    clearTimeout(hoverTimerRef.current);
    setHoveredTranscript(null);
  }, []);

  // ── Screen ↔ image-pixel coordinate conversion ───────────────────────────
  const screenToData = useCallback((sx, sy) => {
    const vs = deckViewStateRef.current;
    if (!vs || !containerRef.current) return null;
    const { width: cW, height: cH } = containerRef.current.getBoundingClientRect();
    const scale = Math.pow(2, vs.zoom);
    // Project screen → rotated view space (same as un-rotated image space for deck.gl)
    const ix = vs.target[0] + (sx - cW / 2) / scale;
    const iy = vs.target[1] + (sy - cH / 2) / scale;
    // Inverse-rotate back to original image coordinates
    return inverseRotate(ix, iy, vs.target[0], vs.target[1], panelRotation);
  }, [panelRotation]);

  // ── Annotation overlay events ─────────────────────────────────────────────
  const handleOverlayMouseMove = useCallback((e) => {
    const rect = containerRef.current.getBoundingClientRect();
    const pt = screenToData(e.clientX - rect.left, e.clientY - rect.top);
    setCursorPos(pt);
  }, [screenToData]);

  const handleOverlayMouseLeave = useCallback(() => setCursorPos(null), []);

  const handleOverlaySingleClick = useCallback((sx, sy) => {
    const pt = screenToData(sx, sy);
    if (!pt) return;
    if (annotationMode === "region") {
      addRegionPoint(pt, panelIndex);
    } else if (annotationMode === "measure") {
      if (!measureFirstRef.current) {
        measureFirstRef.current = pt;
      } else {
        const p1 = measureFirstRef.current;
        const p2 = pt;
        const dx = p2[0] - p1[0], dy = p2[1] - p1[1];
        const distPx = Math.sqrt(dx * dx + dy * dy);
        addMeasurement({ id: Date.now(), p1, p2, distPx }, panelIndex);
        measureFirstRef.current = null;
      }
    }
  }, [annotationMode, addRegionPoint, addMeasurement, screenToData, panelIndex]);

  const handleOverlayClick = useCallback((e) => {
    if (annotationMode === "pan") return;
    const rect = containerRef.current.getBoundingClientRect();
    const sx = e.clientX - rect.left;
    const sy = e.clientY - rect.top;
    clearTimeout(clickTimerRef.current);
    clickTimerRef.current = setTimeout(() => handleOverlaySingleClick(sx, sy), 220);
  }, [annotationMode, handleOverlaySingleClick]);

  const handleOverlayDblClick = useCallback((e) => {
    if (annotationMode !== "region") return;
    clearTimeout(clickTimerRef.current);
    if (activeRegion.length < 3) {
      cancelActiveRegion();
      return;
    }
    const poly = activeRegion;
    const selectedCellIds = cellPolygonsRef.current
      .filter((cell) => {
        const [px, py] = cell.polygon[0];
        let inside = false;
        for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
          const [xi, yi] = poly[i], [xj, yj] = poly[j];
          if ((yi > py) !== (yj > py) && px < ((xj - xi) * (py - yi)) / (yj - yi) + xi) {
            inside = !inside;
          }
        }
        return inside;
      })
      .map((c) => c.cell_id);

    const PALETTE = [
      [255, 200, 0], [0, 200, 255], [255, 80, 200],
      [80, 255, 120], [255, 120, 60], [180, 100, 255],
    ];
    const color = PALETTE[regions.length % PALETTE.length];
    commitRegion({ id: Date.now(), points: poly, selectedCellIds, color }, panelIndex);
  }, [annotationMode, activeRegion, cancelActiveRegion, commitRegion, regions, panelIndex]);

  // ── Data fetching ─────────────────────────────────────────────────────────
  const transcriptsVisible = layerState.transcripts?.visible ?? true;
  const transcriptsOpacity = layerState.transcripts?.opacity ?? 0.8;
  const cellSegmentsVisible = layerState.cellSegments?.visible ?? true;
  const cellSegmentsOpacity = layerState.cellSegments?.opacity ?? 0.6;
  const cellOutlineOpacity = layerState.cellSegments?.outlineOpacity ?? 0.8;
  const edgesVisible = layerState.edges?.visible ?? true;
  const edgesOpacity = layerState.edges?.opacity ?? 0.9;
  const tissueGraphVisible = layerState.tissueGraph?.visible ?? true;
  const tissueGraphOpacity = layerState.tissueGraph?.opacity ?? 0.25;

  const hasTranscripts = platformCapabilities?.has_transcripts ?? true;
  const hasBoundaries = platformCapabilities?.has_boundaries ?? true;

  const { transcripts, total: transcriptTotal, loading: transcriptsLoading } = useTranscripts(
    apiBase, dataset, viewport, imageSize, transcriptsVisible && hasTranscripts, transcriptFraction, selectedGenes, allGenes
  );

  // visibleTranscripts: server already filtered by selectedGenes, so this is a no-op
  // when a gene filter is active — kept as a safety net and for semantic clarity.
  const visibleTranscripts = selectedGenes === null
    ? transcripts
    : transcripts.filter((t) => selectedGenes.has(t.feature_name));

  // Expose live shown/total counts to the LayerPanel via the store (panel 0 only).
  // Use visibleTranscripts.length so the stat always reflects the selected species.
  useEffect(() => {
    patch({ transcriptStats: { shown: visibleTranscripts.length, total: transcriptTotal } });
  }, [visibleTranscripts.length, transcriptTotal]); // eslint-disable-line

  const {
    cells: cellPolygons,
    total: cellBoundaryTotal,
    loading: cellBoundariesLoading,
  } = useCellBoundaries(
    apiBase, dataset, viewport, imageSize, cellSegmentsVisible && hasBoundaries,
    cellBoundaryFraction, cellFilter
  );
  useEffect(() => { cellPolygonsRef.current = cellPolygons; }, [cellPolygons]);

  // Expose live cell boundary counts to LayerPanel (panel 0 only).
  useEffect(() => {
    patch({ cellBoundaryStats: { shown: cellPolygons.length, total: cellBoundaryTotal } });
  }, [cellPolygons.length, cellBoundaryTotal]); // eslint-disable-line

  const { edges, loading: edgesLoading } = useEdges(
    apiBase, dataset, viewport, imageSize, edgesVisible || tissueGraphVisible,
    edgeMinStrength, hiddenLrms, lrmCatalogue, edgeDensity, edgeFile,
    cellFilter, edgeFilter
  );

  // Explicit categorical/continuous choice for the active color-by column, or
  // null (auto-detect) when the user has not overridden it — issue #35.
  const cellCategorical = categoricalOverrides[`cell::${colorBy?.field}`] ?? null;
  const edgeCategorical = categoricalOverrides[`edge::${edgeColorBy?.field}`] ?? null;

  // ── Shared colour scale ───────────────────────────────────────────────────
  // With two panels showing two datasets, letting each auto-range to its own
  // data produces two viridis pictures that look comparable and are not. When
  // linked (the default) both panels clamp to the union of the two ranges, so
  // the single legend is true for everything on screen. An explicit clamp from
  // the sliders always wins — the user asked for that range specifically.
  const allPanels = useStore((s) => s.panels);
  const linkedRange = useMemo(() => {
    if (!linkColorScale || panelCount < 2) return null;
    const rs = allPanels.slice(0, panelCount)
      .map((p) => p.cellColorRange).filter((r) => r && r.vmin != null && r.vmax != null);
    if (rs.length < 2) return null;
    return { lo: Math.min(...rs.map((r) => r.vmin)), hi: Math.max(...rs.map((r) => r.vmax)) };
  }, [allPanels, panelCount, linkColorScale]);

  const effectiveCellClamp = useMemo(() => (
    linkedRange
      ? { low: cellColorClamp?.low ?? linkedRange.lo, high: cellColorClamp?.high ?? linkedRange.hi }
      : cellColorClamp
  ), [linkedRange, cellColorClamp]);

  const {
    colorValues, vmin: cellVmin, vmax: cellVmax,
    type: cellType, categories: cellCategories, loading: cellColorsLoading,
  } = useCellColors(
    apiBase, dataset, colorBy, allGenes, selectedGenes, cellColorPalette,
    cellColorEnabled, effectiveCellClamp, categoryColorOverrides, cellCategorical
  );
  // Only update shared store ranges from panel 0 to avoid redundant updates
  useEffect(() => {
    patch({ cellColorRange: { vmin: cellVmin, vmax: cellVmax } });
  }, [cellVmin, cellVmax]); // eslint-disable-line

  // The backend is the authority on whether a column is categorical, so report
  // the type it actually returned rather than letting the panel re-derive it
  // from the schema dtype — the two disagreed for low-cardinality integers.
  useEffect(() => {
    patch({ cellColorType: cellType, cellColorCategories: cellCategories ?? [] });
  }, [cellType, cellCategories]); // eslint-disable-line

  const edgeColorEnabled = edgeColorBy.mode !== "default";
  const { colorValues: edgeColorValues, vmin: edgeVmin, vmax: edgeVmax, p95: edgeP95, loading: edgeColorsLoading } = useEdgeColors(
    apiBase, dataset, edgeColorBy, hiddenLrms, lrmCatalogue, edgeColorPalette,
    edgeColorEnabled, edgeColorClamp, edges, edgeFile, edgeCategorical
  );
  useEffect(() => {
    patch({ edgeColorRange: { vmin: edgeVmin, vmax: edgeVmax } });
  }, [edgeVmin, edgeVmax]); // eslint-disable-line

  useEffect(() => {
    if (panelIndex === 0 && edgeColorBy?.mode === "lrm_set" && edgeP95 != null) {
      setEdgeColorClamp(edgeColorClamp?.low ?? null, edgeP95);
    }
  }, [edgeP95]); // eslint-disable-line

  useEffect(() => { setSelectedEdge(null, panelIndex); }, [dataset]); // eslint-disable-line

  // ── Loading state → store (for RenderingStatus badge) ────────────────────
  // Aggregate all hook loading booleans into a single per-panel key so the
  // badge appears whenever *any* layer for this panel is still fetching.
  useEffect(() => {
    const anyLoading = transcriptsLoading || cellBoundariesLoading || edgesLoading || cellColorsLoading || edgeColorsLoading;
    setLoadingKey(`panel-${panelIndex}`, anyLoading);
  }, [transcriptsLoading, cellBoundariesLoading, edgesLoading, cellColorsLoading, edgeColorsLoading, panelIndex, setLoadingKey]);

  // Clear this panel's loading key when the panel unmounts (split ↔ single toggle)
  useEffect(() => {
    return () => setLoadingKey(`panel-${panelIndex}`, false);
  }, []); // eslint-disable-line

  // ── deck.gl layers ────────────────────────────────────────────────────────
  const selectedId = selectedCell?.cell_id ?? null;

  const regionCellColors = useMemo(() => {
    const map = new Map();
    for (const region of regions) {
      for (const cid of region.selectedCellIds) {
        map.set(cid, region.color);
      }
    }
    return map;
  }, [regions]);

  const getCellFillColor = (d) => {
    if (d.cell_id === selectedId) return [255, 220, 0, 80];
    const rc = regionCellColors.get(d.cell_id);
    if (rc) return [...rc, 120];
    if (colorValues) {
      const c = colorValues.get(d.cell_id);
      if (c) return [c[0], c[1], c[2], 180];
      return [20, 11, 53, 120];
    }
    return [100, 200, 255, 25];
  };

  const cellFillLayer = new SolidPolygonLayer({
    id: "cell-segments-fill",
    data: cellPolygons,
    modelMatrix: rotModelMatrix,
    visible: cellSegmentsVisible,
    opacity: cellSegmentsOpacity,
    getPolygon: (d) => d.polygon,
    filled: true,
    getFillColor: getCellFillColor,
    extruded: false,
    pickable: true,
    autoHighlight: false,
    updateTriggers: { getFillColor: [selectedId, colorValues, regionCellColors, cellColorEnabled] },
  });

  const cellOutlineLayer = new PathLayer({
    id: "cell-segments-outline",
    data: cellPolygons,
    modelMatrix: rotModelMatrix,
    visible: cellSegmentsVisible,
    opacity: cellOutlineOpacity,
    getPath: (d) => [...d.polygon, d.polygon[0]],
    getColor: (d) =>
      d.cell_id === selectedId ? [255, 220, 0, 255] : [100, 200, 255, 200],
    getWidth: (d) => (d.cell_id === selectedId ? 5 : 3),
    widthMinPixels: 1,
    widthMaxPixels: 6,
    pickable: false,
    jointRounded: true,
    capRounded: true,
    updateTriggers: { getColor: [selectedId], getWidth: [selectedId] },
  });

  const transcriptLayer = new ScatterplotLayer({
    id: "transcripts",
    data: visibleTranscripts,
    modelMatrix: rotModelMatrix,
    visible: transcriptsVisible,
    opacity: transcriptsOpacity,
    getPosition: (d) => [d.x_location, d.y_location],
    getRadius: 4,
    radiusMinPixels: 1,
    radiusMaxPixels: 8,
    getFillColor: (d) => transcriptColorOverrides[d.feature_name] ?? geneColor(d.feature_name),
    pickable: true,
    updateTriggers: { getFillColor: [transcriptColorOverrides] },
  });

  const allDirectedEdges = useMemo(() => {
    const seenPairs = new Set();
    const result = [];
    for (const row of edges) {
      if (row.is_autocrine) continue;
      const pair = [row.sending_cell, row.receiving_cell].sort().join("\0");
      if (!seenPairs.has(pair)) {
        seenPairs.add(pair);
        result.push(row);
      }
    }
    return result;
  }, [edges]);

  // Both layers apply the LRM filter identically. Autocrine used to skip the
  // visible_lrm_count test, so hiding every mechanism removed the directed edges
  // and left a ring on every cell — which reads as the autocrine layer ignoring
  // the controls entirely.
  const hasVisibleLrms = (r) => (r.visible_lrm_count ?? r.lrm_count ?? 0) > 0;
  const { directedEdges, autocrineCells } = useMemo(() => ({
    directedEdges: edges.filter((r) => !r.is_autocrine && hasVisibleLrms(r)),
    autocrineCells: edges
      .filter((r) => r.is_autocrine && hasVisibleLrms(r))
      .map((r) => ({ ...r, x: r.x1, y: r.y1 })),
  }), [edges]);

  const directedEdgesWithOffset = useMemo(() => {
    if (!edgeDirectional) return directedEdges;
    return directedEdges.map((f) => {
      const dx = f.x2 - f.x1, dy = f.y2 - f.y1;
      const len = Math.sqrt(dx * dx + dy * dy) || 1;
      const px = (-dy / len) * edgeOffset;
      const py = (dx / len) * edgeOffset;
      return { ...f, sx: f.x1 + px, sy: f.y1 + py, tx: f.x2 + px, ty: f.y2 + py };
    });
  }, [directedEdges, edgeDirectional, edgeOffset]);

  const getDirectedEdgeColor = useCallback((d) => {
    if (edgeColorValues) {
      const c = edgeColorValues.get(d.edge);
      if (c) return [c[0], c[1], c[2], 200];
    }
    return d.edge === selectedEdge ? [255, 255, 100, 255] : DEFAULT_EDGE_COLOR;
  }, [edgeColorValues, selectedEdge]);

  const getAutocrineCellColor = useCallback((d) => {
    if (edgeColorValues) {
      const c = edgeColorValues.get(d.edge);
      if (c) return [c[0], c[1], c[2], 220];
    }
    return d.edge === selectedEdge ? [255, 255, 100, 255] : DEFAULT_AUTOCRINE_COLOR;
  }, [edgeColorValues, selectedEdge]);

  const arrowheadTriangles = useMemo(() => {
    if (!showArrowheads || !edgeDirectional) return [];
    const arrowLen = Math.max(4, edgeWidth * 4 * arrowheadScale);
    const cos150 = Math.cos((5 * Math.PI) / 6);
    const sin150 = Math.sin((5 * Math.PI) / 6);
    return directedEdgesWithOffset.map((f) => {
      const dx = f.tx - f.sx, dy = f.ty - f.sy;
      const len = Math.sqrt(dx * dx + dy * dy) || 1;
      const ux = dx / len, uy = dy / len;
      const lx = ux * cos150 - uy * sin150, ly = ux * sin150 + uy * cos150;
      const tip = [f.tx, f.ty];
      const outerBase = [f.tx + lx * arrowLen, f.ty + ly * arrowLen];
      if (arrowStyle === "half") {
        const backBase = [f.tx - ux * arrowLen, f.ty - uy * arrowLen];
        return { polygon: [tip, outerBase, backBase], edge: f.edge };
      }
      const rx = ux * cos150 + uy * sin150, ry = -ux * sin150 + uy * cos150;
      const innerBase = [f.tx + rx * arrowLen, f.ty + ry * arrowLen];
      return { polygon: [tip, outerBase, innerBase], edge: f.edge };
    });
  }, [directedEdgesWithOffset, showArrowheads, edgeDirectional, edgeWidth, arrowheadScale, arrowStyle]);

  const tissueGraphLayer = new LineLayer({
    id: "tissue-graph",
    data: allDirectedEdges,
    modelMatrix: rotModelMatrix,
    visible: tissueGraphVisible,
    opacity: tissueGraphOpacity,
    getSourcePosition: (d) => [d.x1, d.y1],
    getTargetPosition: (d) => [d.x2, d.y2],
    getColor: [180, 180, 180, 255],
    getWidth: edgeWidth,
    widthMinPixels: 0.5,
    widthMaxPixels: 5,
    pickable: true,
  });

  const edgeDirectedLayer = new LineLayer({
    id: "edges-directed",
    data: directedEdgesWithOffset,
    modelMatrix: rotModelMatrix,
    visible: edgesVisible,
    opacity: edgesOpacity,
    getSourcePosition: (d) => edgeDirectional ? [d.sx, d.sy] : [d.x1, d.y1],
    getTargetPosition: (d) => edgeDirectional ? [d.tx, d.ty] : [d.x2, d.y2],
    getColor: getDirectedEdgeColor,
    getWidth: edgeWidth,
    widthMinPixels: 1,
    widthMaxPixels: 8,
    pickable: true,
    autoHighlight: true,
    highlightColor: [255, 255, 100, 255],
    updateTriggers: {
      getColor: [edgeColorValues, selectedEdge],
      getSourcePosition: [edgeDirectional],
      getTargetPosition: [edgeDirectional],
    },
  });

  const edgeArrowheadLayer = new SolidPolygonLayer({
    id: "edges-arrowheads",
    data: arrowheadTriangles,
    modelMatrix: rotModelMatrix,
    visible: edgesVisible && showArrowheads && edgeDirectional,
    opacity: edgesOpacity,
    getPolygon: (d) => d.polygon,
    filled: true,
    extruded: false,
    getFillColor: (d) => {
      if (edgeColorValues) {
        const c = edgeColorValues.get(d.edge);
        if (c) return [c[0], c[1], c[2], 220];
      }
      return d.edge === selectedEdge ? [255, 255, 100, 255] : DEFAULT_EDGE_COLOR;
    },
    pickable: false,
    updateTriggers: { getFillColor: [edgeColorValues, selectedEdge] },
  });

  const edgeAutocrineLayer = new ScatterplotLayer({
    id: "edges-autocrine",
    data: autocrineCells,
    modelMatrix: rotModelMatrix,
    visible: edgesVisible && showAutocrine,
    opacity: edgesOpacity,
    getPosition: (d) => [d.x, d.y],
    getRadius: autocrineRadius,
    radiusMinPixels: 5,
    radiusMaxPixels: 60,
    filled: false,
    stroked: true,
    getLineColor: getAutocrineCellColor,
    getLineWidth: autocrineLineWidth,
    lineWidthMinPixels: 1,
    pickable: true,
    autoHighlight: true,
    highlightColor: [255, 255, 100, 255],
    updateTriggers: { getLineColor: [edgeColorValues, selectedEdge] },
  });

  // ── Annotation layers ─────────────────────────────────────────────────────
  const regionFillLayers = regions.map((r) =>
    new SolidPolygonLayer({
      id: `region-fill-${r.id}`,
      data: [r],
      modelMatrix: rotModelMatrix,
      getPolygon: (d) => d.points,
      getFillColor: [...r.color, 40],
      filled: true,
      extruded: false,
      pickable: false,
    })
  );
  const regionOutlineLayers = regions.map((r) =>
    new PathLayer({
      id: `region-outline-${r.id}`,
      data: [[...r.points, r.points[0]]],
      modelMatrix: rotModelMatrix,
      getPath: (d) => d,
      getColor: [...r.color, 220],
      getWidth: 2,
      widthMinPixels: 1.5,
      pickable: false,
    })
  );
  // Empty unless this panel is the one drawing, so the dashed outline and its
  // vertex markers do not shadow the other panel while a polygon is in progress.
  const ownActiveRegion = drawingHere ? activeRegion : [];
  const activePts = cursorPos && ownActiveRegion.length > 0
    ? [...ownActiveRegion, cursorPos]
    : ownActiveRegion;
  const activeRegionLayer = new PathLayer({
    id: "active-region",
    data: activePts.length > 1 ? [activePts] : [],
    modelMatrix: rotModelMatrix,
    getPath: (d) => d,
    getColor: [255, 255, 255, 200],
    getWidth: 2,
    widthMinPixels: 1.5,
    pickable: false,
    getDashArray: [6, 4],
    extensions: [],
  });
  const activeVertexLayer = new ScatterplotLayer({
    id: "active-vertices",
    data: ownActiveRegion,
    modelMatrix: rotModelMatrix,
    getPosition: (d) => d,
    getRadius: 4,
    radiusMinPixels: 4,
    getFillColor: [255, 255, 255, 220],
    pickable: false,
  });
  const measureLineLayer = new LineLayer({
    id: "measure-lines",
    data: measurements,
    modelMatrix: rotModelMatrix,
    getSourcePosition: (d) => d.p1,
    getTargetPosition: (d) => d.p2,
    getColor: [255, 220, 60, 220],
    getWidth: 2,
    widthMinPixels: 1.5,
    pickable: false,
  });
  const measureEndpointLayer = new ScatterplotLayer({
    id: "measure-endpoints",
    data: measurements.flatMap((m) => [m.p1, m.p2]),
    modelMatrix: rotModelMatrix,
    getPosition: (d) => d,
    getRadius: 5,
    radiusMinPixels: 5,
    getFillColor: [255, 220, 60, 220],
    pickable: false,
  });
  const measureFirstLayer = new ScatterplotLayer({
    id: "measure-first",
    data: measureFirstRef.current ? [measureFirstRef.current] : [],
    modelMatrix: rotModelMatrix,
    getPosition: (d) => d,
    getRadius: 5,
    radiusMinPixels: 5,
    getFillColor: [255, 220, 60, 180],
    pickable: false,
  });

  const deckLayers = [
    cellFillLayer, cellOutlineLayer, transcriptLayer,
    tissueGraphLayer, edgeDirectedLayer, edgeArrowheadLayer, edgeAutocrineLayer,
    ...regionFillLayers, ...regionOutlineLayers,
    activeRegionLayer, activeVertexLayer,
    measureLineLayer, measureEndpointLayer, measureFirstLayer,
  ];

  // ── Measurement label positions (screen coords) ───────────────────────────
  const measureLabels = measurements.map((m) => {
    const vs = deckViewStateRef.current;
    if (!vs || !containerRef.current) return null;
    const { width: cW, height: cH } = containerRef.current.getBoundingClientRect();
    const scale = Math.pow(2, vs.zoom);
    const [cx, cy] = vs.target;
    const mx = (m.p1[0] + m.p2[0]) / 2;
    const my = (m.p1[1] + m.p2[1]) / 2;
    // Forward-rotate image midpoint into the rotated view space before projecting
    const [rx, ry] = forwardRotate(mx, my, cx, cy, panelRotation);
    const sx = (rx - cx) * scale + cW / 2;
    const sy = (ry - cy) * scale + cH / 2;
    const distUm = m.distPx * pixelSize;
    return { id: m.id, sx, sy, label: `${distUm.toFixed(1)} µm` };
  }).filter(Boolean);

  // ── Render ────────────────────────────────────────────────────────────────
  const inAnnotationMode = annotationMode !== "pan";
  const cursor = annotationMode === "region" ? "crosshair"
    : annotationMode === "measure" ? "cell" : "default";

  return (
    <div style={{ flex: 1, height: "100%", position: "relative", overflow: "hidden", minWidth: 0 }}>
      {/* OpenSeadragon tile canvas */}
      <div
        ref={containerRef}
        style={{ width: "100%", height: "100%" }}
        onClick={inAnnotationMode ? undefined : handleViewerClick}
        onMouseMove={transcriptsVisible ? handleTranscriptHoverMove : undefined}
        onMouseLeave={handleTranscriptHoverLeave}
      />

      {/* deck.gl overlay — pointerEvents:none so OSD handles pan/zoom */}
      <div style={{ position: "absolute", top: 0, left: 0, width: "100%", height: "100%", pointerEvents: "none" }}>
        {imageSize.w && (
          <DeckGL
            ref={deckRef}
            views={new OrthographicView({ id: VIEW_ID, flipY: true })}
            viewState={{ ...deckViewState, id: VIEW_ID }}
            controller={false}
            layers={deckLayers}
            style={{ position: "absolute", top: 0, left: 0 }}
            glOptions={{ preserveDrawingBuffer: true }}
          />
        )}
      </div>

      {/* Annotation event capture overlay */}
      {inAnnotationMode && (
        <div
          style={{
            position: "absolute", top: 0, left: 0, width: "100%", height: "100%",
            cursor, pointerEvents: "all",
          }}
          onClick={handleOverlayClick}
          onDoubleClick={handleOverlayDblClick}
          onMouseMove={handleOverlayMouseMove}
          onMouseLeave={handleOverlayMouseLeave}
        />
      )}

      {/* Measurement distance labels */}
      {measureLabels.map(({ id, sx, sy, label }) => (
        <div
          key={id}
          style={{
            position: "absolute",
            left: sx, top: sy,
            transform: "translate(-50%, -120%)",
            background: "rgba(0,0,0,0.7)",
            color: "#ffdc3c",
            fontFamily: "monospace", fontSize: 11,
            padding: "2px 5px", borderRadius: 3,
            pointerEvents: "none", whiteSpace: "nowrap",
          }}
        >
          {label}
        </div>
      ))}

      {/* Transcript hover tooltip */}
      {hoveredTranscript && (
        <div style={{
          position: "absolute",
          left: hoveredTranscript.x + 14,
          top: hoveredTranscript.y - 10,
          background: "rgba(0,0,0,0.82)",
          color: "#fff",
          fontFamily: "monospace",
          fontSize: 11,
          padding: "3px 8px",
          borderRadius: 4,
          pointerEvents: "none",
          whiteSpace: "nowrap",
          zIndex: 20,
          border: "1px solid rgba(255,255,255,0.12)",
        }}>
          {hoveredTranscript.gene}
          {hoveredTranscript.qv != null && (
            <span style={{ color: "#777", marginLeft: 7 }}>
              QV {Number(hoveredTranscript.qv).toFixed(1)}
            </span>
          )}
        </div>
      )}

      {/* Annotation toolbar — split toggle only shown on panel 0 */}
      <AnnotationToolbar onScreenshot={handleScreenshot} panelIndex={panelIndex} />

      {/* Loading / computing badge — appears ~400ms after any fetch starts */}
      <RenderingStatus panelIndex={panelIndex} />

      {/* Edge info panel — rendered by the panel that owns the selection, so it
          resolves against that panel's dataset and edge file. It used to be
          pinned to panel 0, which was fine when both panels shared a dataset and
          would show the wrong dataset's edge now. `selectedEdge` is already
          null unless this panel made the selection, so this cannot duplicate. */}
      {selectedEdge && (
        <EdgeInfoPanel
          apiBase={apiBase}
          dataset={dataset}
          edgeId={selectedEdge}
          edgeFile={edgeFile}
          onClose={() => setSelectedEdge(null, panelIndex)}
        />
      )}

      {/* Panel header. In split mode this carries the panel's own dataset /
          image / edge-source pickers, because those are the things that cannot
          be shared once two panels can show two datasets. In single mode it
          stays a passive label and the pickers live in the sidebar, exactly
          where they have always been. */}
      <div
        style={{
          // Bottom-left: the OSD zoom buttons and the annotation toolbar own the
          // top of the panel, and the navigator owns bottom-right. In split mode
          // this strip holds real controls, so it cannot sit under them.
          position: "absolute", bottom: 8, left: 8,
          color: "#aaa", fontFamily: "monospace", fontSize: 11,
          background: "rgba(0,0,0,0.7)", padding: "3px 7px", borderRadius: 3,
          pointerEvents: panelCount >= 2 ? "auto" : "none",
          display: "flex", alignItems: "center", gap: 6, maxWidth: "calc(100% - 16px)",
        }}
      >
        {panelCount >= 2 ? (
          <>
            <span style={{ color: "#555" }}>{panelIndex + 1}</span>
            <DatasetPicker panelIndex={panelIndex} compact />
          </>
        ) : (
          <span>
            {dataset}
            {activeImage && activeImage !== BLANK_IMAGE_NAME ? ` / ${activeImage}` : ""}
          </span>
        )}
        {transcripts.length > 0 && (
          <span style={{ color: "#777" }}>{transcripts.length} tx</span>
        )}
      </div>
    </div>
  );
}

// ── Viewer ────────────────────────────────────────────────────────────────────
// Thin wrapper: renders one or two ViewerPanels side by side.

export default function Viewer() {
  const panelCount = useStore((s) => s.panelCount);
  // Panel 0 gates the placeholder: it is the one DatasetPicker initialises first,
  // and a second panel still resolving its own dataset should not blank the one
  // that is already drawing.
  const panel0Dataset = useStore((s) => s.panels[0].dataset);

  if (!panel0Dataset) {
    return (
      <div style={{ display: "flex", alignItems: "center", justifyContent: "center",
                    width: "100%", height: "100%", color: "#555", fontFamily: "monospace", fontSize: 13 }}>
        Loading datasets…
      </div>
    );
  }

  return (
    <div style={{ display: "flex", width: "100%", height: "100%", background: "#1a1a1a" }}>
      <ViewerPanel panelIndex={0} />
      {panelCount >= 2 && (
        <>
          <div style={{ width: 1, background: "#2a2a2a", flexShrink: 0 }} />
          <ViewerPanel panelIndex={1} />
        </>
      )}
    </div>
  );
}
