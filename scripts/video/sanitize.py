"""Strip machine-specific paths and usernames from exported run bundles before publishing.

    python sanitize.py WORK_DIR in.bundle.json:RUN_DIR_NAME out.bundle.json --project shop-api

Only paths and usernames are rewritten; span/artifact/link/event structure is preserved and
verified. Prints any remaining suspicious strings so you can review before sharing.
"""

import argparse
import json
import re


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("work")
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--project", default="shop-api")
    a = ap.parse_args()
    path, _, run_dir = a.src.partition(":")
    raw = open(path).read()
    work = a.work.rstrip("/")
    variants = {work, "/private" + work if work.startswith("/tmp") else work}
    s = raw
    for w in sorted(variants, key=len, reverse=True):
        s = s.replace(f"{w}/{run_dir}", f"~/code/{a.project}")
        s = s.replace(w.replace("/", "-") + "-" + run_dir, f"-code-{a.project}")
        s = s.replace(w, "~/code")
    s = re.sub(r"/Users/[A-Za-z0-9_.-]+", "~", s)
    s = re.sub(r"/home/[A-Za-z0-9_.-]+", "~", s)
    b = json.loads(s)
    b["run"]["project"] = a.project
    orig = json.loads(raw)
    for k in ("spans", "artifacts", "links", "events"):
        assert len(orig[k]) == len(b[k]), f"structure changed: {k}"
    json.dump(b, open(a.out, "w"), ensure_ascii=False)
    txt = json.dumps(b)
    left = {p: len(re.findall(p, txt)) for p in (r"/Users/", r"/home/", r"/private/", re.escape(work))}
    print(f"sanitized {a.out}; remaining: {({k: v for k, v in left.items() if v}) or 'none'}")


if __name__ == "__main__":
    main()
