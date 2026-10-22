"""Assemble the self-contained HTML the demo tour plays from.

    python scripts/video/build_tour.py -o /tmp/tgvideo/tour.html \
        opus.bundle.clean.json:opus.json sonnet.bundle.clean.json:sonnet.json research.bundle.json

Each argument is a run bundle (``traceglass export <id> --json``). ``bundle.json:result.json``
attaches the cost Claude Code reported for that run (its ``--output-format json`` result) as
``run.metadata.cost_usd`` — transcripts don't record cost, the CLI's result does.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python" / "src"))
from traceglass.export import export_html  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("bundles", nargs="+")
    ap.add_argument("-o", "--out", required=True)
    args = ap.parse_args()
    bundles = []
    for spec in args.bundles:
        path, _, result = spec.partition(":")
        b = json.loads(Path(path).read_text())
        if result:
            r = json.loads(Path(result).read_text())
            if r.get("total_cost_usd") is not None:
                b["run"].setdefault("metadata", {})["cost_usd"] = round(r["total_cost_usd"], 4)
                b["run"]["metadata"]["cost_source"] = "Claude Code"
        bundles.append(b)
    Path(args.out).write_text(export_html(bundles, title="Traceglass"))
    print(f"wrote {args.out} ({len(bundles)} runs)")


if __name__ == "__main__":
    main()
