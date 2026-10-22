import { EyeOff, Lock, Scissors } from "lucide-react";
import { useState, type ReactNode } from "react";
import { isMarker, type Marker } from "../types";

/** Renders the SDK's honesty markers: not captured ≠ empty ≠ redacted ≠ truncated. */
export function MarkerChip({ m }: { m: Marker }) {
  switch (m.$tg) {
    case "not_captured":
      return (
        <span className="marker not_captured" title="The value existed but the instrumentation did not record it">
          <EyeOff size={11} /> not captured{m.reason ? ` — ${m.reason}` : ""}
        </span>
      );
    case "redacted":
      return (
        <span className="marker redacted" title="Removed before it left the agent process">
          <Lock size={11} /> redacted{m.reason ? ` · ${m.reason}` : ""}
        </span>
      );
    case "truncated":
      return (
        <span className="marker" title="Shortened to respect payload limits">
          <Scissors size={11} /> truncated
          {m.length ? ` · ${m.length.toLocaleString()} chars` : m.omitted_items ? ` · ${m.omitted_items} more items` : m.reason ? ` · ${m.reason}` : ""}
        </span>
      );
    case "bytes":
      return <span className="marker">binary · {m.size?.toLocaleString()} bytes</span>;
    case "repr":
      return <span className="marker">{m.type ?? "object"}</span>;
    default:
      return <span className="marker">{m.$tg}</span>;
  }
}

function Str({ s }: { s: string }) {
  const [open, setOpen] = useState(s.length < 600);
  if (open) return <span className="str">"{s}"</span>;
  return (
    <span className="str">
      "{s.slice(0, 400)}
      <span className="toggle" onClick={() => setOpen(true)}>
        … {(s.length - 400).toLocaleString()} more
      </span>
      "
    </span>
  );
}

function Node({ v, depth, name }: { v: unknown; depth: number; name?: string }): ReactNode {
  const [open, setOpen] = useState(depth < 2);
  const label = name != null ? (
    <>
      <span className="key">{name}</span>
      <span className="punct">: </span>
    </>
  ) : null;
  if (isMarker(v)) {
    const m = v as Marker;
    return (
      <div>
        {label}
        <MarkerChip m={m} />
        {m.$tg === "truncated" && m.preview ? (
          <div className="indent">
            <Str s={m.preview} />
          </div>
        ) : m.$tg === "repr" && m.repr ? (
          <span className="faint"> {m.repr}</span>
        ) : null}
      </div>
    );
  }
  if (v === null || v === undefined)
    return (
      <div>
        {label}
        <span className="null">null</span>
      </div>
    );
  if (typeof v === "string")
    return (
      <div>
        {label}
        <Str s={v} />
      </div>
    );
  if (typeof v === "number")
    return (
      <div>
        {label}
        <span className="num">{v}</span>
      </div>
    );
  if (typeof v === "boolean")
    return (
      <div>
        {label}
        <span className="bool">{String(v)}</span>
      </div>
    );
  const isArr = Array.isArray(v);
  const entries: [string, unknown][] = isArr ? (v as unknown[]).map((x, i) => [String(i), x]) : Object.entries(v as object);
  const [o, c] = isArr ? ["[", "]"] : ["{", "}"];
  if (!entries.length)
    return (
      <div>
        {label}
        <span className="punct">
          {o}
          {c}
        </span>
      </div>
    );
  return (
    <div>
      {label}
      <span className="toggle" onClick={() => setOpen(!open)}>
        {open ? "▾" : "▸"}{" "}
      </span>
      <span className="punct">{o}</span>
      {!open && (
        <span className="toggle" onClick={() => setOpen(true)}>
          {" "}
          {entries.length} {isArr ? "items" : "keys"} {c}
        </span>
      )}
      {open && (
        <>
          <div className="indent">
            {entries.map(([k, x]) => (
              <Node key={k} v={x} depth={depth + 1} name={isArr ? undefined : k} />
            ))}
          </div>
          <span className="punct">{c}</span>
        </>
      )}
    </div>
  );
}

export function JsonView({ value }: { value: unknown }) {
  return (
    <div className="json">
      <Node v={value} depth={0} />
    </div>
  );
}

type Msg = { role?: string; content?: unknown; name?: string; type?: string };

function looksLikeMessages(v: unknown): Msg[] | null {
  const arr = Array.isArray(v) ? v : v && typeof v === "object" && Array.isArray((v as any).messages) ? (v as any).messages : null;
  if (!arr || !arr.length) return null;
  if (arr.every((m: any) => m && typeof m === "object" && typeof m.role === "string")) return arr;
  return null;
}

function Block({ b }: { b: any }) {
  if (typeof b === "string") return <>{b}</>;
  if (isMarker(b)) return <MarkerChip m={b} />;
  if (b?.type === "text") return <div>{b.text}</div>;
  if (b?.type === "thinking" || b?.type === "redacted_thinking")
    return (
      <div className="msg-block">
        <div className="msg-block-head">thinking</div>
        {isMarker(b.thinking) ? <MarkerChip m={b.thinking} /> : <span className="faint">{String(b.thinking ?? "")}</span>}
      </div>
    );
  if (b?.type === "tool_use")
    return (
      <div className="msg-block">
        <div className="msg-block-head">→ {b.name}</div>
        <JsonView value={b.input} />
      </div>
    );
  if (b?.type === "tool_result")
    return (
      <div className="msg-block">
        <div className="msg-block-head">← result{b.is_error ? " (error)" : ""}</div>
        {typeof b.content === "string" ? b.content : <JsonView value={b.content} />}
      </div>
    );
  return <JsonView value={b} />;
}

function Messages({ msgs, system }: { msgs: Msg[]; system?: unknown }) {
  return (
    <div>
      {system != null && (
        <div className="msg">
          <div className="msg-role system">system</div>
          <div className="msg-body">{typeof system === "string" ? system : <JsonView value={system} />}</div>
        </div>
      )}
      {msgs.map((m, i) => (
        <div className="msg" key={i}>
          <div className={`msg-role ${m.role}`}>
            {m.role}
            {m.name ? <span className="faint"> · {m.name}</span> : null}
          </div>
          <div className="msg-body">
            {typeof m.content === "string" ? (
              m.content
            ) : Array.isArray(m.content) ? (
              m.content.map((b, j) => <Block key={j} b={b} />)
            ) : (
              <JsonView value={m.content} />
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

/** Smart payload renderer: chat transcripts as messages, plain text as text, everything else as JSON. */
export function Payload({ value, present }: { value: unknown; present: boolean }) {
  if (!present)
    return (
      <div className="notice">
        <EyeOff size={14} />
        <span>This field was not reported by the instrumentation. That is different from an empty value.</span>
      </div>
    );
  if (isMarker(value) && (value as Marker).$tg === "not_captured")
    return (
      <div className="notice">
        <EyeOff size={14} />
        <span>
          The value existed but was <b>not captured</b>
          {(value as Marker).reason ? <> — {(value as Marker).reason}</> : null}.
        </span>
      </div>
    );
  if (value === null || value === "" || (Array.isArray(value) && !value.length))
    return <div className="empty-payload">Empty — the value was captured and is {JSON.stringify(value)}.</div>;
  const msgs = looksLikeMessages(value);
  if (msgs) return <Messages msgs={msgs} system={(value as any)?.system} />;
  if (value && typeof value === "object" && looksLikeMessages((value as any).messages) === null && (value as any).role && (value as any).content)
    return <Messages msgs={[value as Msg]} />;
  if (typeof value === "string")
    return (
      <div className="payload-box" style={{ whiteSpace: "pre-wrap", wordBreak: "break-word", fontSize: 12.5 }}>
        {value}
      </div>
    );
  return (
    <div className="payload-box">
      <JsonView value={value} />
    </div>
  );
}
