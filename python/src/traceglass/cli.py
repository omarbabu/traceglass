"""``traceglass`` command-line interface (stdlib only)."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from ._version import __version__
from .server import DEFAULT_PORT
from .store import Store, default_db_path

GLYPH = {"ok": "✓", "error": "✗", "running": "●", "waiting": "◷", "cancelled": "⊘", "skipped": "–"}
KIND = {"agent": "AGENT", "llm": "LLM", "tool": "TOOL", "retrieval": "RETR", "delegation": "DELEG",
        "approval": "GATE", "guardrail": "GUARD", "embedding": "EMBED", "step": "STEP"}


def _color(s: str, code: str) -> str:
    return f"\033[{code}m{s}\033[0m" if sys.stdout.isatty() and not os.environ.get("NO_COLOR") else s


STATUS_COLOR = {"ok": "32", "error": "31", "running": "36", "waiting": "33"}


def _status(st: Optional[str]) -> str:
    st = st or "?"
    return _color(f"{GLYPH.get(st, '?')} {st}", STATUS_COLOR.get(st, "0"))


def _dur(start: Optional[float], end: Optional[float]) -> str:
    if start is None or end is None:
        return "—"
    ms = end - start
    if ms < 1000:
        return f"{ms:.0f}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    return f"{int(ms // 60_000)}m{int(ms % 60_000 / 1000):02d}s"


def _age(ts: Optional[float]) -> str:
    if ts is None:
        return "—"
    s = time.time() - ts / 1000
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{int(s // n)}{unit} ago"
    return "just now"


def _tokens(n: Optional[int]) -> str:
    n = n or 0
    return f"{n / 1_000_000:.1f}M" if n >= 1_000_000 else f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _db(args: argparse.Namespace) -> Path:
    return Path(args.db).expanduser() if getattr(args, "db", None) else default_db_path()


def _health(port: int) -> Optional[Dict[str, Any]]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=0.5) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _store(args: argparse.Namespace) -> Store:
    return Store(_db(args))


def _resolve_run(store: Store, prefix: str) -> str:
    if store.get_run(prefix):
        return prefix
    matches = [r["id"] for r in store.list_runs(limit=5000) if r["id"].startswith(prefix) or r["id"].endswith(prefix)]
    if len(matches) == 1:
        return matches[0]
    raise SystemExit(f"traceglass: {'ambiguous' if matches else 'no'} run matching {prefix!r}")


# -- commands ---------------------------------------------------------------------------
def _watcher(args: argparse.Namespace, store: Store, quiet: bool = False) -> Any:
    from .watch import Watcher, parse_since, stderr_log

    try:
        since = parse_since(getattr(args, "since", None))
    except ValueError as exc:
        raise SystemExit(f"traceglass: {exc}")
    return Watcher(
        store,
        claude_code=not getattr(args, "no_claude_code", False),
        kiro=not getattr(args, "no_kiro", False),
        since_days=since,
        interval=getattr(args, "interval", 1.0) or 1.0,
        project_dir=getattr(args, "project_dir", None),
        log=None if quiet else stderr_log,
    )


def cmd_serve(args: argparse.Namespace) -> None:
    from .server import serve

    db = _db(args)
    if args.watch:
        w = _watcher(args, Store(db), quiet=args.quiet)
        w.start()
        print("  ◆ Watching Claude Code and Kiro sessions — they appear live as you work.", flush=True)
    serve(host=args.host, port=args.port, db=str(db), open_browser=args.open)


def cmd_watch(args: argparse.Namespace) -> None:
    db = _db(args)
    store = Store(db)
    w = _watcher(args, store, quiet=args.quiet)
    sources = [n for n, on in (("Claude Code", w.claude_code), ("Kiro", w.kiro)) if on]
    window = "all time" if w.since_days is None else (args.since or "3d")
    print(f"\n  ◆ Traceglass watch — {' + '.join(sources) or 'nothing'} · backfill {window}\n  ➜  db: {db}", flush=True)
    if _health(DEFAULT_PORT):
        print(f"  ➜  open http://127.0.0.1:{DEFAULT_PORT}", flush=True)
    else:
        print(f"  ➜  view: traceglass serve --open{'' if db == default_db_path() else f' --db {db}'}", flush=True)
    print("  Ctrl-C to stop.\n", flush=True)
    if args.once:
        w.poll_once()
        print(f"  {w.syncs} session(s) checked, {w.errors} error(s).")
        return
    w.run_forever()


def cmd_hook(args: argparse.Namespace) -> None:  # pragma: no cover - normally dispatched in main() fast path
    from .hooks import main_hook

    main_hook([args.source] + (["--event", args.event] if args.event else []))


def _print_change(ch: Any) -> None:
    mark = {"installed": "✓", "uninstalled": "✓", "unchanged": "·", "skipped": "–"}.get(ch.action, "→")
    print(f"  {mark} {ch.action:<15} {ch.path}" + (f"  ({ch.note})" if ch.note else ""))
    if ch.diff and ch.action.startswith("would"):
        print(ch.diff)


def cmd_hooks(args: argparse.Namespace) -> None:
    from . import hooks

    if args.action == "status":
        print("\n  Claude Code")
        cs = hooks.claude_status()
        if not cs:
            print("    no settings files found")
        for scope, path, found in cs:
            print(f"    {'✓ installed    ' if found else '– not installed'}  {scope:<8} {path}")
        print("\n  Kiro agents")
        ks = hooks.kiro_status()
        if not ks:
            print(f"    no agent files in {hooks.kiro_agents_dir()}")
        installed = [p for p, found in ks if found]
        for path in installed:
            print(f"    ✓ installed      {path.stem}")
        others = [p for p, found in ks if not found]
        default = [p for p in others if p.stem == "default"]
        for path in default:
            print(f"    – not installed  {path.stem}")
        rest = len(others) - len(default)
        if rest:
            print(f"    – not installed  {rest} other agent{'s' if rest != 1 else ''}  (install with --agent NAME or --all)")
        print("\n  Last hook event")
        for source, ev in hooks.last_events().items():
            when = _age(ev.get("ts")) if ev else "never"
            what = f"{ev.get('event')}{' · ' + str(ev.get('tool_name')) if ev and ev.get('tool_name') else ''}" if ev else ""
            print(f"    {source:<12} {when:<10} {what}")
        from .integrations.sessions import watcher_alive

        print(f"\n  Watcher: {'running' if watcher_alive() else 'not running (hooks import sessions themselves when a turn ends)'}\n")
        return
    if not args.source:
        raise SystemExit("traceglass: say which agent: claude-code or kiro")
    try:
        if args.source == "claude-code":
            fn = hooks.install_claude if args.action == "install" else hooks.uninstall_claude
            changes = [fn(scope=args.scope, dry_run=args.dry_run)]
        else:
            if args.action == "install":
                changes = hooks.install_kiro(args.agent or (), all_agents=args.all, dry_run=args.dry_run, force=args.force)
            else:
                changes = hooks.uninstall_kiro(args.agent or (), all_agents=args.all, dry_run=args.dry_run)
    except ValueError as exc:
        raise SystemExit(f"traceglass: {exc}")
    print()
    for ch in changes:
        _print_change(ch)
    if args.action == "install" and not args.dry_run and any(c.action == "installed" for c in changes):
        print("\n  Hooks record timing only (never prompt or tool content) and always exit silently.")
        print("  Undo with: traceglass hooks uninstall " + args.source + ("" if args.source == "kiro" else f" --scope {args.scope}"))
    print()


def cmd_demo(args: argparse.Namespace) -> None:
    import traceglass as tg

    from .demos import REGISTRY

    if args.name not in (None, "all") and args.name not in REGISTRY:
        raise SystemExit(f"traceglass: unknown demo {args.name!r}. Available: {', '.join(REGISTRY)}")
    magic = args.name is None  # `traceglass demo`: seed everything, serve, open the browser
    db = _db(args)
    running = _health(args.port)
    url = f"http://127.0.0.1:{args.port}"
    explicit_db = bool(getattr(args, "db", None) or os.environ.get("TRACEGLASS_DB"))
    if running and Path(running.get("db", "")) != db and not explicit_db:
        tg.configure(sink=tg.HTTPSink(url))  # a server is up on another database: send to it
        print(f"  ↳ sending to the running server at {url} (db: {running['db']})")
    else:
        tg.configure(sink=tg.SQLiteSink(db))
    port_busy = running is not None
    if running and Path(running.get("db", "")) != db and explicit_db:
        running = None  # the server on this port shows a different database; don't point at it

    names = [n for n, d in REGISTRY.items() if d.in_all] if args.name in (None, "all") else [args.name]
    serve_after = (magic or args.serve) and not port_busy
    live_in_background = serve_after and "live" in names and not args.fast
    if live_in_background:
        names.remove("live")

    for name in names:
        demo = REGISTRY[name]
        t = time.time()
        print(f"  ◆ {demo.title:<28}", end="", flush=True)
        run_id = demo.run(fast=args.fast)
        tg.flush()
        print(f" {_color(run_id, '2')}  {time.time() - t:.1f}s")

    if running:
        print(f"\n  ➜  open {url}\n")
    elif not serve_after:
        port_hint = f" --port {args.port + 1}" if port_busy else ""
        print(f"\n  db: {db}\n  view: traceglass serve --open{'' if db == default_db_path() else f' --db {db}'}{port_hint}\n")
    if serve_after:
        if live_in_background:
            def live() -> None:
                time.sleep(3.0)
                REGISTRY["live"].run()
                tg.flush()

            threading.Thread(target=live, daemon=True).start()
            print("  ◆ Live run starts in 3s — watch it build in the browser.")
        from .server import serve

        serve(port=args.port, db=str(db), open_browser=magic or args.open)


def cmd_ls(args: argparse.Namespace) -> None:
    runs = _store(args).list_runs(project=args.project, limit=args.limit)
    if not runs:
        print("No runs yet. Try: traceglass demo")
        return
    rows = [("ID", "STATUS", "PROJECT", "NAME", "SPANS", "TOKENS", "DURATION", "STARTED")]
    for r in runs:
        name = r.get("name") or ""
        rows.append((
            r["id"], r.get("status") or "?", (r.get("project") or "")[:16], name if len(name) <= 46 else name[:45] + "…",
            str(r["summary"]["spans"]), _tokens(r["summary"]["tokens"]), _dur(r.get("started_at"), r.get("ended_at")),
            _age(r.get("started_at")),
        ))
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for j, row in enumerate(rows):
        cells = [c.ljust(w) for c, w in zip(row, widths)]
        if j == 0:
            print(_color("  ".join(cells), "2"))
        else:
            cells[1] = _status(row[1]) + " " * (widths[1] - len(row[1]) - 2)
            print("  ".join(cells))


def cmd_show(args: argparse.Namespace) -> None:
    store = _store(args)
    bundle = store.get_bundle(_resolve_run(store, args.run))
    assert bundle is not None
    run = bundle["run"]
    s = run["summary"]
    cost = f" · ${s['cost_usd']:.4f}" if s.get("cost_usd") else ""
    print(f"\n{_color(run.get('name') or run['id'], '1')}  {_status(run.get('status'))}")
    print(_color(f"{run['id']} · {run.get('project')} · {_dur(run.get('started_at'), run.get('ended_at'))} · "
                 f"{s['spans']} spans · {_tokens(s['tokens'])} tokens{cost}", "2"))
    children: Dict[Optional[str], List[Dict[str, Any]]] = {}
    ids = {sp["id"] for sp in bundle["spans"]}
    for sp in bundle["spans"]:
        parent = sp.get("parent_id") if sp.get("parent_id") in ids else None
        children.setdefault(parent, []).append(sp)
    arts: Dict[Optional[str], List[Dict[str, Any]]] = {}
    for a in bundle["artifacts"]:
        arts.setdefault(a.get("span_id"), []).append(a)

    def walk(parent: Optional[str], prefix: str) -> None:
        items = sorted(children.get(parent, []), key=lambda x: (x.get("started_at") or 0, x.get("seq") or 0))
        for i, sp in enumerate(items):
            last = i == len(items) - 1
            glyph = _color(GLYPH.get(sp.get("status") or "", "?"), STATUS_COLOR.get(sp.get("status") or "", "0"))
            extra = []
            if sp.get("model"):
                extra.append(sp["model"])
            if sp.get("attempt"):
                extra.append(f"attempt {sp['attempt']}")
            if sp.get("usage", {}).get("total_tokens"):
                extra.append(f"{_tokens(sp['usage']['total_tokens'])} tok")
            if sp.get("error"):
                extra.append(_color(sp["error"].get("message", "")[:70], "31"))
            meta = _color(" · ".join(extra), "2")
            print(f"{prefix}{'└─' if last else '├─'} {glyph} {_color(KIND.get(sp.get('kind') or '', 'STEP').ljust(5), '35')} "
                  f"{sp.get('name')}  {_color(_dur(sp.get('started_at'), sp.get('ended_at')), '2')}  {meta}")
            child_prefix = prefix + ("   " if last else "│  ")
            for a in arts.get(sp["id"], []):
                star = " ★" if a.get("final") else ""
                print(f"{child_prefix}  {_color('◇ ' + (a.get('kind') or '') + ':', '34')} {a.get('name')}{star}")
            walk(sp["id"], child_prefix)

    walk(None, "")
    if bundle["links"]:
        rel: Dict[str, int] = {}
        for ln in bundle["links"]:
            rel[ln.get("relation", "?")] = rel.get(ln.get("relation", "?"), 0) + 1
        print(_color("\nevidence links: " + ", ".join(f"{k} ×{v}" for k, v in sorted(rel.items())), "2"))
    print()


def cmd_import(args: argparse.Namespace) -> None:
    store = _store(args)
    if args.source == "claude-code":
        from .integrations.claude_code import find_transcripts, import_transcripts

        if args.paths:
            paths = [Path(p).expanduser() for p in args.paths]
        else:
            project = args.project_dir or (None if args.all else os.getcwd())
            paths = find_transcripts(project_dir=project, latest=args.latest, limit=args.limit)
            if not paths and project and not args.project_dir:
                paths = find_transcripts(project_dir=None, latest=args.latest, limit=args.limit)
        if not paths:
            raise SystemExit("traceglass: no Claude Code transcripts found (looked under ~/.claude/projects)")
        ids = import_transcripts(paths, store)
        print(f"Imported {len(ids)} Claude Code session(s) from {len(paths)} transcript(s).")
        for i in ids[:20]:
            print(f"  {i}")
    elif args.source == "kiro":
        from .integrations.kiro import find_sessions, import_sessions

        if args.paths:
            paths = [Path(p).expanduser() for p in args.paths]
            paths = [p.with_suffix(".jsonl") if p.suffix == ".json" else p for p in paths]
        else:
            cwd = args.project_dir or (None if args.all else os.getcwd())
            paths = find_sessions(cwd=cwd, latest=args.latest, limit=args.limit)
            if not paths and cwd and not args.project_dir:
                paths = find_sessions(cwd=None, latest=args.latest, limit=args.limit)
        if not paths:
            raise SystemExit("traceglass: no Kiro sessions found (looked under ~/.kiro/sessions/cli)")
        ids = import_sessions(paths, store)
        print(f"Imported {len(ids)} Kiro session(s).")
        for i in ids[:20]:
            print(f"  {i}")
    elif args.source == "otlp":
        from .otlp import otlp_to_records

        total = 0
        for p in args.paths:
            total += store.write(otlp_to_records(json.loads(Path(p).read_text(encoding="utf-8"))))
        print(f"Imported {total} records from {len(args.paths)} OTLP file(s).")
    elif args.source == "jsonl":
        total = 0
        for p in args.paths:
            with open(p, encoding="utf-8") as fh:
                total += store.write(json.loads(line) for line in fh if line.strip())
        print(f"Imported {total} records.")


def cmd_export(args: argparse.Namespace) -> None:
    store = _store(args)
    if args.site:
        from .export import export_site

        ids = [_resolve_run(store, r) for r in args.runs] if args.runs else None
        n = export_site(store, Path(args.site), ids)
        print(f"Exported {n} run(s) to {args.site}/ — serve it with any static host (e.g. GitHub Pages).")
        return
    if len(args.runs) != 1:
        raise SystemExit("traceglass: export takes exactly one RUN_ID (or --site DIR [RUN_ID ...])")
    rid = _resolve_run(store, args.runs[0])
    bundle = store.get_bundle(rid)
    assert bundle is not None
    if args.json:
        text = json.dumps(bundle, ensure_ascii=False, indent=2)
        if args.output in (None, "-"):
            print(text)
            return
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        from .export import export_html

        out = Path(args.output or f"traceglass-{rid}.html")
        out.write_text(export_html([bundle], title=f"{bundle['run'].get('name')} · Traceglass"), encoding="utf-8")
        args.output = str(out)
    print(f"Wrote {args.output}")


def cmd_rm(args: argparse.Namespace) -> None:
    store = _store(args)
    for r in args.runs:
        rid = _resolve_run(store, r)
        store.delete_run(rid)
        print(f"Deleted {rid}")


# -- parser -----------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="traceglass", description="A visual, navigable record of how an AI agent arrived at an answer.")
    p.add_argument("--version", action="version", version=f"traceglass {__version__}")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    def with_db(sp: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sp.add_argument("--db", help="SQLite database (default: $TRACEGLASS_DB or ~/.traceglass/traceglass.db)")
        return sp

    s = with_db(sub.add_parser("serve", help="start the local UI and API"))
    s.add_argument("--host", default="127.0.0.1", help="loopback address to bind (default 127.0.0.1)")
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.add_argument("--open", action="store_true", help="open the browser")
    s.add_argument("--watch", action="store_true", help="also watch Claude Code and Kiro sessions (see `traceglass watch`)")
    s.add_argument("--since", default=None, help="with --watch: backfill window, e.g. 3d, 12h, all (default 3d)")
    s.add_argument("--no-claude-code", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--no-kiro", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--project-dir", help=argparse.SUPPRESS)
    s.add_argument("--quiet", action="store_true", help="with --watch: don't log each synced session")
    s.set_defaults(fn=cmd_serve)

    s = with_db(sub.add_parser("watch", help="sync Claude Code and Kiro sessions into Traceglass as they happen"))
    s.add_argument("--since", default=None, help="backfill window: 3d (default), 12h, 30m, 2w or all")
    s.add_argument("--no-claude-code", action="store_true", help="don't watch Claude Code (~/.claude/projects)")
    s.add_argument("--no-kiro", action="store_true", help="don't watch Kiro CLI (~/.kiro/sessions/cli)")
    s.add_argument("--project-dir", help="only sessions whose working directory is DIR")
    s.add_argument("--interval", type=float, default=1.0, help="seconds between polls (default 1)")
    s.add_argument("--once", action="store_true", help="sync once and exit")
    s.add_argument("--quiet", action="store_true", help="don't log each synced session")
    s.set_defaults(fn=cmd_watch)

    s = sub.add_parser("hook", help="(called by agents) record a hook event read from stdin")
    s.add_argument("source", choices=["claude-code", "kiro"])
    s.add_argument("--event", help="event name, when the agent does not include it in the payload")
    s.set_defaults(fn=cmd_hook)

    s = sub.add_parser("hooks", help="install / uninstall / inspect agent hooks (exact live timing)")
    s.add_argument("action", choices=["install", "uninstall", "status"])
    s.add_argument("source", nargs="?", choices=["claude-code", "kiro"])
    s.add_argument("--scope", choices=["user", "project", "local"], default="user", help="claude-code settings file (default user)")
    s.add_argument("--agent", action="append", help="kiro agent name (repeatable; default: default)")
    s.add_argument("--all", action="store_true", help="kiro: every agent in ~/.kiro/agents")
    s.add_argument("--force", action="store_true", help="kiro: also edit agent files generated by other tools")
    s.add_argument("--dry-run", action="store_true", help="show the diff without writing")
    s.set_defaults(fn=cmd_hooks)

    s = with_db(sub.add_parser("demo", help="record showcase runs (no args: seed all, serve, open browser)"))
    s.add_argument("name", nargs="?", help="research | coding | incident | stress | live | claude | all")
    s.add_argument("--fast", action="store_true", help="no simulated latency (tests)")
    s.add_argument("--serve", action="store_true", help="start the server afterwards")
    s.add_argument("--open", action="store_true", help="open the browser (with --serve)")
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.set_defaults(fn=cmd_demo)

    s = with_db(sub.add_parser("ls", help="list runs"))
    s.add_argument("--project")
    s.add_argument("--limit", type=int, default=30)
    s.set_defaults(fn=cmd_ls)

    s = with_db(sub.add_parser("show", help="print a run as a tree"))
    s.add_argument("run", help="run id (or unique prefix/suffix)")
    s.set_defaults(fn=cmd_show)

    s = with_db(sub.add_parser("import", help="import traces: claude-code | kiro | otlp | jsonl"))
    s.add_argument("source", choices=["claude-code", "kiro", "otlp", "jsonl"])
    s.add_argument("paths", nargs="*", help="files (claude-code/kiro: session files; default: this directory's sessions)")
    s.add_argument("--project-dir", help="claude-code/kiro: working directory whose sessions to import")
    s.add_argument("--latest", action="store_true", help="claude-code/kiro: only the most recent session")
    s.add_argument("--all", action="store_true", help="claude-code/kiro: sessions from every project")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(fn=cmd_import)

    s = with_db(sub.add_parser("export", help="export a run as a single HTML file, JSON, or a static site"))
    s.add_argument("runs", nargs="*", metavar="RUN_ID")
    s.add_argument("-o", "--output")
    s.add_argument("--json", action="store_true", help="write the run bundle as JSON")
    s.add_argument("--site", metavar="DIR", help="write a static site (all runs, or the given ones)")
    s.set_defaults(fn=cmd_export)

    s = with_db(sub.add_parser("rm", help="delete runs"))
    s.add_argument("runs", nargs="+", metavar="RUN_ID")
    s.set_defaults(fn=cmd_rm)
    return p


def main(argv: Optional[List[str]] = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["hook"]:  # hot path for agent hooks: skip argparse and heavy imports
        from .hooks import main_hook

        main_hook(argv[1:])
        return
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "fn", None):
        parser.print_help()
        return
    try:
        args.fn(args)
    except RuntimeError as exc:
        if "UI bundle not built" not in str(exc):
            raise
        raise SystemExit(f"traceglass: {exc}")
    except KeyboardInterrupt:
        pass
    except BrokenPipeError:
        pass


if __name__ == "__main__":
    main()
