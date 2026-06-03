#!/usr/bin/env bash
# Bring up the MongoDB MCP server on 127.0.0.1:8088, then uvicorn on $PORT.
# Cloud Run sends SIGTERM to PID 1; tini propagates it; this script's trap
# makes sure both processes terminate cleanly.
set -euo pipefail

# Atlas connection string. We accept either the canonical MCP env var or
# the MongoDB driver env var; the MCP server only honors the former.
MCP_CONN="${MDB_MCP_CONNECTION_STRING:-${MONGODB_URI:-}}"
if [[ -z "$MCP_CONN" ]]; then
  echo "FATAL: MDB_MCP_CONNECTION_STRING (or MONGODB_URI) must be set." >&2
  exit 2
fi
export MDB_MCP_CONNECTION_STRING="$MCP_CONN"

echo "[entrypoint] launching MongoDB MCP server on 127.0.0.1:8088 (conn-len=${#MCP_CONN})"
MDB_MCP_CONNECTION_STRING="$MCP_CONN" \
mongodb-mcp-server \
  --transport http \
  --httpHost 127.0.0.1 \
  --httpPort 8088 &
MCP_PID=$!

shutdown() {
  echo "[entrypoint] caught signal — stopping MCP (pid=$MCP_PID)"
  kill -TERM "$MCP_PID" 2>/dev/null || true
}
trap shutdown TERM INT

# Wait for the MCP server to start listening before we accept HTTP traffic;
# otherwise the first agent invoke fails with connection-refused.
for _ in $(seq 1 30); do
  if (echo > /dev/tcp/127.0.0.1/8088) 2>/dev/null; then
    echo "[entrypoint] MCP ready"
    break
  fi
  sleep 0.5
done

echo "[entrypoint] launching uvicorn on 0.0.0.0:${PORT}"
exec uvicorn main:app \
  --app-dir /app/backend \
  --host 0.0.0.0 \
  --port "${PORT}"
