/**
 * A tiny scripted agent recorded with @traceglass/sdk: a tool call that hits a rate limit and
 * is retried, a model call, and a final answer whose claims cite the retrieved documents.
 *
 *     traceglass serve                       # in another terminal
 *     npm run example                        # or: TRACEGLASS_URL=http://127.0.0.1:7357 node …
 */

import { Traceglass } from "@traceglass/sdk";

const tg = new Traceglass({ project: "ts-sdk-example" });
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

const CORPUS: Record<string, string> = {
  "Vector stores at scale":
    "In production, approximate nearest neighbour indexes trade a little recall for large latency wins. " +
    "HNSW kept recall above 0.95 while cutting p99 latency by 8x compared to exact search.",
  "Chunking strategies":
    "Chunk size matters more than embedding model choice for most question answering workloads. " +
    "Overlapping 400-token chunks gave the best answer accuracy in our tests.",
};

let attempts = 0;
async function searchDocs(query: string): Promise<string[]> {
  await sleep(120);
  if (++attempts === 1) throw Object.assign(new Error("429 Too Many Requests"), { name: "RateLimitError" });
  return Object.keys(CORPUS).filter((t) => query.split(" ").some((w) => (t + CORPUS[t]).toLowerCase().includes(w)));
}

const question = "How should I tune retrieval for a RAG chatbot?";

let runId = "";
const answer = await tg.run("Tune retrieval for a RAG chatbot", { input: { question }, tags: ["example"] }, async (run) => {
  runId = run.id;
  return run.span("rag-assistant", { kind: "agent", model: "claude-sonnet-5-5" }, async () => {
    // A tool call with a retry: the first attempt fails, the second succeeds.
    const first = run.span("search_docs", { kind: "tool", input: { query: "chunk latency recall" }, attempt: 1 });
    let titles: string[] = [];
    try {
      titles = await searchDocs("chunk latency recall");
    } catch (err) {
      first.fail(err);
      await sleep(200);
      titles = await run.span(
        "search_docs",
        { kind: "tool", input: { query: "chunk latency recall" }, attempt: 2, retryOf: first },
        () => searchDocs("chunk latency recall"),
      );
    }

    const docs = await tg.span("fetch_documents", { kind: "retrieval", input: { titles } }, async () =>
      titles.map((t) => run.artifact(t, CORPUS[t], { kind: "document", metadata: { synthetic: true } })),
    );

    const text =
      "Use overlapping 400-token chunks — chunking moved accuracy more than the embedding model did. " +
      "Serve them from an HNSW index: it kept recall above 0.95 with an 8x p99 latency win.";

    await tg.span("draft answer", { kind: "llm", model: "claude-sonnet-5-5", input: { messages: [{ role: "user", content: question }] } }, async (s) => {
      await sleep(300);
      s.uses(...docs);
      s.usage({ inputTokens: 1840, outputTokens: 96, costUsd: 0.0069 });
      s.setOutput({ role: "assistant", content: text });
      const report = run.artifact("Answer", text, { kind: "markdown", final: true });
      report
        .cite(docs[1], { anchor: "Use overlapping 400-token chunks", quote: "Overlapping 400-token chunks gave the best answer accuracy in our tests." })
        .cite(docs[0], { anchor: "it kept recall above 0.95 with an 8x p99 latency win", quote: "HNSW kept recall above 0.95 while cutting p99 latency by 8x" });
    });
    return text;
  });
});

await tg.flush();
console.log(`${runId}\n${answer}`);
