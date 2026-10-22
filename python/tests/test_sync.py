"""Store.sync_run: authoritative re-sync that writes only what changed."""

from traceglass.store import Store


def _records(status="running", end=None, extra_span=True, text="hello"):
    recs = [
        {"type": "run", "id": "run_s", "name": "s", "status": status, "started_at": 1.0, "ended_at": end, "source": "kiro"},
        {"type": "span", "id": "sp_a", "run_id": "run_s", "kind": "step", "name": "turn", "status": "ok", "started_at": 1.0, "ended_at": 5.0},
        {"type": "artifact", "id": "art_a", "run_id": "run_s", "kind": "markdown", "name": "answer", "content": text, "created_at": 5.0},
    ]
    if extra_span:
        recs.append({"type": "span", "id": "sp_b", "run_id": "run_s", "kind": "tool", "name": "shell", "status": "running", "started_at": 2.0, "ended_at": None})
    return recs


def test_first_sync_writes_everything(db):
    st = Store(db)
    assert st.sync_run("run_s", _records()) == (4, 0)
    b = st.get_bundle("run_s")
    assert {s["id"] for s in b["spans"]} == {"sp_a", "sp_b"}


def test_resync_unchanged_writes_nothing(db):
    st = Store(db)
    st.sync_run("run_s", _records())
    seq = st.latest_seq()
    assert st.sync_run("run_s", _records()) == (0, 0)
    assert st.latest_seq() == seq  # no change rows → nothing pushed to live UIs


def test_resync_writes_only_changed_records(db):
    st = Store(db)
    st.sync_run("run_s", _records())
    seq = st.latest_seq()
    recs = _records(status="ok", end=9.0, text="hello world")
    written, removed = st.sync_run("run_s", recs)
    assert (written, removed) == (2, 0)  # run + artifact; both spans untouched
    changed = {(c["type"], c["record"]["id"]) for c in st.changes_since(seq)}
    assert changed == {("run", "run_s"), ("artifact", "art_a")}


def test_replacement_semantics_drop_fields(db):
    """Unlike write(), sync_run replaces: a field the importer no longer emits disappears."""
    st = Store(db)
    recs = _records()
    recs[1]["attributes"] = {"timing": "estimated"}
    st.sync_run("run_s", recs)
    recs = _records()
    recs[1]["attributes"] = {}
    st.sync_run("run_s", recs)
    span = next(s for s in st.get_bundle("run_s")["spans"] if s["id"] == "sp_a")
    assert span["attributes"] == {}


def test_stale_records_are_removed_and_announced(db):
    st = Store(db)
    st.sync_run("run_s", _records())
    seq = st.latest_seq()
    assert st.sync_run("run_s", _records(extra_span=False)) == (0, 1)
    assert [s["id"] for s in st.get_bundle("run_s")["spans"]] == ["sp_a"]
    ch = st.changes_since(seq)
    assert ch == [{"seq": ch[0]["seq"], "type": "remove", "run_id": "run_s", "record": {"id": "sp_b", "type": "span"}}]


def test_sync_does_not_touch_other_runs(db):
    st = Store(db)
    st.sync_run("run_s", _records())
    st.write([{"type": "run", "id": "run_o", "name": "o"}, {"type": "span", "id": "sp_o", "run_id": "run_o", "kind": "tool", "name": "x", "status": "ok"}])
    st.sync_run("run_s", _records(extra_span=False))
    assert [s["id"] for s in st.get_bundle("run_o")["spans"]] == ["sp_o"]
