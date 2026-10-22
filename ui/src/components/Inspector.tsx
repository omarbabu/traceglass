import { CircleAlert, CircleCheck, Copy, Download, Info, Link2, Quote, ShieldAlert, Star, X } from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { fmtClock, fmtCost, fmtDuration, fmtOffset, fmtTokens, preview } from "../model/format";
import { lineage, spanDuration, TASK, type GraphModel } from "../model/graph";
import { artifactIcon, relation, spanKind, statusStyle } from "../model/kinds";
import { useStore } from "../store";
import { isEstimated, runActivity, type Artifact, type Link, type Run, type Span } from "../types";
import { ACTIVITY, sourceOf, sourceStyle } from "../model/sources";
import { ArtifactBody, quoteContext } from "./ArtifactView";
import { JsonView, Payload } from "./Payload";
import { subtitle } from "./nodes";

function copy(text: string, setToast: (s: string) => void, msg = "Link copied") {
  navigator.clipboard?.writeText(text).then(
    () => setToast(msg),
    () => setToast("Copy failed"),
  );
}

export function Ref({ id, rel }: { id: string; rel?: string }) {
  const model = useStore((s) => s.model);
  const select = useStore((s) => s.select);
  if (!model) return null;
  const s = model.spans.get(id);
  const a = model.artifacts.get(id);
  if (!s && !a) return <span className="mono faint">{id}</span>;
  const Icon = s ? spanKind(s.kind).icon : artifactIcon(a!.kind);
  const color = s ? spanKind(s.kind).color : "var(--k-artifact)";
  return (
    <div className="ref-row" style={{ ["--c" as any]: color }} onClick={() => select(id, { focus: true })}>
      <Icon size={13} className="icon" />
      <span className="ellipsis">{s ? s.name : a!.name}</span>
      {s?.status === "error" && <CircleAlert size={12} color="var(--s-error)" />}
      {rel && <span className="rel">{rel}</span>}
    </div>
  );
}

function InlineRef({ id }: { id: string }) {
  const model = useStore((s) => s.model);
  const select = useStore((s) => s.select);
  const s = model?.spans.get(id);
  const a = model?.artifacts.get(id);
  if (!s && !a) return <span className="mono faint">{id}</span>;
  return (
    <span className="ref" onClick={() => select(id, { focus: true })}>
      <span className="ellipsis">{s?.name ?? a?.name}</span>
    </span>
  );
}

function Tabs({ tabs, value, onChange }: { tabs: { id: string; label: string; n?: number }[]; value: string; onChange: (v: string) => void }) {
  return (
    <div className="tabs">
      {tabs.map((t) => (
        <button key={t.id} className={`tab ${value === t.id ? "on" : ""}`} onClick={() => onChange(t.id)}>
          {t.label}
          {t.n ? <span className="n">{t.n}</span> : null}
        </button>
      ))}
    </div>
  );
}

function Section({ title, children, icon }: { title: string; children: ReactNode; icon?: ReactNode }) {
  return (
    <div className="section">
      <div className="section-title">
        {icon}
        {title}
      </div>
      {children}
    </div>
  );
}

function Metric({ label, value, sub }: { label: string; value: ReactNode; sub?: ReactNode }) {
  return (
    <div className="metric">
      <div className="label">{label}</div>
      <div className="value">
        {value} {sub ? <small>{sub}</small> : null}
      </div>
    </div>
  );
}

function Honesty() {
  return (
    <div className="honesty">
      Traceglass shows what the instrumentation observed: inputs, outputs, tool calls and declared evidence. It does not reveal a model's private
      reasoning, and a well-supported graph is not proof that an answer is correct.
    </div>
  );
}

function Header({ kind, color, icon, title, right, onClose }: { kind: string; color: string; icon: ReactNode; title: string; right?: ReactNode; onClose?: () => void }) {
  return (
    <div className="insp-head">
      <div className="row">
        <div className="insp-kind" style={{ ["--c" as any]: color }}>
          {icon}
          {kind}
        </div>
        <div className="grow" />
        {right}
        {onClose && (
          <button className="icon-btn" onClick={onClose} title="Close (Esc)">
            <X size={15} />
          </button>
        )}
      </div>
      <div className="insp-title">{title}</div>
    </div>
  );
}

function StatusPill({ status, dur }: { status: string; dur?: number | null }) {
  const st = statusStyle(status);
  return (
    <span className="pill" style={{ ["--c" as any]: st.color }}>
      <st.icon size={11} className={status === "running" ? "spin" : undefined} />
      {st.label}
      {dur != null ? ` · ${fmtDuration(dur)}` : ""}
    </span>
  );
}

// ------------------------------------------------------------------------------------- run
function RunInspector({ m }: { m: GraphModel }) {
  const run = m.run;
  const select = useStore((s) => s.select);
  const backend = useStore((s) => s.backend);
  const fin = m.finalArtifact;
  const dur = run.ended_at && run.started_at ? run.ended_at - run.started_at : m.t1 - m.t0;
  const errors = [...m.spans.values()].filter((s) => s.status === "error");
  const timeScale = Number(run.metadata?.["time_scale"] ?? 0);
  const meta = { ...(run.metadata ?? {}) };
  for (const k of ["time_scale", "activity", "timing", "parent_run_id", "parent_inferred", "agent_tool"]) delete meta[k];
  const exportUrl = backend.exportUrl?.(run.id);
  const coding = run.source === "claude-code" || run.source === "kiro";
  // Remembered across selections, so returning from a step lands back in the conversation.
  const tab = useStore((s) => s.runTab);
  const setTab = (t: string) => useStore.getState().set("runTab", t);
  return (
    <>
      <Header
        kind="Run"
        color="var(--accent)"
        icon={<Star size={12} />}
        title={run.name}
        right={
          <>
            <StatusPill status={run.status ?? "ok"} />
            {exportUrl && (
              <a className="icon-btn" href={exportUrl} title="Download as a self-contained HTML file" download>
                <Download size={15} />
              </a>
            )}
          </>
        }
      />
      {coding && (
        <Tabs
          value={tab}
          onChange={setTab}
          tabs={[
            { id: "overview", label: "Overview" },
            { id: "conversation", label: "Conversation", n: (m.children.get("__root__") ?? []).length },
          ]}
        />
      )}
      {tab === "conversation" && coding ? (
        <Conversation m={m} />
      ) : (
      <div className="insp-body">
        {timeScale > 1 && (
          <div className="notice">
            <Info size={14} />
            <span>Demo run, recorded at {timeScale}× time compression. Durations show simulated time.</span>
          </div>
        )}
        <SessionNotices run={run} />
        <div className="metrics">
          <Metric label="Duration" value={fmtDuration(dur)} />
          <Metric label="Tokens" value={m.totals.tokens ? fmtTokens(m.totals.tokens) : "—"} sub={!m.totals.tokens && runActivity(run) === "working" ? "at turn end" : undefined} />
          <Metric label="Cost" value={fmtCost(m.totals.cost)} />
          <Metric label="Steps" value={m.spans.size} />
          <Metric label="Errors" value={<span style={{ color: errors.length ? "var(--s-error)" : undefined }}>{errors.length}</span>} />
          <Metric label="Artifacts" value={m.artifacts.size} />
        </div>
        {fin && (
          <Section title="Final answer" icon={<Star size={11} />}>
            <div className="evidence-card" style={{ cursor: "pointer" }} onClick={() => select(fin.id, { focus: true })}>
              <div className="row" style={{ fontWeight: 600 }}>
                {fin.name}
                <span className="grow" />
                <span className="faint" style={{ fontSize: 11.5 }}>
                  {(m.linksBySubject.get(fin.id) ?? []).length} evidence links →
                </span>
              </div>
              <div className="muted" style={{ marginTop: 6, fontSize: 12.5 }}>
                {preview(typeof fin.content === "string" ? fin.content.replace(/[#*_`>]/g, "") : fin.content, 260)}
              </div>
            </div>
          </Section>
        )}
        <FilesChanged m={m} />
        <RelatedRuns run={run} />
        {errors.length > 0 && (
          <Section title="Failures" icon={<CircleAlert size={11} />}>
            <div className="ref-list">
              {errors.slice(0, 6).map((s) => (
                <Ref key={s.id} id={s.id} rel={s.error?.type} />
              ))}
            </div>
            {errors.length > 6 && (
              <div className="faint" style={{ fontSize: 12, padding: "4px 8px" }}>
                and {errors.length - 6} more — press <span className="kbd">e</span> to step through failures
              </div>
            )}
          </Section>
        )}
        <Section title="Task">
          <Payload value={run.input} present={run.input !== undefined} />
        </Section>
        {run.output !== undefined && (
          <Section title="Output">
            <Payload value={run.output} present />
          </Section>
        )}
        <Section title="Details">
          <div className="kv">
            <span className="k">Project</span>
            <span className="v">{run.project ?? "—"}</span>
            {run.session && (
              <>
                <span className="k">Session</span>
                <span className="v mono">{run.session}</span>
              </>
            )}
            <span className="k">Started</span>
            <span className="v">{fmtClock(run.started_at ?? m.t0)}</span>
            <span className="k">Source</span>
            <span className="v">{run.source ?? "sdk"}</span>
            <span className="k">Run ID</span>
            <span className="v mono">{run.id}</span>
            {run.tags?.length ? (
              <>
                <span className="k">Tags</span>
                <span className="v">{run.tags.join(", ")}</span>
              </>
            ) : null}
          </div>
        </Section>
        {Object.keys(meta).length > 0 && (
          <Section title="Metadata">
            <div className="payload-box">
              <JsonView value={meta} />
            </div>
          </Section>
        )}
        <Honesty />
      </div>
      )}
    </>
  );
}

function textOf(v: unknown): string {
  if (typeof v === "string") return v;
  const c = (v as any)?.content;
  if (Array.isArray(c))
    return c
      .filter((b: any) => b?.type === "text" && typeof b.text === "string")
      .map((b: any) => b.text)
      .join("\n");
  return "";
}

/** The session as a readable transcript — every line jumps to its node in the graph. */
function Conversation({ m }: { m: GraphModel }) {
  const select = useStore((s) => s.select);
  const selected = useStore((s) => s.selected);
  const turns = m.children.get("__root__") ?? [];
  const [limit, setLimit] = useState(40);
  return (
    <div className="insp-body conversation">
      {turns.slice(-limit).map((tid) => {
        const t = m.spans.get(tid)!;
        const prompt = typeof t.input === "string" ? t.input : textOf(t.input) || t.name;
        const steps = m.children.get(tid) ?? [];
        return (
          <div key={tid} className="conv-turn">
            <div className={`conv-prompt ${selected === tid ? "on" : ""}`} onClick={() => select(tid, { focus: true })}>
              <div className="conv-who">You</div>
              <div className="conv-text">{prompt.length > 600 ? prompt.slice(0, 600) + "…" : prompt}</div>
            </div>
            {steps.map((sid) => {
              const sp = m.spans.get(sid)!;
              const k = spanKind(sp.kind);
              if (sp.kind === "llm") {
                const said = textOf(sp.output).trim();
                if (!said) return null;
                return (
                  <div key={sid} data-id={sid} className={`conv-reply ${selected === sid ? "on" : ""}`} onClick={() => select(sid, { focus: true })}>
                    {said.length > 700 ? said.slice(0, 700) + "…" : said}
                  </div>
                );
              }
              const sub = subtitle(sp);
              const kids = m.descendants.get(sid) ?? 0;
              return (
                <div
                  key={sid}
                  data-id={sid}
                  className={`conv-step ${selected === sid ? "on" : ""} ${sp.status}`}
                  style={{ ["--c" as any]: k.color }}
                  onClick={() => select(sid, { focus: true })}
                >
                  <k.icon size={12} className="icon" />
                  <span className="conv-tool">{sp.name}</span>
                  <span className="ellipsis faint">{sub}</span>
                  {kids > 0 && <span className="badge">{kids} steps</span>}
                  {sp.status === "error" && <CircleAlert size={12} color="var(--s-error)" />}
                  {sp.status === "running" && <span className="dot live" style={{ ["--c" as any]: "var(--s-running)" }} />}
                </div>
              );
            })}
          </div>
        );
      })}
      {turns.length > limit && (
        <button className="btn" style={{ marginTop: 8 }} onClick={() => setLimit(limit + 40)}>
          Show {Math.min(40, turns.length - limit)} earlier turns
        </button>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------- coding sessions
function SessionNotices({ run }: { run: Run }) {
  const act = runActivity(run);
  const timing = run.metadata?.["timing"];
  const src = sourceStyle(sourceOf(run));
  const hookCmd = sourceOf(run) === "kiro" ? "traceglass hooks install kiro" : "traceglass hooks install claude-code";
  return (
    <>
      {act && (
        <div className="notice" style={{ borderColor: `color-mix(in srgb, ${ACTIVITY[act].color} 40%, transparent)` }}>
          <span className={`dot ${act === "working" ? "live" : ""}`} style={{ ["--c" as any]: ACTIVITY[act].color, marginTop: 5 }} />
          <span>
            <b>{ACTIVITY[act].label}</b> — {ACTIVITY[act].hint.toLowerCase()}. {src.label} session
            {act === "working" ? "; new steps appear as the agent takes them." : "."}
          </span>
        </div>
      )}
      {(timing === "estimated" || timing === "mixed") && (
        <div className="notice">
          <Info size={14} />
          {timing === "estimated" ? (
            <span>
              Step timing is estimated: {src.label} timestamps whole turns, not individual steps, so durations marked <span className="est">~</span> are
              spread evenly across each turn. For exact timing, run <span className="mono">{hookCmd}</span>.
            </span>
          ) : (
            <span>
              Tool timing is exact (recorded by hooks). Model-call timing between tool calls is interpolated and marked <span className="est">~</span>.
            </span>
          )}
        </div>
      )}
    </>
  );
}

/** "Where did this change come from?" — every file the agent touched, newest edit first. */
function FilesChanged({ m }: { m: GraphModel }) {
  const select = useStore((s) => s.select);
  const files = useMemo(() => {
    const by = new Map<string, Artifact[]>();
    for (const a of m.artifacts.values()) {
      if (a.kind !== "diff" && a.kind !== "code") continue;
      const path = String(a.metadata?.["path"] ?? a.name);
      if (!by.has(path)) by.set(path, []);
      by.get(path)!.push(a);
    }
    return [...by.entries()].sort((x, y) => (y[1].at(-1)?.created_at ?? 0) - (x[1].at(-1)?.created_at ?? 0));
  }, [m]);
  if (!files.length) return null;
  const cwd = String(m.run.metadata?.["cwd"] ?? "");
  const short = (p: string) => {
    const rel = cwd && p.startsWith(cwd.replace(/\/$/, "") + "/") ? p.slice(cwd.replace(/\/$/, "").length + 1) : p;
    return rel.length > 46 ? "…" + rel.slice(-45) : rel;
  };
  return (
    <Section title={`Files changed · ${files.length}`}>
      <div className="ref-list">
        {files.slice(0, 12).map(([path, arts]) => (
          <div key={path} className="file-row" data-path={short(path)} title={path} onClick={() => select(arts[arts.length - 1].id, { focus: true })}>
            <span className="ellipsis">{short(path)}</span>
            <span className="n">
              {arts.length} {arts.length === 1 ? "edit" : "edits"}
            </span>
          </div>
        ))}
      </div>
      {files.length > 12 && <div className="faint" style={{ fontSize: 12, padding: "4px 8px" }}>and {files.length - 12} more</div>}
    </Section>
  );
}

function RelatedRuns({ run }: { run: Run }) {
  const runs = useStore((s) => s.runs);
  const openRun = useStore((s) => s.openRun);
  const parentId = run.metadata?.["parent_run_id"] as string | undefined;
  const parent = parentId ? runs.find((r) => r.id === parentId) : undefined;
  const children = runs.filter((r) => r.metadata?.["parent_run_id"] === run.id);
  if (!parentId && !children.length) return null;
  const inferred = Boolean(run.metadata?.["parent_inferred"]) || children.some((c) => c.metadata?.["parent_inferred"]);
  return (
    <Section title="Related sessions">
      <div className="ref-list">
        {parentId && (
          <div className="ref-row" onClick={() => parent && openRun(parentId)} style={{ ["--c" as any]: "var(--accent)" }}>
            <span className="ellipsis">{parent?.name ?? parentId}</span>
            <span className="rel">spawned this</span>
          </div>
        )}
        {children.slice(0, 10).map((c) => (
          <div key={c.id} className="ref-row" onClick={() => openRun(c.id)}>
            <span className={`dot ${runActivity(c) === "working" ? "live" : ""}`} style={{ ["--c" as any]: runActivity(c) ? ACTIVITY[runActivity(c)!].color : "var(--muted)" }} />
            <span className="ellipsis">{c.name}</span>
            <span className="rel">subagent</span>
          </div>
        ))}
      </div>
      {inferred && <div className="faint" style={{ fontSize: 11.5, padding: "4px 8px" }}>Linked by timing and working directory — the source does not record it.</div>}
    </Section>
  );
}

// ------------------------------------------------------------------------------------ span
function SpanInspector({ m, s }: { m: GraphModel; s: Span }) {
  const [tab, setTab] = useState("overview");
  const select = useStore((st) => st.select);
  const set = useStore((st) => st.set);
  useEffect(() => setTab((t) => (t === "lineage" || t === "events" || t === "input" || t === "output" ? t : "overview")), [s.id]);
  const k = spanKind(s.kind);
  const events = m.eventsBySpan.get(s.id) ?? [];
  const lin = useMemo(() => lineage(m, [s.id]), [m, s.id]);
  const produced = m.artifactsBySpan.get(s.id) ?? [];
  const retries = m.retries.get(s.id) ?? [];
  const dur = spanDuration(s);
  const kids = m.children.get(s.id) ?? [];
  const attrs = s.attributes ?? {};
  return (
    <>
      <Header
        kind={k.label}
        color={k.color}
        icon={<k.icon size={12} />}
        title={s.name}
        right={
          <>
            <StatusPill status={s.status} dur={dur} />
            <button className="icon-btn" title="Copy deep link" onClick={() => copy(location.href, (t) => set("toast", t))}>
              <Link2 size={15} />
            </button>
          </>
        }
        onClose={() => select(null)}
      />
      <Tabs
        value={tab}
        onChange={setTab}
        tabs={[
          { id: "overview", label: "Overview" },
          { id: "input", label: "Input" },
          { id: "output", label: "Output" },
          { id: "events", label: "Events", n: events.length },
          { id: "lineage", label: "Lineage", n: lin.upstream.size + lin.downstream.size },
        ]}
      />
      <div className="insp-body">
        {tab === "overview" && (
          <>
            {typeof (attrs["purpose"] ?? (s.input as any)?.description) === "string" && (s.kind === "tool" || s.kind === "delegation") && (
              <div className="purpose" title="The agent's stated reason for this call">
                “{String(attrs["purpose"] ?? (s.input as any)?.description)}”
              </div>
            )}
            {isEstimated(s) && (
              <div className="notice">
                <Info size={14} />
                <span>Timing estimated — this source records when the turn started and ended, not each step.</span>
              </div>
            )}
            {s.status === "error" && (
              <div className="error-box">
                <div className="etype">{s.error?.type ?? "Error"}</div>
                <div className="emsg">{s.error?.message ?? "This step failed."}</div>
                {s.error?.stack && (
                  <details>
                    <summary className="muted" style={{ cursor: "pointer", fontSize: 12, marginTop: 6 }}>
                      Stack trace
                    </summary>
                    <pre className="mono">{s.error.stack}</pre>
                  </details>
                )}
                {(retries.length > 0 || lin.downstream.size > 0) && (
                  <div style={{ marginTop: 10 }}>
                    <div className="section-title" style={{ marginBottom: 4 }}>
                      <ShieldAlert size={11} /> Downstream effects
                    </div>
                    <div className="ref-list">
                      {retries.map((r) => (
                        <Ref key={r} id={r} rel={`retry → ${m.spans.get(r)?.status === "ok" ? "recovered" : m.spans.get(r)?.status ?? ""}`} />
                      ))}
                      {[...lin.downstream]
                        .filter((d) => !retries.includes(d))
                        .slice(0, 8)
                        .map((d) => (
                          <Ref key={d} id={d} rel={lin.edges.find((e) => e.target === d)?.relation === "failed" ? "failed with it" : "affected"} />
                        ))}
                    </div>
                  </div>
                )}
              </div>
            )}
            <div className="metrics">
              <Metric label="Duration" value={(isEstimated(s) ? "~" : "") + fmtDuration(dur)} />
              {s.usage ? (
                <>
                  <Metric label="Tokens" value={fmtTokens(s.usage.input_tokens)} sub={`in · ${fmtTokens(s.usage.output_tokens)} out`} />
                  <Metric label="Cost" value={fmtCost(s.usage.cost_usd)} />
                </>
              ) : (
                <>
                  <Metric label="Started" value={fmtOffset((s.started_at ?? m.t0) - m.t0)} />
                  <Metric label={kids.length ? "Steps" : "Attempt"} value={kids.length ? m.descendants.get(s.id) : s.attempt ?? 1} />
                </>
              )}
            </div>
            <Section title="Details">
              <div className="kv">
                <span className="k">Kind</span>
                <span className="v">{k.label}</span>
                {s.agent && (
                  <>
                    <span className="k">Agent</span>
                    <span className="v">{s.agent}</span>
                  </>
                )}
                {s.model && (
                  <>
                    <span className="k">Model</span>
                    <span className="v mono">{s.model}</span>
                  </>
                )}
                <span className="k">Started</span>
                <span className="v">
                  {fmtClock(s.started_at)} <span className="faint">({fmtOffset((s.started_at ?? m.t0) - m.t0)})</span>
                </span>
                {s.parent_id && m.spans.has(s.parent_id) && (
                  <>
                    <span className="k">Parent</span>
                    <span className="v">
                      <InlineRef id={s.parent_id} />
                    </span>
                  </>
                )}
                {s.depends_on?.length ? (
                  <>
                    <span className="k">Depends on</span>
                    <span className="v" style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                      {s.depends_on.map((d) => (
                        <InlineRef key={d} id={d} />
                      ))}
                    </span>
                  </>
                ) : null}
                {s.retry_of && (
                  <>
                    <span className="k">Retry of</span>
                    <span className="v">
                      <InlineRef id={s.retry_of} /> <span className="faint">· attempt {s.attempt ?? "?"}</span>
                    </span>
                  </>
                )}
                {s.usage?.cache_read_tokens ? (
                  <>
                    <span className="k">Cache reads</span>
                    <span className="v">{fmtTokens(s.usage.cache_read_tokens)} tokens</span>
                  </>
                ) : null}
                <span className="k">Span ID</span>
                <span className="v mono">{s.id}</span>
              </div>
            </Section>
            {produced.length > 0 && (
              <Section title="Produced">
                <div className="ref-list">
                  {produced.map((a) => (
                    <Ref key={a.id} id={a.id} rel={a.kind} />
                  ))}
                </div>
              </Section>
            )}
            {Object.keys(attrs).length > 0 && (
              <Section title="Attributes">
                <div className="payload-box">
                  <JsonView value={attrs} />
                </div>
              </Section>
            )}
          </>
        )}
        {tab === "input" && <Payload value={s.input} present={s.input !== undefined} />}
        {tab === "output" && <Payload value={s.output} present={s.output !== undefined} />}
        {tab === "events" &&
          (events.length ? (
            events.map((e) => (
              <div key={e.id} className={`event ${e.level === "error" ? "error" : e.level === "warning" || e.level === "warn" ? "warn" : ""}`}>
                <span className="t">{e.ts ? fmtOffset(e.ts - m.t0) : ""}</span>
                <span className="dot" style={{ ["--c" as any]: e.level === "error" ? "var(--s-error)" : e.kind === "retry" ? "var(--s-waiting)" : "var(--muted)", marginTop: 6 }} />
                <div>
                  <div className="name">
                    {e.name} <span className="faint" style={{ fontWeight: 400 }}>· {e.kind}</span>
                  </div>
                  {e.message && <div className="muted">{e.message}</div>}
                  {e.data != null && (
                    <div style={{ marginTop: 4 }}>
                      <JsonView value={e.data} />
                    </div>
                  )}
                </div>
              </div>
            ))
          ) : (
            <div className="empty-payload">No events were recorded for this step.</div>
          ))}
        {tab === "lineage" && (
          <>
            <Section title={`Upstream · ${lin.upstream.size}`}>
              {lin.upstream.size ? (
                <div className="ref-list">
                  {[...lin.upstream].map((id) => (
                    <Ref key={id} id={id} rel={lin.edges.find((e) => e.source === id)?.relation.replace(/_/g, " ")} />
                  ))}
                </div>
              ) : (
                <div className="empty-payload">No declared inputs, dependencies or evidence.</div>
              )}
            </Section>
            <Section title={`Downstream · ${lin.downstream.size}`}>
              {lin.downstream.size ? (
                <div className="ref-list">
                  {[...lin.downstream].map((id) => (
                    <Ref key={id} id={id} rel={lin.edges.find((e) => e.target === id)?.relation.replace(/_/g, " ")} />
                  ))}
                </div>
              ) : (
                <div className="empty-payload">Nothing recorded as consuming this step's output.</div>
              )}
            </Section>
            <div className="notice">
              <Info size={14} />
              <span>Lineage follows declared dependencies, artifacts and evidence links — the observable execution path.</span>
            </div>
          </>
        )}
      </div>
    </>
  );
}

// -------------------------------------------------------------------------------- artifact
function EvidenceCard({ m, link }: { m: GraphModel; link: Link }) {
  const ev = m.artifacts.get(link.evidence.id);
  const evSpan = m.spans.get(link.evidence.id);
  const ctx = quoteContext(ev, link);
  const r = relation(link.relation);
  return (
    <div className={`evidence-card ${r.tone === "contradict" ? "contradict" : ""}`}>
      <div className="row">
        <span className="pill" style={{ ["--c" as any]: r.tone === "contradict" ? "var(--contradict)" : r.tone === "support" ? "var(--support)" : "var(--muted)" }}>
          {r.label}
        </span>
        <span className="grow" />
        {link.confidence != null && <span className="faint" style={{ fontSize: 11.5 }}>confidence {Math.round(link.confidence * 100)}%</span>}
      </div>
      {link.subject.anchor?.text && <div className="claim">“{link.subject.anchor.text}”</div>}
      <div style={{ margin: "6px 0" }}>
        <Ref id={link.evidence.id} rel={ev ? ev.kind : evSpan ? spanKind(evSpan.kind).label : undefined} />
      </div>
      {ctx ? (
        <div className="source-quote">
          <Quote size={11} className="faint" /> {ctx.before}
          <mark className="quote">{ctx.quote}</mark>
          {ctx.after}
        </div>
      ) : link.evidence.anchor?.text ? (
        <div className="source-quote">
          <mark className="quote">{link.evidence.anchor.text}</mark>
        </div>
      ) : null}
      {link.note && <div className="muted" style={{ marginTop: 6, fontSize: 12 }}>{link.note}</div>}
      <div className="faint" style={{ fontSize: 11, marginTop: 7 }}>
        Declared by the {link.declared_by ?? "agent"} — a recorded claim of support, not a verification.
      </div>
    </div>
  );
}

function ArtifactInspector({ m, a }: { m: GraphModel; a: Artifact }) {
  const [tab, setTab] = useState("preview");
  const select = useStore((s) => s.select);
  const focusLink = useStore((s) => s.focusLink);
  const set = useStore((s) => s.set);
  useEffect(() => setTab("preview"), [a.id]);
  const asSubject = m.linksBySubject.get(a.id) ?? [];
  const asEvidence = m.linksByEvidence.get(a.id) ?? [];
  const focused = focusLink ? [...asSubject, ...asEvidence].find((l) => l.id === focusLink) : undefined;
  // All links sharing the focused claim's anchor are shown together.
  const group = focused
    ? focused.subject.id === a.id
      ? asSubject.filter((l) => l.subject.anchor?.text && l.subject.anchor.text === focused.subject.anchor?.text)
      : [focused]
    : [];
  const Icon = artifactIcon(a.kind);
  const onAnchor = (ids: string[]) => {
    const next = ids.includes(focusLink ?? "") && ids.length > 1 ? ids[(ids.indexOf(focusLink!) + 1) % ids.length] : ids[0];
    select(a.id, { link: next });
  };
  return (
    <>
      <Header
        kind={a.final ? "Final answer" : a.kind}
        color="var(--k-artifact)"
        icon={<Icon size={12} />}
        title={a.name}
        right={
          <button
            className="icon-btn"
            title="Copy content"
            onClick={() => copy(typeof a.content === "string" ? a.content : JSON.stringify(a.content, null, 2), (t) => set("toast", t), "Content copied")}
          >
            <Copy size={15} />
          </button>
        }
        onClose={() => select(null)}
      />
      <Tabs
        value={tab}
        onChange={setTab}
        tabs={[
          { id: "preview", label: "Preview" },
          { id: "evidence", label: "Evidence", n: asSubject.length + asEvidence.length },
          { id: "details", label: "Details" },
        ]}
      />
      <div className="insp-body">
        {tab === "preview" && (
          <>
            {a.review && (
              <div className={`review ${a.review.status}`}>
                {a.review.status === "approved" ? <CircleCheck size={14} color="var(--s-ok)" /> : <CircleAlert size={14} color="var(--contradict)" />}
                <span>
                  <b style={{ textTransform: "capitalize" }}>{a.review.status.replace(/_/g, " ")}</b>
                  {a.review.by && <span className="muted"> by {a.review.by}</span>}
                  {a.review.note && <span className="muted"> — {a.review.note}</span>}
                </span>
              </div>
            )}
            {group.map((l) => (
              <EvidenceCard key={l.id} m={m} link={l} />
            ))}
            {!focused && asSubject.some((l) => l.subject.anchor) && (
              <div className="notice">
                <Quote size={14} />
                <span>Highlighted passages are linked to evidence. Click one to trace it back to its source.</span>
              </div>
            )}
            <ArtifactBody m={m} a={a} focus={focusLink} onAnchor={onAnchor} />
          </>
        )}
        {tab === "evidence" && (
          <>
            <Section title={`This artifact cites · ${asSubject.length}`}>
              {asSubject.length ? (
                <div className="ref-list">
                  {asSubject.map((l) => (
                    <div key={l.id}>
                      <Ref id={l.evidence.id} rel={relation(l.relation).label} />
                      {l.subject.anchor?.text && (
                        <div className="muted" style={{ fontSize: 12, padding: "0 8px 6px 29px", cursor: "pointer" }} onClick={() => select(a.id, { link: l.id })}>
                          “{l.subject.anchor.text}”
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              ) : (
                <div className="empty-payload">No evidence links declared.</div>
              )}
            </Section>
            <Section title={`Cited by · ${asEvidence.length}`}>
              {asEvidence.length ? (
                <div className="ref-list">
                  {asEvidence.map((l) => (
                    <Ref key={l.id} id={l.subject.id} rel={relation(l.relation).inverse} />
                  ))}
                </div>
              ) : (
                <div className="empty-payload">Nothing cites this artifact.</div>
              )}
            </Section>
          </>
        )}
        {tab === "details" && (
          <>
            <div className="kv" style={{ marginBottom: 16 }}>
              <span className="k">Kind</span>
              <span className="v">{a.kind}</span>
              {a.mime && (
                <>
                  <span className="k">MIME</span>
                  <span className="v mono">{a.mime}</span>
                </>
              )}
              {a.size != null && (
                <>
                  <span className="k">Size</span>
                  <span className="v">{a.size.toLocaleString()} chars</span>
                </>
              )}
              <span className="k">Created</span>
              <span className="v">
                {fmtClock(a.created_at)} <span className="faint">({fmtOffset((a.created_at ?? m.t0) - m.t0)})</span>
              </span>
              {a.span_id && m.spans.has(a.span_id) && (
                <>
                  <span className="k">Produced by</span>
                  <span className="v">
                    <InlineRef id={a.span_id} />
                  </span>
                </>
              )}
              {a.previous_id && (
                <>
                  <span className="k">Revises</span>
                  <span className="v">
                    <InlineRef id={a.previous_id} />
                  </span>
                </>
              )}
              {a.uri && (
                <>
                  <span className="k">URI</span>
                  <span className="v mono">{a.uri}</span>
                </>
              )}
              <span className="k">Artifact ID</span>
              <span className="v mono">{a.id}</span>
            </div>
            {a.metadata && Object.keys(a.metadata).length > 0 && (
              <Section title="Metadata">
                <div className="payload-box">
                  <JsonView value={a.metadata} />
                </div>
              </Section>
            )}
          </>
        )}
      </div>
    </>
  );
}

export function Inspector() {
  const model = useStore((s) => s.model);
  const selected = useStore((s) => s.selected);
  if (!model) return <aside className="inspector" />;
  const span = selected ? model.spans.get(selected) : undefined;
  const art = selected ? model.artifacts.get(selected) : undefined;
  return (
    <aside className="inspector">
      {span ? <SpanInspector m={model} s={span} /> : art ? <ArtifactInspector m={model} a={art} /> : <RunInspector m={model} />}
      {selected === TASK && null}
    </aside>
  );
}
