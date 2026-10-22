"""Framework integrations — thin translators onto Traceglass runs, spans and artifacts.

Each module imports its framework lazily, so ``traceglass`` itself stays dependency-free:

* ``traceglass.integrations.langchain``   — LangChain / LangGraph callback handler
* ``traceglass.integrations.anthropic``   — instrument an Anthropic SDK client
* ``traceglass.integrations.claude_code`` — import Claude Code session transcripts
"""
