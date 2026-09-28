"""Verifier (verify_criterion) and consolidator (match_subject,
relevance_rubric, consolidate_fact, render_note)."""
from bench.checks import contains, custom, eq, one_of, words
from sandman.calls import keep_fact


def ver(i, title, criterion, result, expect):
    return {"id": f"verify_{i}", "call": "verify_criterion", "group": "verifier",
            "inputs": {"title": title, "criterion": criterion, "result_text": result}, "checks": [eq("pass", expect)]}


KITS = ("GARDENA Micro-Drip: CHF 89.90, 15 m², setup 1 h. Hozelock Easy Drip: CHF 75, 10 m², setup 90 min. "
        "Claber Oasis: CHF 119, 4 m², no tap needed.")

CASES = [
    ver(1, "Recommend a drip kit", "recommends one option", KITS + "\nRecommendation: GARDENA Micro-Drip, it covers "
                                                                   "the most area per franc.", True),
    ver(2, "Recommend a drip kit", "recommends one option", KITS + "\nAll three are good choices; it depends on "
                                                                   "your needs.", False),
    ver(3, "EU grants", "each program lists deadline and eligibility",
        "1. Horizon Community Energy: deadline 2027-02-15, open to co-ops in EU/CH.\n2. LIFE Clean Energy: deadline "
        "2027-01-10, open to non-profits.\n3. Pronovo EIV: open to all Swiss plant owners.", False),
    ver(4, "EU grants", "each program lists deadline and eligibility",
        "1. Horizon Community Energy: deadline 2027-02-15, open to co-ops in EU/CH.\n2. LIFE Clean Energy: deadline "
        "2027-01-10, open to non-profits.\n3. Pronovo EIV: rolling deadline (any time), open to all Swiss plant "
        "owners.", True),
    ver(5, "Email to landlord", "the email is polite and asks for permission",
        "Dear Ms. Keller,\nI hope you are well. I would like to install a small drip irrigation kit on my balcony. "
        "It needs no drilling and connects to the existing tap. Would that be okay with you?\nKind regards, Alex",
        True),
    ver(6, "Translate", "is written in German", "The meeting moves to Thursday at 3pm.", False),
    ver(7, "Drip vs manual", "covers cost, setup effort, and water use",
        "Drip kits cost CHF 75-119 upfront, manual watering costs nothing. Setup takes 20-90 minutes.", False),
    ver(8, "Kit prices", "cites sources for the prices",
        "GARDENA: CHF 89.90 (https://www.gardena.com/ch/micro-drip-starter-set). Hozelock: CHF 75 "
        "(https://www.hozelock.com/ch/easy-drip-universal-kit).", True),
    ver(9, "Kit price", "states the price in CHF", "The Gardena starter set costs 89.90 Swiss francs.", True),
    ver(10, "Short summary", "the summary is under 50 words",
        "Drip irrigation is a method of watering plants where water drips slowly to the roots through a network of "
        "pipes, tubes and emitters. It saves 30 to 50 percent of water compared with hand watering and saves time "
        "every day. For raised beds, starter kits from Gardena and Hozelock cost between 75 and 90 francs and cover "
        "10 to 15 square meters each, so several may be needed.", False),
    ver(11, "Recommend a drip kit", "recommends one option",
        "I recommend the Hozelock kit, although Gardena is also good.", True),
    ver(12, "Dentists", "lists at least 3 dentists that accept new patients",
        "1. Dr. Meier, Oerlikon – accepting new patients\n2. Zahnarzt Zentrum Oerlikon – accepting new patients\n"
        "3. Dr. Rossi – currently not accepting new patients", False),
]

NOTES = [{"id": "not_gardena", "title": "GARDENA Micro-Drip starter set", "one_liner": "Drip kit: price, coverage"},
         {"id": "not_hozelock", "title": "Hozelock Easy Drip kit", "one_liner": "Budget drip kit, 10 m²"},
         {"id": "not_beds", "title": "Owner's raised beds", "one_liner": "3 beds, 40 m², south-facing"},
         {"id": "not_lisbon", "title": "Lisbon", "one_liner": "Travel facts about Lisbon"},
         {"id": "not_swica", "title": "Swica health insurance", "one_liner": "Swiss health insurer: premiums"}]


def match(i, subject, claim, expect):
    return {"id": f"match_{i}", "call": "match_subject", "group": "consolidator",
            "inputs": {"subject": subject, "claim": claim, "notes": NOTES}, "checks": [eq("note_id", expect)]}


CASES += [
    match(1, "Gardena Micro Drip Kit", "The kit includes 20 drippers", "not_gardena"),
    match(2, "Hozelock", "The Easy Drip kit costs CHF 75", "not_hozelock"),
    match(3, "Porto", "October highs in Porto average 20 °C", "none"),
    match(4, "raised beds", "The owner's beds are 80 cm high", "not_beds"),
    match(5, "Claber Oasis", "The Claber Oasis kit has its own 25 litre tank", "none"),
    match(6, "Lisboa", "Lisboa has five historic tram lines", "not_lisbon"),
    match(7, "Helsana", "Helsana's 2027 basic premium in Zurich is CHF 405/month", "none"),
]


def rub(i, subject, claim, volatility, expect_keep, card="Evaluate drip irrigation for raised beds", source="url"):
    return {"id": f"rubric_{i}", "call": "relevance_rubric", "group": "consolidator",
            "inputs": {"subject": subject, "claim": claim, "source": source, "card_title": card},
            "volatility": volatility,
            "checks": [custom(f"keep == {expect_keep}", lambda o, c, v=volatility: keep_fact(o, v) == expect_keep)]}


CASES += [
    rub(1, "GARDENA Micro-Drip", "The GARDENA Micro-Drip starter set costs CHF 89.90 at Jumbo", "volatile", True),
    rub(2, "search", "I used web_search with the query 'drip kit price'", "evergreen", False, source="card"),
    rub(3, "water", "Water boils at 100 °C at sea level", "evergreen", False),
    rub(4, "Zentralbibliothek Zürich", "Open Saturdays 10:00-17:00", "slow", True, card="Library hours"),
    rub(5, "search results", "The search results page had 10 results", "volatile", False, source="card"),
    rub(6, "France", "Paris is the capital of France", "evergreen", False, card="Plan a trip"),
    rub(7, "Swica", "Swica's 2027 basic premium for a 30-year-old in Zurich is CHF 412/month", "slow", True,
        card="Compare insurers"),
    rub(8, "gardena.com", "The page took 4 seconds to load", "volatile", False),
    rub(9, "Hozelock Easy Drip", "The kit covers up to 10 m²", "evergreen", True),
    rub(10, "Community Energy 2027", "The EU Horizon call 'Community Energy 2027' closes on 2027-02-15", "slow", True,
        card="Find EU grants for solar co-ops"),
    rub(11, "task", "Step 3 of the task was completed successfully", "volatile", False, source="card"),
]

CLAIMS = [{"id": "clm_1", "text": "The GARDENA Micro-Drip starter set costs CHF 89 at Jumbo", "source": "url",
           "observed_at": "2026-09-20"},
          {"id": "clm_2", "text": "The set covers about 15 m²", "source": "url", "observed_at": "2026-09-20"},
          {"id": "clm_3", "text": "The set includes 20 drippers", "source": "url", "observed_at": "2026-09-20"}]


def cons(i, claim, decisions, target=None, observed="2026-10-12"):
    checks = [one_of("decision", decisions if isinstance(decisions, list) else [decisions])]
    if target:
        checks.append(eq("target_claim_id", target))
    return {"id": f"consolidate_{i}", "call": "consolidate_fact", "group": "consolidator",
            "inputs": {"note_title": "GARDENA Micro-Drip starter set", "claims": CLAIMS, "claim": claim,
                       "source": "url", "observed_at": observed}, "checks": checks}


CASES += [
    cons(1, "The GARDENA Micro-Drip starter kit is priced at CHF 89 (Jumbo)", "duplicate", "clm_1"),
    cons(2, "The GARDENA Micro-Drip starter set now costs CHF 79 at Jumbo", "update", "clm_1"),
    cons(3, "The set needs a water pressure of at least 1 bar", "new"),
    # "update" is acceptable: the harness never lets an update overwrite an evergreen claim (memory.py)
    cons(4, "The set covers about 40 m²", ["contradicts", "update"], "clm_2"),
    cons(5, "The starter set comes with 20 drip emitters", "duplicate", "clm_3"),
    cons(6, "The kit can be extended with additional drip lines", "new"),
    cons(7, "The set covers roughly 15 square meters", "duplicate", "clm_2"),
    cons(8, "Since the 2026 model the set includes 25 drippers", ["update", "contradicts"], "clm_3"),
]

CASES += [
    {"id": "render_note_1", "call": "render_note", "group": "consolidator",
     "inputs": {"title": "GARDENA Micro-Drip starter set", "claims": CLAIMS},
     "checks": [words("one_liner", 3, 20), contains("one_liner", ("drip", "kit", "set"))]},
    {"id": "render_note_2", "call": "render_note", "group": "consolidator",
     "inputs": {"title": "Owner's raised beds", "claims": [
         {"text": "The owner has 3 raised beds, 40 m² in total"}, {"text": "The beds are south-facing"},
         {"text": "The beds are 80 cm high"}]},
     "checks": [words("one_liner", 3, 20), contains("one_liner", ("bed", "40"))]},
]
