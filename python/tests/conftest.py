import pytest

import traceglass as tg
from traceglass import recorder


@pytest.fixture
def mem():
    """Route all recording to an in-memory sink for the duration of a test."""
    old = recorder._tracer
    sink = tg.MemorySink()
    tg.configure(sink=sink, project="test")
    yield sink
    recorder._tracer = old


@pytest.fixture
def db(tmp_path):
    return tmp_path / "traceglass.db"
