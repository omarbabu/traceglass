import { ArrowRight, Command, Search, Star, TriangleAlert } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { fmtRelative } from "../model/format";
import { artifactIcon, spanKind, statusStyle } from "../model/kinds";
import { useStore } from "../store";
import type { SearchHit } from "../types";
import { actions } from "./actions";

interface Item {
  key: string;
  section: string;
  label: string;
  detail?: string;
  icon: ReactNode;
  color?: string;
  run: () => void;
  score: number;
}

/** Subsequence fuzzy match with bonuses for word starts and contiguous runs. */
export function fuzzy(q: string, text: string): number {
  if (!q) return 1;
  const t = text.toLowerCase();
  const n = q.toLowerCase();
  const direct = t.indexOf(n);
  if (direct >= 0) return 100 - direct + (direct === 0 || /\W/.test(t[direct - 1]) ? 30 : 0);
  let score = 0;
  let ti = 0;
  let streak = 0;
  for (const ch of n) {
    const i = t.indexOf(ch, ti);
    if (i < 0) return 0;
    streak = i === ti ? streak + 1 : 0;
    score += 1 + streak * 2 + (i === 0 || /\W/.test(t[i - 1]) ? 4 : 0);
    ti = i + 1;
  }
  return score;
}

export function CommandPalette() {
  const open = useStore((s) => s.paletteOpen);
  const set = useStore((s) => s.set);
  const model = useStore((s) => s.model);
  const runs = useStore((s) => s.runs);
  const backend = useStore((s) => s.backend);
  const [q, setQ] = useState("");
  const [idx, setIdx] = useState(0);
  const [remote, setRemote] = useState<SearchHit[]>([]);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (open) {
      setQ("");
      setIdx(0);
      setRemote([]);
      setTimeout(() => inputRef.current?.focus(), 10);
    }
  }, [open]);

  useEffect(() => {
    if (!open || q.trim().length < 3) {
      setRemote([]);
      return;
    }
    const t = setTimeout(() => backend.search(q.trim()).then(setRemote).catch(() => setRemote([])), 180);
    return () => clearTimeout(t);
  }, [q, open, backend]);

  const close = () => set("paletteOpen", false);

  const items = useMemo(() => {
    const out: Item[] = [];
    const s = useStore.getState();
    const query = q.trim();
    if (model) {
      for (const sp of model.spans.values()) {
        const k = spanKind(sp.kind);
        const score = fuzzy(query, `${sp.name} ${sp.kind} ${sp.agent ?? ""} ${sp.model ?? ""}`);
        if (score > 0)
          out.push({
            key: sp.id,
            section: "In this run",
            label: sp.name,
            detail: [k.label, sp.agent && sp.agent !== sp.name ? sp.agent : null, sp.status === "error" ? "failed" : null].filter(Boolean).join(" · "),
            icon: sp.status === "error" ? <TriangleAlert size={14} /> : <k.icon size={14} />,
            color: sp.status === "error" ? "var(--s-error)" : k.color,
            run: () => s.select(sp.id, { focus: true }),
            score: score + (sp.status === "error" && /err|fail/.test(query) ? 50 : 0),
          });
      }
      for (const a of model.artifacts.values()) {
        const Icon = a.final ? Star : artifactIcon(a.kind);
        const score = fuzzy(query, `${a.name} ${a.kind} ${a.final ? "final answer" : ""}`);
        if (score > 0)
          out.push({
            key: a.id,
            section: "In this run",
            label: a.name,
            detail: a.final ? "final answer" : a.kind,
            icon: <Icon size={14} />,
            color: "var(--k-artifact)",
            run: () => s.select(a.id, { focus: true }),
            score: score + (a.final ? 5 : 0),
          });
      }
    }
    for (const act of actions()) {
      const score = fuzzy(query, act.label + " " + (act.keywords ?? ""));
      if (score > 0 && (query || act.pinned))
        out.push({ key: `act:${act.id}`, section: "Actions", label: act.label, detail: act.keys, icon: <ArrowRight size={14} />, run: act.run, score: score - 1 });
    }
    for (const r of runs) {
      if (r.id === s.runId) continue;
      const score = fuzzy(query, `${r.name} ${r.project ?? ""} ${r.id}`);
      if (score > 0)
        out.push({
          key: `run:${r.id}`,
          section: "Runs",
          label: r.name,
          detail: `${r.project ?? ""} · ${fmtRelative(r.started_at)}`,
          icon: <span className="dot" style={{ ["--c" as any]: statusStyle(r.status).color }} />,
          run: () => s.openRun(r.id),
          score: score - 2,
        });
    }
    for (const h of remote) {
      if (h.run_id === s.runId || h.kind === "run") continue;
      out.push({
        key: `remote:${h.id}`,
        section: "Across all runs",
        label: h.name,
        detail: h.detail,
        icon: <Search size={14} />,
        run: () => s.openRun(h.run_id, h.id),
        score: -10,
      });
    }
    const order = ["In this run", "Actions", "Runs", "Across all runs"];
    const bySection = new Map<string, Item[]>();
    for (const it of out) {
      if (!bySection.has(it.section)) bySection.set(it.section, []);
      bySection.get(it.section)!.push(it);
    }
    const limits: Record<string, number> = { "In this run": query ? 30 : 8, Actions: query ? 8 : 6, Runs: query ? 10 : 6, "Across all runs": 12 };
    return order.flatMap((sec) => (bySection.get(sec) ?? []).sort((a, b) => b.score - a.score).slice(0, limits[sec]));
  }, [q, model, runs, remote]);

  useEffect(() => setIdx(0), [q]);
  useEffect(() => {
    listRef.current?.querySelector(".palette-item.on")?.scrollIntoView({ block: "nearest" });
  }, [idx]);

  if (!open) return null;
  let lastSection = "";
  return (
    <div className="overlay" onMouseDown={close}>
      <div className="palette" onMouseDown={(e) => e.stopPropagation()}>
        <div className="palette-input">
          <Command size={16} />
          <input
            ref={inputRef}
            value={q}
            placeholder="Jump to a step, artifact, run or action…"
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "ArrowDown") {
                e.preventDefault();
                setIdx((i) => Math.min(items.length - 1, i + 1));
              } else if (e.key === "ArrowUp") {
                e.preventDefault();
                setIdx((i) => Math.max(0, i - 1));
              } else if (e.key === "Enter") {
                e.preventDefault();
                const it = items[idx];
                if (it) {
                  close();
                  it.run();
                }
              } else if (e.key === "Escape") close();
            }}
            spellCheck={false}
          />
        </div>
        <div className="palette-list" ref={listRef}>
          {items.map((it, i) => {
            const head = it.section !== lastSection ? <div className="palette-section">{it.section}</div> : null;
            lastSection = it.section;
            return (
              <div key={it.key}>
                {head}
                <div
                  className={`palette-item ${i === idx ? "on" : ""}`}
                  style={{ ["--c" as any]: it.color }}
                  onMouseMove={() => setIdx(i)}
                  onClick={() => {
                    close();
                    it.run();
                  }}
                >
                  <span className="icon">{it.icon}</span>
                  <span className="ellipsis">{it.label}</span>
                  {it.detail && <span className="detail">{it.detail}</span>}
                </div>
              </div>
            );
          })}
          {!items.length && <div className="faint" style={{ padding: 16 }}>Nothing matches “{q}”.</div>}
        </div>
        <div className="palette-foot">
          <span>
            <span className="kbd">↑</span> <span className="kbd">↓</span> navigate
          </span>
          <span>
            <span className="kbd">↵</span> open
          </span>
          <span>
            <span className="kbd">esc</span> close
          </span>
        </div>
      </div>
    </div>
  );
}

const SHORTCUTS: [string, string[]][] = [
  ["Command palette", ["⌘", "K"]],
  ["Filter runs", ["/"]],
  ["Next / previous step", ["j", "k"]],
  ["Parent / first child", ["h", "l"]],
  ["Expand or collapse", ["↵"]],
  ["Previous / next run", ["[", "]"]],
  ["Jump to final answer", ["a"]],
  ["Jump to next failure", ["e"]],
  ["Replay / pause", ["space"]],
  ["Toggle timeline", ["t"]],
  ["Toggle evidence links", ["v"]],
  ["Toggle run list", ["b"]],
  ["Fit graph to screen", ["f"]],
  ["Copy deep link", ["c"]],
  ["Clear selection / close", ["esc"]],
  ["This help", ["?"]],
];

export function HelpOverlay() {
  const open = useStore((s) => s.helpOpen);
  const set = useStore((s) => s.set);
  if (!open) return null;
  return (
    <div className="overlay" onMouseDown={() => set("helpOpen", false)}>
      <div className="help" onMouseDown={(e) => e.stopPropagation()}>
        <h3>Keyboard shortcuts</h3>
        <div className="help-grid">
          {SHORTCUTS.map(([label, keys]) => (
            <div className="help-row" key={label}>
              <span>{label}</span>
              <span className="keys">
                {keys.map((k) => (
                  <span className="kbd" key={k}>
                    {k}
                  </span>
                ))}
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

export function Toast() {
  const toast = useStore((s) => s.toast);
  const set = useStore((s) => s.set);
  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => set("toast", null), 1800);
    return () => clearTimeout(t);
  }, [toast, set]);
  return toast ? <div className="toast">{toast}</div> : null;
}
