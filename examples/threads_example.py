"""Parallel work keeps its parent/child structure.

Spans are tracked with contextvars, so asyncio tasks inherit the current span
automatically. For threads, use ``tg.ThreadPoolExecutor`` (or wrap a target with
``tg.bind``) so each worker's spans nest under the span that submitted it.
"""

import threading
import time

import traceglass as tg


@tg.tool
def fetch(url: str) -> dict:
    time.sleep(0.2)
    return {"url": url, "status": 200}


with tg.run("Parallel fetch", project="examples"):
    with tg.agent("crawler"):
        # 1) A context-propagating pool: three fetches run concurrently under "crawler".
        with tg.ThreadPoolExecutor(max_workers=3) as pool:
            pages = list(pool.map(fetch, ["https://a.example", "https://b.example", "https://c.example"]))

        # 2) A raw thread: tg.bind() carries the current run/span into it.
        t = threading.Thread(target=tg.bind(lambda: fetch("https://d.example")))
        t.start()
        t.join()

        # 3) Declare a data dependency explicitly when it is not implied by nesting.
        with tg.step("merge") as merge:
            merge.set_output({"pages": len(pages) + 1})

tg.flush()
print("Recorded. View with: traceglass serve --open")
