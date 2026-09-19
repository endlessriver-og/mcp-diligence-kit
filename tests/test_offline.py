"""Offline tests: no model calls. Citation verifier, approval hook, scorer, and eval gold data."""
import json
import os
import sys
from pathlib import Path

import anyio

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "agent"), str(ROOT / "evals"), str(ROOT / "server")]
os.environ.setdefault("MCP_JWT_SECRET", "offline-test-secret-0123456789abcdef")

from corpus import load_corpus  # noqa: E402
from diligence import approval_gate, verify_citations  # noqa: E402
from run_evals import score  # noqa: E402

DOCS = load_corpus(ROOT / "corpus")
CASES = [json.loads(l) for l in (ROOT / "evals" / "cases.jsonl").read_text().splitlines() if l.strip()]


def test_corpus_parses_every_page():
    assert len(DOCS) == 5
    assert all(d["pages"] and min(d["pages"]) == 1 for d in DOCS.values())


def test_verifier_accepts_real_quote_and_rejects_fabrication():
    report = {"findings": [
        {"claim": "c", "doc_id": "ground-lease-parcel-7", "page": 2, "quote": "annual Base Rent of $2,150 per acre"},
        {"claim": "c", "doc_id": "ground-lease-parcel-7", "page": 3, "quote": "annual Base Rent of $2,150 per acre"},
        {"claim": "c", "doc_id": "ground-lease-parcel-7", "page": 2, "quote": "annual Base Rent of $2,500 per acre"},
        {"claim": "c", "doc_id": "no-such-doc", "page": 1, "quote": "anything at all here"},
    ]}
    assert [f["verified"] for f in verify_citations(report, DOCS)] == [True, False, False, False]


def test_verifier_tolerates_whitespace_and_smart_quotes():
    report = {"findings": [{"claim": "c", "doc_id": "ground-lease-parcel-7", "page": 2,
                            "quote": "(the  “Development Period”)"}]}
    assert verify_citations(report, DOCS)[0]["verified"]


def test_approval_gate_denies_and_queues(tmp_path, monkeypatch):
    import diligence
    monkeypatch.setattr(diligence, "RUNS", tmp_path)
    out = anyio.run(approval_gate, {"tool_name": "mcp__docs__file_document",
                                    "tool_input": {"doc_id": "x", "folder": "diligence/leases"}}, None, None)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert json.loads((tmp_path / "approvals_pending.jsonl").read_text())["input"]["folder"] == "diligence/leases"


def test_every_gold_fact_and_citation_exists_in_the_corpus():
    """An eval whose gold answer is not in the data room measures nothing."""
    for c in CASES:
        for doc_id, page in c["must_cite"]:
            assert page in DOCS[doc_id]["pages"], (c["id"], doc_id, page)
        cited_text = " ".join(DOCS[d]["pages"][p] for d, p in c["must_cite"]) or " ".join(
            t for d in DOCS.values() for t in d["pages"].values())
        for fact in c["must_contain"]:
            if fact.lower().startswith("form "):
                continue  # a conclusion, not a quotable string
            assert fact.lower() in " ".join(cited_text.split()).lower(), (c["id"], fact)


def test_missing_fact_case_really_is_missing():
    assert not any("property tax" in t.lower() for t in DOCS["ground-lease-parcel-7"]["pages"].values())


def test_scorer():
    case = {"must_contain": ["$425 per acre"], "must_cite": [["ground-lease-parcel-7", 2]], "expect_review": True}
    good = {"answer": "Development rent is $425 per acre.", "needs_human_review": True, "findings": [
        {"claim": "dev rent", "doc_id": "ground-lease-parcel-7", "page": 2, "quote": "q", "verified": True}]}
    assert all(score(case, good).values())
    bad = {**good, "needs_human_review": False}
    assert not score(case, bad)["review"]


def test_weak_signing_secret_is_refused():
    import pytest
    from docs_mcp import JwtVerifier
    with pytest.raises(SystemExit):
        JwtVerifier("short", "http://a/mcp", "http://i")
    assert JwtVerifier("x" * 32, "http://a/mcp", "http://i")
