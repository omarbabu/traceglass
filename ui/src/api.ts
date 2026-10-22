import type { Bundle, Change, Run, SearchHit } from "./types";

export type Mode = "server" | "embedded" | "static";

/** Where runs come from: a live local server, data embedded in an exported HTML file, or a static site. */
export interface Backend {
  mode: Mode;
  listRuns(): Promise<Run[]>;
  getRun(id: string): Promise<Bundle | null>;
  search(q: string): Promise<SearchHit[]>;
  subscribe?(onChanges: (c: Change[]) => void, onStatus: (connected: boolean) => void): () => void;
  exportUrl?(runId: string): string;
}

class ServerBackend implements Backend {
  mode: Mode = "server";

  async listRuns(): Promise<Run[]> {
    const r = await fetch("api/runs?limit=1000");
    if (!r.ok) throw new Error(`GET /api/runs → ${r.status}`);
    return (await r.json()).runs;
  }

  async getRun(id: string): Promise<Bundle | null> {
    const r = await fetch(`api/runs/${encodeURIComponent(id)}`);
    if (r.status === 404) return null;
    if (!r.ok) throw new Error(`GET /api/runs/${id} → ${r.status}`);
    return r.json();
  }

  async search(q: string): Promise<SearchHit[]> {
    const r = await fetch(`api/search?q=${encodeURIComponent(q)}`);
    return r.ok ? (await r.json()).hits : [];
  }

  subscribe(onChanges: (c: Change[]) => void, onStatus: (connected: boolean) => void): () => void {
    let es: EventSource | null = null;
    let closed = false;
    let retry: ReturnType<typeof setTimeout> | undefined;
    const connect = () => {
      es = new EventSource("api/stream");
      es.addEventListener("hello", () => onStatus(true));
      es.addEventListener("changes", (ev) => {
        try {
          onChanges(JSON.parse((ev as MessageEvent).data));
        } catch {
          /* ignore malformed frames */
        }
      });
      es.onerror = () => {
        onStatus(false);
        es?.close();
        if (!closed) retry = setTimeout(connect, 1500);
      };
    };
    connect();
    return () => {
      closed = true;
      clearTimeout(retry);
      es?.close();
    };
  }

  exportUrl(runId: string): string {
    return `api/runs/${encodeURIComponent(runId)}/export.html`;
  }
}

interface EmbeddedData {
  runs: Run[];
  bundles: Record<string, Bundle>;
}

class EmbeddedBackend implements Backend {
  mode: Mode = "embedded";
  constructor(private data: EmbeddedData) {}
  async listRuns() {
    return this.data.runs;
  }
  async getRun(id: string) {
    return this.data.bundles[id] ?? null;
  }
  async search(q: string) {
    return localSearch(Object.values(this.data.bundles), q);
  }
}

class StaticBackend implements Backend {
  mode: Mode = "static";
  private cache = new Map<string, Bundle>();
  constructor(private base: string) {}
  async listRuns() {
    const r = await fetch(`${this.base}index.json`);
    return (await r.json()).runs as Run[];
  }
  async getRun(id: string) {
    if (!this.cache.has(id)) {
      const r = await fetch(`${this.base}runs/${encodeURIComponent(id)}.json`);
      if (!r.ok) return null;
      this.cache.set(id, await r.json());
    }
    return this.cache.get(id)!;
  }
  async search(q: string) {
    return localSearch([...this.cache.values()], q);
  }
}

function localSearch(bundles: Bundle[], q: string): SearchHit[] {
  const needle = q.toLowerCase();
  const hits: SearchHit[] = [];
  for (const b of bundles) {
    for (const s of b.spans)
      if (s.name.toLowerCase().includes(needle))
        hits.push({ kind: "span", id: s.id, run_id: b.run.id, name: s.name, detail: `${s.kind} · ${b.run.name}`, status: s.status });
    for (const a of b.artifacts) {
      const text = typeof a.content === "string" ? a.content : "";
      if (a.name.toLowerCase().includes(needle) || text.toLowerCase().includes(needle))
        hits.push({ kind: "artifact", id: a.id, run_id: b.run.id, name: a.name, detail: `${a.kind} · ${b.run.name}` });
    }
  }
  return hits.slice(0, 100);
}

export function createBackend(): Backend {
  const embedded = document.getElementById("traceglass-data");
  if (embedded?.textContent) return new EmbeddedBackend(JSON.parse(embedded.textContent));
  const meta = document.querySelector<HTMLMetaElement>('meta[name="traceglass-static"]');
  if (meta) return new StaticBackend(meta.content || "data/");
  return new ServerBackend();
}
