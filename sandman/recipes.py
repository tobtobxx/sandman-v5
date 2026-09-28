"""Seed recipes (design §5.6, §7.9). The model only fills `params`;
fan-out and wiring are done by the harness."""
import re

RECIPES = {
    "rcp_research_compare_recommend": {
        "id": "rcp_research_compare_recommend",
        "title": "Research options, compare, recommend",
        "params": {
            "subject": "what kind of options to look for, e.g. 'drip irrigation kits sold in Switzerland'",
            "criteria": "what to compare them on, e.g. 'price, setup effort, water use'",
            "max_options": "how many options to look at in detail (1-5)",
        },
        "steps": [
            {"key": "gather", "role": "research", "title": "Find candidate {subject}",
             "goal": "Find up to {max_options} candidate {subject}. Add one fact per candidate, with the candidate's "
                     "name as the fact subject.",
             "done_when": ["lists at least 2 named candidates as facts"]},
            {"key": "detail", "role": "research", "title": "Details on {item}", "depends_on": ["gather"],
             "fanout": "gather",
             "goal": "Find details on {item} ({subject}) for these criteria: {criteria}.",
             "done_when": ["covers each of these criteria: {criteria}", "cites at least 1 source"]},
            {"key": "compare", "role": "synthesize", "title": "Compare {subject}", "depends_on": ["detail"],
             "goal": "Compare the candidates on {criteria} and recommend one.",
             "done_when": ["compares the candidates on {criteria}", "recommends one option"]},
        ],
    },
    "rcp_research_write": {
        "id": "rcp_research_write",
        "title": "Research a subject, then write a text about it",
        "params": {
            "subject": "what to research",
            "text": "what text to write and for whom, e.g. 'a 200-word email to my landlord'",
        },
        "steps": [
            {"key": "research", "role": "research", "title": "Research {subject}",
             "goal": "Find the key facts about {subject} that are needed for {text}.",
             "done_when": ["cites at least 2 sources"]},
            {"key": "write", "role": "write", "title": "Write {text}", "depends_on": ["research"],
             "goal": "Write {text} using the research results.",
             "done_when": ["the text is saved as a file", "is based on the research results"]},
        ],
    },
}


def fill(template, params):
    return re.sub(r"\{(\w+)\}", lambda m: str(params.get(m.group(1), m.group(0))), template)


def for_prompt(r):
    return {"id": r["id"], "title": r["title"], "params": r["params"],
            "steps": [f"{s['key']} ({s['role']}): {s['title']}" for s in r["steps"]]}


def shortlist(goal, k=6):
    # Tiny catalog: offer them all. Retrieval by FTS/vector once there are many.
    return [{"id": r["id"], "title": r["title"]} for r in list(RECIPES.values())[:k]]
