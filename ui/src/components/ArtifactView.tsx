import { useMemo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { GraphModel } from "../model/graph";
import { relation } from "../model/kinds";
import type { Artifact, Link } from "../types";
import { isMarker } from "../types";
import { JsonView, MarkerChip } from "./Payload";

interface AnchorSpec {
  text: string;
  linkIds: string[];
  tone: "support" | "contradict" | "neutral";
  cls: "ev" | "quote";
  n: number[];
}

const stripMd = (s: string) => s.replace(/\*\*|__|`/g, "").replace(/\s+/g, " ").trim();

/** A tiny rehype plugin that wraps anchored passages in <mark> so claims point at their evidence. */
function rehypeAnchors(anchors: AnchorSpec[], focus: string | null) {
  return () => (tree: any) => {
    const used = new Set<AnchorSpec>();
    const split = (value: string): any[] => {
      const out: any[] = [];
      let rest = value;
      for (;;) {
        let best: { i: number; a: AnchorSpec; len: number } | null = null;
        for (const a of anchors) {
          if (used.has(a) || !a.text) continue;
          const i = rest.indexOf(a.text);
          if (i >= 0 && (!best || i < best.i)) best = { i, a, len: a.text.length };
        }
        if (!best) break;
        used.add(best.a);
        if (best.i > 0) out.push({ type: "text", value: rest.slice(0, best.i) });
        const focused = focus != null && best.a.linkIds.includes(focus);
        out.push({
          type: "element",
          tagName: "mark",
          properties: {
            className: [best.a.cls, best.a.tone === "contradict" ? "contradict" : "", focused ? "focus" : ""].filter(Boolean),
            dataLinks: best.a.linkIds.join(","),
          },
          children: [{ type: "text", value: rest.slice(best.i, best.i + best.len) }],
        });
        if (best.a.cls === "ev")
          out.push({
            type: "element",
            tagName: "sup",
            properties: { className: ["cite"], dataLinks: best.a.linkIds.join(",") },
            children: [{ type: "text", value: `[${best.a.n.join(",")}]` }],
          });
        rest = rest.slice(best.i + best.len);
      }
      if (rest) out.push({ type: "text", value: rest });
      return out;
    };
    const visit = (node: any) => {
      if (!node.children) return;
      const next: any[] = [];
      for (const c of node.children) {
        if (c.type === "text") next.push(...split(c.value));
        else {
          if (c.tagName !== "code" && c.tagName !== "pre") visit(c);
          next.push(c);
        }
      }
      node.children = next;
    };
    visit(tree);
  };
}

export function anchorsFor(m: GraphModel, a: Artifact): AnchorSpec[] {
  const groups = new Map<string, AnchorSpec>();
  let n = 0;
  for (const l of m.linksBySubject.get(a.id) ?? []) {
    const t = l.subject.anchor?.text;
    if (!t) continue;
    const key = `ev:${stripMd(t)}`;
    const g = groups.get(key);
    n++;
    if (g) {
      g.linkIds.push(l.id);
      g.n.push(n);
      if (relation(l.relation).tone === "contradict") g.tone = "contradict";
    } else groups.set(key, { text: stripMd(t), linkIds: [l.id], tone: relation(l.relation).tone, cls: "ev", n: [n] });
  }
  for (const l of m.linksByEvidence.get(a.id) ?? []) {
    const t = l.evidence.anchor?.text;
    if (!t) continue;
    const key = `q:${stripMd(t)}`;
    const g = groups.get(key);
    if (g) g.linkIds.push(l.id);
    else groups.set(key, { text: stripMd(t), linkIds: [l.id], tone: "support", cls: "quote", n: [] });
  }
  return [...groups.values()];
}

function Markdown({ text, anchors, focus, onAnchor }: { text: string; anchors: AnchorSpec[]; focus: string | null; onAnchor: (ids: string[]) => void }) {
  const plugins = useMemo(() => [rehypeAnchors(anchors, focus)], [anchors, focus]);
  return (
    <div
      className="md"
      onClick={(e) => {
        const el = (e.target as HTMLElement).closest("[data-links]") as HTMLElement | null;
        if (el?.dataset.links) onAnchor(el.dataset.links.split(","));
      }}
    >
      <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={plugins}>
        {text}
      </ReactMarkdown>
    </div>
  );
}

export function DiffView({ text }: { text: string }) {
  const lines = text.split("\n");
  let oldN = 0;
  let newN = 0;
  return (
    <div className="code diff">
      {lines.map((ln, i) => {
        if (ln.startsWith("diff --git") || ln.startsWith("index ")) return null;
        if (ln.startsWith("--- ")) return null;
        if (ln.startsWith("+++ "))
          return (
            <div className="file" key={i}>
              {ln.slice(4).replace(/^b\//, "")}
            </div>
          );
        if (ln.startsWith("@@")) {
          const m = /@@ -(\d+)(?:,\d+)? \+(\d+)/.exec(ln);
          if (m) {
            oldN = Number(m[1]);
            newN = Number(m[2]);
          }
          return (
            <div className="code-line hunk" key={i}>
              <span className="ln" />
              <span className="ln" />
              <span className="tx">{ln}</span>
            </div>
          );
        }
        const kind = ln[0] === "+" ? "add" : ln[0] === "-" ? "del" : "ctx";
        const o = kind !== "add" ? oldN++ : "";
        const nn = kind !== "del" ? newN++ : "";
        return (
          <div className={`code-line ${kind}`} key={i}>
            <span className="ln">{o}</span>
            <span className="ln">{nn}</span>
            <span className="tx">{ln || " "}</span>
          </div>
        );
      })}
    </div>
  );
}

export function CodeView({ text }: { text: string }) {
  return (
    <div className="code">
      {text.split("\n").map((ln, i) => (
        <div className="code-line" key={i}>
          <span className="ln">{i + 1}</span>
          <span className="tx">{ln || " "}</span>
        </div>
      ))}
    </div>
  );
}

const looksLikeDiff = (s: string) => /^(diff --git|--- |\+\+\+ |@@ )/m.test(s) && /^@@ /m.test(s);

export function ArtifactBody({
  m,
  a,
  focus,
  onAnchor,
}: {
  m: GraphModel;
  a: Artifact;
  focus: string | null;
  onAnchor: (ids: string[]) => void;
}) {
  const anchors = useMemo(() => anchorsFor(m, a), [m, a]);
  const c = a.content;
  if (c === undefined)
    return (
      <div className="notice">
        This artifact's content was not recorded{a.uri ? <> — it lives at <span className="mono">{a.uri}</span></> : null}.
      </div>
    );
  if (isMarker(c)) {
    if (c.$tg === "truncated" && c.preview)
      return (
        <>
          <div style={{ marginBottom: 8 }}>
            <MarkerChip m={c} />
          </div>
          <Markdown text={c.preview} anchors={anchors} focus={focus} onAnchor={onAnchor} />
        </>
      );
    return <MarkerChip m={c} />;
  }
  if (typeof c !== "string")
    return (
      <div className="payload-box">
        <JsonView value={c} />
      </div>
    );
  if (a.kind === "diff" || looksLikeDiff(c)) return <DiffView text={c} />;
  if (a.kind === "code") return <CodeView text={c} />;
  if (a.kind === "json") {
    try {
      return (
        <div className="payload-box">
          <JsonView value={JSON.parse(c)} />
        </div>
      );
    } catch {
      return <CodeView text={c} />;
    }
  }
  return <Markdown text={c} anchors={anchors} focus={focus} onAnchor={onAnchor} />;
}

/** Context around a quoted passage inside its source. */
export function quoteContext(source: Artifact | undefined, link: Link): { before: string; quote: string; after: string } | null {
  const q = link.evidence.anchor;
  if (!source || typeof source.content !== "string" || !q?.text) return null;
  const text = source.content;
  let start = q.start ?? text.indexOf(q.text);
  if (start < 0) return null;
  const end = q.end ?? start + q.text.length;
  const pad = 200;
  const b = Math.max(0, start - pad);
  const e = Math.min(text.length, end + pad);
  return {
    before: (b > 0 ? "…" : "") + text.slice(b, start),
    quote: text.slice(start, end),
    after: text.slice(end, e) + (e < text.length ? "…" : ""),
  };
}
