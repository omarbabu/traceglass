"""Live demo: a travel-planning agent that runs in real time (~25s).

Open the UI first, then run ``traceglass demo live`` and watch nodes appear, run, retry
and settle. Nothing is time-compressed here — what you see is wall-clock.
"""

from __future__ import annotations

import traceglass as tg

from ._common import HAIKU, SONNET, ScriptedModel, cost, demo_run, pace

REQUEST = "Plan 3 days in Lisbon, Nov 14–17, for two. Mid-range budget, we love food and walking, no car."

GUIDE = """Lisbon in November: mild (14–19°C) with occasional rain; pack a light waterproof.
Alfama and Mouraria are best explored on foot early in the morning before tour groups arrive.
Time Out Market is busy at lunch; Mercado de Campo de Ourique is a calmer local alternative.
Tram 28 is crowded — walking the same route between Graça and Estrela takes about 75 minutes.
Belém is a 20-minute train ride from Cais do Sodré; go on a weekday to avoid queues at the monastery."""

ITINERARY = """# Lisbon · Nov 14–17 · 2 travellers

**Flights:** TP 1351, Thu 08:05 → 10:40 (€214 pp) · **Hotel:** Casa do Largo, Graça — €148/night, held until Fri 18:00.

## Day 1 — Alfama & Mouraria
Walk Alfama and Mouraria early in the morning before tour groups arrive. Late lunch at Mercado de Campo de Ourique, a calmer local alternative to Time Out Market.

## Day 2 — Graça to Estrela on foot
Skip the crowded Tram 28 and walk the route instead — about 75 minutes with miradouro stops. Dinner in Campo de Ourique.

## Day 3 — Belém
Weekday train from Cais do Sodré (20 minutes) to beat the monastery queues; pastéis on the way back.

**Weather:** mild with some rain — pack a light waterproof.
"""

planner = ScriptedModel(SONNET, seed=41)
helper = ScriptedModel(HAIKU, seed=42)


@tg.tool
def search_flights(origin: str, destination: str, depart: str, ret: str, travellers: int) -> list:
    pace(4.0)
    return [
        {"flight": "TP 1351", "depart": "08:05", "arrive": "10:40", "price_eur": 214, "stops": 0},
        {"flight": "FR 7712", "depart": "13:30", "arrive": "16:15", "price_eur": 96, "stops": 0, "bags": "extra"},
    ]


@tg.tool
def search_hotels(city: str, nights: int, budget: str, near: str) -> list:
    pace(5.0)
    return [
        {"hotel": "Casa do Largo", "area": "Graça", "price_eur": 148, "rating": 9.1},
        {"hotel": "Lisboa Prata", "area": "Baixa", "price_eur": 172, "rating": 8.8},
    ]


@tg.tool
def get_weather(city: str, dates: str) -> dict:
    pace(2.0)
    return {"high_c": 19, "low_c": 14, "rain_days": 1, "summary": "mild, one rainy afternoon likely"}


@tg.retrieval("search_local_guides")
def search_local_guides(city: str, interests: list) -> str:
    pace(3.0)
    DOCS["guide"] = tg.artifact("Local guide notes: Lisbon in November", GUIDE, kind="document", uri="guides://lisbon/november")
    return DOCS["guide"].id


DOCS: dict = {}


class Conflict(Exception):
    pass


def hold_room(hotel: str) -> dict:
    previous = None
    for attempt in (1, 2):
        with tg.span("hold_room", kind="tool", input={"hotel": hotel, "nights": 3}, attempt=attempt, retry_of=previous) as s:
            pace(2.0)
            if attempt == 1:
                s.fail(Conflict("409 Conflict: rate changed since search (€142 → €148)"))
                previous = s
                continue
            s.set_output({"hold_id": "H-77Q2", "price_eur": 148, "expires": "Fri 18:00"})
            return {"hold_id": "H-77Q2"}
    raise Conflict("could not hold room")


def run(fast: bool = False) -> str:
    with demo_run("Live: plan a weekend in Lisbon", project="travel-agent", session="lisbon-nov",
                  input={"request": REQUEST}, tags=["live"], time_scale=1, fast=fast) as r:
        with tg.agent("trip-planner", model=SONNET):
            helper.chat("parse request", system="Extract structured trip constraints.",
                        messages=[{"role": "user", "content": REQUEST}],
                        reply='{"city": "Lisbon", "dates": "Nov 14-17", "travellers": 2, "budget": "mid", "interests": ["food", "walking"], "car": false}',
                        latency=2.0)
            with tg.ThreadPoolExecutor(max_workers=4) as pool:
                jobs = [
                    pool.submit(search_flights, "LHR", "LIS", "2026-11-14", "2026-11-17", 2),
                    pool.submit(search_hotels, "Lisbon", 3, "mid", "walkable, central"),
                    pool.submit(get_weather, "Lisbon", "Nov 14-17"),
                    pool.submit(search_local_guides, "Lisbon", ["food", "walking"]),
                ]
                flights, hotels, weather, _ = [j.result() for j in jobs]
            guide = DOCS["guide"]
            planner.chat("compare options", system="Pick the best flight and hotel for the constraints. Explain trade-offs briefly.",
                         messages=[{"role": "user", "content": str({"flights": flights, "hotels": hotels, "weather": weather})}],
                         reply="TP 1351 (morning arrival, bags included) and Casa do Largo in Graça (walkable, best rating within budget).",
                         latency=4.0)
            hold_room("Casa do Largo")
            with tg.llm("write itinerary", model=SONNET) as w:
                w.set_input({"messages": [{"role": "user", "content": "Write a 3-day itinerary using the guide notes."}]})
                w.uses(guide)
                pace(5.0)
                w.set_output({"role": "assistant", "content": ITINERARY})
                w.usage(input_tokens=2630, output_tokens=410, cost_usd=cost(SONNET, 2630, 410))
                plan = tg.artifact("Lisbon itinerary", ITINERARY, kind="markdown", final=True)
                plan.cite(guide, anchor="Walk Alfama and Mouraria early in the morning before tour groups arrive.",
                          quote="Alfama and Mouraria are best explored on foot early in the morning before tour groups arrive.")
                plan.cite(guide, anchor="Skip the crowded Tram 28 and walk the route instead",
                          quote="Tram 28 is crowded — walking the same route between Graça and Estrela takes about 75 minutes.")
                plan.cite(guide, anchor="pack a light waterproof", quote="pack a light waterproof")
        r.set_output({"itinerary": plan.id})
        return r.id
