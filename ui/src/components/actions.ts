import { ancestors, ROOT } from "../model/graph";
import { useStore } from "../store";

export interface Action {
  id: string;
  label: string;
  keys?: string;
  keywords?: string;
  pinned?: boolean;
  run: () => void;
}

const st = () => useStore.getState();

export function stepSelection(delta: number) {
  const s = st();
  const m = s.model;
  if (!m || !m.order.length) return;
  const cur = s.selected && m.spans.has(s.selected) ? m.order.indexOf(s.selected) : -1;
  const next = cur < 0 ? (delta > 0 ? 0 : m.order.length - 1) : Math.min(m.order.length - 1, Math.max(0, cur + delta));
  s.select(m.order[next], { focus: true });
}

export function selectParent() {
  const s = st();
  const m = s.model;
  if (!m || !s.selected) return;
  const a = m.artifacts.get(s.selected);
  if (a?.span_id) return s.select(a.span_id, { focus: true });
  const p = ancestors(m, s.selected)[0];
  if (p) s.select(p, { focus: true });
}

export function selectChild() {
  const s = st();
  const m = s.model;
  if (!m) return;
  if (!s.selected) return stepSelection(1);
  const kids = m.children.get(s.selected);
  if (kids?.length) return s.select(kids[0], { focus: true });
  const arts = m.artifactsBySpan.get(s.selected);
  if (arts?.length) s.select(arts[0].id, { focus: true });
}

export function nextError() {
  const s = st();
  const m = s.model;
  if (!m) return;
  const errs = m.order.filter((id) => m.spans.get(id)!.status === "error");
  if (!errs.length) return s.set("toast", "No failures in this run");
  const cur = s.selected ? m.order.indexOf(s.selected) : -1;
  const next = errs.find((id) => m.order.indexOf(id) > cur) ?? errs[0];
  s.select(next, { focus: true });
}

export function finalAnswer() {
  const s = st();
  const f = s.model?.finalArtifact;
  if (f) s.select(f.id, { focus: true });
  else s.set("toast", "This run has no final artifact");
}

export function stepRun(delta: number) {
  const s = st();
  const i = s.runs.findIndex((r) => r.id === s.runId);
  const next = s.runs[Math.min(s.runs.length - 1, Math.max(0, i + delta))];
  if (next && next.id !== s.runId) s.openRun(next.id);
}

export function toggleSelected() {
  const s = st();
  const m = s.model;
  if (!m || !s.selected) return;
  if ((m.children.get(s.selected)?.length ?? 0) > 0) s.toggleCollapsed(s.selected);
}

export function copyLink() {
  navigator.clipboard?.writeText(location.href).then(() => st().set("toast", "Deep link copied"));
}

export function actions(): Action[] {
  const s = st();
  return [
    { id: "final", label: "Jump to final answer", keys: "a", pinned: true, keywords: "answer result output report", run: finalAnswer },
    { id: "error", label: "Jump to next failure", keys: "e", pinned: true, keywords: "error failed exception", run: nextError },
    { id: "replay", label: s.playing ? "Pause replay" : "Replay run", keys: "space", pinned: true, keywords: "play time travel", run: () => s.setPlaying(!s.playing) },
    { id: "evidence", label: `${s.showEvidence ? "Hide" : "Show"} evidence links`, keys: "v", keywords: "provenance citations", run: () => s.set("showEvidence", !s.showEvidence) },
    { id: "timeline", label: `${s.showTimeline ? "Hide" : "Show"} timeline`, keys: "t", run: () => s.set("showTimeline", !s.showTimeline) },
    { id: "fit", label: "Fit graph to screen", keys: "f", run: () => s.set("fitTick", s.fitTick + 1) },
    { id: "expand", label: "Expand all", keywords: "open subagents", run: s.expandAll },
    { id: "collapse", label: "Collapse subagents", keywords: "close fold", run: s.collapseAll },
    { id: "copy", label: "Copy deep link", keys: "c", keywords: "share url", run: copyLink },
    { id: "help", label: "Keyboard shortcuts", keys: "?", run: () => s.set("helpOpen", true) },
    { id: "connect", label: "Connect Claude Code, Kiro and other agents", keywords: "watch hooks import sessions", pinned: true, run: () => s.set("connectOpen", true) },
    { id: "compare", label: "Compare with another run…", keywords: "versus diff harness models cost speed", pinned: true, run: () => s.set("toast", "Type “compare” and a run name, or shift-click a run in the list") },
    { id: "follow", label: s.follow ? "Stop following the live run" : "Follow the live run", keywords: "live tail", run: () => s.set("follow", !s.follow) },
    ...(s.backend.exportUrl && s.runId
      ? [{ id: "export", label: "Download run as HTML", keywords: "export share offline", run: () => window.open(s.backend.exportUrl!(s.runId!), "_blank") }]
      : []),
    { id: "root", label: "Show run overview", keywords: "summary", run: () => s.select(null) },
  ].filter((a) => a.id !== "root" || s.selected) as Action[];
}

export { ROOT };
