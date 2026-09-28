"""Planner: triage, plan_fill, plan_generate."""
from bench.checks import contains, custom, eq, judge, one_of
from sandman import calls, recipes

RESEARCH_TOOLS = [("web_search", "search the web"), ("web_fetch", "read a web page"),
                  ("read_artifact", "read a saved file"),
                  ("write_artifact", "save a file")]
WRITE_TOOLS = [("read_artifact", "read a saved file"), ("write_artifact", "save a file")]
RECIPES = recipes.shortlist("")


def tri(i, title, goal, done_when, checks, tools=RESEARCH_TOOLS, max_turns=12):
    return {"id": f"triage_{i}", "call": "triage", "group": "planner",
            "inputs": {"card": {"title": title, "goal": goal, "done_when": done_when}, "tools": tools,
                       "recipes": RECIPES, "max_turns": max_turns}, "checks": checks}


no_q = eq("missing_info", None)
single = custom("effective decision: single session", lambda o, c: not calls.triage_split(o))
split = custom("effective decision: split", lambda o, c: calls.triage_split(o))

CASES = [
    tri(1, "Summarize article", "Summarize the saved article art_12 into 5 bullet points", ["5 bullet points"],
        [single, no_q], tools=WRITE_TOOLS, max_turns=8),
    tri(2, "Compare insurers", "Compare 4 Swiss health insurers on 2027 premium and coverage and recommend one",
        ["covers 4 insurers", "recommends one"],
        [split, eq("recipe_id", "rcp_research_compare_recommend")]),
    tri(3, "Library hours", "Find the Saturday opening hours of the Zentralbibliothek Zürich", ["states the hours"],
        [single, no_q]),
    tri(4, "Airbnb permission", "Find out the Zurich rules for renting out a room on Airbnb, then write a short "
                                "email to my landlord asking for permission",
        ["email cites the rules", "email is saved as a file"],
        [split, eq("recipe_id", "rcp_research_write")]),
    tri(5, "Find a hotel", "Find a hotel for my trip", ["lists 3 hotels"],
        [custom("asks for missing info", lambda o, c: bool(o["missing_info"]))]),
    tri(6, "Birthday message", "Write a warm birthday message for my sister Lena, who turns 30",
        ["under 80 words"], [single, no_q], tools=WRITE_TOOLS, max_turns=8),
    tri(7, "Drip kits", "Compare drip irrigation kits available in Switzerland and recommend one for 40 m² of "
                        "raised beds", ["compares at least 3 kits", "recommends one"],
        [split, eq("recipe_id", "rcp_research_compare_recommend")]),
    tri(8, "VAT rate", "What is the current standard VAT rate in Switzerland?", ["states the rate with a source"],
        [single, no_q]),
    tri(9, "Move to Basel", "Plan my move to Basel: find 3 apartments, compare moving companies, choose a health "
                            "insurer and list the registration steps at the Gemeinde",
        ["covers apartments, movers, insurance and registration"], [split]),
    tri(10, "Buy ticket", "Buy me the cheapest ticket", ["ticket bought"],
        [custom("asks for missing info", lambda o, c: bool(o["missing_info"]))]),
    tri(11, "Translate", "Translate this paragraph into German: 'The meeting moves to Thursday at 3pm.'",
        ["German translation"], [single, no_q], tools=WRITE_TOOLS, max_turns=8),
]

CR = recipes.for_prompt(recipes.RECIPES["rcp_research_compare_recommend"])
RW = recipes.for_prompt(recipes.RECIPES["rcp_research_write"])


def fill(i, title, goal, recipe, checks):
    return {"id": f"plan_fill_{i}", "call": "plan_fill", "group": "planner",
            "inputs": {"card": {"title": title, "goal": goal, "done_when": []}, "recipe": recipe}, "checks": checks}


CASES += [
    fill(1, "Drip kits", "Compare drip irrigation kits available in Switzerland on price, setup effort and water "
                         "use, and recommend one for 40 m² of raised beds", CR,
         [contains("subject", "drip"), contains("criteria", "price", "setup", "water"),
          custom("max_options 2-5", lambda o, c: 2 <= o["max_options"] <= 5)]),
    fill(2, "Insurers", "Compare Swica, Helsana, CSS and Sanitas on 2027 premium and deductible options for a "
                        "30-year-old in Zurich", CR,
         [contains("subject", ("insur", "swica")), contains("criteria", "premium", "deductible"),
          eq("max_options", 4)]),
    fill(3, "Airbnb permission", "Find out the Zurich rules for renting out a room on Airbnb, then write a short "
                                 "email to my landlord asking for permission", RW,
         [contains("subject", "airbnb"), contains("text", "email", "landlord")]),
]


def dag_ok(o, c):
    subs = o["subtasks"]
    return all(all(d < i for d in s["depends_on"]) for i, s in enumerate(subs))


def gen(i, title, goal, done_when, extra):
    return {"id": f"plan_generate_{i}", "call": "plan_generate", "group": "planner",
            "inputs": {"card": {"title": title, "goal": goal, "done_when": done_when}},
            "checks": [custom("valid DAG", dag_ok),
                       judge("The subtasks together cover the whole goal, do not overlap much, and each is small "
                             "enough for one work session.")] + extra}


last_joins = custom("last subtask depends on an earlier one",
                    lambda o, c: len(o["subtasks"][-1]["depends_on"]) >= 1)

CASES += [
    gen(1, "Move to Basel", "Plan my move to Basel: find 3 apartments, compare moving companies, choose a health "
                            "insurer and list the registration steps at the Gemeinde",
        ["covers apartments, movers, insurance and registration"], []),
    gen(2, "Note apps blog post", "Write a blog post comparing three note-taking apps (Obsidian, Notion, Logseq) "
                                  "for privacy-minded users", ["about 800 words", "covers all three apps"],
        [last_joins, custom("ends with a write step", lambda o, c: o["subtasks"][-1]["role"] == "write")]),
    gen(3, "Birthday party", "Organize a birthday party for 20 people in Zurich in November: find a venue, catering "
                             "options, and write the invitation text", ["venue options", "catering options",
                                                                       "invitation text"],
        [custom("has a write step", lambda o, c: any(s["role"] == "write" for s in o["subtasks"]))]),
    gen(4, "Solar for the house", "Find out if a solar roof pays off for my house in Zurich: estimate yield, "
                                  "costs, subsidies and payback time", ["states payback time in years"],
        [last_joins]),
]
