import { create } from "zustand";
import { createBackend, type Backend } from "./api";
import { buildModel, mergeRecord, removeRecord, type GraphModel } from "./model/graph";
import { defaultCollapsed, revealPath, toggleSegmentKey } from "./model/layout";
import type { Bundle, Change, Run } from "./types";

export interface Route {
  runId: string | null;
  node: string | null;
  t: number | null;
  compare?: [string, string] | null;
  tour?: string | null;
}

export function parseHash(hash: string): Route {
  // #/run/<runId>/<nodeId>?t=<ms offset>
  const [path, query] = hash.replace(/^#\/?/, "").split("?");
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  const params = new URLSearchParams(query ?? "");
  const t = params.get("t");
  return {
    runId: parts[0] === "run" ? parts[1] ?? null : null,
    node: parts[0] === "run" ? parts[2] ?? null : null,
    t: t != null && t !== "" && isFinite(Number(t)) ? Number(t) : null,
    compare: parts[0] === "compare" && parts[1] && parts[2] ? [parts[1], parts[2]] : null,
    tour: params.get("tour"),
  };
}

export function routeHash(r: Route): string {
  if (r.compare) return `#/compare/${encodeURIComponent(r.compare[0])}/${encodeURIComponent(r.compare[1])}`;
  if (!r.runId) return "#/";
  let h = `#/run/${encodeURIComponent(r.runId)}`;
  if (r.node) h += `/${encodeURIComponent(r.node)}`;
  if (r.t != null) h += `?t=${Math.round(r.t)}`;
  return h;
}

interface State {
  backend: Backend;
  runs: Run[];
  runsLoaded: boolean;
  connected: boolean;
  error: string | null;

  runId: string | null;
  bundle: Bundle | null;
  model: GraphModel | null;
  loadingRun: boolean;

  selected: string | null;
  focusLink: string | null;
  hovered: string | null;
  collapsed: Set<string>;
  hiddenKinds: Set<string>;
  showEvidence: boolean;
  /** Keep the newest step of a live run in view. */
  follow: boolean;
  /** Navigator filter by where runs came from (sdk, claude-code, kiro, otlp…); null = all. */
  sourceFilter: string | null;
  direction: "DOWN" | "RIGHT";
  showTimeline: boolean;
  showNavigator: boolean;
  replayT: number | null;
  playing: boolean;
  speed: number;
  paletteOpen: boolean;
  helpOpen: boolean;
  connectOpen: boolean;
  runTab: string;
  /** Two runs side by side (speed, tokens, tool calls, failures) — null when not comparing. */
  compare: { ids: [string, string]; bundles: [Bundle, Bundle] | null } | null;
  toast: string | null;
  /** Bumped when the canvas should centre on the current selection. */
  focusTick: number;
  fitTick: number;

  init(): Promise<void>;
  refreshRuns(): Promise<void>;
  openRun(id: string | null, node?: string | null, t?: number | null): Promise<void>;
  select(id: string | null, opts?: { focus?: boolean; link?: string | null }): void;
  openCompare(a: string, b: string): Promise<void>;
  closeCompare(): void;
  /** Show a bundle directly (no backend fetch) — used by the tour's live playback. */
  loadBundle(bundle: Bundle): void;
  setHovered(id: string | null): void;
  toggleCollapsed(id: string): void;
  toggleSegment(segId: string, isLast: boolean): void;
  expandAll(): void;
  collapseAll(): void;
  toggleKind(kind: string): void;
  setReplay(t: number | null): void;
  setPlaying(p: boolean): void;
  setSpeed(s: number): void;
  set<K extends keyof State>(k: K, v: State[K]): void;
  applyChanges(changes: Change[]): void;
  syncHash(): void;
}

let initialized = false;
let refreshTimer: ReturnType<typeof setTimeout> | undefined;
let rebuildTimer: ReturnType<typeof setTimeout> | undefined;

export const useStore = create<State>((set, get) => ({
  backend: createBackend(),
  runs: [],
  runsLoaded: false,
  connected: false,
  error: null,
  runId: null,
  bundle: null,
  model: null,
  loadingRun: false,
  selected: null,
  focusLink: null,
  hovered: null,
  collapsed: new Set(),
  hiddenKinds: new Set(),
  showEvidence: false,
  follow: true,
  sourceFilter: null,
  direction: (localStorage.getItem("tg.direction") as "DOWN" | "RIGHT") || "DOWN",
  showTimeline: true,
  showNavigator: true,
  replayT: null,
  playing: false,
  speed: 8,
  paletteOpen: false,
  helpOpen: false,
  connectOpen: false,
  runTab: "overview",
  compare: null,
  toast: null,
  focusTick: 0,
  fitTick: 0,

  async init() {
    if (initialized) return; // StrictMode mounts twice; one stream and one hash listener are enough
    initialized = true;
    const { backend } = get();
    await get().refreshRuns();
    backend.subscribe?.(
      (changes) => get().applyChanges(changes),
      (connected) => set({ connected }),
    );
    const route = parseHash(location.hash);
    const runs = get().runs;
    // Default to what's happening now: a session an agent is working in, else the newest run.
    const working = runs.find((r) => r.status === "running" || r.metadata?.["activity"] === "working");
    const runId = route.runId ?? (route.compare ? route.compare[0] : null) ?? working?.id ?? runs[0]?.id ?? null;
    await get().openRun(runId, route.node, route.t);
    if (route.compare) await get().openCompare(route.compare[0], route.compare[1]);
    window.addEventListener("hashchange", () => {
      const r = parseHash(location.hash);
      const s = get();
      if (r.compare) {
        if (s.compare?.ids[0] !== r.compare[0] || s.compare?.ids[1] !== r.compare[1]) s.openCompare(r.compare[0], r.compare[1]);
        return;
      }
      if (s.compare) set({ compare: null });
      if (r.runId && r.runId !== s.runId) s.openRun(r.runId, r.node, r.t);
      else if (r.node !== s.selected) s.select(r.node, { focus: true });
    });
  },

  async refreshRuns() {
    try {
      const runs = await get().backend.listRuns();
      set({ runs, runsLoaded: true, error: null });
    } catch (e) {
      set({ runsLoaded: true, error: String(e) });
    }
  },

  async openRun(id, node = null, t = null) {
    if (!id) {
      set({ runId: null, bundle: null, model: null, selected: null });
      get().syncHash();
      return;
    }
    set({ loadingRun: true, runId: id, playing: false, follow: true, compare: null });
    const bundle = await get().backend.getRun(id);
    if (get().runId !== id) return; // superseded by a later navigation
    if (!bundle) {
      set({ loadingRun: false, bundle: null, model: null, error: `Run ${id} not found` });
      return;
    }
    const model = buildModel(bundle);
    let collapsed = defaultCollapsed(model);
    const selected = node && (model.spans.has(node) || model.artifacts.has(node)) ? node : null;
    if (selected) collapsed = revealPath(model, selected, collapsed);
    set({
      bundle,
      model,
      collapsed,
      loadingRun: false,
      selected,
      focusLink: null,
      replayT: t != null ? model.t0 + t : null,
      error: null,
      fitTick: get().fitTick + 1,
      focusTick: selected ? get().focusTick + 1 : get().focusTick,
    });
    get().syncHash();
  },

  async openCompare(a, b) {
    set({ compare: { ids: [a, b], bundles: null }, playing: false });
    get().syncHash();
    const [ba, bb] = await Promise.all([get().backend.getRun(a), get().backend.getRun(b)]);
    if (!ba || !bb) {
      set({ compare: null, toast: "Couldn't load both runs" });
      return;
    }
    if (get().compare?.ids[0] === a && get().compare?.ids[1] === b) set({ compare: { ids: [a, b], bundles: [ba, bb] } });
  },

  closeCompare() {
    set({ compare: null });
    get().syncHash();
  },

  loadBundle(bundle) {
    const model = buildModel(bundle);
    set({ runId: bundle.run.id, bundle, model, loadingRun: false, error: null });
  },

  select(id, opts = {}) {
    const { model, collapsed } = get();
    let nextCollapsed = collapsed;
    if (id && model && (model.spans.has(id) || model.artifacts.has(id))) {
      const revealed = revealPath(model, id, collapsed);
      if (revealed.size !== collapsed.size || [...revealed].some((x) => !collapsed.has(x))) nextCollapsed = revealed;
    }
    set({
      selected: id,
      // Inspecting a step pauses follow mode so the canvas doesn't drift away from it.
      follow: id ? false : get().follow,
      focusLink: opts.link ?? null,
      collapsed: nextCollapsed,
      focusTick: opts.focus ? get().focusTick + 1 : get().focusTick,
    });
    get().syncHash();
  },

  setHovered(id) {
    if (get().hovered !== id) set({ hovered: id });
  },

  toggleCollapsed(id) {
    const c = new Set(get().collapsed);
    if (c.has(id)) c.delete(id);
    else c.add(id);
    set({ collapsed: c });
  },

  toggleSegment(segId, isLast) {
    set({ collapsed: toggleSegmentKey(get().collapsed, segId, isLast) });
  },

  expandAll() {
    set({ collapsed: new Set(), fitTick: get().fitTick + 1 });
  },

  collapseAll() {
    const m = get().model;
    if (!m) return;
    const c = new Set<string>();
    for (const id of m.order) if ((m.descendants.get(id) ?? 0) > 0 && (m.depth.get(id) ?? 0) >= 1) c.add(id);
    set({ collapsed: c, fitTick: get().fitTick + 1 });
  },

  toggleKind(kind) {
    const h = new Set(get().hiddenKinds);
    if (h.has(kind)) h.delete(kind);
    else h.add(kind);
    set({ hiddenKinds: h });
  },

  setReplay(t) {
    set({ replayT: t });
    if (!get().playing) get().syncHash();
  },
  setPlaying(p) {
    const { model, replayT } = get();
    if (p && model && (replayT == null || replayT >= model.t1)) set({ replayT: model.t0 });
    set({ playing: p });
    if (!p) get().syncHash();
  },
  setSpeed(s) {
    set({ speed: s });
  },
  set(k, v) {
    set({ [k]: v } as Partial<State>);
  },

  applyChanges(changes) {
    const s = get();
    let bundle = s.bundle;
    let touchedList = false;
    for (const ch of changes) {
      if (ch.type === "delete") {
        touchedList = true;
        if (ch.run_id === s.runId) bundle = null;
        continue;
      }
      if (ch.type === "run") touchedList = true;
      if (ch.type === "remove") {
        const r = ch.record as { id: string; type: string } | null;
        if (bundle && r && ch.run_id === bundle.run.id) bundle = removeRecord(bundle, r.type, r.id);
        continue;
      }
      if (bundle && ch.run_id === bundle.run.id && ch.record) bundle = mergeRecord(bundle, ch.type, ch.record);
      else if (!s.runs.some((r) => r.id === ch.run_id)) touchedList = true;
    }
    if (touchedList || changes.some((c) => c.type === "span")) {
      clearTimeout(refreshTimer);
      refreshTimer = setTimeout(() => {
        get().refreshRuns().then(() => {
          // Follow the newest run when nothing is open yet (e.g. the first live demo).
          if (!get().runId && get().runs.length) get().openRun(get().runs[0].id);
        });
      }, 400);
    }
    if (bundle !== s.bundle) {
      set({ bundle });
      clearTimeout(rebuildTimer);
      rebuildTimer = setTimeout(() => {
        const b = get().bundle;
        if (!b) {
          set({ model: null });
          return;
        }
        set({ model: buildModel(b) });
      }, 120);
    }
  },

  syncHash() {
    const { runId, selected, replayT, model, compare } = get();
    if (new URLSearchParams(location.hash.split("?")[1] ?? "").get("tour")) return; // tours own the URL
    const h = routeHash({ runId, node: selected, t: replayT != null && model ? replayT - model.t0 : null, compare: compare?.ids ?? null });
    if (h !== location.hash) history.replaceState(null, "", h);
  },
}));

export function selectedRun(): Run | undefined {
  const s = useStore.getState();
  return s.runs.find((r) => r.id === s.runId) ?? s.bundle?.run;
}
