"""Front desk: frontdesk_step (single decisions), frontdesk episodes (the real
front desk loop on a scratch DB), summarize_topic, extract_owner_facts."""
from bench.checks import contains, custom, eq, judge, lacks, length, one_of, words

NOW = "Tuesday 2026-09-29 10:02"
PROFILE = "- The owner lives in Zurich.\n- The owner prefers low-maintenance solutions."


def ctx(text, **kw):
    base = {"owner": "Alex", "now": NOW, "profile": PROFILE, "topic_title": "(new topic)", "topic_summary": "",
            "history": [], "cards": [], "questions": [], "memory": [], "text": text}
    base.update(kw)
    return base


def step(i, c, checks, steps=(), k=1, n=4):
    return {"id": f"fd_step_{i}", "call": "frontdesk_step", "group": "frontdesk",
            "inputs": {"ctx": c, "steps": list(steps), "k": k, "n": n}, "checks": checks}


not_card = custom("does not create a card", lambda o, c: o["action"] != "create_card")

CASES = [
    step(1, ctx("can you figure out whether drip irrigation makes sense for my raised beds"),
         [eq("action", "create_card"), one_of("role", ["research"]), length("done_when", 1, 5)]),
    step(2, ctx("thanks, that's all for now!", topic_title="Garden irrigation",
                history=["owner: which kit should I buy?", "assistant: Done: I recommend the Gardena Micro-Drip kit "
                                                           "(CHF 89), it fits your 40 m² beds."]),
         [one_of("action", ["no_reply", "reply"])]),
    step(3, ctx("hi!"), [eq("action", "reply")]),
    step(4, ctx("remind me friday to file the tax extension", topic_title="Tax return 2026"),
         [eq("action", "remind"), contains("when", ("fri", "2026-10-02", "oct 2", "2 oct"))]),
    step(5, ctx("how is the irrigation research going?", topic_title="Garden irrigation",
                cards=[{"id": "crd_a1", "title": "Evaluate drip irrigation for raised beds", "state": "running"}]),
         [eq("action", "reply"), not_card]),
    step(6, ctx("under 200 is fine", topic_title="Garden irrigation",
                questions=[{"id": "qst_7", "text": "Q1 (card crd_a1): Which budget should I assume for the drip kit? "
                                                   "Options: under 100 / under 200"}]),
         [eq("action", "answer_question"), eq("question_id", "qst_7"), contains("answer", "200")]),
    step(7, ctx("write a short polite email to my landlord asking if I may install a drip irrigation tap on the "
                "balcony", topic_title="Garden irrigation"),
         [eq("action", "create_card"), eq("role", "write")]),
    step(8, ctx("what's 15% of 240?"), [eq("action", "reply"), contains("text", "36")]),
    step(9, ctx("how big are my raised beds again?", topic_title="Garden irrigation",
                memory=[{"id": "not_beds", "title": "Owner's raised beds", "one_liner": "Size of the owner's beds",
                         "claims": [{"text": "3 raised beds, 40 m² in total, south-facing", "observed_at": "2026-09-01"}]}]),
         [eq("action", "reply"), contains("text", "40")]),
    step(10, ctx("the test in my budget script at ~/code/budget fails since yesterday, can you fix it?"),
         [eq("action", "create_card"), eq("role", "code")]),
    step(11, ctx("compare the drip kits you can buy in Switzerland and recommend one"),
         [eq("action", "reply"), lacks("text", "crd_")],
         steps=[("create_card(title='Compare drip kits in Switzerland', ...)", "Created card crd_b7. Handle anything else the message asks for, then reply to Alex.")], k=2),
    step(12, ctx("compare three health insurers for 2027 and tell me which one is cheapest for me"),
         [eq("action", "create_card"), one_of("role", ["research", "synthesize"])]),
    step(13, ctx("what did you find out?", topic_title="Garden irrigation",
                 cards=[{"id": "crd_a1", "title": "Evaluate drip irrigation for raised beds", "state": "done",
                         "result": "GARDENA Micro-Drip (CHF 89) covers 15 m²; you need 3 sets for 40 m². Manual "
                                   "watering is cheaper but takes 20 min a day."}]),
         [eq("action", "reply"), contains("text", "gardena")]),
    step(14, ctx("so which kit should I buy, and how many?", topic_title="Garden irrigation",
                 cards=[{"id": "crd_a1", "title": "Evaluate drip irrigation for raised beds", "state": "done",
                         "result": "GARDENA Micro-Drip (CHF 89) covers 15 m²; you need 3 sets for 40 m². Manual "
                                   "watering is cheaper but takes 20 min a day."}]),
         [eq("action", "reply"), contains("text", "gardena", ("3", "three")), not_card]),
]

# Episodes run the real Conversation.frontdesk on a scratch DB (see bench/runners.py).
CASES += [
    {"id": "fd_ep_1", "call": "frontdesk_episode", "group": "frontdesk",
     "inputs": {"text": "can you figure out whether drip irrigation makes sense for my raised beds"},
     "checks": [length("cards", 1, 1), length("replies", 1, 1),
                judge("The reply to the owner says work has started. It does not claim results or invent facts "
                      "about drip irrigation.", "replies")]},
    {"id": "fd_ep_2", "call": "frontdesk_episode", "group": "frontdesk",
     "inputs": {"text": "remind me tomorrow at 8am to call the plumber"},
     "checks": [length("reminders", 1, 1),
                custom("reminder due tomorrow 08:00", lambda o, c: o["reminders"][0]["due_at"].endswith("T08:00")
                       and o["reminders"][0]["due_at"][:10] == o["tomorrow"]),
                length("replies", 1, 1), length("cards", 0, 0)]},
    {"id": "fd_ep_3", "call": "frontdesk_episode", "group": "frontdesk",
     "inputs": {"text": "go with the cheaper one", "question": {
         "text": "Two kits fit your beds: Gardena (CHF 89) or Hozelock (CHF 75). Which one should I detail?",
         "options": ["Gardena", "Hozelock"]}},
     "checks": [eq("question_status", "answered"), eq("blocked_card_state", "ready"), length("cards", 0, 0)]},
    {"id": "fd_ep_4", "call": "frontdesk_episode", "group": "frontdesk", "inputs": {"text": "thanks!"},
     "checks": [length("cards", 0, 0), length("reminders", 0, 0), length("replies", 0, 1)]},
    {"id": "fd_ep_5", "call": "frontdesk_episode", "group": "frontdesk",
     "inputs": {"text": "please research the best e-bike under 3000 CHF, and remind me on friday to call mom"},
     "checks": [length("cards", 1, 1), length("reminders", 1, 1), length("replies", 1, 1)]},
]

# summarize_topic
CASES += [
    {"id": "summary_1", "call": "summarize_topic", "group": "frontdesk",
     "inputs": {"topic_title": "Garden irrigation", "old_summary": "", "messages": [
         "owner: can you figure out whether drip irrigation makes sense for my raised beds",
         "assistant: On it — I'll compare drip kits against manual watering.",
         "assistant: Done: the Gardena Micro-Drip kit (CHF 89, 15 m² per set) is the best fit. You'd need 3 sets.",
         "owner: great, I'll buy it next week. btw the beds are 40 m² in total",
         "assistant: Noted!"]},
     "checks": [contains("summary", "gardena", "40"), words("summary", 5, 150),
                judge("The summary says the owner decided to buy the recommended kit.", "summary")]},
    {"id": "summary_2", "call": "summarize_topic", "group": "frontdesk",
     "inputs": {"topic_title": "Tax return 2026",
                "old_summary": "The owner is preparing the 2026 tax return. Deadline: March 31. They asked which "
                               "home office costs are deductible; a research card is open.",
                "messages": ["owner: I found the receipt for the desk, it was CHF 450",
                             "assistant: Noted, I'll include it."]},
     "checks": [contains("summary", "450", ("march 31", "deadline")), words("summary", 5, 150)]},
    {"id": "summary_3", "call": "summarize_topic", "group": "frontdesk",
     "inputs": {"topic_title": "Vacation Portugal", "old_summary": "", "messages": [
         "owner: haha that meme was great", "assistant: :)", "owner: how are you today?",
         "assistant: All good, thanks!",
         "owner: ok let's decide: we go to Porto, not Lisbon, second week of October",
         "owner: and I don't want to rent a car"]},
     "checks": [contains("summary", "porto", "car"), lacks("summary", "meme"),
                judge("The summary states the decision for Porto in the second week of October and that the owner "
                      "does not want a rental car.", "summary")]},
]

# extract_owner_facts
def facts(i, messages, checks):
    return {"id": f"owner_facts_{i}", "call": "extract_owner_facts", "group": "frontdesk",
            "inputs": {"messages": messages}, "checks": checks}


CASES += [
    facts(1, ["owner: I live in Basel now, moved last month."], [length("facts", 1, 2), contains("facts", "basel")]),
    facts(2, ["owner: can you find a cheap flight to Lisbon?"], [length("facts", 0, 0)]),
    facts(3, ["assistant: The Gardena kit costs CHF 89.",
              "owner: I don't want anything that needs an app. Also, I'm vegetarian."],
          [contains("facts", "app", "vegetarian"), lacks("facts", "89", "gardena")]),
    facts(4, ["owner: we decided to go with Porto instead of Lisbon"], [length("facts", 1, 2), contains("facts", "porto")]),
    facts(5, ["owner: my budget for the bike is max 2000 CHF"], [length("facts", 1, 1), contains("facts", "2000")]),
    facts(6, ["owner: thanks!"], [length("facts", 0, 0)]),
]
