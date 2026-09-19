"""Document-diligence MCP server (Streamable HTTP, OAuth bearer tokens, per-tool scopes).

The model never sees a credential. Two secrets exist and both stay server-side:
  - MCP_JWT_SECRET   verifies the bearer token the *client process* sends in a header
  - STORE_TOKEN      the backend credential (Box/SharePoint in production; unused by
                     the local backend) — read here, never returned by any tool

Scopes:
  docs:read   list / search / read pages / compare versions
  docs:file   file a document into a folder (a write — the agent's token lacks it)

Every tool call appends one line to audit.jsonl: who, which tool, which args, outcome.
Document text is not written to the audit log.
"""
from __future__ import annotations

import difflib
import functools
import json
import os
import re
import time
from pathlib import Path

import jwt
import uvicorn
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from corpus import load_corpus

ROOT = Path(__file__).resolve().parent.parent
CORPUS = Path(os.environ.get("CORPUS_DIR", ROOT / "corpus"))
AUDIT_LOG = Path(os.environ.get("AUDIT_LOG", ROOT / "runs" / "audit.jsonl"))
FILED = Path(os.environ.get("FILED_LOG", ROOT / "runs" / "filed.jsonl"))
HOST = os.environ.get("MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("MCP_PORT", "8765"))
RESOURCE_URL = os.environ.get("MCP_RESOURCE_URL", f"http://{HOST}:{PORT}/mcp")
ISSUER_URL = os.environ.get("MCP_ISSUER_URL", "http://127.0.0.1:9000")
ALLOWED_FOLDERS = {"diligence/leases", "diligence/ndas", "diligence/power", "diligence/archive"}


# ---------- auth ----------

class JwtVerifier:
    """HS256 for the demo; production swaps in the IdP's JWKS (RS256) with the same checks."""

    def __init__(self, secret: str, audience: str, issuer: str):
        self.secret, self.audience, self.issuer = secret, audience, issuer

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = jwt.decode(token, self.secret, algorithms=["HS256"],
                                audience=self.audience, issuer=self.issuer,
                                options={"require": ["exp", "aud", "iss", "sub"]})
        except jwt.PyJWTError:
            return None
        return AccessToken(token=token, client_id=claims["sub"], scopes=claims.get("scope", "").split(),
                           expires_at=claims["exp"], resource=self.audience, subject=claims["sub"])


def require(scope: str) -> str:
    tok = get_access_token()
    if tok is None or scope not in tok.scopes:
        raise ToolError(f"forbidden: this token lacks the '{scope}' scope")
    return tok.client_id


def audit(client: str | None, tool: str, args: dict, outcome: str) -> None:
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_LOG.open("a") as f:
        f.write(json.dumps({"ts": round(time.time(), 3), "client": client, "tool": tool,
                            "args": args, "outcome": outcome}) + "\n")


def audited(tool: str, scope: str):
    def wrap(fn):
        @functools.wraps(fn)
        def inner(**kwargs):
            tok = get_access_token()
            client = tok.client_id if tok else None
            try:
                require(scope)
                out = fn(**kwargs)
            except ToolError as e:
                audit(client, tool, kwargs, f"error: {e}")
                raise
            audit(client, tool, kwargs, "ok")
            return out
        return inner
    return wrap


# ---------- local document store (swap for a Box/SharePoint backend) ----------

DOCS = load_corpus(CORPUS)


def get_doc(doc_id: str) -> dict:
    if doc_id not in DOCS:  # ids come from a fixed index, so no path from the model reaches the filesystem
        raise ToolError(f"unknown doc_id '{doc_id}'. Call list_documents for valid ids.")
    return DOCS[doc_id]


# ---------- MCP server ----------

mcp = MCPServer(
    name="diligence-docs",
    instructions="Read-only access to a diligence data room. Cite every claim as (doc_id, page).",
    token_verifier=JwtVerifier(os.environ["MCP_JWT_SECRET"], RESOURCE_URL, ISSUER_URL),
    auth=AuthSettings(issuer_url=ISSUER_URL, resource_server_url=RESOURCE_URL,
                      required_scopes=["docs:read"], validate_token_resource=True),
)
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)


@mcp.tool(annotations=READ_ONLY)
@audited("list_documents", "docs:read")
def list_documents() -> list[dict]:
    """List every document in the data room: id, title, type and page count."""
    return [{"doc_id": d["id"], "title": d["title"], "doc_type": d["doc_type"], "pages": len(d["pages"])}
            for d in DOCS.values()]


@mcp.tool(annotations=READ_ONLY)
@audited("search_documents", "docs:read")
def search_documents(query: str, doc_id: str | None = None, limit: int = 5) -> list[dict]:
    """Keyword search across pages. Returns doc_id, page and a snippet for each hit, best first."""
    terms = [t for t in re.findall(r"\w+", query.lower()) if len(t) > 2]
    if not terms:
        raise ToolError("query needs at least one word longer than two letters")
    scope = [get_doc(doc_id)] if doc_id else DOCS.values()
    hits = []
    for d in scope:
        for n, text in d["pages"].items():
            low = text.lower()
            score = sum(low.count(t) for t in terms)
            if score:
                i = min((low.find(t) for t in terms if t in low))
                hits.append({"doc_id": d["id"], "page": n, "score": score,
                             "snippet": text[max(0, i - 120): i + 280].replace("\n", " ")})
    return sorted(hits, key=lambda h: -h["score"])[: max(1, min(limit, 20))]


@mcp.tool(annotations=READ_ONLY)
@audited("read_page", "docs:read")
def read_page(doc_id: str, page: int) -> dict:
    """Return the full text of one page. Quote from this text when citing."""
    d = get_doc(doc_id)
    if page not in d["pages"]:
        raise ToolError(f"{doc_id} has pages 1-{len(d['pages'])}")
    return {"doc_id": doc_id, "page": page, "text": d["pages"][page]}


@mcp.tool(annotations=READ_ONLY)
@audited("compare_documents", "docs:read")
def compare_documents(base_id: str, revised_id: str) -> list[dict]:
    """Page-by-page redline: every changed sentence in the revised document, with page numbers."""
    base, rev = get_doc(base_id), get_doc(revised_id)
    changes = []
    for n in sorted(set(base["pages"]) | set(rev["pages"])):
        a = re.split(r"(?<=[.;:])\s+", base["pages"].get(n, ""))
        b = re.split(r"(?<=[.;:])\s+", rev["pages"].get(n, ""))
        removed = [s for s in difflib.ndiff(a, b) if s.startswith("- ")]
        added = [s for s in difflib.ndiff(a, b) if s.startswith("+ ")]
        if removed or added:
            changes.append({"page": n, "removed": [s[2:] for s in removed], "added": [s[2:] for s in added]})
    return changes


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                      idempotent_hint=True, open_world_hint=False))
@audited("file_document", "docs:file")
def file_document(doc_id: str, folder: str) -> dict:
    """File a document into a diligence folder. Requires the docs:file scope."""
    get_doc(doc_id)
    if folder not in ALLOWED_FOLDERS:
        raise ToolError(f"folder must be one of {sorted(ALLOWED_FOLDERS)}")
    FILED.parent.mkdir(parents=True, exist_ok=True)
    with FILED.open("a") as f:
        f.write(json.dumps({"ts": time.time(), "doc_id": doc_id, "folder": folder}) + "\n")
    return {"filed": doc_id, "folder": folder}


app = mcp.streamable_http_app(host=HOST)

if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
