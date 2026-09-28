"""Sandman v5 benchmark: can this model do each job the design gives it?

  python -m bench.run --model qwen/qwen3.6-35b-a3b [--repeat 3] [--only triage,worker_ep]

Every case is one small, isolated task for one call type (or one short
episode). Checks are mechanical where possible, else an LLM judge
(a stronger model, --judge-model). Writes bench/results/<model>_<time>.{json,md}.
"""
import argparse
import importlib
import json
import os
import pkgutil
import re
import shutil
import statistics
import tempfile
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

from sandman.llm import Gateway, LLMFailure

from bench import cases as cases_pkg
from bench.runners import RUNNERS

HERE = os.path.dirname(__file__)


def load_cases():
    out = []
    for m in pkgutil.iter_modules(cases_pkg.__path__):
        out += importlib.import_module(f"bench.cases.{m.name}").CASES
    ids = [c["id"] for c in out]
    assert len(ids) == len(set(ids)), "duplicate case ids"
    return out


def run_case(case, rep, args, judge_gw):
    recs = []
    gw = Gateway(model=args.model, reasoning=args.reasoning, log=recs.append)
    tmp = tempfile.mkdtemp(prefix="sandman_bench_")
    ctx = {"inputs": case["inputs"], "judge_gw": judge_gw}
    t0 = time.time()
    out, error, checks = None, None, []
    try:
        out = RUNNERS[case["call"]](gw, case["inputs"], tmp)
    except LLMFailure as e:
        error = f"invalid output: {e}"
    except Exception as e:
        error = f"crash: {type(e).__name__}: {e}\n{traceback.format_exc()[-800:]}"
    wall = time.time() - t0
    if out is not None:
        for ch in case["checks"]:
            try:
                ok, desc = ch(out, ctx)
            except LLMFailure as e:
                ok, desc = False, f"{ch.desc} (judge failed: {e})"
            checks.append({"ok": ok, "check": desc})
    shutil.rmtree(tmp, ignore_errors=True)
    passed = out is not None and all(c["ok"] for c in checks)
    return {"id": case["id"], "call": case["call"], "group": case["group"], "suite": case.get("suite", "core"),
            "rep": rep, "pass": passed,
            "error": error, "checks": checks, "judge_reasons": ctx.get("judge_reasons", []),
            "output": out, "wall_s": round(wall, 2), "llm_calls": len(recs),
            "invalid_attempts": sum(1 for r in recs if not r["ok"]),
            "tokens_in": sum(r["tokens_in"] or 0 for r in recs), "tokens_out": sum(r["tokens_out"] or 0 for r in recs),
            "ms": [r["ms"] for r in recs], "cost": sum(r["cost"] or 0 for r in recs),
            "raw_errors": [r["error"] for r in recs if not r["ok"]][:3],
            "providers": sorted({r.get("provider") or "?" for r in recs})}


def summarize(results, key):
    rows = {}
    for r in results:
        rows.setdefault(r[key], []).append(r)
    table = []
    for k, rs in rows.items():
        ids = {r["id"] for r in rs}
        per_case = [sum(r["pass"] for r in rs if r["id"] == i) / sum(1 for r in rs if r["id"] == i) for i in ids]
        ms = [m for r in rs for m in r["ms"]]
        table.append({key: k, "cases": len(ids), "runs": len(rs),
                      "pass_rate": sum(r["pass"] for r in rs) / len(rs),
                      "flaky_cases": sum(1 for p in per_case if 0 < p < 1),
                      "invalid_runs": sum(1 for r in rs if r["error"] and r["error"].startswith("invalid")),
                      "retry_rate": sum(r["invalid_attempts"] for r in rs) / max(1, sum(r["llm_calls"] for r in rs)),
                      "median_ms": int(statistics.median(ms)) if ms else 0,
                      "tok_out_per_call": int(sum(r["tokens_out"] for r in rs) / max(1, sum(r["llm_calls"] for r in rs))),
                      "calls_per_run": sum(r["llm_calls"] for r in rs) / len(rs),
                      "cost": sum(r["cost"] for r in rs), "cost_per_run": sum(r["cost"] for r in rs) / len(rs)})
    return sorted(table, key=lambda x: x[key])


def md_table(rows, key):
    h = (f"| {key} | cases | pass | flaky | invalid | retry | median ms | calls/run | tok out/call | cost | "
         f"cost/run |\n|---|---|---|---|---|---|---|---|---|---|---|\n")
    return h + "\n".join(f"| {r[key]} | {r['cases']} | {r['pass_rate']:.0%} | {r['flaky_cases']} | {r['invalid_runs']} "
                         f"| {r['retry_rate']:.0%} | {r['median_ms']} | {r['calls_per_run']:.1f} | "
                         f"{r['tok_out_per_call']} | ${r['cost']:.4f} | ${r['cost_per_run']:.5f} |" for r in rows)


def key_usage(gw):
    """OpenRouter's own usage counter for the key, to cross-check the summed costs."""
    try:
        import requests
        r = requests.get(f"{gw.base_url}/key", headers={"Authorization": f"Bearer {gw.api_key}"}, timeout=20)
        return r.json()["data"]["usage"]
    except Exception:
        return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--model", default=os.environ.get("SANDMAN_MODEL", "qwen/qwen3.6-35b-a3b"))
    p.add_argument("--judge-model", default=os.environ.get("SANDMAN_JUDGE_MODEL", "google/gemini-3.8-flash"))
    p.add_argument("--reasoning", action="store_true", help="enable thinking (default: off)")
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--only", default="", help="comma list of case-id prefixes, calls or groups")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--tag", default="")
    args = p.parse_args()

    cases = load_cases()
    if args.only:
        sel = args.only.split(",")
        cases = [c for c in cases if any(c["id"].startswith(s) or c["call"] == s or c["group"] == s
                                         or c.get("suite", "core") == s for s in sel)]
    judge_gw = Gateway(model=args.judge_model, reasoning=True)
    jobs = [(c, r) for r in range(args.repeat) for c in cases]
    print(f"{len(cases)} cases × {args.repeat} = {len(jobs)} runs on {args.model} "
          f"(reasoning {'on' if args.reasoning else 'off'}), judge {args.judge_model}")
    t0 = time.time()
    usage0 = key_usage(judge_gw)
    results = []
    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    partial = open(os.path.join(tempfile.gettempdir(), "sandman_bench_partial.jsonl"), "w")
    with ThreadPoolExecutor(args.workers) as ex:
        futs = [ex.submit(run_case, c, rep, args, judge_gw) for c, rep in jobs]
        for f in as_completed(futs):
            r = f.result()
            results.append(r)
            partial.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
            partial.flush()
            mark = "✓" if r["pass"] else "✗"
            why = "" if r["pass"] else " — " + (r["error"] or "; ".join(c["check"] for c in r["checks"] if not c["ok"]))
            print(f"  {mark} {r['id']}#{r['rep']} ({r['wall_s']}s){why[:160]}", flush=True)

    by_call, by_group = summarize(results, "call"), summarize(results, "group")
    by_suite = summarize(results, "suite")
    total = sum(r["pass"] for r in results) / len(results)
    cost = sum(r["cost"] for r in results)
    time.sleep(5)  # the key counter lags a little
    usage1 = key_usage(judge_gw)
    delta = f" · key usage Δ ${usage1 - usage0:.4f}" if usage0 is not None and usage1 is not None else ""
    header = (f"# Bench: {args.model} (reasoning {'on' if args.reasoning else 'off'}){' · ' + args.tag if args.tag else ''}\n\n"
              f"{time.strftime('%Y-%m-%d %H:%M')} · {len(cases)} cases × {args.repeat} · overall pass "
              f"{total:.1%} · {time.time() - t0:.0f}s\n\n"
              f"Cost (sum of usage.cost of every response, retries included): model ${cost:.4f} "
              f"(${cost / len(results):.5f} per case run, {sum(r['llm_calls'] for r in results)} calls) · "
              f"judge ${judge_gw.cost:.4f} · total ${cost + judge_gw.cost:.4f}{delta}\n")
    fails = [r for r in results if not r["pass"]]
    fail_md = "\n".join(
        f"- **{r['id']}**#{r['rep']}: " + (r["error"] or "; ".join(c["check"] for c in r["checks"] if not c["ok"]))[:300]
        + (f"  \n  judge: {' | '.join(r['judge_reasons'])[:300]}" if r["judge_reasons"] else "")
        + f"  \n  output: `{json.dumps(r['output'], ensure_ascii=False, default=str)[:400]}`" for r in fails)
    md = (header + "\n## By suite\n\n" + md_table(by_suite, "suite") + "\n\n## By role/group\n\n" + md_table(by_group, "group") + "\n\n## By call type\n\n"
          + md_table(by_call, "call") + "\n\n## Failures\n\n" + (fail_md or "none") + "\n")
    print("\n" + md_table(by_suite, "suite") + "\n\n" + md_table(by_group, "group") + "\n\n" + md_table(by_call, "call"))
    prov = {}
    for r in results:
        for pv in r["providers"]:
            prov[pv] = prov.get(pv, 0) + 1
    header += "\nProviders (case runs touching each): " + ", ".join(f"{k} {v}" for k, v in sorted(prov.items(), key=lambda x: -x[1])) + "\n"
    md = md.replace("\n## By suite", header[header.index("\nProviders"):] + "\n## By suite", 1)
    print("\n" + header)
    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    slug = re.sub(r"[^\w.-]", "_", args.model) + ("_think" if args.reasoning else "") + (f"_{args.tag}" if args.tag else "")
    base = os.path.join(HERE, "results", f"{slug}_{time.strftime('%Y%m%d-%H%M%S')}")
    open(base + ".md", "w").write(md)
    json.dump({"model": args.model, "reasoning": args.reasoning, "tag": args.tag, "by_suite": by_suite,
               "by_group": by_group, "by_call": by_call, "judge_cost": judge_gw.cost,
               "results": results}, open(base + ".json", "w"), indent=1, ensure_ascii=False, default=str)
    print(f"wrote {base}.md / .json")


if __name__ == "__main__":
    main()
