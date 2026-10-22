import { ChevronDown, ChevronRight, Search } from "lucide-react";
import { useMemo, useState } from "react";
import { fmtDuration, fmtRelative, fmtTokens } from "../model/format";
import { statusStyle } from "../model/kinds";
import { ACTIVITY, SOURCES, sourceOf, sourceStyle } from "../model/sources";
import { useStore } from "../store";
import { runActivity, type Run } from "../types";

interface Group {
  project: string;
  sessions: { session: string | null; runs: Run[] }[];
  count: number;
}

function group(runs: Run[]): Group[] {
  const byProject = new Map<string, Map<string | null, Run[]>>();
  for (const r of runs) {
    const p = r.project || "default";
    if (!byProject.has(p)) byProject.set(p, new Map());
    const sessions = byProject.get(p)!;
    const s = r.session ?? null;
    if (!sessions.has(s)) sessions.set(s, []);
    sessions.get(s)!.push(r);
  }
  return [...byProject.entries()].map(([project, sessions]) => ({
    project,
    sessions: [...sessions.entries()].map(([session, runs]) => ({ session, runs })),
    count: [...sessions.values()].reduce((n, r) => n + r.length, 0),
  }));
}

export function SourceBadge({ src }: { src: string }) {
  const st = sourceStyle(src);
  if (src === "sdk") return null;
  return (
    <span className="src-badge" style={{ ["--c" as any]: st.color }} title={`Recorded from ${st.label}`}>
      {st.label}
    </span>
  );
}

function RunRow({ r, active }: { r: Run; active: boolean }) {
  const openRun = useStore((s) => s.openRun);
  const st = statusStyle(r.status);
  const act = runActivity(r);
  const dur = r.ended_at && r.started_at ? r.ended_at - r.started_at : null;
  const color = act ? ACTIVITY[act].color : st.color;
  const live = r.status === "running" || act === "working";
  return (
    <div className={`run-row ${active ? "active" : ""}`} onClick={() => openRun(r.id)} data-run={r.id}>
      <span className={`dot ${live ? "live" : ""}`} style={{ ["--c" as any]: color }} title={act ? ACTIVITY[act].hint : st.label} />
      <div className="grow">
        <div className="name" title={r.name}>
          {r.name}
        </div>
        <div className="meta">
          <SourceBadge src={sourceOf(r)} />
          <span>{fmtRelative(r.started_at)}</span>
          {live ? (
            <span style={{ color: "var(--s-running)" }}>{act ? "working" : "live"}</span>
          ) : act && act !== "idle" ? (
            <span style={{ color: ACTIVITY[act].color }}>{ACTIVITY[act].label.toLowerCase()}</span>
          ) : (
            dur != null && <span>{fmtDuration(dur)}</span>
          )}
          <span>{r.summary?.spans ?? 0} steps</span>
          {r.summary?.tokens ? <span>{fmtTokens(r.summary.tokens)} tok</span> : null}
          {r.summary?.errors ? <span className="err">{r.summary.errors} err</span> : null}
        </div>
      </div>
    </div>
  );
}

export function Navigator() {
  const runs = useStore((s) => s.runs);
  const runId = useStore((s) => s.runId);
  const connected = useStore((s) => s.connected);
  const mode = useStore((s) => s.backend.mode);
  const sourceFilter = useStore((s) => s.sourceFilter);
  const setStore = useStore((s) => s.set);
  const [q, setQ] = useState("");
  const [closed, setClosed] = useState<Set<string>>(new Set());
  const sources = useMemo(() => {
    const c = new Map<string, number>();
    for (const r of runs) c.set(sourceOf(r), (c.get(sourceOf(r)) ?? 0) + 1);
    return [...c.entries()].sort((a, b) => (Object.keys(SOURCES).indexOf(a[0]) + 99) % 99 - ((Object.keys(SOURCES).indexOf(b[0]) + 99) % 99));
  }, [runs]);
  const filtered = useMemo(() => {
    const n = q.trim().toLowerCase();
    return runs.filter(
      (r) =>
        (!sourceFilter || sourceOf(r) === sourceFilter) &&
        (!n || [r.name, r.id, r.project, r.session, r.source, ...(r.tags ?? [])].some((x) => x && x.toLowerCase().includes(n))),
    );
  }, [runs, q, sourceFilter]);
  // Sessions an agent is working in right now, across every project, pinned to the top.
  // Only worth a separate section when the live runs would otherwise be buried in a longer list.
  const liveNow = useMemo(() => {
    const live = filtered.filter((r) => r.status === "running" || runActivity(r) === "working");
    return filtered.length - live.length >= 4 ? live : [];
  }, [filtered]);
  const groups = useMemo(() => group(filtered), [filtered]);
  return (
    <nav className="nav">
      <div className="nav-head">
        <label className="search">
          <Search size={14} />
          <input id="run-search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter runs…" spellCheck={false} />
          <span className="kbd">/</span>
        </label>
        <div className="nav-meta">
          <span>
            {filtered.length} {filtered.length === 1 ? "run" : "runs"}
          </span>
          {mode === "server" ? (
            <span className="row" style={{ gap: 6 }}>
              <span className={`dot ${connected ? "live" : ""}`} style={{ ["--c" as any]: connected ? "var(--s-ok)" : "var(--s-muted)" }} />
              {connected ? "Live" : "Offline"}
            </span>
          ) : (
            <span>{mode === "embedded" ? "Snapshot" : "Static"}</span>
          )}
        </div>
      </div>
      {sources.length > 1 && (
        <div className="src-filter">
          <button className={`src-chip ${!sourceFilter ? "on" : ""}`} onClick={() => setStore("sourceFilter", null)}>
            All
          </button>
          {sources.map(([src, n]) => (
            <button
              key={src}
              className={`src-chip ${sourceFilter === src ? "on" : ""}`}
              style={{ ["--c" as any]: sourceStyle(src).color }}
              onClick={() => setStore("sourceFilter", sourceFilter === src ? null : src)}
            >
              <span className="dot" />
              {sourceStyle(src).label}
              <span className="n">{n}</span>
            </button>
          ))}
        </div>
      )}
      <div className="nav-list">
        {liveNow.length > 0 && (
          <div className="live-now">
            <div className="nav-group-head" style={{ cursor: "default", color: "var(--accent-ink)" }}>
              <span className="dot live" style={{ ["--c" as any]: "var(--s-running)" }} />
              Live now
              <span className="count">{liveNow.length}</span>
            </div>
            {liveNow.map((r) => (
              <RunRow key={"live:" + r.id} r={r} active={r.id === runId} />
            ))}
          </div>
        )}
        {groups.map((g) => {
          const isClosed = closed.has(g.project);
          return (
            <div key={g.project}>
              <div
                className="nav-group-head"
                onClick={() => {
                  const c = new Set(closed);
                  if (c.has(g.project)) c.delete(g.project);
                  else c.add(g.project);
                  setClosed(c);
                }}
              >
                {isClosed ? <ChevronRight size={12} /> : <ChevronDown size={12} />}
                {g.project}
                <span className="count">{g.count}</span>
              </div>
              {!isClosed &&
                g.sessions.map((s) => (
                  <div key={s.session ?? "_"}>
                    {s.session && g.sessions.length > 1 && s.runs.length > 1 && (
                      <div className="nav-session" title={s.session}>
                        session · <span className="ellipsis mono" style={{ fontSize: 10.5 }}>{s.session}</span>
                      </div>
                    )}
                    {s.runs.map((r) => (
                      <RunRow key={r.id} r={r} active={r.id === runId} />
                    ))}
                  </div>
                ))}
            </div>
          );
        })}
        {!filtered.length && runs.length > 0 && <div className="faint" style={{ padding: 14 }}>No runs match “{q}”.</div>}
      </div>
    </nav>
  );
}
