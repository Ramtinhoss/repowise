# =============================================================================
# repowise — Railway deployment image
# =============================================================================
# Derived from docker/Dockerfile, with three deployment-specific changes:
#
#   1. NEXT_PUBLIC_REPOWISE_API_URL is empty at build time, so the browser
#      bundle issues same-origin requests. Railway publishes exactly one port
#      per service, so the backend (7337) is not reachable from the internet —
#      all browser traffic must go through the Next.js server on 3000, whose
#      middleware rewrites /api/*, /health and /metrics to the local backend.
#
#   2. A wrapper entrypoint fixes ownership of the Railway volume mounted at
#      /data (volumes attach as root) before dropping to the non-root user,
#      and maps Railway's injected $PORT onto PORT_FRONTEND.
#
#   3. The postgres extra is installed so REPOWISE_DB_URL can point at a
#      Railway PostgreSQL service without rebuilding the image.
# =============================================================================

FROM node:20-bookworm-slim AS frontend-builder

WORKDIR /app

COPY package.json package-lock.json* ./

COPY packages/web/package.json        ./packages/web/package.json
COPY packages/types/package.json      ./packages/types/package.json
COPY packages/ui/package.json         ./packages/ui/package.json
COPY packages/api-client/package.json ./packages/api-client/package.json

WORKDIR /app/packages/web
RUN npm install --production=false

WORKDIR /app
COPY packages/ ./packages/

WORKDIR /app/packages/web
ENV NEXT_TELEMETRY_DISABLED=1
# Empty => relative URLs from the browser, proxied by src/middleware.ts.
ENV NEXT_PUBLIC_REPOWISE_API_URL=""
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2: Python backend + frontend runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=frontend-builder /usr/local/bin/node /usr/local/bin/node
COPY --from=frontend-builder /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm

WORKDIR /app

COPY pyproject.toml README.md LICENSE MANIFEST.in ./
COPY packages/core/ packages/core/
COPY packages/cli/ packages/cli/
COPY packages/server/ packages/server/
RUN pip install --no-cache-dir ".[postgres]"

COPY --from=frontend-builder /app/packages/web/.next/standalone /app/web
COPY --from=frontend-builder /app/packages/web/.next/static /app/web/packages/web/.next/static
COPY --from=frontend-builder /app/packages/web/public /app/web/packages/web/public

RUN mkdir -p /data

ENV REPOWISE_DB_URL=sqlite+aiosqlite:////data/wiki.db
ENV LANCEDB_PATH=/data/lancedb
ENV GRAPH_PATH=/data/graphs
# Repositories added by URL are cloned here. On the mounted volume, so a
# redeploy does not discard every checkout the instance has indexed.
ENV REPOWISE_REPOS_DIR=/data/repos
ENV REPOWISE_EMBEDDER=mock
ENV PORT_BACKEND=7337
ENV PORT_FRONTEND=3000
ENV HOSTNAME=0.0.0.0

EXPOSE 3000

COPY docker/entrypoint.sh /app/entrypoint.sh
COPY docker/railway-entrypoint.sh /app/railway-entrypoint.sh
RUN chmod +x /app/entrypoint.sh /app/railway-entrypoint.sh

RUN groupadd -r repowise && useradd -r -g repowise -d /app -s /sbin/nologin repowise \
    && chown -R repowise:repowise /app /data

# Starts as root only long enough to chown the mounted volume, then execs the
# real entrypoint as the unprivileged repowise user.
ENTRYPOINT ["/app/railway-entrypoint.sh"]
