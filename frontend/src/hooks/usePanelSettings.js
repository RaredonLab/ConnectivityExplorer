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
 * Known cost, to fix later rather than now: like the bare `useStore()` it
 * replaces, this subscribes to the entire store, so every consumer re-renders on
 * any state change — including each viewport update during a pan. Narrowing the
 * subscriptions is worth doing, but it changes render behaviour, and mixing that
 * into the state migration would defeat the point of a frozen stage.
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
export function usePanelSettings(explicitIndex = null) {
  const i = usePanelIndex(explicitIndex);
  const store = useStore();
  const settings = store.panels[i]?.settings ?? store.panels[0].settings;
  return { ...store, ...settings, panelIndex: i };
}
