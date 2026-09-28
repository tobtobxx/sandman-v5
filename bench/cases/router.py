"""Router call types: route_sticky, route_shortlist, topic_title."""
from bench.checks import contains, eq, one_of, custom

GARDEN = ["owner: can you figure out whether drip irrigation makes sense for my raised beds",
          "assistant: On it — I'll compare drip kits against manual watering for your 3 beds."]
BIKE = ["owner: my rear brake squeaks and the gears skip", "assistant: I'll look for a bike shop near you."]
TAX = ["owner: which home office costs can I deduct in my 2026 tax return?",
       "assistant: I'm checking the Zurich rules for home office deductions."]
TRIP = ["owner: compare Lisbon vs Porto for a week in October",
        "assistant: Working on it: weather, cost and things to do."]
JOB = ["owner: draft a cover letter for the Siemens data engineer position",
       "assistant: Here is a first draft: ..."]


def sticky(i, title, last, text, expect):
    exp = expect if isinstance(expect, list) else [expect]
    return {"id": f"sticky_{i}", "call": "route_sticky", "group": "router",
            "inputs": {"topic_title": title, "last_lines": last, "text": text}, "checks": [one_of("same", exp)]}


CASES = [
    sticky(1, "Garden irrigation", GARDEN, "how much water does a drip system use per week?", "yes"),
    sticky(2, "Garden irrigation", GARDEN, "also remind me to file the tax extension friday", "no"),
    sticky(3, "Garden irrigation", GARDEN, "ok thanks!", "yes"),
    sticky(4, "Bike service", BIKE, "can they also check the chain?", "yes"),
    sticky(5, "Bike service", BIKE, "what's the weather tomorrow in Zurich?", "no"),
    sticky(6, "Tax return 2026", TAX, "do I need to keep the receipts for the desk?", "yes"),
    sticky(7, "Tax return 2026", TAX, "book a table for two at an Italian place on Friday", "no"),
    sticky(8, "Vacation Portugal", TRIP, "what about the Algarve instead?", "yes"),
    sticky(9, "Vacation Portugal", TRIP, "my laptop won't boot after the update", "no"),
    sticky(10, "Garden irrigation", GARDEN, "and do the tomatoes need more water than the herbs?", ["yes", "unsure"]),
    sticky(11, "Job applications", JOB, "make it a bit more formal", "yes"),
    sticky(12, "Job applications", JOB, "yes", "yes"),
]

TOPICS = [
    {"slug": "taxes", "title": "Tax return 2026", "last": "assistant: The deadline is March 31."},
    {"slug": "bike", "title": "Bike service", "last": "assistant: Velo Kurier Oerlikon can take it on Thursday."},
    {"slug": "garden", "title": "Garden irrigation", "last": "assistant: I recommend the Gardena Micro-Drip kit."},
    {"slug": "trip", "title": "Vacation Portugal", "last": "owner: let's do the second week of October"},
    {"slug": "laptop", "title": "Laptop problems", "last": "assistant: Try updating the BIOS first."},
]


def short(i, text, expect, checks=None):
    exp = expect if isinstance(expect, list) else [expect]
    return {"id": f"shortlist_{i}", "call": "route_shortlist", "group": "router",
            "inputs": {"topics": TOPICS, "text": text}, "checks": checks or [one_of("choice", exp)]}


CASES += [
    short(1, "did you find out if I can deduct the new desk?", "taxes"),
    short(2, "the shop says the brake pads need replacing, is 80 CHF fair?", "bike"),
    short(3, "which drip kit was the cheapest again?", "garden"),
    short(4, "are there direct trains from Lisbon to Porto?", "trip"),
    short(5, "the fan is super loud now and it gets really hot", "laptop"),
    short(6, "can you find me a good recipe for vegan lasagna?", "new"),
    short(7, "please look into health insurance options for 2027", "new"),
    short(8, "when is the deadline again?", None,
          [custom("choice taxes, or confidence low", lambda o, c: o["choice"] == "taxes" or o["confidence"] == "low")]),
    short(9, "book the flights for the dates we discussed", "trip"),
    short(10, "also the chain is rusty", "bike"),
    short(11, "set up a weekly reminder to water the beds", "garden"),
    short(12, "my sister's wedding is in May, I need a gift idea", "new"),
]


def title(i, text, *kw):
    return {"id": f"title_{i}", "call": "topic_title", "group": "router", "inputs": {"text": text},
            "checks": [contains("title", tuple(kw))]}


CASES += [
    title(1, "can you figure out whether drip irrigation makes sense for my raised beds", "irrigation", "drip",
          "watering"),
    title(2, "I need to prepare my tax return for 2026, what documents do I need?", "tax"),
    title(3, "please find a good dentist near Zurich Oerlikon who takes new patients", "dentist", "dental"),
    title(4, "hey, my laptop keeps crashing when I plug in the second monitor", "laptop", "monitor", "display",
          "crash"),
    title(5, "let's plan a birthday party for Anna, around 20 people, in November", "birthday", "party"),
]
