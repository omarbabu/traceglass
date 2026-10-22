import asyncio

import pytest

pytest.importorskip("langchain_core")

from langchain_core.documents import Document  # noqa: E402
from langchain_core.language_models.fake_chat_models import FakeListChatModel  # noqa: E402
from langchain_core.prompts import ChatPromptTemplate  # noqa: E402
from langchain_core.retrievers import BaseRetriever  # noqa: E402
from langchain_core.runnables import RunnableLambda  # noqa: E402
from langchain_core.tools import tool  # noqa: E402

import traceglass as tg  # noqa: E402
from traceglass.integrations.langchain import TraceglassCallbackHandler  # noqa: E402


class Retriever(BaseRetriever):
    def _get_relevant_documents(self, query, *, run_manager):
        return [Document(page_content=f"facts about {query}", metadata={"source": "kb/a.md"}), Document(page_content="more")]


@tool
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@tool
def explode(x: str) -> str:
    """Always fails."""
    raise ValueError("kaboom")


def chain():
    return {"ctx": Retriever(), "q": RunnableLambda(lambda x: x)} | ChatPromptTemplate.from_template("{ctx} {q}") | FakeListChatModel(responses=["the answer"])


def spans(sink):
    return sink.of_type("span")


def test_explicit_handler_maps_kinds(mem):
    with tg.run("lc") as r:
        out = chain().invoke("cats", config={"callbacks": [TraceglassCallbackHandler()]})
    assert out.content == "the answer"
    ss = spans(mem)
    kinds = {s["name"]: s["kind"] for s in ss}
    assert kinds["Retriever"] == "retrieval" and kinds["FakeListChatModel"] == "llm" and kinds["RunnableSequence"] == "step"
    assert all(s["run_id"] == r.id for s in ss)
    root = next(s for s in ss if s["name"] == "RunnableSequence")
    llm = next(s for s in ss if s["kind"] == "llm")
    assert llm["output"] == {"role": "ai", "content": "the answer"}
    assert llm["input"]["messages"][0]["role"] == "human"
    assert all(s.get("parent_id") for s in ss if s is not root)
    docs = mem.of_type("artifact")
    assert [d["name"] for d in docs] == ["kb/a.md", "document 2"] and docs[0]["kind"] == "document"
    ret = next(s for s in ss if s["kind"] == "retrieval")
    assert docs[0]["span_id"] == ret["id"] and ret["input"] == {"query": "cats"}


def test_auto_install_inside_observe(mem):
    with tg.observe(project="auto") as r:
        chain().invoke("dogs")
        add.invoke({"a": 2, "b": 3})
    ss = spans(mem)
    tool_span = next(s for s in ss if s["name"] == "add")
    assert tool_span["kind"] == "tool" and tool_span["output"] == 5 and tool_span["input"] == {"a": 2, "b": 3}
    assert {s["run_id"] for s in ss} == {r.id}
    n = len(mem.records)
    chain().invoke("outside any run")  # auto handler only records while a run is active
    assert len(mem.records) == n


def test_auto_and_explicit_do_not_duplicate(mem):
    with tg.observe(project="dup"):
        add.invoke({"a": 1, "b": 1}, config={"callbacks": [TraceglassCallbackHandler()]})
    assert len([s for s in spans(mem) if s["name"] == "add"]) == 1


def test_explicit_handler_without_run_creates_implicit_run(mem):
    chain().invoke("x", config={"callbacks": [TraceglassCallbackHandler()]})
    runs = mem.of_type("run")
    assert len(runs) == 1 and runs[0]["name"] == "RunnableSequence"
    assert {s["run_id"] for s in spans(mem)} == {runs[0]["id"]}


def test_tool_error(mem):
    with tg.run("err"):
        with pytest.raises(ValueError):
            explode.invoke({"x": "y"}, config={"callbacks": [TraceglassCallbackHandler()]})
    s = next(s for s in spans(mem) if s["name"] == "explode")
    assert s["status"] == "error" and s["error"]["message"] == "kaboom"


def test_async_invoke(mem):
    async def main():
        with tg.run("async"):
            return await chain().ainvoke("z", config={"callbacks": [TraceglassCallbackHandler()]})

    assert asyncio.run(main()).content == "the answer"
    assert any(s["kind"] == "llm" and s["status"] == "ok" for s in spans(mem))


def test_langgraph_hides_plumbing(mem):
    pytest.importorskip("langgraph")
    import importlib.util
    import sys
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "examples" / "langgraph_agent.py"
    spec = importlib.util.spec_from_file_location("lg_example", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["lg_example"] = mod
    spec.loader.exec_module(mod)
    run_id = mod.main()
    ss = [s for s in spans(mem) if s["run_id"] == run_id]
    names = [s["name"] for s in ss]
    assert "LangGraph" in names and not any(n.startswith(("ChannelWrite", "_write")) for n in names)
    graph = next(s for s in ss if s["name"] == "LangGraph")
    assert graph["kind"] == "agent"
    assert [s["name"] for s in ss if s["kind"] == "tool"] == ["get_weather", "get_weather", "compare"]
    assert sum(1 for s in ss if s["kind"] == "llm") == 3
    ids = {s["id"] for s in ss}
    assert all(s["parent_id"] in ids for s in ss if s.get("parent_id"))
    arts = mem.of_type("artifact")
    assert any(a.get("final") for a in arts)
