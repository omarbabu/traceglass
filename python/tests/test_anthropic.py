import asyncio

import pytest

anthropic = pytest.importorskip("anthropic")

from anthropic.types import Message  # noqa: E402

import traceglass as tg  # noqa: E402
from traceglass.integrations.anthropic import instrument  # noqa: E402


def fake_message(**over):
    data = {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [
            {"type": "thinking", "thinking": "private reasoning", "signature": "sig"},
            {"type": "text", "text": "Let me search."},
            {"type": "tool_use", "id": "toolu_1", "name": "search", "input": {"q": "x"}},
        ],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {"input_tokens": 50, "output_tokens": 12, "cache_read_input_tokens": 7, "cache_creation_input_tokens": 3},
    }
    data.update(over)
    return Message.model_validate(data)


def make_client(result=None, error=None, is_async=False):
    client = (anthropic.AsyncAnthropic if is_async else anthropic.Anthropic)(api_key="sk-ant-test-not-real-0000000000000000")
    calls = []

    if is_async:
        async def create(**kw):
            calls.append(kw)
            if error:
                raise error
            return result

    else:
        def create(**kw):
            calls.append(kw)
            if error:
                raise error
            return result

    client.messages.create = create  # instance-level stub: no network
    return client, calls


def test_records_llm_span(mem):
    client, calls = make_client(fake_message())
    instrument(client)
    with tg.run("q"):
        msg = client.messages.create(
            model="claude-opus-5-5", max_tokens=1024, system="be brief",
            messages=[{"role": "user", "content": "hi"}],
            tools=[{"name": "search", "description": "d", "input_schema": {"type": "object"}}],
            thinking={"type": "adaptive"},
        )
    assert msg.id == "msg_01" and calls[0]["model"] == "claude-opus-5-5"
    s = mem.of_type("span")[0]
    assert s["kind"] == "llm" and s["model"] == "claude-opus-5-5" and s["name"] == "messages.create · claude-opus-5-5"
    assert s["input"] == {"messages": [{"role": "user", "content": "hi"}], "system": "be brief", "tools": ["search"]}
    assert s["attributes"]["max_tokens"] == 1024 and s["attributes"]["thinking"] == {"type": "adaptive"}
    assert s["usage"] == {"input_tokens": 50, "output_tokens": 12, "cache_read_tokens": 7, "cache_write_tokens": 3, "total_tokens": 62}
    blocks = s["output"]["content"]
    assert blocks[0]["$tg"] == "not_captured" and "private reasoning" not in str(s)
    assert blocks[1] == {"type": "text", "text": "Let me search."} and blocks[2]["name"] == "search"
    assert s["attributes"]["stop_reason"] == "tool_use" and s["attributes"]["tool_calls"] == ["search"]
    assert s["status"] == "ok"


def test_error_recorded_and_reraised(mem):
    client, _ = make_client(error=RuntimeError("overloaded"))
    instrument(client)
    with tg.run("q"):
        with pytest.raises(RuntimeError):
            client.messages.create(model="m", max_tokens=1, messages=[])
    s = mem.of_type("span")[0]
    assert s["status"] == "error" and s["error"]["message"] == "overloaded"


def test_instrument_is_idempotent_and_local(mem):
    client, _ = make_client(fake_message())
    instrument(client)
    instrument(client)
    other, _ = make_client(fake_message())
    with tg.run("q"):
        client.messages.create(model="m", max_tokens=1, messages=[])
        other.messages.create(model="m", max_tokens=1, messages=[])
    assert len(mem.of_type("span")) == 1


def test_stream_true_marks_output_not_captured(mem):
    client, _ = make_client(object())
    instrument(client)
    with tg.run("q"):
        client.messages.create(model="m", max_tokens=1, messages=[], stream=True)
    assert mem.of_type("span")[0]["output"]["$tg"] == "not_captured"


def test_async_client(mem):
    client, _ = make_client(fake_message(content=[{"type": "text", "text": "ok"}], stop_reason="end_turn"), is_async=True)
    instrument(client)

    async def main():
        with tg.run("q"):
            return await client.messages.create(model="m", max_tokens=1, messages=[])

    assert asyncio.run(main()).content[0].text == "ok"
    s = mem.of_type("span")[0]
    assert s["output"]["content"] == [{"type": "text", "text": "ok"}] and s["attributes"]["stop_reason"] == "end_turn"


def test_stream_manager_wrapper(mem):
    client = anthropic.Anthropic(api_key="sk-ant-test-not-real-0000000000000000")
    final = fake_message(content=[{"type": "text", "text": "streamed"}])

    class FakeStream:
        current_message_snapshot = final

        def __iter__(self):
            return iter(())

    class FakeManager:
        def __enter__(self):
            return FakeStream()

        def __exit__(self, *a):
            return None

    client.messages.stream = lambda **kw: FakeManager()
    instrument(client)
    with tg.run("q"):
        with client.messages.stream(model="claude-opus-5-5", max_tokens=10, messages=[]) as stream:
            list(stream)
    s = mem.of_type("span")[0]
    assert s["output"]["content"] == [{"type": "text", "text": "streamed"}] and s["usage"]["output_tokens"] == 12
