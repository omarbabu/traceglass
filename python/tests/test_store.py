import traceglass as tg
from traceglass.store import Store


def _run(store, **kw):
    tg.configure(sink=tg.SQLiteSink(store.path), project="p")
    with tg.run(kw.get("name", "my run"), session="sess") as r:
        with tg.llm("think", model="m") as s:
            s.usage(100, 20, cost_usd=0.5)
        with tg.span("fail", kind="tool") as f:
            f.record_error("bad")
        a = tg.artifact("notes", "searchable needle text", kind="text")
        a.derived_from(s)
    tg.flush()
    return r


def test_upsert_merges_records(db):
    st = Store(db)
    st.write([{"type": "span", "id": "s1", "run_id": "r1", "name": "a", "status": "running", "attributes": {"x": 1}, "started_at": 1}])
    st.write([{"type": "span", "id": "s1", "run_id": "r1", "status": "ok", "attributes": {"y": 2}, "ended_at": 5, "name": None}])
    span = st.get_bundle("r1")["spans"][0]
    assert span["status"] == "ok" and span["name"] == "a" and span["attributes"] == {"x": 1, "y": 2} and span["ended_at"] == 5


def test_run_window_widens_across_batches(db):
    st = Store(db)
    st.write([{"type": "run", "id": "r", "name": "x", "started_at": 10, "ended_at": 20}])
    st.write([{"type": "run", "id": "r", "started_at": 5, "ended_at": 15}])
    run = st.get_run("r")
    assert run["started_at"] == 5 and run["ended_at"] == 20


def test_ignores_invalid_records(db):
    st = Store(db)
    assert st.write([{"type": "bogus", "id": "x"}, {"type": "span"}, "nope"]) == 0


def test_bundle_and_summary(db, mem):
    st = Store(db)
    r = _run(st)
    b = st.get_bundle(r.id)
    assert b["schema"] == "traceglass/v1"
    assert {s["name"] for s in b["spans"]} == {"think", "fail"}
    assert b["run"]["summary"] == {"spans": 2, "errors": 1, "tokens": 120, "cost_usd": 0.5}
    assert len(b["artifacts"]) == 1 and len(b["links"]) == 1
    runs = st.list_runs()
    assert runs[0]["id"] == r.id and runs[0]["summary"]["artifacts"] == 1 and runs[0]["summary"]["tokens"] == 120
    assert "input" not in runs[0]
    assert st.list_runs(project="p") and not st.list_runs(project="nope")
    assert st.list_runs(q="my r") and not st.list_runs(q="zzz")
    assert st.get_bundle("missing") is None


def test_bundle_without_run_record_derives_status(db):
    st = Store(db)
    st.write([{"type": "span", "id": "a", "run_id": "otel_1", "name": "root", "status": "error", "started_at": 1, "ended_at": 3}])
    b = st.get_bundle("otel_1")
    assert b["run"]["status"] == "error" and b["run"]["name"] == "root" and b["run"]["ended_at"] == 3


def test_search(db, mem):
    st = Store(db)
    r = _run(st)
    kinds = {(h["kind"], h["name"]) for h in st.search("needle")}
    assert ("artifact", "notes") in kinds
    assert any(h["kind"] == "span" and h["run_id"] == r.id for h in st.search("think"))
    assert any(h["kind"] == "run" for h in st.search("my run"))


def test_changes_and_delete(db, mem):
    st = Store(db)
    seq0 = st.latest_seq()
    r = _run(st)
    changes = st.changes_since(seq0)
    assert changes and all(c["run_id"] == r.id for c in changes)
    assert {c["type"] for c in changes} >= {"run", "span", "artifact", "link"}
    assert changes[-1]["record"]["id"]
    seq1 = st.latest_seq()
    assert st.delete_run(r.id) is True
    assert st.get_bundle(r.id) is None and st.list_runs() == []
    assert st.changes_since(seq1)[-1]["type"] == "delete"
    assert st.delete_run(r.id) is False
    st.prune_changes(older_than_s=-1)
    assert st.changes_since(0) == []
