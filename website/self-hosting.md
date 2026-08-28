---
layout: default
title: Self-hosting & Deployment
nav_order: 10
---

# Self-hosting & Deployment
{: .no_toc }

Docker setup, environment variables, and running repowise in production or CI.
{: .fs-6 .fw-300 }

---

## Table of contents
{: .no_toc .text-delta }

1. TOC
{:toc}

---

## Docker (quickstart)

The Docker image bundles the Python backend, the Next.js frontend, and all dependencies. No Node.js required on the host.

```bash
docker pull ghcr.io/repowise-dev/repowise:latest

docker run \
  -p 7337:7337 \
  -p 3000:3000 \
  -v $(pwd):/repo \
  -v $(pwd)/.repowise:/data \
  -e ANTHROPIC_API_KEY="sk-ant-..." \
  ghcr.io/repowise-dev/repowise:latest \
  serve /repo
```

Open [http://localhost:3000](http://localhost:3000) to access the dashboard.

---

## Docker Compose

A full setup with the API server, web UI, and a PostgreSQL database:

```yaml
# docker-compose.yml
services:
  db:
    image: postgres:16
    environment:
      POSTGRES_DB: repowise
      POSTGRES_USER: repowise
      POSTGRES_PASSWORD: repowise
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U repowise"]
      interval: 5s
      timeout: 5s
      retries: 5

  repowise:
    image: ghcr.io/repowise-dev/repowise:latest
    ports:
      - "7337:7337"
      - "3000:3000"
    volumes:
      - ./:/repo
      - lancedb_data:/data/lancedb
    environment:
      REPOWISE_DB_URL: postgresql+asyncpg://repowise:repowise@db:5432/repowise
      REPOWISE_HOST: 0.0.0.0
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
    depends_on:
      db:
        condition: service_healthy
    command: serve /repo --host 0.0.0.0

volumes:
  postgres_data:
  lancedb_data:
```

```bash
ANTHROPIC_API_KEY=sk-ant-... docker compose up
```

---

## Adding repositories on a remote server

Registering a repository by local path assumes the checkout is on the same
filesystem as the server — true on a laptop, never true on a container host.
On a hosted deployment, add repositories by URL instead and let the server
clone them:

```bash
# Start the clone. Returns immediately with a handle.
curl -X POST https://your-instance/api/repos/remote \
  -H "Authorization: Bearer $REPOWISE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://github.com/org/repo"}'

# Poll it — a clone outlives an HTTP request.
curl https://your-instance/api/repos/remote/$CLONE_ID \
  -H "Authorization: Bearer $REPOWISE_API_KEY"
```

Once `repo_id` appears on the handle, index it with
`POST /api/repos/{repo_id}/index`. The dashboard's **Add Repository → From
URL** tab does all of this for you, including the cost estimate before any
LLM spend.

The URL may be a full `https://` URL, GitHub's `git@host:owner/repo.git` ssh
form, or bare `owner/repo` shorthand. Only `https` is cloned — ssh would need
key material the server does not manage.

**Private repositories** take an `access_token` (a GitHub PAT or equivalent).
It is used for that clone and then discarded: it is never written to disk, to
the database, or into the cloned `.git/config`, and never appears in an API
response or in the logs. Re-cloning a private repository asks for it again.

{: .note }
Clones are full, not shallow. repowise derives ownership and churn from
`git log`, so `--depth 1` would silently degrade that analysis rather than
fail loudly. Set `REPOWISE_REPOS_DIR` to somewhere persistent — the Docker
image defaults it to `/data/repos` on the data volume — or every redeploy
re-clones from scratch.

---

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | — | Anthropic API key |
| `OPENAI_API_KEY` | — | OpenAI API key |
| `GEMINI_API_KEY` | — | Google Gemini API key |
| `REPOWISE_PROVIDER` | auto | Override provider detection |
| `REPOWISE_DB_URL` | SQLite (`.repowise/wiki.db`) | PostgreSQL connection string |
| `REPOWISE_HOST` | `127.0.0.1` | API server bind host |
| `REPOWISE_API_KEY` | — | Bearer token for the API. Required to serve callers on other hosts; without it requests from off-host are refused |
| `REPOWISE_PORT` | `7337` | API server port |
| `REPOWISE_REPOS_DIR` | `~/.repowise/repos` | Where repositories added by URL are cloned. Put it on persistent storage |
| `REPOWISE_MCP_PORT` | `7338` | MCP SSE server port |

---

## Building from source

```bash
git clone https://github.com/repowise-dev/repowise.git
cd repowise

# Python dependencies
uv sync --all-extras

# Web frontend
npm install
npm run build

# Run
repowise serve
```

---

## Running in CI

Use `--no-prose` for CI pipelines that don't need model-written prose, just the analysis signals and a structural wiki:

```yaml
# GitHub Actions example
- name: Index codebase
  run: |
    pip install repowise
    repowise init --no-prose --yes

- name: Check dead code
  run: repowise dead-code --safe-only --format json > dead-code-report.json

- name: Upload report
  uses: actions/upload-artifact@v4
  with:
    name: dead-code-report
    path: dead-code-report.json
```

For full documentation generation in CI (with LLM):

```yaml
- name: Generate docs
  env:
    ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
  run: |
    pip install repowise
    repowise init --yes --concurrency 10

- name: Export to markdown
  run: repowise export --format markdown --output ./wiki-export
```

---

## Keeping the index current in CI

Update the wiki on every push to main:

```yaml
on:
  push:
    branches: [main]

jobs:
  update-wiki:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0   # Full history for git analysis

      - name: Restore index cache
        uses: actions/cache@v4
        with:
          path: .repowise
          key: repowise-${{ github.sha }}
          restore-keys: repowise-

      - name: Update wiki
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: |
          pip install repowise
          repowise update --yes
```

{: .note }
Use `fetch-depth: 0` to include full git history. repowise uses git log for ownership and churn analysis — shallow clones will produce incomplete results.

---

## Reverse proxy (nginx)

To serve the dashboard at a custom domain:

```nginx
server {
    listen 80;
    server_name docs.yourcompany.com;

    location / {
        proxy_pass http://localhost:3000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location /api/ {
        proxy_pass http://localhost:7337/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

---

## Database migrations

repowise uses Alembic for PostgreSQL schema management. Migrations run automatically at startup. To run them manually:

```bash
cd packages/server
alembic upgrade head
```

---

## Requirements summary

| Component | Requirement |
|-----------|-------------|
| Python | 3.11+ |
| Git | Any recent version |
| Node.js | 20+ (for web UI without Docker) |
| Docker | Any version (alternative to Node.js) |
| PostgreSQL | 14+ (optional, SQLite is the default) |
| LLM API key | Anthropic, OpenAI, Gemini, or Ollama (not needed for `--no-prose`) |
