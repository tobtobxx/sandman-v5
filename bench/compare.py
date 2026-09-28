"""Compare two bench result files: python -m bench.compare A.json B.json"""
import json
import sys
from collections import defaultdict


def rates(results, key):
    d = defaultdict(list)
    for r in results:
        d[r[key] if key != "id" else r["id"]].append(r["pass"])
    return {k: sum(v) / len(v) for k, v in d.items()}


def main(a_path, b_path):
    a, b = json.load(open(a_path)), json.load(open(b_path))
    ra, rb = a["results"], b["results"]
    print(f"A: {a.get('tag') or a_path}\nB: {b.get('tag') or b_path}\n")
    for key in ("suite", "group"):
        pa, pb = rates(ra, key), rates(rb, key)
        print(f"| {key} | A | B | Δ |\n|---|---|---|---|")
        for k in sorted(set(pa) | set(pb)):
            x, y = pa.get(k), pb.get(k)
            d = f"{(y - x) * 100:+.0f} pp" if x is not None and y is not None else ""
            print(f"| {k} | {'' if x is None else f'{x:.0%}'} | {'' if y is None else f'{y:.0%}'} | {d} |")
        print()
    for name, rs, j in (("A", ra, a), ("B", rb, b)):
        cost = sum(r["cost"] for r in rs)
        print(f"{name}: pass {sum(r['pass'] for r in rs) / len(rs):.1%}, model ${cost:.4f} "
              f"(${cost / len(rs):.5f}/case run), {sum(r['llm_calls'] for r in rs) / len(rs):.2f} calls/case run, "
              f"judge ${j.get('judge_cost', 0):.4f}")
    pa, pb = rates(ra, "id"), rates(rb, "id")
    moved = sorted((pb[k] - pa[k], k) for k in set(pa) & set(pb) if abs(pb[k] - pa[k]) > 0.01)
    print("\nChanged cases (B - A):")
    for d, k in moved:
        print(f"  {d * 100:+4.0f} pp  {k}  ({pa[k]:.0%} → {pb[k]:.0%})")


if __name__ == "__main__":
    main(*sys.argv[1:3])
