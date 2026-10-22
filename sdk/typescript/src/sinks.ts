/** Where records go. A sink is anything with `emit`, `flush` and `close`. */

export type TraceRecord = Record<string, unknown> & { type: string; id: string };

export interface Sink {
  emit(record: TraceRecord): void;
  flush(): Promise<void>;
  close(): Promise<void>;
}

export class NullSink implements Sink {
  emit(): void {}
  async flush(): Promise<void> {}
  async close(): Promise<void> {}
}

/** Keeps records in memory — handy for tests and for building bundles in-process. */
export class MemorySink implements Sink {
  readonly records: TraceRecord[] = [];

  emit(record: TraceRecord): void {
    this.records.push(JSON.parse(JSON.stringify(record)));
  }
  async flush(): Promise<void> {}
  async close(): Promise<void> {}

  /** Latest version of each record of a type (records are upserts). */
  ofType(type: string): TraceRecord[] {
    const latest = new Map<string, TraceRecord>();
    for (const r of this.records) {
      if (r.type !== type) continue;
      const prev = latest.get(r.id) ?? {};
      const merged: Record<string, unknown> = { ...prev };
      for (const [k, v] of Object.entries(r)) if (v !== null && v !== undefined) merged[k] = v;
      latest.set(r.id, merged as TraceRecord);
    }
    return [...latest.values()];
  }
}

export interface HTTPSinkOptions {
  url?: string;
  batchSize?: number;
  /** Milliseconds to wait for more records before posting a batch. */
  interval?: number;
  fetch?: typeof fetch;
}

const warned = new Set<string>();
function warnOnce(msg: string) {
  if (warned.has(msg)) return;
  warned.add(msg);
  console.warn(msg);
}

/**
 * Ships records to a running `traceglass serve` (`POST /api/ingest`) in small batches.
 * Failures are reported once and never thrown: tracing must not break the agent.
 */
export class HTTPSink implements Sink {
  readonly url: string;
  private queue: TraceRecord[] = [];
  private timer: ReturnType<typeof setTimeout> | null = null;
  private inflight: Promise<void> = Promise.resolve();
  private batchSize: number;
  private interval: number;
  private fetchImpl: typeof fetch;

  constructor(opts: HTTPSinkOptions = {}) {
    this.url = (opts.url ?? "http://127.0.0.1:7357").replace(/\/+$/, "") + "/api/ingest";
    this.batchSize = opts.batchSize ?? 500;
    this.interval = opts.interval ?? 50;
    this.fetchImpl = opts.fetch ?? globalThis.fetch.bind(globalThis);
  }

  emit(record: TraceRecord): void {
    this.queue.push(record);
    if (this.queue.length >= this.batchSize) this.drain();
    else if (!this.timer) {
      this.timer = setTimeout(() => this.drain(), this.interval);
      (this.timer as { unref?: () => void }).unref?.();
    }
  }

  private drain(): Promise<void> {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    while (this.queue.length) {
      const batch = this.queue.splice(0, this.batchSize);
      this.inflight = this.inflight.then(() => this.post(batch));
    }
    return this.inflight;
  }

  private async post(batch: TraceRecord[]): Promise<void> {
    try {
      const res = await this.fetchImpl(this.url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ records: batch }),
      });
      if (!res.ok) warnOnce(`traceglass: ingest returned HTTP ${res.status} (${this.url})`);
    } catch (err) {
      warnOnce(`traceglass: failed to send ${batch.length} records to ${this.url}: ${(err as Error).message}`);
    }
  }

  async flush(): Promise<void> {
    await this.drain();
  }

  async close(): Promise<void> {
    await this.flush();
  }
}

export class FanoutSink implements Sink {
  constructor(private sinks: Sink[]) {}
  emit(record: TraceRecord): void {
    for (const s of this.sinks) s.emit(record);
  }
  async flush(): Promise<void> {
    await Promise.all(this.sinks.map((s) => s.flush()));
  }
  async close(): Promise<void> {
    await Promise.all(this.sinks.map((s) => s.close()));
  }
}
