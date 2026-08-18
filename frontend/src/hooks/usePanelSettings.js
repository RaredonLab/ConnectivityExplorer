/**
 * Reading a panel's display settings.
 *
 * Settings live in `panels[i].settings` (see `store.js::makeSettings`), but most
 * of the UI that edits them — the whole sidebar — has no idea which panel it is
 * editing. Threading a `panelIndex` prop through LayerPanel's nineteen section
 * components would be noisy and easy to get half-right, so the panel travels in
 * context instead.
 *
 * `usePanelSettings()` returns the whole store *merged with* that panel's
 * settings. That shape is deliberate: every call site was already
 * `const { layers, setLayerProp } = useStore()`, so the migration is a one-word
 * swap and the destructuring below it is untouched. Phase 2a is meant to be
 * provably behaviour-preserving, and a mechanical diff is the way to be sure.
 *
 * It subscribes to the whole store, which is what the bare `useStore()` it
 * replaced did. That is coarse, but only one thing made it actually expensive:
 * `viewports` and `viewportActual` are rewritten on every OpenSeadragon
 * viewport-change event, so a single pan pushed dozens of re-renders through
 * every sidebar section. `IGNORED_KEYS` below drops exactly those, and nothing
 * else — see the note there.
 *
 * Narrowing the rest properly means giving each of the nineteen sidebar sections
 * its own selector, which is a real refactor with no component tests behind it.
 * Not worth it for what remains once the pan storm is gone.
 */
import { createContext, useContext } from "react";
import { useStore } from "../store";

/**
 * Which panel the subtree below is editing. `null` means "not inside a panel" —
 * the sidebar today, which falls back to `activePanel`.
 */
export const PanelIndexContext = createContext(null);

export const PanelIndexProvider = PanelIndexContext.Provider;

/** The panel index in effect here, honouring an explicit override. */
export function usePanelIndex(explicit = null) {
  const fromContext = useContext(PanelIndexContext);
  const activePanel = useStore((s) => s.activePanel ?? 0);
  return explicit ?? fromContext ?? activePanel;
}

/**
 * The store, with `panels[i].settings` spread over the top.
 *
 * @param explicitIndex pass when the component already knows its panel
 *                      (ViewerPanel does); otherwise the context decides.
 */
/**
 * State that changes continuously and that no consumer of this hook reads.
 *
 * Both are rewritten on every OSD viewport-change event — dozens of times during
 * one pan — and both are read only through their own narrow selectors:
 * `ViewerPanel` takes `viewports[panelIndex]`, and ⇔ Match zoom reads
 * `viewportActual` via `getState()`. LayerPanel, CellInfoPanel and
 * AnnotationToolbar reference neither.
 *
 * **Before adding a key here, check nothing reading it comes through this hook** —
 * ignoring a key a consumer does read makes that consumer silently stale, which
 * is a much worse bug than a redundant render.
 */
const IGNORED_KEYS = ["viewports", "viewportActual"];

function equalIgnoringHotKeys(a, b) {
  if (Object.is(a, b)) return true;
  if (!a || !b) return false;
  const keys = Object.keys(a);
  if (keys.length !== Object.keys(b).length) return false;
  for (const k of keys) {
    if (IGNORED_KEYS.includes(k)) continue;
    if (!Object.is(a[k], b[k])) return false;
  }
  return true;
}

export function usePanelSettings(explicitIndex = null) {
  const i = usePanelIndex(explicitIndex);
  const store = useStore((s) => s, equalIgnoringHotKeys);
  const settings = store.panels[i]?.settings ?? store.panels[0].settings;
  return { ...store, ...settings, panelIndex: i };
}
