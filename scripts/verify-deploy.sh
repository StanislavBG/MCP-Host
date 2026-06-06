#!/usr/bin/env bash
# Post-deploy health-watch: poll production /health until it reports the expected version,
# then assert the durable backend. Run this AFTER clicking Redeploy in the Replit Deploy pane
# (this script cannot trigger the deploy — that step is human-gated).
#
# Usage: scripts/verify-deploy.sh [VERSION]
#   VERSION defaults to __version__ in mcp_host/__init__.py.
set -uo pipefail
cd "$(git rev-parse --show-toplevel)" 2>/dev/null || true

URL="https://mcp-host.replit.app/health"
want="${1:-}"
if [ -z "$want" ]; then
  want=$(sed -nE 's/^__version__ = "(.*)"/\1/p' mcp_host/__init__.py)
fi

_field() {  # extract a top-level JSON string field from stdin; empty on any error
  python3 -c "import sys,json
try: print(json.load(sys.stdin).get('$1',''))
except Exception: print('')" 2>/dev/null
}

echo "[verify-deploy] waiting for $URL to report version $want ..."
for _ in $(seq 1 80); do
  body=$(curl -s -m 10 "$URL" 2>/dev/null)
  v=$(printf '%s' "$body" | _field version)
  if [ "$v" = "$want" ]; then
    backend=$(printf '%s' "$body" | _field backend)
    echo "DEPLOY OK v$want (backend: $backend)"
    printf '%s' "$body" | python3 -m json.tool
    case "$backend" in
      sqlite-file*) exit 0 ;;
      *) echo "WARNING: backend '$backend' — expected sqlite-file (durable keys). NOT a clean success."; exit 2 ;;
    esac
  fi
  sleep 30
done
echo "TIMEOUT (last seen version: ${v:-unreachable})"
exit 1
