import { createServer, type IncomingMessage } from "node:http";
import type { AddressInfo } from "node:net";
import { describe, expect, it } from "vitest";
import { capture, HTTPSink, MemorySink, newId, notCaptured, Redactor, Traceglass, type TraceRecord } from "../src/index.js";

function setup() {
  const sink = new MemorySink();
  const tg = new Traceglass({ sink, project: "test" });
  return { sink, tg };
}

const byName = (recs: TraceRecord[], name: string) => recs.find((r) => r.name === name)!;

describe("ids", () => {
  it("match the Python shape and sort by time", () => {
    const a = newId("span", 1_700_000_000_000);
    const b = newId("span", 1_700_000_000_001);
    expect(a).toMatch(/^sp_[0-9a-hjkmnp-tv-z]{20}$/);
    expect(a < b).toBe(true);
    expect(newId("run")).toMatch(/^run_/);
    expect(newId("artifact")).toMatch(/^art_/);
    expect(newId("link")).toMatch(/^ln_/);
    expect(newId("event")).toMatch(/^ev_/);
  });
});

describe("capture", () => {
  it("redacts secret keys and value patterns, with markers", () => {
    const out = capture({
      api_key: "abc",
      input_tokens: 12,
      max_tokens: 100,
      nested: { Authorization: "Bearer abcdefghijklmnopqrstuvwxyz" },
      note: "my key is sk-ant-abcdefghijklmnopqrstuvwxyz0123 ok",
      aws: "AKIAABCDEFGHIJKLMNOP",
      empty_secret: "",
    }) as Record<string, any>;
    expect(out.api_key).toEqual({ $tg: "redacted", reason: "secret-key" });
    expect(out.input_tokens).toBe(12);
    expect(out.max_tokens).toBe(100);
    expect(out.nested.Authorization).toEqual({ $tg: "redacted", reason: "secret-key" });
    expect(out.note).toBe("my key is ‹redacted:anthropic-key› ok");
    expect(out.aws).toBe("‹redacted:aws-access-key›");
    expect(out.empty_secret).toBe("");
  });

  it("truncates, marks bytes/cycles/functions, and keeps markers", () => {
    const cyc: any = { a: 1 };
    cyc.self = cyc;
    const out = capture({ big: "x".repeat(40_000), buf: new Uint8Array(8), cyc, fn: () => 1, nc: notCaptured("hidden"), when: new Date(0) }) as any;
    expect(out.big).toMatchObject({ $tg: "truncated", length: 40_000 });
    expect(out.big.preview).toHaveLength(32_000);
    expect(out.buf).toEqual({ $tg: "bytes", size: 8 });
    expect(out.cyc.self).toEqual({ $tg: "truncated", reason: "cycle" });
    expect(out.fn.$tg).toBe("repr");
    expect(out.nc).toEqual({ $tg: "not_captured", reason: "hidden" });
    expect(out.when).toBe("1970-01-01T00:00:00.000Z");
  });

  it("supports custom redactors", () => {
    const r = new Redactor({ keys: ["ssn"], patterns: [["card", /\b\d{4}-\d{4}-\d{4}-\d{4}\b/]] });
    const out = capture({ ssn: "123", text: "card 1111-2222-3333-4444" }, { maxString: 100, maxItems: 10, maxDepth: 5, redactor: r }) as any;
    expect(out.ssn.$tg).toBe("redacted");
    expect(out.text).toBe("card ‹redacted:card›");
  });
});

describe("recording", () => {
  it("records runs, nested spans and wire-compatible fields", async () => {
    const { sink, tg } = setup();
    const result = await tg.run("Compare papers", { input: { question: "q?" }, session: "s1", tags: ["t"] }, async (run) => {
      return run.span("researcher", { kind: "agent", model: "claude-opus-5-5" }, async (agent) => {
        const hits = await tg.span("search", { kind: "tool", input: { query: "agents" } }, async () => ["a", "b"]);
        await agent.span("synthesize", { kind: "llm", model: "claude-sonnet-5-5" }, async (s) => {
          s.usage({ inputTokens: 100, outputTokens: 50, costUsd: 0.01 });
        });
        return hits.length;
      });
    });
    expect(result).toBe(2);
    const runs = sink.ofType("run");
    expect(runs).toHaveLength(1);
    const run = runs[0];
    expect(run).toMatchObject({ v: 1, name: "Compare papers", project: "test", session: "s1", status: "ok", source: "sdk", input: { question: "q?" }, output: 2 });
    expect(run.sdk).toMatchObject({ name: "traceglass-typescript" });
    expect(typeof run.started_at).toBe("number");
    expect(run.ended_at as number).toBeGreaterThanOrEqual(run.started_at as number);

    const spans = sink.ofType("span");
    const agent = byName(spans, "researcher");
    const search = byName(spans, "search");
    const synth = byName(spans, "synthesize");
    expect(agent.parent_id).toBeUndefined(); // ofType drops nulls; raw record has null
    expect(sink.records.find((r) => r.id === agent.id)!.parent_id).toBeNull();
    expect(search.parent_id).toBe(agent.id); // implicit (AsyncLocalStorage)
    expect(synth.parent_id).toBe(agent.id); // explicit
    expect(search.agent).toBe("researcher");
    expect(search).toMatchObject({ kind: "tool", status: "ok", input: { query: "agents" }, output: ["a", "b"], run_id: run.id });
    expect(synth.usage).toEqual({ input_tokens: 100, output_tokens: 50, cost_usd: 0.01, total_tokens: 150 });
    expect(Object.keys(sink.records.find((r) => r.type === "span")!).sort()).toEqual(
      expect.arrayContaining(["type", "id", "run_id", "parent_id", "kind", "name", "status", "started_at", "ended_at", "seq", "attributes", "v"]),
    );
    expect((agent.seq as number) < (search.seq as number)).toBe(true);
  });

  it("parents concurrent spans correctly across Promise.all", async () => {
    const { sink, tg } = setup();
    await tg.run("parallel", async (run) => {
      await Promise.all(
        ["a", "b", "c"].map((n) =>
          run.span(`agent ${n}`, { kind: "agent" }, async () => {
            await new Promise((r) => setTimeout(r, 5));
            await tg.span(`tool ${n}`, { kind: "tool" }, async () => n);
          }),
        ),
      );
    });
    const spans = sink.ofType("span");
    for (const n of ["a", "b", "c"]) expect(byName(spans, `tool ${n}`).parent_id).toBe(byName(spans, `agent ${n}`).id);
  });

  it("records errors, retries and rethrows", async () => {
    const { sink, tg } = setup();
    await tg.run("retry", async (run) => {
      const first = run.span("search", { kind: "tool", attempt: 1 });
      first.fail(new TypeError("rate limited"));
      await run.span("search", { kind: "tool", attempt: 2, retryOf: first }, async () => "ok");
      await expect(run.span("boom", {}, async () => {
        throw new RangeError("nope");
      })).rejects.toThrow("nope");
    });
    const spans = sink.ofType("span");
    const [a1, a2] = spans.filter((s) => s.name === "search");
    expect(a1).toMatchObject({ status: "error", attempt: 1, error: { type: "TypeError", message: "rate limited" } });
    expect(a2).toMatchObject({ status: "ok", attempt: 2, retry_of: a1.id });
    expect(byName(spans, "boom")).toMatchObject({ status: "error", error: { type: "RangeError" } });
    const events = sink.ofType("event");
    expect(events.filter((e) => e.kind === "error").map((e) => e.name)).toEqual(["TypeError", "RangeError"]);
    expect(sink.ofType("run")[0].status).toBe("ok"); // the run itself recovered
  });

  it("marks a run as failed when its callback throws", async () => {
    const { sink, tg } = setup();
    await expect(tg.run("bad", async () => {
      throw new Error("kaput");
    })).rejects.toThrow();
    expect(sink.ofType("run")[0]).toMatchObject({ status: "error", metadata: { error: { message: "kaput" } } });
  });

  it("creates an implicit run for a span with no enclosing run", async () => {
    const { sink, tg } = setup();
    await tg.span("solo", { kind: "tool", input: "x" }, async () => 42);
    const run = sink.ofType("run")[0];
    expect(run).toMatchObject({ name: "solo", status: "ok", output: 42, input: "x" });
    expect(sink.ofType("span")[0].run_id).toBe(run.id);
  });

  it("records artifacts, anchored citations, uses, dependsOn, reviews", async () => {
    const { sink, tg } = setup();
    await tg.run("evidence", async (run) => {
      const ret = await run.span("retrieve", { kind: "retrieval" }, async () => run.artifact("doc", "Agents are cool. They use tools.", { kind: "document" }));
      const plan = run.startSpan("plan", { kind: "llm" }).end();
      await run.span("synthesize", { kind: "llm", dependsOn: [plan] }, async (s) => {
        s.uses(ret);
        const report = run.artifact("Report", "# Report\nAgents use tools.", { kind: "markdown", final: true });
        report.cite(ret, { anchor: "Agents use tools.", quote: "They use tools.", confidence: 0.9 });
        report.cite(ret, { anchor: "not in text", relation: "contradicted_by" });
        report.review("approved", { by: "me" });
      });
    });
    const arts = sink.ofType("artifact");
    const doc = byName(arts, "doc");
    const report = byName(arts, "Report");
    const spans = sink.ofType("span");
    expect(doc.span_id).toBe(byName(spans, "retrieve").id);
    expect(doc.size).toBe(32);
    expect(report).toMatchObject({ final: true, span_id: byName(spans, "synthesize").id, review: { status: "approved", by: "me" } });
    expect(sink.ofType("run")[0].final_artifact_id).toBe(report.id);
    expect(byName(spans, "synthesize").depends_on).toEqual([byName(spans, "plan").id]);
    const links = sink.ofType("link");
    const used = links.find((l) => l.relation === "used")!;
    expect(used.subject).toEqual({ kind: "span", id: byName(spans, "synthesize").id });
    const cite = links.find((l) => l.relation === "supported_by")!;
    expect(cite).toMatchObject({
      subject: { kind: "artifact", id: report.id, anchor: { start: 9, end: 26, text: "Agents use tools." } },
      evidence: { kind: "artifact", id: doc.id, anchor: { start: 17, end: 32, text: "They use tools." } },
      confidence: 0.9,
      declared_by: "agent",
    });
    const contra = links.find((l) => l.relation === "contradicted_by")!;
    expect((contra.subject as any).anchor).toEqual({ text: "not in text" });
  });

  it("emits waiting status immediately for approval gates", async () => {
    const { sink, tg } = setup();
    await tg.run("gate", async (run) => {
      const gate = run.startSpan("approve", { kind: "approval" });
      gate.setStatus("waiting");
      expect(sink.records.filter((r) => r.id === gate.id).at(-1)!.status).toBe("waiting");
      gate.setAttributes({ decision: "approved" }).end();
    });
    expect(sink.ofType("span")[0]).toMatchObject({ status: "ok", attributes: { decision: "approved" } });
  });

  it("respects capture switches", async () => {
    const sink = new MemorySink();
    const tg = new Traceglass({ sink, captureInputs: false, redact: false });
    await tg.run("r", async (run) => {
      await run.span("s", { input: "secret prompt" }, async (s) => {
        s.setOutput({ api_key: "visible" });
        s.markNotCaptured("tool_args", "framework hides them");
      });
    });
    const s = sink.ofType("span")[0];
    expect(s.input).toEqual({ $tg: "not_captured", reason: "input capture disabled" });
    expect(s.output).toEqual({ api_key: "visible" });
    expect((s.attributes as any).tool_args.$tg).toBe("not_captured");
  });
});

describe("HTTPSink", () => {
  it("batches records to /api/ingest as JSON", async () => {
    const bodies: any[] = [];
    const headers: IncomingMessage["headers"][] = [];
    const server = createServer((req, res) => {
      let data = "";
      req.on("data", (c) => (data += c));
      req.on("end", () => {
        bodies.push({ url: req.url, body: JSON.parse(data) });
        headers.push(req.headers);
        res.writeHead(200, { "Content-Type": "application/json" }).end('{"accepted":1}');
      });
    });
    await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
    const url = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
    const tg = new Traceglass({ sink: new HTTPSink({ url, interval: 5 }) });
    await tg.run("over http", async (run) => {
      await run.span("a", {}, async () => 1);
    });
    server.close();
    expect(bodies.length).toBeGreaterThan(0);
    expect(bodies.every((b) => b.url === "/api/ingest")).toBe(true);
    expect(headers[0]["content-type"]).toBe("application/json");
    const all = bodies.flatMap((b) => b.body.records);
    expect(all.filter((r: any) => r.type === "run").at(-1).status).toBe("ok");
    expect(all.filter((r: any) => r.type === "span")).toHaveLength(2);
  });

  it("never throws when the server is unreachable", async () => {
    const tg = new Traceglass({ sink: new HTTPSink({ url: "http://127.0.0.1:1", interval: 1 }) });
    const warn = console.warn;
    console.warn = () => {};
    try {
      await expect(tg.run("offline", async () => "fine")).resolves.toBe("fine");
    } finally {
      console.warn = warn;
    }
  });
});
