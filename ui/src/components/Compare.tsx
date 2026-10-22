import { ArrowUpRight, Scale, X } from "lucide-react";
import { useMemo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { fmtCost, fmtDuration, fmtTokens, preview } from "../model/format";
import { buildModel, type GraphModel } from "../model/graph";
import { spanKind } from "../model/kinds";
import { runMetrics, type RunMetrics } from "../model/metrics";
import { sourceOf } from "../model/sources";
import { useStore } from "../store";
import { SourceBadge } from "./Navigator";

const SIDE = [
  { key: "A", color: "#8b90ff" },
  { key: "B", color: "#45d0c2" },
];

interface Row {
  label: string;
  get: (m: RunMetrics) => number | null;
  fmt: (v: number) => string;
  /** How to phrase the smaller value: "faster", "fewer"… (smaller is cheaper, not necessarily better). */
  less: string;
}

const ROWS: Row[] = [
  { label: "Wall-clock time", get: (m) => m.wallMs, fmt: fmtDuration, less: "faster" },
  { label: "Model calls", get: (m) => m.modelCalls, fmt: String, less: "fewer" },
  { label: "Tool calls", get: (m) => m.toolCalls, fmt: String, less: "fewer" },
  { label: "Failed tool calls", get: (m) => m.failedTools, fmt: String, less: "fewer" },
  { label: "Tokens in + out", get: (m) => m.totalTokens, fmt: fmtTokens, less: "fewer" },
  { label: "Cache reads", get: (m) => m.cacheReadTokens, fmt: fmtTokens, less: "fewer" },
  { label: "Cost", get: (m) => m.costUsd, fmt: (v) => fmtCost(v), less: "cheaper" },
  { label: "Files changed", get: (m) => m.files.length, fmt: String, less: "fewer" },
];

function modelLabel(m: GraphModel, met: RunMetrics) {
  return met.models.join(", ") || String(m.run.metadata?.["model"] ?? "") || "—";
}

function Lanes({ models, maxMs }: { models: GraphModel[]; maxMs: number }) {
  const ticks: number[] = [];
  const step = [5e3, 10e3, 15e3, 30e3, 60e3, 120e3, 300e3, 600e3].find((s) => maxMs / s <= 8) ?? 600e3;
  for (let t = 0; t <= maxMs; t += step) ticks.push(t);
  return (
    <div className="cmp-lanes">
      <div className="cmp-axis">
        {ticks.map((t) => (
          <span key={t} style={{ left: `${(t / maxMs) * 100}%` }}>
            {fmtDuration(t)}
          </span>
        ))}
      </div>
      {models.map((m, i) => {
        const rows: [string, (k: string) => boolean][] = [
          ["model", (k) => k === "llm"],
          ["tools", (k) => k === "tool" || k === "delegation" || k === "retrieval"],
        ];
        return (
          <div className="cmp-run-lanes" key={m.run.id} style={{ ["--c" as any]: SIDE[i].color }}>
            <div className="cmp-lane-label">
              <span className="cmp-key">{SIDE[i].key}</span>
              <span className="cmp-lane-names">
                <span>model calls</span>
                <span>tool calls</span>
              </span>
            </div>
            <div className="grow">
              {rows.map(([name, test]) => (
                <div className="cmp-lane" key={name}>
                  {[...m.spans.values()]
                    .filter((s) => test(s.kind) && !(m.children.get(s.id)?.length) && s.started_at != null)
                    .map((s) => {
                      const start = s.started_at! - m.t0;
                      const dur = Math.max((s.ended_at ?? s.started_at!) - s.started_at!, maxMs / 600);
                      return (
                        <span
                          key={s.id}
                          className={`cmp-bar ${s.status === "error" ? "error" : ""}`}
                          title={`${s.name} · ${fmtDuration(dur)}`}
                          style={{ left: `${(start / maxMs) * 100}%`, width: `${(dur / maxMs) * 100}%`, ["--k" as any]: spanKind(s.kind).color }}
                        />
                      );
                    })}
                </div>
              ))}
              <div className="cmp-end" style={{ left: `${((m.t1 - m.t0) / maxMs) * 100}%` }} title="run finished" />
            </div>
          </div>
        );
      })}
    </div>
  );
}

export function Compare() {
  const compare = useStore((s) => s.compare);
  const close = useStore((s) => s.closeCompare);
  const openRun = useStore((s) => s.openRun);
  const models = useMemo(() => (compare?.bundles ? compare.bundles.map(buildModel) : null), [compare?.bundles]);
  const metrics = useMemo(() => models?.map(runMetrics) ?? null, [models]);
  if (!compare) return null;
  if (!models || !metrics)
    return (
      <div className="compare">
        <div className="loading-bar" />
      </div>
    );
  const [ma, mb] = models;
  const sameTask = preview(ma.run.input, 400) === preview(mb.run.input, 400);
  const maxMs = Math.max(metrics[0].wallMs, metrics[1].wallMs, 1);
  return (
    <div className="compare">
      <div className="cmp-head">
        <Scale size={16} color="var(--accent-ink)" />
        <h2>Compare runs</h2>
        <span className="grow" />
        <button className="icon-btn" onClick={close} title="Close (Esc)">
          <X size={15} />
        </button>
      </div>
      {sameTask && (
        <div className="cmp-task">
          <span className="faint">Same task</span> {preview(ma.run.input, 220)}
        </div>
      )}
      <div className="cmp-cards">
        {models.map((m, i) => (
          <div key={m.run.id} className="cmp-card" style={{ ["--c" as any]: SIDE[i].color }}>
            <div className="row">
              <span className="cmp-key">{SIDE[i].key}</span>
              <span className="cmp-model mono">{modelLabel(m, metrics[i])}</span>
              <SourceBadge src={sourceOf(m.run)} />
              <span className="grow" />
              <button className="btn ghost" onClick={() => openRun(m.run.id)}>
                Open <ArrowUpRight size={12} />
              </button>
            </div>
            <div className="cmp-name">{m.run.name}</div>
          </div>
        ))}
      </div>

      <div className="cmp-table">
        {ROWS.map((row, ri) => {
          const va = row.get(metrics[0]);
          const vb = row.get(metrics[1]);
          if (va == null && vb == null) return null;
          const max = Math.max(va ?? 0, vb ?? 0) || 1;
          const lo = va != null && vb != null && va !== vb ? (va < vb ? 0 : 1) : null;
          const pct = lo != null && va && vb ? Math.round((1 - Math.min(va, vb) / Math.max(va, vb)) * 100) : null;
          return (
            <div className="cmp-row" key={row.label} style={{ animationDelay: `${120 + ri * 70}ms` }}>
              <div className="cmp-label">{row.label}</div>
              {[va, vb].map((v, i) => (
                <div className="cmp-cell" key={i} style={{ ["--c" as any]: SIDE[i].color }}>
                  <span className="cmp-val">{v == null ? "—" : row.fmt(v)}</span>
                  <span className="cmp-track">
                    <span className="cmp-fill" style={{ width: `${((v ?? 0) / max) * 100}%`, animationDelay: `${200 + ri * 70}ms` }} />
                  </span>
                  {lo === i && pct != null && pct > 0 && (
                    <span className="cmp-delta">
                      {pct}% {row.less}
                    </span>
                  )}
                </div>
              ))}
            </div>
          );
        })}
      </div>

      <div className="section-title" style={{ margin: "22px 0 10px" }}>
        Timeline · same scale
      </div>
      <Lanes models={models} maxMs={maxMs} />

      <div className="cmp-outcomes">
        {models.map((m, i) => (
          <div key={m.run.id} className="cmp-outcome" style={{ ["--c" as any]: SIDE[i].color }}>
            <div className="section-title">
              <span className="cmp-key small">{SIDE[i].key}</span> Outcome
            </div>
            <div className="md cmp-md">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{metrics[i].finalText.slice(0, 900) || "_No final answer recorded._"}</ReactMarkdown>
            </div>
            {metrics[i].files.length > 0 && (
              <div className="cmp-files mono">
                {metrics[i].files.map((f) => (
                  <div key={f}>{f}</div>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
      <div className="honesty">
        Lower isn't automatically better: Traceglass lays out what each run spent and did, side by side, so the trade-off between speed, cost and the
        quality of the outcome is visible — not collapsed into one score.
        {models.some((m) => m.run.metadata?.["cost_source"]) && ` Cost as reported by ${String(models.find((m) => m.run.metadata?.["cost_source"])!.run.metadata!["cost_source"])}.`}
      </div>
    </div>
  );
}
