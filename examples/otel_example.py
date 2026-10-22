"""Send OpenTelemetry GenAI spans to Traceglass — no OpenTelemetry dependency needed.

Traceglass accepts OTLP/HTTP **JSON** at ``POST /v1/traces``. This script builds one trace by
hand (an agent invocation containing a chat call and a tool call, using the
``gen_ai.*`` semantic conventions) and posts it with urllib.

With a real OpenTelemetry SDK, point the OTLP HTTP exporter at Traceglass and select JSON:

    export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://127.0.0.1:7357/v1/traces
    export OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/json

(Protobuf encoding is not accepted yet.) OpenInference attributes
(``openinference.span.kind``, ``llm.*``, ``retrieval.documents.*``) are understood too.

Run ``traceglass serve`` first, then ``python examples/otel_example.py``.
"""

import json
import os
import time
import urllib.request

BASE = os.environ.get("TRACEGLASS_URL", "http://127.0.0.1:7357")
URL = BASE + "/v1/traces"


def attr(key, value):
    if isinstance(value, bool):
        v = {"boolValue": value}
    elif isinstance(value, int):
        v = {"intValue": str(value)}
    elif isinstance(value, float):
        v = {"doubleValue": value}
    else:
        v = {"stringValue": value}
    return {"key": key, "value": v}


now = time.time_ns()
trace_id = os.urandom(16).hex()
root, chat, tool = (os.urandom(8).hex() for _ in range(3))
ms = 1_000_000

spans = [
    {
        "traceId": trace_id, "spanId": root, "name": "invoke_agent weather-assistant",
        "startTimeUnixNano": str(now), "endTimeUnixNano": str(now + 2400 * ms), "status": {"code": 1},
        "attributes": [attr("gen_ai.operation.name", "invoke_agent"), attr("gen_ai.agent.name", "weather-assistant"),
                       attr("input.value", "Do I need an umbrella in Paris today?")],
    },
    {
        "traceId": trace_id, "spanId": chat, "parentSpanId": root, "name": "chat claude-sonnet-5-5",
        "startTimeUnixNano": str(now + 50 * ms), "endTimeUnixNano": str(now + 1300 * ms), "status": {"code": 1},
        "attributes": [attr("gen_ai.operation.name", "chat"), attr("gen_ai.provider.name", "anthropic"),
                       attr("gen_ai.request.model", "claude-sonnet-5-5"), attr("gen_ai.usage.input_tokens", 412),
                       attr("gen_ai.usage.output_tokens", 38)],
    },
    {
        "traceId": trace_id, "spanId": tool, "parentSpanId": root, "name": "execute_tool get_forecast",
        "startTimeUnixNano": str(now + 1350 * ms), "endTimeUnixNano": str(now + 2300 * ms), "status": {"code": 1},
        "attributes": [attr("gen_ai.operation.name", "execute_tool"), attr("gen_ai.tool.name", "get_forecast"),
                       attr("gen_ai.tool.call.arguments", json.dumps({"city": "Paris"})),
                       attr("gen_ai.tool.call.result", json.dumps({"rain_probability": 0.7}))],
    },
]

payload = {"resourceSpans": [{"resource": {"attributes": [attr("service.name", "otel-example")]},
                              "scopeSpans": [{"scope": {"name": "manual"}, "spans": spans}]}]}
req = urllib.request.Request(URL, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
with urllib.request.urlopen(req) as resp:
    print(resp.status, resp.read().decode())
print(f"Open {BASE} — run id otel_{trace_id}")
