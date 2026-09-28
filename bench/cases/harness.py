"""Harness suite: tasks where the harness (context packing, toolsets,
truncation, board wiring) matters as much as the model. Longer episodes,
long pages, saved files, memory, follow-ups, full dispatcher pipelines."""
from bench.checks import contains, custom, eq, judge, lacks, length, one_of, words
from bench.cases.worker import PROFILE, RESEARCH_RESULT, bullets, wctx

H = {"suite": "harness"}


def ep(i, role, title, goal, done_when, checks, max_turns=8, artifacts=None, files=None, notes=None, **kw):
    return {"id": f"h_{role}_{i}", "call": "worker_episode", "group": f"worker_{role}", **H,
            "inputs": {"ctx": wctx(role, title, goal, done_when, **kw), "max_turns": max_turns,
                       "artifacts": artifacts, "files": files, "notes": notes}, "checks": checks}


finished = eq("final.action", "finish")

GARDENA_NOTE = {"id": "not_gardena", "title": "GARDENA Micro-Drip starter set",
                "one_liner": "Drip kit for raised beds: price, coverage, contents",
                "claims": [{"text": "Costs CHF 89.90", "observed_at": "2026-09-27"},
                           {"text": "One set covers 15 m²", "observed_at": "2026-09-27"},
                           {"text": "Includes 20 drippers and a pressure reducer", "observed_at": "2026-09-27"}]}
REPORT = ("# Drip kit comparison\n\n" + "Background: drip irrigation brings water to the roots. " * 20 +
          "\n\n## Kits\n\n| Kit | Price | Coverage | Needs tap |\n|---|---|---|---|\n"
          "| GARDENA Micro-Drip | CHF 89.90 | 15 m² | yes |\n| Hozelock Easy Drip | CHF 75 | 10 m² | yes |\n"
          "| Claber Oasis | CHF 119 | 4 m² | no |\n\n## Recommendation\n\nGARDENA gives the most area per franc.")

CODE = {
    "shop": {
        "shop.py": "\n".join([
            '"""Tiny shop module."""', "", "VAT = 0.081", "",
            "PRODUCTS = {", "    'gardena': {'name': 'GARDENA Micro-Drip', 'price': 89.90},",
            "    'hozelock': {'name': 'Hozelock Easy Drip', 'price': 75.00},",
            "    'claber': {'name': 'Claber Oasis', 'price': 119.00},", "}", "", "",
            "def price(key):", "    return PRODUCTS[key]['price']", "", "",
            "def with_vat(amount):", "    return round(amount * (1 + VAT), 2)", "", "",
            "def discount(amount, percent):", '    """Reduce amount by percent (e.g. 10 -> 10% off)."""',
            "    return round(amount - amount * percent, 2)", "", "",
            "def cart_total(items, percent=0):", '    """items: list of (key, quantity)."""', "    total = 0",
            "    for key, qty in items:", "        total += price(key) * qty",
            "    if percent:", "        total = discount(total, percent)", "    return round(total, 2)", "", "",
            "def describe(key):", "    p = PRODUCTS[key]", "    return f\"{p['name']}: CHF {p['price']:.2f}\"", ""]),
        "test_shop.py": "from shop import cart_total, discount, describe, with_vat\n\n"
                        "assert discount(100, 10) == 90.0, discount(100, 10)\n"
                        "assert cart_total([('gardena', 2)]) == 179.8\n"
                        "assert cart_total([('hozelock', 1), ('claber', 1)], percent=10) == 174.6\n"
                        "assert with_vat(100) == 108.1\n"
                        "assert describe('claber') == 'Claber Oasis: CHF 119.00'\n"
                        "print('ok')\n",
        "check": "python test_shop.py",
    },
    "report": {
        "budget.py": "def monthly_total(rows, month):\n"
                     "    \"\"\"rows: list of (iso_date, category, amount).\"\"\"\n"
                     "    return sum(a for d, c, a in rows if int(d[5:7]) == month)\n",
        "report.py": "from budget import monthly_total\n\n\n"
                     "def report(rows, month):\n"
                     "    \"\"\"Return {'total': ..., 'by_category': {category: total}} for the month.\"\"\"\n"
                     "    return {'total': monthly_total(rows, month)}\n",
        "test_report.py": "from report import report\n\n"
                          "rows = [('2026-03-01', 'food', 50), ('2026-03-05', 'garden', 90), "
                          "('2026-03-09', 'food', 20), ('2026-04-01', 'food', 5)]\n\n"
                          "r = report(rows, 3)\n"
                          "assert r['total'] == 160, r\n"
                          "assert r['by_category'] == {'food': 70, 'garden': 90}, r\n"
                          "print('ok')\n",
        "check": "python test_report.py",
    },
}


CASES = [
    # --- research: harness-dependent ---
    ep(1, "research", "Balcony rules", "Do the Zurich standard house rules allow a drip irrigation system on a "
                                       "balcony? Quote the rule.", ["answers yes or no", "quotes the rule", "cites the source"],
       [finished, contains("final", "drip", ("lower balconies", "facade", "consent")),
        contains("final.sources", "hev-zuerich")]),
    ep(2, "research", "Cheapest per m²", "Which is cheaper per m² of coverage: the GARDENA Micro-Drip starter set or "
                                         "the Hozelock Easy Drip kit?", ["names the cheaper kit per m²", "shows the numbers"],
       [finished, judge("The result says GARDENA is cheaper per m² (about CHF 6/m² vs CHF 7.50/m² for Hozelock).",
                        "final")]),
    ep(3, "research", "Kit for my balcony", "Recommend a drip kit for the owner's balcony plant boxes (about 2 m²).",
       ["recommends one kit that fits the owner's situation", "cites a source"],
       [finished, contains("final", ("blumat", "claber")), lacks("final.summary", "recommend the gardena")],
       profile=PROFILE + "\n- The owner's balcony has no water tap."),
    ep(4, "research", "Gardena price", "Find the price of the GARDENA Micro-Drip starter set.",
       ["states the price in CHF"],
       [finished, contains("final.summary", "89.90"),
        custom("uses memory instead of the web", lambda o, c: not any(t.startswith("web_") for t in o["tools_used"]))],
       memory=[GARDENA_NOTE]),
    ep(5, "research", "Library hours", "Find the Saturday opening hours of the Zentralbibliothek Zürich.",
       ["states the Saturday hours", "cites a source"],
       [finished, contains("final.summary", "17"), contains("final.sources", "zb.uzh.ch")],
       comments=["verifier: Not met: cites a source: the previous result cited zuerich.com, which has no hours"],
       inputs=["Progress so far: searched 'Zurich library hours', the zuerich.com page lists libraries but no "
               "hours.\nNext step: find the Zentralbibliothek's own page."]),
    # --- write ---
    ep(1, "write", "Five bullets", "Summarize the drip kit report into exactly 5 bullet points, with the prices, "
                                   "and save them.", ["exactly 5 bullet points", "includes the prices", "saved as a file"],
       [finished, contains("last_artifact", "89.90", "75", "119"),
        custom("exactly 5 bullets (or numbered points)", lambda o, c: bullets(o["last_artifact"]) == 5)],
       artifacts={"report.md": REPORT},
       inputs=["Result of \"Compare drip kits\" [done]:\nCompared three drip kits. Full report in {art:report.md}."]),
    ep(2, "write", "Email in German", "Write a short polite email in German (under 120 words) to the landlord, "
                                      "Frau Keller, asking for permission to install a drip irrigation kit on the "
                                      "balcony.", ["email is saved as a file", "written in German", "under 120 words"],
       [finished, contains("last_artifact", "keller"), words("last_artifact", 25, 140),
        judge("This email text is written in German, polite, and asks Frau Keller for permission.", "last_artifact")],
       inputs=[RESEARCH_RESULT]),
    # --- synthesize ---
    ep(1, "synthesize", "Recommend a kit", "Recommend the kit with the lowest price per m² of coverage.",
       ["recommends one kit", "states its price per m²"],
       [finished, contains("final.recommendation", "gardena"), contains("final", ("5.99", "6.0", "5.9", "6 chf", "chf 6"))],
       max_turns=6, artifacts={"report.md": REPORT},
       inputs=["Result of \"Details on three kits\" [done]:\nPrices and coverage of GARDENA, Hozelock and Claber "
               "kits collected. The table is in {art:report.md}."]),
    # --- code ---
    ep(1, "code", "Fix discount", "The tests in test_shop.py fail. Fix the bug in shop.py without changing the tests.",
       ["python test_shop.py exits with code 0"],
       [custom("tests pass afterwards", lambda o, c: o["command_ok"])], max_turns=10, files=CODE["shop"]),
    ep(2, "code", "Add total_by_category", "Add a function total_by_category(rows) to budget.py that returns a dict "
                                           "{category: total}, and use it in report.py so that test_report.py passes.",
       ["python test_report.py exits with code 0"],
       [custom("tests pass afterwards", lambda o, c: o["command_ok"])], max_turns=12, files=CODE["report"]),
]

# --- front desk with existing board state ---
DONE_CARD = {"title": "Evaluate drip irrigation for raised beds", "state": "done",
             "result": {"summary": "GARDENA Micro-Drip (CHF 89.90, 15 m² per set) fits best: 3 sets for 40 m². "
                                   "Manual watering is cheaper but takes 20 minutes a day.", "sources": [],
                        "facts": [], "open_questions": []}}
RUNNING_CARD = {"title": "Compare drip kits sold in Switzerland", "state": "running"}
BLOCKED_CARD = {"title": "Compare drip kits sold in Switzerland", "state": "blocked"}


def fd(i, text, checks, **setup):
    return {"id": f"h_fd_{i}", "call": "frontdesk_setup_episode", "group": "frontdesk", **H,
            "inputs": {"text": text, **setup}, "checks": checks}


CASES += [
    fd(1, "what did you find out?", [length("replies", 1, 1), contains("replies", "gardena", ("89", "269")),
                                     length("cards", 0, 0)],
       cards=[DONE_CARD]),
    fd(2, "any news on the drip kits?",
       [length("replies", 1, 1), contains("replies", ("budget", "200")), length("cards", 0, 0)],
       cards=[BLOCKED_CARD], questions=[{"card": 0, "text": "What is the maximum budget for the kit?",
                                         "options": ["under 100", "under 200"]}]),
    fd(3, "the second week of October works",
       [custom("Q2 answered, Q1 still open",
               lambda o, c: [q["status"] for q in o["questions"]] == ["open", "answered"]), length("cards", 0, 0)],
       cards=[BLOCKED_CARD, {"title": "Plan the Porto trip", "state": "blocked"}],
       questions=[{"card": 0, "text": "What is the maximum budget for the kit?"},
                  {"card": 1, "text": "Which week in October should I plan for?"}]),
    fd(4, "oh, and the kit must work without a tap", [length("cards", 0, 0),
       custom("the running card got the new requirement",
              lambda o, c: any("tap" in cm.lower() for cm in o["seeded"][0]["comments"])), length("replies", 1, 1)],
       cards=[RUNNING_CARD],
       history=[("in", "compare the drip kits sold in Switzerland"), ("out", "On it: I'll compare the kits.")]),
    fd(5, "remind me in 2 hours to water the tomatoes", [length("reminders", 1, 1), length("replies", 1, 1)]),
]

# --- several messages, full inbound path (router + front desk) ---
CASES += [
    {"id": "h_conv_1", "call": "conversation_episode", "group": "frontdesk", **H,
     "inputs": {"messages": ["can you compare the drip kits sold in Switzerland for my raised beds?",
                             "also remind me on friday to file the tax extension",
                             "for the drip kits: max 100 CHF please"]},
     "checks": [length("cards", 1, 1), length("reminders", 1, 1),
                custom("two topics", lambda o, c: len(o["topics"]) == 2),
                custom("reminder in a different topic than the card",
                       lambda o, c: o["reminders"] and o["cards"] and o["reminders"][0]["topic"] != o["cards"][0]["topic"]),
                length("replies", 3, 3)]},
]

# --- whole pipeline through the dispatcher ---
HOURS_NOTE = {"title": "Zentralbibliothek Zürich", "aliases": ["ZB Zürich", "Zentralbibliothek"],
              "one_liner": "Zurich's central library: address and opening hours",
              "claims": [{"text": "Opening hours: Monday to Friday 08:00-20:00, Saturday 10:00-17:00, Sunday closed",
                          "observed_at": "2026-09-26", "volatility": "slow", "source": "https://www.zb.uzh.ch/en/opening-hours"}]}


def pipe(i, card, checks, **kw):
    return {"id": f"h_pipe_{i}", "call": "pipeline", "group": "pipeline", **H,
            "inputs": {"card": card, **kw}, "checks": checks}


done = eq("state", "done")
CASES += [
    pipe(1, {"title": "Gardena price", "goal": "Find the price and covered area of the GARDENA Micro-Drip starter set",
             "done_when": ["states the price in CHF", "cites at least 1 source"]},
         [done, contains("result_text", "89", "15"), custom("≤ 12 model calls", lambda o, c: sum(o["calls"].values()) <= 12)]),
    pipe(2, {"title": "Library hours", "goal": "What are the Saturday opening hours of the Zentralbibliothek Zürich?",
             "done_when": ["states the Saturday hours"]},
         [done, contains("result_text", "10", "17"),
          custom("answered from memory (no worker session)", lambda o, c: o["worker_steps"] == 0)],
         notes=[HOURS_NOTE]),
    pipe(3, {"title": "Compare drip kits", "goal": "Compare the GARDENA Micro-Drip, Hozelock Easy Drip and Claber Oasis "
                                                  "kits on price and coverage and recommend one for 40 m² of raised beds",
             "done_when": ["compares all three kits on price and coverage", "recommends one kit"]},
         [done, contains("result_text", "89", ("75", "74.95"), "119"), contains("result_text", "recommend"),
          custom("≤ 70 model calls", lambda o, c: sum(o["calls"].values()) <= 70)], max_ticks=80),
    pipe(4, {"title": "Allotment fee", "goal": "Find the 2027 membership fee of the Zurich Allotment Gardeners "
                                              "Association", "done_when": ["states the fee with a source"]},
         [custom("does not end done with an invented fee",
                 lambda o, c: o["state"] != "done" or "not" in o["result_text"].lower()),
          custom("owner is asked or card failed", lambda o, c: o["state"] in ("blocked", "failed") or o["questions"])]),
]
