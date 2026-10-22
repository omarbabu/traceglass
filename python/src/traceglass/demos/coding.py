"""Coding agent: reproduce a flaky test, patch it, prove the fix, and get human approval.

What it shows in Traceglass:
  * file reads recorded as ``code`` artifacts, test runs as tool spans with exit codes
  * a unified ``diff`` artifact, linked ``tested_by`` the test run that verified it
  * an ``approval`` gate that waits on a maintainer, and an artifact review decision
  * a pull-request description whose sentences are cited back to the diff and test output
"""

from __future__ import annotations

import difflib
from typing import Dict

import traceglass as tg

from ._common import SONNET, ScriptedModel, cost, demo_run, pace

REPO = "acme/billing"

FILES: Dict[str, str] = {
    "billing/dates.py": '''"""Invoice date helpers."""
from datetime import date, datetime


def parse_invoice_date(value: str) -> date:
    """Parse an ISO-8601 timestamp from the payments API into a billing date."""
    parsed = datetime.fromisoformat(value)
    return parsed.date()


def is_overdue(due: str, today: date | None = None) -> bool:
    today = today or date.today()
    return parse_invoice_date(due) < today
''',
    "tests/test_dates.py": '''from datetime import date

from billing.dates import is_overdue, parse_invoice_date


def test_parses_utc_timestamp():
    assert parse_invoice_date("2026-03-01T10:00:00+00:00") == date(2026, 3, 1)


def test_naive_timestamp_is_treated_as_utc():
    assert parse_invoice_date("2026-03-01T10:00:00") == date(2026, 3, 1)


def test_offset_timestamp_uses_utc_billing_day():
    assert parse_invoice_date("2026-03-01T23:30:00-08:00") == date(2026, 3, 2)


def test_is_overdue_compares_billing_days():
    assert is_overdue("2026-03-01T10:00:00+00:00", today=date(2026, 3, 2))
''',
}

FIXED_DATES = '''"""Invoice date helpers."""
from datetime import date, datetime, timezone


def parse_invoice_date(value: str) -> date:
    """Parse an ISO-8601 timestamp from the payments API into a UTC billing date."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).date()


def is_overdue(due: str, today: date | None = None) -> bool:
    today = today or datetime.now(timezone.utc).date()
    return parse_invoice_date(due) < today
'''

PYTEST_FAIL = """============================= test session starts ==============================
platform linux -- Python 3.12.4, pytest-8.3.3, pluggy-1.5.0
rootdir: /workspace/billing
collected 4 items

tests/test_dates.py ..F.                                                 [100%]

=================================== FAILURES ===================================
_________________ test_offset_timestamp_uses_utc_billing_day __________________

    def test_offset_timestamp_uses_utc_billing_day():
>       assert parse_invoice_date("2026-03-01T23:30:00-08:00") == date(2026, 3, 2)
E       AssertionError: assert datetime.date(2026, 3, 1) == datetime.date(2026, 3, 2)
E        +  where datetime.date(2026, 3, 1) = parse_invoice_date('2026-03-01T23:30:00-08:00')

tests/test_dates.py:14: AssertionError
=========================== short test summary info ============================
FAILED tests/test_dates.py::test_offset_timestamp_uses_utc_billing_day - AssertionError
========================= 1 failed, 3 passed in 0.09s ==========================
"""

PYTEST_PASS = """============================= test session starts ==============================
platform linux -- Python 3.12.4, pytest-8.3.3, pluggy-1.5.0
rootdir: /workspace/billing
collected 4 items

tests/test_dates.py ....                                                 [100%]

============================== 4 passed in 0.08s ===============================
"""

DIAGNOSIS = (
    "Root cause: `parse_invoice_date` calls `.date()` on an offset-aware datetime, which returns the "
    "*local* calendar day of the offset (Mar 1 in -08:00) rather than the UTC billing day (Mar 2). "
    "`is_overdue` compounds this by defaulting to `date.today()`, which depends on the host timezone — "
    "that is why the test only fails on CI runners in UTC-8 after 16:00. Fix: normalise to UTC before "
    "taking the date, treat naive timestamps as UTC, and use a UTC 'today'."
)

model = ScriptedModel(SONNET, seed=21)


@tg.tool
def read_file(path: str) -> str:
    pace(0.3)
    content = FILES[path]
    tg.artifact(path, content, kind="code", mime="text/x-python", uri=f"repo://{REPO}/{path}", metadata={"lines": content.count("\n")})
    return content


@tg.tool
def search_code(pattern: str) -> list:
    pace(0.6)
    return [
        {"path": "billing/dates.py", "line": 5, "text": "def parse_invoice_date(value: str) -> date:"},
        {"path": "billing/invoices.py", "line": 41, "text": "due = parse_invoice_date(payload['due_at'])"},
        {"path": "tests/test_dates.py", "line": 3, "text": "from billing.dates import is_overdue, parse_invoice_date"},
    ]


def run_tests(command: str, output: str, label: str) -> tg.Span:
    """Run the test suite. A failing suite is a *successful* tool call with exit_code=1."""
    with tg.span("run_tests", kind="tool", input={"command": command, "cwd": "/workspace/billing"}) as s:
        pace(6.5)
        exit_code = 0 if "failed" not in output else 1
        s.set_attributes(exit_code=exit_code, passed=4 if exit_code == 0 else 3, failed=exit_code)
        s.set_output({"exit_code": exit_code, "stdout": output})
        tg.artifact(f"pytest output ({label})", output, kind="text", mime="text/plain")
        return s


@tg.tool
def apply_patch(path: str, new_content: str) -> dict:
    pace(0.5)
    old = FILES[path]
    diff = "".join(difflib.unified_diff(old.splitlines(True), new_content.splitlines(True), f"a/{path}", f"b/{path}"))
    FILES[path] = new_content
    PATCH["artifact"] = tg.artifact(f"patch: {path}", diff, kind="diff", mime="text/x-diff",
                                    metadata={"files_changed": 1, "insertions": diff.count("\n+") - 1, "deletions": diff.count("\n-") - 1})
    return {"path": path, "applied": True, "hunks": diff.count("\n@@ -")}


PATCH: Dict[str, tg.Artifact] = {}

PR_BODY = """## Fix timezone-dependent invoice dates

`test_offset_timestamp_uses_utc_billing_day` failed intermittently on CI.

**Cause.** Offset-aware timestamps were converted to a date in their *local* offset, so an invoice due at 23:30 in UTC-8 landed on the wrong billing day.

**Change.** Timestamps are now normalised to UTC before taking the date, naive timestamps are treated as UTC, and `is_overdue` uses a UTC "today" instead of the host clock.

**Verification.** The full date test module passes: 4 passed, 0 failed.

Risk: low — one module, no schema or API changes.
"""


def run(fast: bool = False) -> str:
    original = dict(FILES)  # the scripted repo is restored afterwards so the demo is re-runnable
    PATCH.clear()
    task = "CI is flaky: tests/test_dates.py::test_offset_timestamp_uses_utc_billing_day fails on some runners. Fix it."
    try:
        with demo_run(
            "Coding agent: fix flaky date parsing test",
            project="coding-agent",
            session=f"repo:{REPO}",
            task="BILL-2291",
            input={"task": task, "repo": REPO, "branch": "main@4f2c9e1"},
            tags=["coding", "tests", "approval"],
            time_scale=15,
            fast=fast,
            expected_seconds=51,
        ) as r:
            with tg.agent("coder", model=SONNET) as coder:
                model.chat(
                    "understand task",
                    system="You are a senior engineer working in a sandboxed checkout. Reproduce before you fix.",
                    messages=[{"role": "user", "content": task}],
                    reply="I'll read the failing test and the module under test, reproduce the failure, then patch it.",
                    latency=2.5,
                )
                matches = search_code("parse_invoice_date")
                source = read_file("billing/dates.py")
                tests = read_file("tests/test_dates.py")
                before = run_tests("pytest tests/test_dates.py -q", PYTEST_FAIL, "before fix")

                model.chat(
                    "diagnose failure",
                    system="Explain the root cause precisely, then propose the smallest correct fix.",
                    messages=[{"role": "user", "content": f"{source}\n\n{tests}\n\n{PYTEST_FAIL}\n\ncall sites: {matches}"}],
                    reply=DIAGNOSIS,
                    latency=7.0,
                )
                apply_patch("billing/dates.py", FIXED_DATES)
                diff = PATCH["artifact"]
                after = run_tests("pytest tests/test_dates.py -q", PYTEST_PASS, "after fix")
                after.depends_on(before)
                tg.link(diff, after, relation="tested_by", note="4/4 tests pass after the patch")
                tg.link(diff, before, relation="derived_from", note="fixes the failure reproduced here")

                with tg.span("maintainer review", kind="approval", input={"artifact": diff.id, "reviewers": ["@billing-maintainers"]}) as gate:
                    gate.set_status("waiting")
                    pace(22.0)  # a human reads the diff
                    gate.set_attributes(reviewer="j.alvarez", decision="approved", comment="LGTM — nice catch on the naive case.")
                    diff.review("approved", by="j.alvarez", note="LGTM — nice catch on the naive case.")
                    gate.set_output({"decision": "approved"})

                with tg.llm("write PR description", model=SONNET) as pr_span:
                    pr_span.set_input({"messages": [{"role": "user", "content": "Write a PR description for the change."}]})
                    pace(4.0)
                    pr_span.set_output({"role": "assistant", "content": PR_BODY})
                    pr_span.usage(input_tokens=2210, output_tokens=286, cost_usd=cost(SONNET, 2210, 286))
                    pr = tg.artifact("Pull request #482: Fix timezone-dependent invoice dates", PR_BODY, kind="markdown", final=True,
                                     metadata={"pr": 482, "branch": "fix/utc-billing-dates"})
                    pr.cite(diff, anchor="Timestamps are now normalised to UTC before taking the date",
                            quote="+    return parsed.astimezone(timezone.utc).date()")
                    pr.cite(diff, anchor="naive timestamps are treated as UTC",
                            quote="+        parsed = parsed.replace(tzinfo=timezone.utc)")
                    pr.cite(diff, anchor="`is_overdue` uses a UTC \"today\" instead of the host clock",
                            quote="+    today = today or datetime.now(timezone.utc).date()")
                    pr.cite(after, anchor="4 passed, 0 failed", relation="tested_by")
                    pr.cite(before, anchor="`test_offset_timestamp_uses_utc_billing_day` failed intermittently on CI.",
                            relation="derived_from")
                coder.set_output({"pr": 482, "status": "ready to merge"})
            r.set_output({"pull_request": 482, "tests": "4 passed"})
            return r.id
    finally:
        FILES.clear()
        FILES.update(original)
