import { Coins, Command, Clock, Keyboard, Layers, PanelBottom, PanelLeft, Plug } from "lucide-react";
import { useEffect, useState } from "react";
import { BrandMark } from "./components/Brand";
import { Canvas } from "./components/Canvas";
import { Inspector } from "./components/Inspector";
import { Navigator } from "./components/Navigator";
import { CommandPalette, HelpOverlay, Toast } from "./components/Overlays";
import { ConnectPanel } from "./components/Connect";
import { Compare } from "./components/Compare";
import { Timeline } from "./components/Timeline";
import {
  copyLink,
  finalAnswer,
  nextError,
  selectChild,
  selectParent,
  stepRun,
  stepSelection,
  toggleSelected,
} from "./components/actions";
import { fmtCost, fmtDuration, fmtTokens } from "./model/format";
import { statusStyle } from "./model/kinds";
import { ACTIVITY, sourceOf } from "./model/sources";
import { runActivity } from "./types";
import { SourceBadge } from "./components/Navigator";
import { parseHash, useStore } from "./store";
import { TourLayer } from "./tour/TourLayer";
import { mainTour } from "./tour/main";

function TopBar() {
  const model = useStore((s) => s.model);
  const runs = useStore((s) => s.runs);
  const runId = useStore((s) => s.runId);
  const mode = useStore((s) => s.backend.mode);
  const showTimeline = useStore((s) => s.showTimeline);
  const showNavigator = useStore((s) => s.showNavigator);
  const set = useStore((s) => s.set);
  const run = model?.run ?? runs.find((r) => r.id === runId);
  const summary = model ? { tokens: model.totals.tokens, cost_usd: model.totals.cost } : run?.summary ?? {};
  const [, tick] = useState(0);
  useEffect(() => {
    if (run?.status !== "running" && runActivity(run) !== "working") return;
    const t = setInterval(() => tick((x) => x + 1), 1000);
    return () => clearInterval(t);
  }, [run?.status]);
  const st = statusStyle(run?.status);
  const dur = model
    ? run?.ended_at && run?.started_at
      ? run.ended_at - run.started_at
      : (run?.status === "running" ? Date.now() : model.t1) - model.t0
    : null;
  return (
    <header className="topbar">
      <div className="brand">
        <BrandMark />
        Traceglass
        {mode !== "server" && <span className="mode">{mode === "embedded" ? "snapshot" : "static"}</span>}
      </div>
      <div className="crumbs">
        {run ? (
          <>
            <span className="ellipsis" style={{ flex: "none", maxWidth: 160 }}>
              {run.project ?? "default"}
            </span>
            <span className="sep">/</span>
            <span className="current" title={run.name}>
              {run.name}
            </span>
            {(() => {
              const act = runActivity(run);
              if (act)
                return (
                  <span className="pill" style={{ ["--c" as any]: ACTIVITY[act].color, flex: "none" }} title={ACTIVITY[act].hint}>
                    <span className={`dot ${act === "working" ? "live" : ""}`} style={{ ["--c" as any]: ACTIVITY[act].color, width: 6, height: 6 }} />
                    {ACTIVITY[act].label}
                  </span>
                );
              return (
                <span className="pill" style={{ ["--c" as any]: st.color, flex: "none" }}>
                  <st.icon size={11} className={run.status === "running" ? "spin" : undefined} />
                  {st.label}
                </span>
              );
            })()}
            <SourceBadge src={sourceOf(run)} />
          </>
        ) : null}
      </div>
      {run && (
        <div className="stats">
          <span className="stat" title="Duration">
            <Clock size={12} /> {fmtDuration(dur)}
          </span>
          <span className="stat opt" title="Steps">
            <Layers size={12} /> {model?.spans.size ?? summary.spans ?? 0}
          </span>
          <span className="stat opt" title="Tokens">
            <span className="muted" style={{ fontSize: 10.5 }}>TOK</span> {summary.tokens ? fmtTokens(summary.tokens) : "—"}
          </span>
          {summary.cost_usd != null && (
            <span className="stat opt" title="Model cost">
              <Coins size={12} /> {fmtCost(summary.cost_usd)}
            </span>
          )}
        </div>
      )}
      <div className="row" style={{ gap: 4 }}>
        <button className={`icon-btn ${showNavigator ? "on" : ""}`} title="Toggle run list (b)" onClick={() => set("showNavigator", !showNavigator)}>
          <PanelLeft size={15} />
        </button>
        <button className={`icon-btn ${showTimeline ? "on" : ""}`} title="Toggle timeline (t)" onClick={() => set("showTimeline", !showTimeline)}>
          <PanelBottom size={15} />
        </button>
        <button className="btn ghost" title="Connect Claude Code, Kiro and your own agents" onClick={() => set("connectOpen", true)}>
          <Plug size={13} /> Connect
        </button>
        <button className="icon-btn" title="Keyboard shortcuts (?)" onClick={() => set("helpOpen", true)}>
          <Keyboard size={15} />
        </button>
        <button className="btn" onClick={() => set("paletteOpen", true)} title="Command palette">
          <Command size={13} /> <span>Search</span> <span className="kbd">⌘K</span>
        </button>
      </div>
    </header>
  );
}

function Empty() {
  const runsLoaded = useStore((s) => s.runsLoaded);
  const runs = useStore((s) => s.runs);
  const error = useStore((s) => s.error);
  const loadingRun = useStore((s) => s.loadingRun);
  if (!runsLoaded || loadingRun) return null;
  if (error && runs.length)
    return (
      <div className="empty">
        <div>
          <h2>Couldn’t open this run</h2>
          <p>{error}</p>
        </div>
      </div>
    );
  if (runs.length) return null;
  return (
    <div className="empty">
      <div>
        <BrandMark className="hero-mark" />
        <h2>No runs yet</h2>
        <p>Wrap any agent in a run and it will appear here — live, as it executes. Watch your Claude Code and Kiro sessions, or seed the showcase demos.</p>
        <p>
          <button className="btn on" onClick={() => useStore.getState().set("connectOpen", true)}>
            <Plug size={13} /> Connect your agents
          </button>
        </p>
        <pre>
          {`import traceglass as tg

with tg.run("Compare papers", project="research"):
    with tg.span("search", kind="tool", input=q) as s:
        s.set_output(search(q))

# or, in a terminal:
$ traceglass demo`}
        </pre>
      </div>
    </div>
  );
}

function useShortcuts() {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const s = useStore.getState();
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        s.set("paletteOpen", !s.paletteOpen);
        return;
      }
      const target = e.target as HTMLElement;
      if (target.closest("input, textarea, [contenteditable]")) {
        if (e.key === "Escape") target.blur();
        return;
      }
      if (s.paletteOpen || e.metaKey || e.ctrlKey || e.altKey) return;
      if (s.helpOpen) {
        if (e.key === "Escape" || e.key === "?") s.set("helpOpen", false);
        return;
      }
      const map: Record<string, () => void> = {
        j: () => stepSelection(1),
        ArrowDown: () => stepSelection(1),
        k: () => stepSelection(-1),
        ArrowUp: () => stepSelection(-1),
        h: selectParent,
        ArrowLeft: selectParent,
        l: selectChild,
        ArrowRight: selectChild,
        Enter: toggleSelected,
        "[": () => stepRun(-1),
        "]": () => stepRun(1),
        e: nextError,
        a: finalAnswer,
        f: () => s.set("fitTick", s.fitTick + 1),
        t: () => s.set("showTimeline", !s.showTimeline),
        v: () => s.set("showEvidence", !s.showEvidence),
        b: () => s.set("showNavigator", !s.showNavigator),
        c: copyLink,
        " ": () => s.setPlaying(!s.playing),
        "?": () => s.set("helpOpen", true),
        "/": () => document.getElementById("run-search")?.focus(),
        Escape: () => (s.compare ? s.closeCompare() : s.replayT != null && !s.selected ? s.setReplay(null) : s.select(null)),
      };
      const fn = map[e.key];
      if (fn) {
        e.preventDefault();
        fn();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
}

export default function App() {
  const init = useStore((s) => s.init);
  const model = useStore((s) => s.model);
  const loadingRun = useStore((s) => s.loadingRun);
  const showTimeline = useStore((s) => s.showTimeline);
  const showNavigator = useStore((s) => s.showNavigator);
  const comparing = useStore((s) => s.compare !== null);
  useEffect(() => {
    const tour = parseHash(location.hash).tour;
    init().then(() => {
      if (tour === "main") mainTour();
    });
  }, [init]);
  useShortcuts();
  useEffect(() => {
    document.title = model ? `${model.run.name} · Traceglass` : "Traceglass";
  }, [model]);
  return (
    <div className={`app ${showNavigator ? "" : "no-nav"} ${model && !comparing ? "" : "no-insp"}`}>
      <TopBar />
      <Navigator />
      <main className="main">
        {loadingRun && <div className="loading-bar" />}
        {comparing ? (
          <Compare />
        ) : model ? (
          <>
            <Canvas />
            {showTimeline && <Timeline />}
          </>
        ) : (
          <Empty />
        )}
      </main>
      <Inspector />
      <CommandPalette />
      <HelpOverlay />
      <ConnectPanel />
      <TourLayer />
      <Toast />
    </div>
  );
}
