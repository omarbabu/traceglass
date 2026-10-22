"""Where records go. A sink is anything with ``emit(record)``, ``flush()`` and ``close()``."""

from __future__ import annotations

import json
import os
import queue
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol


class Sink(Protocol):
    def emit(self, record: Dict[str, Any]) -> None: ...
    def flush(self, timeout: Optional[float] = None) -> None: ...
    def close(self) -> None: ...


class NullSink:
    def emit(self, record: Dict[str, Any]) -> None:
        pass

    def flush(self, timeout: Optional[float] = None) -> None:
        pass

    def close(self) -> None:
        pass


class MemorySink:
    """Keeps records in a list — handy for tests and for building bundles in-process."""

    def __init__(self) -> None:
        self.records: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    def emit(self, record: Dict[str, Any]) -> None:
        with self._lock:
            self.records.append(json.loads(json.dumps(record)))

    def flush(self, timeout: Optional[float] = None) -> None:
        pass

    def close(self) -> None:
        pass

    def of_type(self, type_: str) -> List[Dict[str, Any]]:
        """Latest version of each record of a type (records are upserts)."""
        latest: Dict[str, Dict[str, Any]] = {}
        for r in self.records:
            if r["type"] == type_:
                latest[r["id"]] = {**latest.get(r["id"], {}), **{k: v for k, v in r.items() if v is not None}}
        return list(latest.values())


class _BatchingSink:
    """Writes in a background thread so instrumentation never blocks the agent on I/O."""

    def __init__(self, batch_size: int = 500, interval: float = 0.05) -> None:
        self._q: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()
        self._batch_size = batch_size
        self._interval = interval
        self._thread = threading.Thread(target=self._loop, name=f"traceglass-{type(self).__name__}", daemon=True)
        self._closed = False
        self._thread.start()

    def emit(self, record: Dict[str, Any]) -> None:
        if not self._closed:
            self._q.put(record)

    def flush(self, timeout: Optional[float] = None) -> None:
        done = threading.Event()
        self._q.put({"__flush__": done})
        done.wait(timeout)

    def close(self) -> None:
        if not self._closed:
            self.flush(5)
            self._closed = True
            self._q.put(None)

    def _loop(self) -> None:
        stop = False
        while not stop:
            item = self._q.get()
            if item is None:
                return
            batch, flushes = [], []
            while True:
                if "__flush__" in item:
                    flushes.append(item["__flush__"])
                else:
                    batch.append(item)
                if len(batch) >= self._batch_size:
                    break
                try:
                    nxt = self._q.get(timeout=self._interval) if not flushes else self._q.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    stop = True
                    break
                item = nxt
            if batch:
                try:
                    self._write(batch)
                except Exception as exc:  # never crash the host agent because tracing failed
                    _warn(f"traceglass: failed to write {len(batch)} records: {exc}")
            for f in flushes:
                f.set()

    def _write(self, batch: List[Dict[str, Any]]) -> None:  # pragma: no cover - abstract
        raise NotImplementedError


class SQLiteSink(_BatchingSink):
    """Default sink: the local Traceglass database (``~/.traceglass/traceglass.db``)."""

    def __init__(self, path: Optional[os.PathLike] = None, **kw: Any) -> None:
        from .store import Store

        self._store_path = path
        self._store: Optional[Store] = None
        self._Store = Store
        super().__init__(**kw)

    def _write(self, batch: List[Dict[str, Any]]) -> None:
        if self._store is None:
            self._store = self._Store(self._store_path)
        self._store.write(batch)

    @property
    def path(self) -> Path:
        from .store import default_db_path

        return Path(self._store_path) if self._store_path else default_db_path()


class HTTPSink(_BatchingSink):
    """Ships records to a running ``traceglass serve`` (``POST /api/ingest``)."""

    def __init__(self, url: str = "http://127.0.0.1:7357", **kw: Any) -> None:
        self.url = url.rstrip("/") + "/api/ingest"
        super().__init__(**kw)

    def _write(self, batch: List[Dict[str, Any]]) -> None:
        import urllib.request

        req = urllib.request.Request(
            self.url,
            data=json.dumps({"records": batch}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()


class JSONLSink:
    """Appends records to a JSON-lines file; ``traceglass import jsonl`` loads it later."""

    def __init__(self, path: os.PathLike) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fh = open(self.path, "a", encoding="utf-8")

    def emit(self, record: Dict[str, Any]) -> None:
        with self._lock:
            self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def flush(self, timeout: Optional[float] = None) -> None:
        with self._lock:
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            self._fh.close()


class FanoutSink:
    def __init__(self, *sinks: Sink) -> None:
        self.sinks = sinks

    def emit(self, record: Dict[str, Any]) -> None:
        for s in self.sinks:
            s.emit(record)

    def flush(self, timeout: Optional[float] = None) -> None:
        for s in self.sinks:
            s.flush(timeout)

    def close(self) -> None:
        for s in self.sinks:
            s.close()


_warned: set = set()


def _warn(msg: str) -> None:
    if msg not in _warned:
        _warned.add(msg)
        import sys

        print(msg, file=sys.stderr)


def default_sink() -> Sink:
    if os.environ.get("TRACEGLASS_DISABLED", "").lower() in ("1", "true", "yes"):
        return NullSink()
    url = os.environ.get("TRACEGLASS_URL")
    if url:
        return HTTPSink(url)
    return SQLiteSink()
