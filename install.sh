#!/bin/sh
# MCP-Host build for Replit. Always exit 0 so a transient failure doesn't kill the deploy
# (matches edgar-rag's hardened install pattern).
set -u

echo "[install] installing runtime deps"
pip install --no-cache-dir -r deps.txt 2>/dev/null \
  || python3 -m pip install --no-cache-dir -r deps.txt 2>/dev/null \
  || echo "[install] pip install reported errors; continuing"

# Sanity import; retry once on failure.
python3 -c "import fastapi, uvicorn, pydantic" 2>/dev/null \
  || python3 -m pip install --no-cache-dir -r deps.txt 2>/dev/null \
  || echo "[install] dependency import still failing; check deps.txt pins"

# Provider artifacts (vectors/blobs) are pulled at deploy time from object store / GitHub
# Releases into per-provider mounts, OR pushed later via the upload API. Nothing to fetch here
# for the embedded-corpus dev build.

echo "[install] done"
exit 0
