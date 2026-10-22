# Integrations

Every integration translates into the same [canonical records](schema.md). Mix them freely — e.g. a LangGraph agent inside a `tg.run` whose tools also emit SDK spans.

## Python SDK

```python
import traceglass as tg

tg.configure(project="research-agent")         # optional; see "Configuration" below

@tg.tool                                        # also: @tg.llm, @tg.agent, @tg.retrieval, @tg.step, @tg.guardrail
def search(query: str, k: int = 5) -> list[dict]:
    ...                                         # arguments → span input, return value → output, exceptions → error

@tg.agent("researcher")
async def research(question: str) -> str:       # async functions work the same way
    ...

with tg.run("Compare papers", project="research-agent", session="s-42", input={"question": q}) as run:
    with tg.span("plan", kind="llm", model="claude-opus-5-5", input=messages) as s:
        s.usage(input_tokens=1200, output_tokens=310, cost_usd=0.012)
        s.set_output(plan)
```

**Artifacts and evidence**

```python
doc = tg.artifact("Okafor 2025 §4", passage, kind="document", metadata={"url": url})
report = tg.artifact("Report", markdown, kind="markdown", final=True)   # final=True → the run's answer

report.cite(doc, anchor="RAG halves unsupported claims",        # text inside the report
                 quote="9.4% of answers, compared with 21.7%",   # text inside the document
                 relation="supported_by")                        # or contradicted_by, cites, …
synthesis_span.uses(doc)                                         # the step consumed the document
patch.derived_from(read_span); tg.link(patch, test_span, relation="tested_by")
patch.review("approved", by="maintainer", note="LGTM")
v2 = tg.artifact("Report", new_text, kind="markdown", previous=report)  # versioning
```

**Dependencies, retries, events, honesty markers**

```python
with tg.span("merge", kind="step") as m:
    m.depends_on(branch_a, branch_b)                 # data dependencies beyond parent/child

first = tg.start_span("web_search", kind="tool", attempt=1)
first.fail(RateLimitError("429"))
retry = tg.start_span("web_search", kind="tool", attempt=2, retry_of=first)

s.event("backoff", kind="retry", level="warning", data={"sleep_s": 2})
s.mark_not_captured("input", "framework does not expose tool arguments")
```

**Concurrency.** Context propagates through `asyncio` automatically. For threads use `tg.ThreadPoolExecutor` (drop-in) or wrap a target with `tg.bind(fn)`.

**Configuration**

| | |
|---|---|
| `TRACEGLASS_DB` | SQLite path (default `~/.traceglass/traceglass.db`) |
| `TRACEGLASS_URL` | send to a server over HTTP instead of writing SQLite |
| `TRACEGLASS_DISABLED=1` | no-op sink |
| `tg.configure(sink=...)` | `SQLiteSink`, `HTTPSink`, `JSONLSink`, `MemorySink`, `FanoutSink`, `NullSink` |
| `tg.configure(redact=Redactor(keys=["ssn"], patterns=[("card", r"\b\d{16}\b")]))` | extend redaction; `redact=False` disables it |
| `tg.configure(max_payload_chars=..., capture_inputs=False, capture_outputs=False)` | payload limits and opt-outs |

## LangChain / LangGraph

```python
import traceglass as tg

with tg.observe(project="support-bot"):          # every runnable inside reports automatically
    graph.invoke({"messages": [("user", "Where is my order?")]})
```

Or explicitly: `graph.invoke(inputs, config={"callbacks": [TraceglassCallbackHandler()]})` from `traceglass.integrations.langchain`.

Chains, chat models, tools, retrievers and agent actions map to `step` / `llm` / `tool` / `retrieval` / `agent` spans; retrieved `Document`s become `document` artifacts; token usage is read from the model response. LangGraph's internal plumbing (channel writes, `langsmith:hidden` runs) is folded away. Requires `langchain-core` (`pip install 'traceglass[langchain]'`). Example: [`examples/langgraph_agent.py`](../examples/langgraph_agent.py).

## Anthropic SDK

```python
from anthropic import Anthropic
from traceglass.integrations.anthropic import instrument

client = instrument(Anthropic())                 # instruments this client only; returns it
with tg.run("Answer with tools", project="claude-agent"):
    msg = client.messages.create(model="claude-opus-5-5", max_tokens=1024, messages=[...], tools=[...])
```

Each `messages.create` / `messages.stream` call (sync or async) becomes an `llm` span with the request (system, messages, tool names), response content, `stop_reason`, tool calls, and usage including cache reads/writes. Thinking blocks are recorded as *not captured*. A full tool-use agent: `traceglass demo claude` ([source](../python/src/traceglass/demos/claude.py)).

## OpenTelemetry (OTLP/HTTP JSON)

```bash
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://127.0.0.1:7357/v1/traces
export OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/json
```

One trace becomes one run. Kinds come from `openinference.span.kind` or `gen_ai.operation.name` (`chat`, `execute_tool`, `invoke_agent`, …); model, token usage, input/output messages, tool names and agent names are read from the GenAI and OpenInference conventions; `retrieval.documents.*` become document artifacts; exception events become errors. Unrecognised attributes are kept. Protobuf is not accepted yet. Files can be imported offline with `traceglass import otlp trace.json`. Example: [`examples/otel_example.py`](../examples/otel_example.py).

## Claude Code and Kiro sessions

Live watching, hooks and timing semantics: [coding-agents.md](coding-agents.md). The import commands below are the one-off path.

### Claude Code transcripts

```bash
traceglass import claude-code               # this directory's sessions
traceglass import claude-code --latest      # just the most recent
traceglass import claude-code --all         # every project
traceglass import claude-code path/to/session.jsonl
```

A session becomes a run; each prompt a turn; each API response an `llm` span with usage; each tool call a `tool` span with its input and result; `Agent`/`Task` calls become `delegation` spans containing the subagent's full transcript; `Edit`/`Write` become `diff`/`code` artifacts; the last reply becomes the final artifact. Thinking is never imported. Re-importing replaces the run.

### Kiro CLI sessions

```bash
traceglass import kiro                      # this directory's sessions
traceglass import kiro --latest | --all
traceglass import kiro ~/.kiro/sessions/cli/<id>.jsonl
```

A session becomes a run; each prompt a turn (with its token counts and credits); each assistant message an `llm` span; each tool call a `tool` span labelled with the agent's stated purpose; `write` calls become `diff`/`code` artifacts; context clears and compactions become events. Step timing inside a turn is estimated unless hooks are installed.

## TypeScript / Node

See [`sdk/typescript`](../sdk/typescript). Same record shapes, posted to `/api/ingest`.

## Raw HTTP

```bash
curl -s http://127.0.0.1:7357/api/ingest -H 'Content-Type: application/json' -d '{
  "records": [
    {"type":"run","id":"run_demo1","name":"From curl","project":"http","status":"ok","started_at":1730000000000,"ended_at":1730000002000},
    {"type":"span","id":"sp_demo1","run_id":"run_demo1","kind":"tool","name":"lookup","status":"ok","started_at":1730000000100,"ended_at":1730000001900,"input":{"q":"hi"},"output":"hello"}
  ]}'
```

Writes must be `application/json` and same-origin (or carry no `Origin`, as non-browser clients don't).

## API

| | |
|---|---|
| `GET /api/runs?q=&project=&status=&limit=` | run summaries |
| `GET /api/runs/<id>` | full bundle |
| `GET /api/runs/<id>/export.html` | self-contained HTML snapshot |
| `GET /api/search?q=` | runs, steps and artifacts (including artifact content) |
| `GET /api/stream` | server-sent events: `changes` batches of `{seq, type, run_id, record}` |
| `POST /api/ingest` | `{"records": [...]}` |
| `POST /v1/traces` | OTLP/HTTP JSON |
| `DELETE /api/runs/<id>` | delete a run |
