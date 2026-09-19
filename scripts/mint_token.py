"""Stand-in for the authorization server: mint a short-lived, audience-bound token.

    python scripts/mint_token.py --sub diligence-agent --scope "docs:read" --ttl 900

In production the IdP (Entra ID, Okta, Auth0) issues this token via OAuth; the server-side
checks in server/docs_mcp.py (signature, exp, iss, aud, scopes) stay the same.
"""
import argparse
import os
import time

import jwt

ap = argparse.ArgumentParser()
ap.add_argument("--sub", required=True)
ap.add_argument("--scope", default="docs:read")
ap.add_argument("--ttl", type=int, default=900)
a = ap.parse_args()

host, port = os.environ.get("MCP_HOST", "127.0.0.1"), os.environ.get("MCP_PORT", "8765")
now = int(time.time())
print(jwt.encode({
    "sub": a.sub, "scope": a.scope, "iat": now, "exp": now + a.ttl,
    "iss": os.environ.get("MCP_ISSUER_URL", "http://127.0.0.1:9000"),
    "aud": os.environ.get("MCP_RESOURCE_URL", f"http://{host}:{port}/mcp"),
}, os.environ["MCP_JWT_SECRET"], algorithm="HS256"))
