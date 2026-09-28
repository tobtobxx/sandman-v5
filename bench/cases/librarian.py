"""Librarian: extract_entities, librarian decision, render_answer."""
from bench.checks import contains, custom, eq, judge, lacks, one_of


def ents(i, title, goal, expect):
    def fn(o, c):
        got = [e.lower() for e in o["entities"]]
        return all(any(any(x in g for g in got) for x in (alts if isinstance(alts, tuple) else (alts,)))
                   for alts in expect)
    return {"id": f"entities_{i}", "call": "extract_entities", "group": "librarian",
            "inputs": {"title": title, "goal": goal},
            "checks": [custom(f"entities cover {expect}", fn),
                       custom("no generic words", lambda o, c: not any(e.lower() in ("options", "price", "prices",
                                                                                     "comparison", "task")
                                                                       for e in o["entities"]))]}


CASES = [
    ents(1, "Evaluate drip irrigation for raised beds",
         "Compare drip irrigation kits (e.g. Gardena Micro-Drip) against manual watering for the owner's raised beds",
         ["drip", "gardena", "raised bed"]),
    ents(2, "Compare Lisbon and Porto", "Compare Lisbon and Porto for a one-week trip in October: weather, cost, "
                                        "things to do", ["lisbon", "porto"]),
    ents(3, "Health insurance 2027", "Check whether Swica or Helsana is cheaper for basic health insurance in "
                                     "Zurich in 2027", ["swica", "helsana"]),
    ents(4, "Fix failing test", "Fix the failing pytest test in budget.py of the owner's budget script",
         [("pytest", "test"), "budget"]),
    ents(5, "EU grants for solar co-ops", "Find currently open EU or Swiss funding programs for small solar co-ops",
         [("eu", "european union"), "solar"]),
    ents(6, "revDSG summary", "Summarize the revised Swiss data protection act (revDSG) for small businesses",
         [("revdsg", "data protection")]),
]

GARDENA = {"id": "not_gardena", "title": "Gardena Micro-Drip starter set",
           "one_liner": "Drip kit for beds and planters: price, coverage, contents",
           "claims": [{"text": "Costs CHF 89 at Jumbo", "observed_at": "2026-09-10", "stale": True},
                      {"text": "One set covers about 15 m²", "observed_at": "2026-09-10"},
                      {"text": "Includes 20 drippers and a pressure reducer", "observed_at": "2026-09-10"}]}
BEDS = {"id": "not_beds", "title": "Owner's raised beds", "one_liner": "Size and location of the owner's beds",
        "claims": [{"text": "The owner has 3 raised beds, 40 m² in total, south-facing", "observed_at": "2026-09-01"}]}
LISBON = {"id": "not_lisbon", "title": "Lisbon", "one_liner": "Travel facts about Lisbon",
          "claims": [{"text": "October weather in Lisbon: 22 °C average high, about 8 rainy days",
                      "observed_at": "2026-09-15"},
                     {"text": "A week in a mid-range Lisbon hotel costs about EUR 900", "observed_at": "2026-09-15"}]}
PORTO = {"id": "not_porto", "title": "Porto", "one_liner": "Travel facts about Porto",
         "claims": [{"text": "October weather in Porto: 20 °C average high, about 11 rainy days",
                     "observed_at": "2026-09-15"}]}
HELSANA = {"id": "not_helsana", "title": "Helsana", "one_liner": "Swiss health insurer, customer contact",
           "claims": [{"text": "Helsana customer service phone: 0844 80 81 82", "observed_at": "2026-05-02"}]}
NEG_SOLAR = {"id": "not_neg_solar", "title": "Swiss grants for small solar co-ops (search result)",
             "one_liner": "Negative result: no open programs found",
             "claims": [{"text": "Searched on 2026-09-20 for Swiss grant programs for solar co-ops with <50 members: "
                                 "none open, the next Pronovo call opens in March 2027", "observed_at": "2026-09-20"}]}


def lib(i, card, notes, checks):
    return {"id": f"librarian_{i}", "call": "librarian", "group": "librarian",
            "inputs": {"card": card, "notes": notes}, "checks": checks}


def card(title, goal, done_when):
    return {"title": title, "goal": goal, "done_when": done_when}


CASES += [
    lib(1, card("Gardena kit coverage", "How many m² does one Gardena Micro-Drip starter set cover?",
                ["states the area"]), [GARDENA, BEDS],
        [eq("decision", "answered"), contains("answer_note_ids", "not_gardena")]),
    lib(2, card("Gardena kit price", "What does the Gardena Micro-Drip starter set cost now, and is it on sale?",
                ["states the current price"]), [GARDENA],
        [eq("decision", "narrow"), contains("narrowed_goal", ("price", "cost", "sale"))]),
    lib(3, card("Find a dentist", "Find a dentist in Zurich Oerlikon who takes new patients",
                ["lists at least 2 dentists"]), [GARDENA, BEDS], [eq("decision", "proceed")]),
    lib(4, card("Lisbon vs Porto", "Compare Lisbon and Porto for a week in October: weather and hotel cost",
                ["covers weather and hotel cost for both cities"]), [LISBON, PORTO],
        [eq("decision", "narrow"), contains("narrowed_goal", "porto", ("hotel", "cost")),
         lacks("narrowed_goal", "weather in lisbon")]),
    lib(5, card("Bed size", "How much area do the owner's raised beds have?", ["states the area"]), [BEDS, GARDENA],
        [eq("decision", "answered"), eq("answer_note_ids", ["not_beds"])]),
    lib(6, card("Insurer premiums 2027", "Compare the 2027 basic insurance premiums of Helsana, Swica and CSS in "
                                         "Zurich", ["lists a premium for each insurer"]), [HELSANA],
        [eq("decision", "proceed")]),
    lib(7, card("Solar co-op grants", "Find Swiss grant programs for small solar co-ops (<50 members) that are open "
                                      "now", ["lists open programs or says there are none"]), [NEG_SOLAR],
        [one_of("decision", ["answered", "narrow"])]),
    lib(8, card("October weather", "What's the typical October weather in Lisbon and in Porto?",
                ["covers both cities"]), [LISBON, PORTO],
        [eq("decision", "answered"), contains("answer_note_ids", "not_lisbon", "not_porto")]),
]

CASES += [
    {"id": "render_answer_1", "call": "render_answer", "group": "librarian",
     "inputs": {"card": card("October weather", "What's the typical October weather in Lisbon and in Porto?",
                             ["covers both cities"]), "notes": [LISBON, PORTO]},
     "checks": [contains("summary", "22", "20"),
                judge("The answer only uses facts from the notes and adds no other numbers or claims.", "summary")]},
    {"id": "render_answer_2", "call": "render_answer", "group": "librarian",
     "inputs": {"card": card("Kits for my beds", "How many Gardena Micro-Drip sets do I need for my raised beds?",
                             ["states a number of sets"]), "notes": [GARDENA, BEDS]},
     "checks": [contains("summary", "3"), judge("The answer concludes about 3 sets are needed (40 m² / 15 m² per "
                                                "set), and does not invent facts.", "summary")]},
]
