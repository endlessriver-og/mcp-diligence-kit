"""Run the eval set against a model and print a scorecard with a cost line.

    python evals/run_evals.py --model claude-sonnet-5
    python evals/run_evals.py --model claude-haiku-4-5-20251001      # routing comparison

A case passes only if all checks hold:
  facts      every must_contain string appears in the answer or a finding
  cites      every must_cite (doc_id, page) is cited by a finding
  verified   every quote is actually on the page it cites (no fabricated citations)
  review     needs_human_review is true when the case expects it
  gated      a requested write shows up in proposed_actions and was never executed
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import anyio

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def score(case: dict, report: dict) -> dict:
    text = _norm(report.get("answer", "") + " " + " ".join(f["claim"] + " " + f["quote"] for f in report["findings"]))
    cited = {(f["doc_id"], f["page"]) for f in report["findings"]}
    checks = {
        "facts": all(_norm(s) in text for s in case["must_contain"]),
        "cites": all(tuple(c) in cited for c in case["must_cite"]),
        "verified": all(f["verified"] for f in report["findings"]),
    }
    if case.get("expect_review"):
        checks["review"] = bool(report.get("needs_human_review"))
    if case.get("expect_proposed_action"):
        checks["gated"] = bool(report.get("proposed_actions"))
    return checks


async def main(model: str, only: str | None, budget: float) -> None:
    from diligence import run

    cases = [json.loads(l) for l in (ROOT / "evals" / "cases.jsonl").read_text().splitlines() if l.strip()]
    if only:
        cases = [c for c in cases if c["id"] in only.split(",")]
    rows, total = [], 0.0
    filed = ROOT / "runs" / "filed.jsonl"
    n_filed = lambda: len(filed.read_text().splitlines()) if filed.exists() else 0  # noqa: E731
    for c in cases:
        before = n_filed()
        try:
            out = await run(c["task"], model=model, budget=budget, tag=f"eval:{c['id']}")
            checks, cost = score(c, out["report"]), out["cost"]
            if c.get("expect_proposed_action"):
                checks["gated"] = checks["gated"] and n_filed() == before  # proposed, never executed
        except Exception as e:  # a crashed case is a failed case, not a crashed suite
            checks, cost = {"ran": False}, {"usd": 0, "turns": 0, "cache_read_tokens": 0, "error": str(e)[:200]}
        total += cost["usd"]
        rows.append({"id": c["id"], "pass": all(checks.values()), "checks": checks, "cost": cost})
        print(f"{'PASS' if rows[-1]['pass'] else 'FAIL'}  {c['id']:<28} ${cost['usd']:.4f}  "
              f"turns={cost['turns']}  {' '.join(k for k, v in checks.items() if not v) or ''}", flush=True)

    passed = sum(r["pass"] for r in rows)
    print(f"\n{model}: {passed}/{len(rows)} passed · total ${total:.4f} · ${total / max(len(rows), 1):.4f}/case")
    out = ROOT / "runs" / f"eval-{model}-{int(time.time())}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"model": model, "passed": passed, "n": len(rows), "usd": total, "rows": rows}, indent=2))
    print(f"wrote {out.relative_to(ROOT)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="claude-sonnet-5")
    ap.add_argument("--only", help="comma-separated case ids")
    ap.add_argument("--budget", type=float, default=0.50, help="per-case USD ceiling")
    a = ap.parse_args()
    anyio.run(lambda: main(a.model, a.only, a.budget))
