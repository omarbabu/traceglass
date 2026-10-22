import { Check, Copy, Plug, X } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { fmtRelative } from "../model/format";
import { sourceStyle } from "../model/sources";
import { useStore } from "../store";

interface HookInfo {
  installed?: { scope?: string; agent?: string; path: string }[];
  last_event?: number | null;
}

interface Status {
  version?: string;
  db?: string;
  watcher?: { alive?: boolean; pid?: number | null; last_beat?: number | null; in_process?: boolean };
  hooks?: Record<string, HookInfo>;
  sources?: Record<string, { available?: boolean; dir?: string; runs?: number }>;
}

function Cmd({ children }: { children: string }) {
  const [done, setDone] = useState(false);
  return (
    <div className="cmd">
      <span className="mono grow ellipsis">
        <span className="faint">$ </span>
        {children}
      </span>
      <button
        className="icon-btn"
        title="Copy"
        onClick={() =>
          navigator.clipboard?.writeText(children).then(() => {
            setDone(true);
            setTimeout(() => setDone(false), 1200);
          })
        }
      >
        {done ? <Check size={13} /> : <Copy size={13} />}
      </button>
    </div>
  );
}

function State({ ok, children }: { ok: boolean | undefined; children: ReactNode }) {
  return (
    <span className="conn-state">
      <span className="dot" style={{ ["--c" as any]: ok ? "var(--s-ok)" : "var(--s-muted)" }} />
      {children}
    </span>
  );
}

function AgentCard({ id, status, blurb, hookCmd, importCmd }: { id: string; status: Status | null; blurb: string; hookCmd: string; importCmd: string }) {
  const src = sourceStyle(id);
  const s = status?.sources?.[id];
  const h = status?.hooks?.[id];
  const installed = h?.installed ?? [];
  const watching = status?.watcher?.alive || status?.watcher?.in_process;
  return (
    <div className="conn-card" style={{ ["--c" as any]: src.color }}>
      <div className="row">
        <span className="src-badge">{src.label}</span>
        <span className="grow" />
        {status && (s?.available ? <State ok>{s.runs ?? 0} sessions recorded</State> : <State ok={false}>not found on this machine</State>)}
      </div>
      <p className="muted">{blurb}</p>
      <div className="conn-steps">
        <div className="conn-step">
          <div className="row">
            <b>1 · Watch sessions</b>
            <span className="grow" />
            {status && <State ok={watching}>{watching ? "watcher running" : "not watching"}</State>}
          </div>
          <span className="faint">Running and closed sessions stream in as their transcripts change.</span>
          <Cmd>traceglass serve --watch --open</Cmd>
        </div>
        <div className="conn-step">
          <div className="row">
            <b>2 · Exact live timing</b> <span className="faint">(optional)</span>
            <span className="grow" />
            {status && (
              <State ok={installed.length > 0}>
                {installed.length ? `hooks in ${installed.map((i) => i.scope ?? i.agent).join(", ")}` : "hooks not installed"}
                {h?.last_event ? ` · last event ${fmtRelative(h.last_event)}` : ""}
              </State>
            )}
          </div>
          <span className="faint">Lifecycle hooks record each tool call the moment it starts and ends. They run asynchronously and print nothing.</span>
          <Cmd>{hookCmd + " --dry-run"}</Cmd>
          <Cmd>{hookCmd}</Cmd>
        </div>
        <div className="conn-step">
          <b>3 · Backfill history</b>
          <Cmd>{importCmd}</Cmd>
        </div>
      </div>
    </div>
  );
}

export function ConnectPanel() {
  const open = useStore((s) => s.connectOpen);
  const set = useStore((s) => s.set);
  const mode = useStore((s) => s.backend.mode);
  const [status, setStatus] = useState<Status | null>(null);
  useEffect(() => {
    if (!open || mode !== "server") return;
    let alive = true;
    const load = () =>
      fetch("api/status")
        .then((r) => (r.ok ? r.json() : null))
        .then((j) => alive && setStatus(j))
        .catch(() => alive && setStatus(null));
    load();
    const t = setInterval(load, 3000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [open, mode]);
  if (!open) return null;
  return (
    <div className="overlay" onMouseDown={() => set("connectOpen", false)}>
      <div className="connect" onMouseDown={(e) => e.stopPropagation()}>
        <div className="row" style={{ marginBottom: 4 }}>
          <Plug size={16} color="var(--accent-ink)" />
          <h3 style={{ margin: 0 }}>Connect your agents</h3>
          <span className="grow" />
          <button className="icon-btn" onClick={() => set("connectOpen", false)}>
            <X size={15} />
          </button>
        </div>
        <p className="muted" style={{ marginTop: 4 }}>
          Traceglass reads coding-agent sessions straight from disk — nothing to change in how you work. Everything stays on this machine.
        </p>
        <div className="conn-grid">
          <AgentCard
            id="claude-code"
            status={status}
            blurb="Every session, turn, model call, tool call and subagent — file edits become reviewable diffs."
            hookCmd="traceglass hooks install claude-code"
            importCmd="traceglass import claude-code --all"
          />
          <AgentCard
            id="kiro"
            status={status}
            blurb="Kiro CLI sessions with turns, tool calls and each call's stated purpose; subagent sessions are grouped with their parent."
            hookCmd="traceglass hooks install kiro"
            importCmd="traceglass import kiro --all"
          />
        </div>
        <div className="conn-card" style={{ marginTop: 12 }}>
          <b>Your own agents</b>
          <p className="muted">
            Python: <span className="mono">import traceglass as tg</span> · LangChain/LangGraph: <span className="mono">with tg.observe():</span> · OpenTelemetry:
            point an OTLP/HTTP JSON exporter at <span className="mono">/v1/traces</span> · anything else: <span className="mono">POST /api/ingest</span>.
          </p>
        </div>
        {mode !== "server" && <div className="faint" style={{ marginTop: 10 }}>This is a snapshot; live status is available when running <span className="mono">traceglass serve</span>.</div>}
      </div>
    </div>
  );
}
