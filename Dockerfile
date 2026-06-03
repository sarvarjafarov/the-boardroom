# The Boardroom — single-container image for Cloud Run / any container host.
#
# Bundles:
#   - Python 3.13 + FastAPI + ADK + Gemini SDK
#   - Node 22 (for the MongoDB MCP server `mongodb-mcp-server`)
#   - ffmpeg + yt-dlp (for the YouTube Live source extractor)
#   - Static frontend at /
#
# A tiny entrypoint script starts the MongoDB MCP server first (binding to
# loopback) and then exec's uvicorn so SIGTERM from Cloud Run kills both.
FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NODE_VERSION=22.x

# System deps: Node (for the MCP server) + ffmpeg (for YouTube Live audio)
# + curl/ca-certificates (for the install) + tini (PID 1).
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates tini gnupg ffmpeg \
 && curl -fsSL "https://deb.nodesource.com/setup_${NODE_VERSION}" | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/* \
 && node --version && npm --version && ffmpeg -version | head -1

WORKDIR /app

# Python deps first (better layer caching).
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install -r /app/backend/requirements.txt

# Pre-fetch the MongoDB MCP server so cold starts don't pay the npm install cost.
RUN npm install -g mongodb-mcp-server@1.11.0

# Application code.
COPY backend /app/backend
COPY frontend /app/frontend
COPY scripts /app/scripts
COPY seed_data /app/seed_data

# Default port Cloud Run sends traffic to.
ENV PORT=8080 \
    MONGODB_MCP_URL=http://127.0.0.1:8088 \
    MONGODB_DB=boardroom

# Supervisor: start the MCP server in the background, then uvicorn in the
# foreground. tini reaps zombies and propagates SIGTERM.
RUN chmod +x /app/scripts/entrypoint.sh

EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--", "/app/scripts/entrypoint.sh"]
