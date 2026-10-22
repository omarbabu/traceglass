"""A tiny LangGraph tool-calling agent, traced with Traceglass — runs fully offline.

    pip install traceglass langgraph
    python examples/langgraph_agent.py
    traceglass serve        # open http://127.0.0.1:7357

The "model" is a scripted fake chat model, so the example is deterministic and needs no
API key; swap in ``ChatAnthropic(model="claude-opus-5-5")`` for a real agent.
"""

from __future__ import annotations

from typing import Annotated, TypedDict

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

import traceglass as tg

CITIES = {"paris": 18, "tokyo": 24, "oslo": 3}


@tool
def get_weather(city: str) -> str:
    """Current temperature for a city, in °C."""
    if city.lower() not in CITIES:
        raise ValueError(f"no weather station for {city!r}")
    return f"{CITIES[city.lower()]}°C in {city.title()}"


@tool
def compare(a: float, b: float) -> str:
    """Compare two temperatures."""
    return f"{abs(a - b):.0f}°C difference"


SCRIPT = [
    AIMessage(content="", tool_calls=[
        {"name": "get_weather", "args": {"city": "Paris"}, "id": "call_1"},
        {"name": "get_weather", "args": {"city": "Tokyo"}, "id": "call_2"},
    ]),
    AIMessage(content="", tool_calls=[{"name": "compare", "args": {"a": 18, "b": 24}, "id": "call_3"}]),
    AIMessage(content="Tokyo (24°C) is 6°C warmer than Paris (18°C) right now."),
]


class State(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


def build_graph():
    model = GenericFakeChatModel(messages=iter(SCRIPT))

    def agent(state: State) -> dict:
        return {"messages": [model.invoke(state["messages"])]}

    def route(state: State) -> str:
        return "tools" if state["messages"][-1].tool_calls else END

    g = StateGraph(State)
    g.add_node("agent", agent)
    g.add_node("tools", ToolNode([get_weather, compare]))
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", route, ["tools", END])
    g.add_edge("tools", "agent")
    return g.compile()


def main() -> str:
    graph = build_graph()
    question = "Is it warmer in Paris or Tokyo?"
    with tg.observe(project="langgraph-example", name=question, input=question) as run:
        result = graph.invoke({"messages": [HumanMessage(question)]})
        answer = result["messages"][-1].content
        tg.artifact("Answer", answer, kind="answer", final=True)
        run.set_output(answer)
    tg.flush()
    print(f"{answer}\n\nrecorded run {run.id} — view it with `traceglass serve`")
    return run.id


if __name__ == "__main__":
    main()
