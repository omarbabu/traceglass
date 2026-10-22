"""Record Anthropic SDK calls as ``llm`` spans.

    from anthropic import Anthropic
    from traceglass.integrations.anthropic import instrument

    client = instrument(Anthropic())
    with tg.run("Answer question"):
        client.messages.create(model="claude-opus-5-5", max_tokens=1024, messages=[...])

Only the given client instance is wrapped (its ``messages.create`` and ``messages.stream``);
global SDK state is never touched. Works with ``Anthropic`` and ``AsyncAnthropic``.
"""

from __future__ import annotations

import functools
import inspect
from typing import Any, Dict, Optional

from ..recorder import Span

_MARK = "__traceglass_instrumented__"


def _dump(obj: Any) -> Any:
    fn = getattr(obj, "model_dump", None)
    if callable(fn):
        try:
            return fn(mode="json", exclude_none=True)
        except TypeError:
            return fn()
    if isinstance(obj, (list, tuple)):
        return [_dump(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _dump(v) for k, v in obj.items()}
    return obj


def _request_input(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    inp: Dict[str, Any] = {"messages": _dump(kwargs.get("messages", []))}
    if kwargs.get("system") is not None:
        inp["system"] = _dump(kwargs["system"])
    tools = kwargs.get("tools")
    if tools:
        inp["tools"] = [t.get("name") if isinstance(t, dict) else getattr(t, "name", str(t)) for t in tools]
    return inp


def _request_attrs(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    attrs: Dict[str, Any] = {"provider": "anthropic"}
    for k in ("max_tokens", "thinking", "output_config", "tool_choice", "temperature", "stop_sequences", "metadata"):
        if kwargs.get(k) is not None:
            attrs[k] = _dump(kwargs[k])
    return attrs


def _start(kwargs: Dict[str, Any], name: Optional[str]) -> Span:
    model = kwargs.get("model")
    default_name = f"messages.create · {model}" if model else "messages.create"
    return Span(
        name or default_name,
        kind="llm",
        input=_request_input(kwargs),
        model=str(model) if model else None,
        attributes=_request_attrs(kwargs),
    ).start()


def _finish(span: Span, message: Any) -> None:
    content = getattr(message, "content", None)
    if content is not None:
        blocks = []
        for b in content:
            d = _dump(b)
            if isinstance(d, dict) and d.get("type") in ("thinking", "redacted_thinking"):
                # Thinking text is summarised or omitted by the API; never present it as reasoning.
                d = {"type": d["type"], "$tg": "not_captured", "reason": "thinking content is not recorded"}
            blocks.append(d)
        span.set_output({"role": "assistant", "content": blocks})
    usage = getattr(message, "usage", None)
    if usage is not None:
        span.usage(
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", None),
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", None),
        )
    if getattr(message, "model", None):
        span.set_model(str(message.model))
    for attr in ("stop_reason", "id"):
        val = getattr(message, attr, None)
        if val is not None:
            span.set_attribute("message_id" if attr == "id" else attr, str(val))
    tool_uses = [b for b in (content or []) if getattr(b, "type", None) == "tool_use"]
    if tool_uses:
        span.set_attribute("tool_calls", [getattr(b, "name", None) for b in tool_uses])
    span.end()


class _StreamManager:
    """Wraps ``MessageStreamManager`` so the span covers the whole streamed response."""

    def __init__(self, inner: Any, kwargs: Dict[str, Any], name: Optional[str]) -> None:
        self._inner, self._kwargs, self._name = inner, kwargs, name
        self._span: Optional[Span] = None
        self._stream: Any = None

    def _close(self, exc: Optional[BaseException]) -> None:
        span = self._span
        if span is None:
            return
        if exc is not None:
            span.fail(exc)
            return
        try:
            _finish(span, self._stream.current_message_snapshot)
        except Exception:
            span.mark_not_captured("output", "stream ended before a message snapshot was available")
            span.end()

    def __enter__(self) -> Any:
        self._span = _start(self._kwargs, self._name)
        self._stream = self._inner.__enter__()
        return self._stream

    def __exit__(self, et: Any, exc: Any, tb: Any) -> Any:
        try:
            return self._inner.__exit__(et, exc, tb)
        finally:
            self._close(exc)

    async def __aenter__(self) -> Any:
        self._span = _start(self._kwargs, self._name)
        self._stream = await self._inner.__aenter__()
        return self._stream

    async def __aexit__(self, et: Any, exc: Any, tb: Any) -> Any:
        try:
            return await self._inner.__aexit__(et, exc, tb)
        finally:
            self._close(exc)


def instrument(client: Any, *, name: Optional[str] = None) -> Any:
    """Instrument one ``Anthropic`` / ``AsyncAnthropic`` client in place and return it."""
    messages = client.messages
    if getattr(messages, _MARK, False):
        return client
    create = messages.create

    if inspect.iscoroutinefunction(create):

        @functools.wraps(create)
        async def acreate(*args: Any, **kwargs: Any) -> Any:
            span = _start(kwargs, name)
            try:
                result = await create(*args, **kwargs)
            except BaseException as exc:
                span.fail(exc)
                raise
            if kwargs.get("stream"):
                span.mark_not_captured("output", "stream=True responses are not captured; use messages.stream()")
                span.end()
            else:
                _finish(span, result)
            return result

        messages.create = acreate
    else:

        @functools.wraps(create)
        def screate(*args: Any, **kwargs: Any) -> Any:
            span = _start(kwargs, name)
            try:
                result = create(*args, **kwargs)
            except BaseException as exc:
                span.fail(exc)
                raise
            if kwargs.get("stream"):
                span.mark_not_captured("output", "stream=True responses are not captured; use messages.stream()")
                span.end()
            else:
                _finish(span, result)
            return result

        messages.create = screate

    stream = getattr(messages, "stream", None)
    if callable(stream):

        @functools.wraps(stream)
        def wstream(*args: Any, **kwargs: Any) -> Any:
            return _StreamManager(stream(*args, **kwargs), kwargs, name)

        messages.stream = wstream
    setattr(messages, _MARK, True)
    return client
