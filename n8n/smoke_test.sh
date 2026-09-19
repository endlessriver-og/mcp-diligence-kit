#!/usr/bin/env bash
# End-to-end check of the n8n side without an LLM: a throwaway self-hosted n8n imports both
# workflows and credentials, then runs the health check against the live MCP server twice —
# once with a valid token (expect success) and once after rotating the server secret
# (expect the error branch). Nothing touches your real n8n; everything lives in $WORK.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
WORK=${WORK:-$(mktemp -d)}
N8N=${N8N:-"npx --yes n8n@2.37.10"}
export N8N_USER_FOLDER="$WORK/n8n-home" N8N_RUNNERS_ENABLED=false N8N_DIAGNOSTICS_ENABLED=false \
       N8N_ENCRYPTION_KEY=smoke-test-key-not-secret
mkdir -p "$N8N_USER_FOLDER" "$WORK/wf"

start_server() { MCP_JWT_SECRET="$1" AUDIT_LOG="$WORK/audit.jsonl" FILED_LOG="$WORK/filed.jsonl" \
                 $PY server/docs_mcp.py & echo $! > "$WORK/server.pid"; sleep 2; }
stop_server()  { kill "$(cat "$WORK/server.pid")" 2>/dev/null || true; sleep 1; }
trap stop_server EXIT

SECRET=$($PY -c 'import secrets; print(secrets.token_hex(32))')
start_server "$SECRET"
TOKEN=$(MCP_JWT_SECRET=$SECRET $PY scripts/mint_token.py --sub n8n-healthcheck --scope docs:read --ttl 3600)

cat > "$WORK/creds.json" <<EOF
[{"id":"dlgMcpBearer01","name":"Diligence MCP (docs:read)","type":"httpBearerAuth","data":{"token":"$TOKEN"}},
 {"id":"dlgWebhookTok01","name":"Diligence webhook token","type":"httpHeaderAuth","data":{"name":"X-Webhook-Token","value":"$(openssl rand -hex 16)"}},
 {"id":"dlgAnthropic01","name":"Anthropic","type":"anthropicApi","data":{"apiKey":"${ANTHROPIC_API_KEY:-sk-ant-placeholder}"}}]
EOF
cp n8n/diligence-*.json "$WORK/wf/"
$N8N import:credentials --input="$WORK/creds.json" 2>&1 | tail -1
$N8N import:workflow --separate --input="$WORK/wf" 2>&1 | tail -1
ID=$($N8N list:workflow 2>/dev/null | grep healthcheck | cut -d'|' -f1)

echo "--- run 1: valid token (expect success)"
$N8N execute --id="$ID" --rawOutput > "$WORK/run1.txt" 2>&1 || true
grep -q '"ground-lease-parcel-7"' "$WORK/run1.txt" && grep -q '"lastNodeExecuted": "Healthy"' "$WORK/run1.txt" \
  && echo "PASS: n8n listed documents over MCP and reached Healthy" || { echo FAIL; cat "$WORK/run1.txt"; exit 1; }

echo "--- run 2: server secret rotated (expect error branch)"
stop_server; start_server "$($PY -c 'import secrets; print(secrets.token_hex(32))')"
$N8N execute --id="$ID" --rawOutput > "$WORK/run2.txt" 2>&1 || true
grep -q "Diligence MCP unreachable or unauthorized" "$WORK/run2.txt" && echo "PASS: error branch fired" || { echo FAIL; tail -30 "$WORK/run2.txt"; exit 1; }

echo "--- audit log (server side)"
cat "$WORK/audit.jsonl"
