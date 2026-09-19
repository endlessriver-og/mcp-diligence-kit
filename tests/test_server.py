"""Integration tests: start the real server on a free port and talk to it over Streamable HTTP."""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import anyio
import httpx
import jwt
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

ROOT = Path(__file__).resolve().parent.parent
SECRET = "test-secret-not-for-production-0123456789"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    port = free_port()
    runs = tmp_path_factory.mktemp("runs")
    env = {**os.environ, "MCP_JWT_SECRET": SECRET, "MCP_PORT": str(port),
           "AUDIT_LOG": str(runs / "audit.jsonl"), "FILED_LOG": str(runs / "filed.jsonl")}
    proc = subprocess.Popen([sys.executable, str(ROOT / "server" / "docs_mcp.py")], env=env)
    url = f"http://127.0.0.1:{port}/mcp"
    for _ in range(50):
        try:
            httpx.get(url, timeout=0.5)
            break
        except httpx.HTTPError:
            time.sleep(0.2)
    yield {"url": url, "runs": runs}
    proc.terminate()
    proc.wait(5)


def token(url, scope="docs:read", sub="test-agent", aud=None, ttl=300, secret=SECRET):
    now = int(time.time())
    return jwt.encode({"sub": sub, "scope": scope, "iat": now, "exp": now + ttl,
                       "iss": "http://127.0.0.1:9000", "aud": aud or url}, secret, algorithm="HS256")


def call(url, tok, tool, args=None):
    async def go():
        async with create_mcp_http_client(headers={"Authorization": f"Bearer {tok}"}) as http:
            async with streamable_http_client(url, http_client=http) as (r, w):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    return await s.call_tool(tool, args or {})
    return anyio.run(go)


def payload(result):
    if result.structured_content is not None:
        sc = result.structured_content
        return sc.get("result", sc)
    return json.loads(result.content[0].text)


def post_init(url, headers):
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "0"}}}
    return httpx.post(url, json=body, headers={"Accept": "application/json, text/event-stream", **headers})


def test_no_token_is_401_with_resource_metadata(server):
    r = post_init(server["url"], {})
    assert r.status_code == 401
    assert "resource_metadata" in r.headers.get("www-authenticate", "")


def test_wrong_audience_is_rejected(server):
    r = post_init(server["url"], {"Authorization": f"Bearer {token(server['url'], aud='http://other/mcp')}"})
    assert r.status_code == 401


def test_expired_and_forged_tokens_are_rejected(server):
    for bad in (token(server["url"], ttl=-10), token(server["url"], secret="x" * 40)):
        assert post_init(server["url"], {"Authorization": f"Bearer {bad}"}).status_code == 401


def test_read_scope_lists_and_reads(server):
    tok = token(server["url"])
    docs = payload(call(server["url"], tok, "list_documents"))
    assert {d["doc_id"] for d in docs} >= {"ground-lease-parcel-7", "nda-form-a-mutual"}
    page = payload(call(server["url"], tok, "read_page", {"doc_id": "ground-lease-parcel-7", "page": 2}))
    assert "$2,150 per acre" in page["text"]


def test_compare_finds_redline_changes(server):
    tok = token(server["url"])
    changes = payload(call(server["url"], tok, "compare_documents",
                           {"base_id": "ground-lease-parcel-7", "revised_id": "ground-lease-parcel-7-redline-v2"}))
    added = " ".join(s for c in changes for s in c["added"])
    assert "$600 per acre" in added and "$6,500,000" in added
    assert {c["page"] for c in changes} == {2, 3, 4}


def test_write_tool_needs_file_scope(server):
    res = call(server["url"], token(server["url"]), "file_document",
               {"doc_id": "nda-form-a-mutual", "folder": "diligence/ndas"})
    assert res.is_error and "docs:file" in res.content[0].text
    ok = call(server["url"], token(server["url"], scope="docs:read docs:file"), "file_document",
              {"doc_id": "nda-form-a-mutual", "folder": "diligence/ndas"})
    assert not ok.is_error


def test_unknown_doc_and_bad_folder_rejected(server):
    tok = token(server["url"], scope="docs:read docs:file")
    assert call(server["url"], tok, "read_page", {"doc_id": "../../etc/passwd", "page": 1}).is_error
    assert call(server["url"], tok, "file_document", {"doc_id": "nda-form-a-mutual", "folder": "/tmp"}).is_error


def test_audit_log_records_caller_and_outcome(server):
    lines = [json.loads(l) for l in (server["runs"] / "audit.jsonl").read_text().splitlines()]
    assert all(l["client"] == "test-agent" for l in lines)
    assert any(l["tool"] == "file_document" and l["outcome"].startswith("error: forbidden") for l in lines)
    assert not any("$2,150" in json.dumps(l) for l in lines), "document text leaked into the audit log"
