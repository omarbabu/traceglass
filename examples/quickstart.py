"""Traceglass in 15 lines. Run it, then `traceglass serve --open`."""

import traceglass as tg


@tg.tool
def search(query: str) -> list:
    return ["Agents call tools.", "Traces explain answers."]


with tg.run("Hello, Traceglass", project="quickstart", input={"question": "What is a trace?"}):
    with tg.agent("assistant"):
        hits = search("what is a trace")
        source = tg.artifact("search result", hits[1], kind="document")
        with tg.llm("answer", model="claude-opus-5-5") as call:
            call.usage(input_tokens=120, output_tokens=24)
            answer = tg.artifact("Answer", "A trace explains how an answer was produced.", kind="markdown", final=True)
            answer.cite(source, anchor="explains how an answer was produced", quote="Traces explain answers.")
