"""Quick set (default, without --full): the harness suite plus the core cases
that failed at least once in any recorded run (bench/results/*.md, all models
and harness versions up to v6). The other core cases passed every time; they
only confirm the ceiling, so they run with --full."""

SENSITIVE_CORE = [
    "entities_5", "fd_ep_3", "fd_ep_5", "fd_step_1", "fd_step_11", "fd_step_14", "fd_step_7", "librarian_2",
    "librarian_6", "match_3", "match_5", "match_7", "owner_facts_2", "owner_facts_4", "owner_facts_5",
    "plan_generate_1", "plan_generate_2", "render_answer_2", "rubric_3", "rubric_7", "rubric_8", "rubric_9",
    "summary_3", "triage_10", "triage_4", "triage_5", "triage_7", "verify_4", "worker_ep_3", "worker_ep_4",
    "worker_ep_5", "worker_ep_6", "worker_ep_7", "worker_ep_8", "worker_step_12", "worker_step_2",
    "worker_step_3", "worker_step_4", "worker_step_7", "worker_step_8", "worker_step_9",
]


def is_quick(case):
    return case.get("suite") == "harness" or case["id"] in SENSITIVE_CORE


CASES = []  # this module only defines the selection
