# Backend Compose Deployment Design

**Date:** 2026-08-31
**Status:** Approved for implementation
**Target:** `/home/aicreator/apps/travel-agent-backend` and `/home/aicreator/apps/travel-agent-infra`

## Context

The CentOS 7 host has a custom `/usr/local/bin/python3.12` installation that was
built without the `_ssl` extension. Its system OpenSSL is 1.0.2, so neither pip
over HTTPS nor the backend's DashScope and LLM HTTPS calls can work reliably on
that interpreter. The host already runs MySQL and Qdrant successfully through
Docker Compose, and it can pull images from GHCR.

The verified replacement runtime is:

```text
ghcr.io/astral-sh/uv:0.9.5-python3.12-bookworm
manifest digest: sha256:19a8c92b461bbc32e8bd30c15132cec1d16c49c61f4359c9225262938485f513
Python 3.12.12
OpenSSL 3.0.17
```

The backend must join the existing Compose project, remain reachable only on
the server loopback interface, and keep all credentials and service-specific
runtime configuration out of Git.

## Goals

- Run the backend with a complete, reproducible Python 3.12 and SSL runtime.
- Manage the backend, MySQL, and Qdrant from one Compose project.
- Use Compose service discovery for backend-to-MySQL and backend-to-Qdrant
  traffic.
- Keep secrets and server-specific settings outside the repository and image.
- Apply MySQL schema migrations and provision the Qdrant collection safely
  before enabling sharing or RAG.
- Preserve the existing MySQL and Qdrant named volumes.
- Support staged rollout, health verification, and rollback without deleting
  business data.

## Non-goals

- Exposing port 8000 directly to the LAN or public internet.
- Adding Nginx, TLS termination, Redis, or a container registry publishing
  pipeline in this change.
- Automatically running Alembic migrations whenever the backend starts.
- Automatically deleting or recreating an incompatible Qdrant collection.
- Running the full pytest suite as part of the server deployment procedure.

## Approaches considered

### 1. Versioned generic image build with server-only runtime configuration

Add a generic `Dockerfile`, `.dockerignore`, and bounded provisioning/readiness
scripts to the repository. Keep `compose.yaml`, `infra.env`, and `backend.env`
only under `/home/aicreator/apps/travel-agent-infra`.

This is the selected approach because the image build is reviewable and
reproducible while service addresses, passwords, and API keys remain outside
Git.

### 2. Keep the Dockerfile only on the server

This avoids repository changes but makes the build process unversioned and
harder to reproduce or review. Server loss or manual drift could change the
runtime without a corresponding code change.

### 3. Bind-mount source and install dependencies at every start

This minimizes initial files but makes startup depend on the package index,
increases restart time, and permits dependency drift. It is unsuitable for the
long-running deployment.

## Repository-owned components

The implementation will add the following non-secret, environment-neutral
files to Git:

- `Dockerfile`: builds the backend image from the pinned GHCR Python runtime,
  installs `requirements.txt`, copies only required application files, and
  launches Uvicorn on container port 8000.
- `.dockerignore`: excludes `.git`, `.env`, `.env.local`, `.env*` variants,
  `.venv`, caches, logs, test artifacts, local data, and IDE files from the
  Docker build context.
- `scripts/wait_for_dependencies.py`: uses only the Python standard library to
  wait for MySQL TCP connectivity and Qdrant `/readyz` before Uvicorn imports
  `main.py`.
- `scripts/ensure_shared_guide_collection.py`: idempotently creates the
  configured Qdrant collection when absent and validates its schema when
  present. It never deletes or recreates a mismatched collection.

The image build must not copy repository environment files even if a developer
has them locally. The image contains no MySQL password, JWT secret, DashScope
key, LLM key, workspace URL, or server IP address.

## Server-owned components

The following files remain only in
`/home/aicreator/apps/travel-agent-infra` and are not committed:

- `compose.yaml`: declares `mysql`, `qdrant`, and `backend` services.
- `infra.env`: provides Compose interpolation values required by the MySQL
  container.
- `backend.env`: provides backend runtime environment variables.

Both environment files use mode `0600`. `backend.env` includes the existing
LLM and map-provider settings plus the following deployment settings:

```text
DATABASE_BACKEND=mysql
MYSQL_HOST=mysql
MYSQL_PORT=3306
MYSQL_DATABASE=travel_agent
MYSQL_USER=travel_agent_app

QDRANT_URL=http://qdrant:6333
QDRANT_COLLECTION=shared_guide_embeddings_v1

EMBEDDING_MODEL=qwen3.7-text-embedding
EMBEDDING_DIMENSION=768

SHARE_SQUARE_ENABLED=false
RAG_ENABLED=false
```

`MYSQL_PASSWORD`, `JWT_SECRET_KEY`, `QDRANT_API_KEY`, `DASHSCOPE_API_KEY`, and
`DASHSCOPE_BASE_URL` are populated only in the server file. The JWT secret is
at least 32 characters. `QDRANT_API_KEY` may remain empty only for the current
host-local deployment; the other listed values are required before their
corresponding runtime features are enabled.

## Network and port model

Compose creates one project-scoped user-defined bridge network and attaches all
three services. Docker DNS resolves the Compose service names, so the backend
uses `mysql:3306` and `qdrant:6333` internally.

Only the backend API is published for host access, and it is restricted to the
loopback interface:

```text
127.0.0.1:8000:8000
```

The existing loopback-only MySQL and Qdrant publications may remain for host
diagnostics. Container-to-container traffic does not use those published host
ports.

## Container lifecycle and readiness

The backend uses `restart: unless-stopped` and a `768m` memory limit. The current
MySQL and Qdrant limits remain at `1g` each. This keeps the declared maximums
within a reasonable envelope for the host's 3.7 GiB RAM and 4 GiB swap while
leaving capacity for Docker and the operating system.

Before Uvicorn imports the application, the readiness script performs bounded
checks for up to 60 seconds:

1. Open a TCP connection to `MYSQL_HOST:MYSQL_PORT`.
2. Request `${QDRANT_URL}/readyz` and require a successful response.

It prints only service names and sanitized status. It never prints URLs with
credentials, passwords, keys, provider response bodies, or application data.
If readiness is not reached, the process exits nonzero. Compose restarts the
container and retries from a clean process, preventing a one-time Qdrant race
from leaving `RagRuntime` permanently degraded.

The backend healthcheck calls `http://127.0.0.1:8000/api/health` from inside the
container. The health endpoint does not make a DashScope embedding request.

## Database migration

Alembic is executed as an explicit one-off Compose command after MySQL is
healthy and before the backend is started. It is not part of the container
entrypoint.

For the current release, `alembic upgrade head` creates the sharing and indexing
schema, including:

- `shared_guides`
- `shared_guide_likes`
- `share_index_jobs`
- their foreign keys, uniqueness constraints, and retrieval/job indexes

The expected current revision is:

```text
f4c2a81d9e30 (head)
```

A failed migration aborts deployment. Operational rollback disables features
and rolls back the backend image; it does not run `alembic downgrade`, because
that downgrade would drop the sharing tables.

## Qdrant collection provisioning

The provisioning script runs after Qdrant is ready and before maintenance
dry-runs. It accepts only the configured explicit versioned collection name.
For V1, it requires one unnamed vector with:

```text
dimension: 768
distance: Cosine
```

If the collection is absent, the script creates it and validates the result. If
it exists, the script validates it without mutation. A mismatch exits nonzero
and blocks rollout. This closes the first-deployment gap where reindex and
reconcile intentionally require an existing collection but do not create one.

## First deployment sequence

The first deployment is intentionally staged rather than using one immediate
`docker compose up -d`:

1. Validate Compose configuration without printing resolved secrets.
2. Build the backend image.
3. Start MySQL and Qdrant, verify MySQL container health, and verify Qdrant's
   `/readyz` endpoint.
4. Run `alembic upgrade head`, then verify `alembic current` is
   `f4c2a81d9e30`.
5. Run the idempotent Qdrant collection provisioning script.
6. Start the backend with sharing and RAG disabled.
7. Verify `/api/health` and confirm port 8000 listens only on `127.0.0.1`.
8. Run reindex and reconcile in dry-run mode.
9. Set `SHARE_SQUARE_ENABLED=true`, recreate only the backend container, and
   verify one authenticated publication and public read.
10. Set `RAG_ENABLED=true`, recreate only the backend container, and verify RAG
    health and retrieval behavior.

After initial acceptance, normal server startup uses `docker compose up -d`.
Changing an `env_file` value uses `docker compose up -d --force-recreate
backend`; `docker compose restart backend` is not used for environment changes
because restart may retain the old container environment.

## Failure handling

- A dependency readiness timeout exits the backend process and relies on the
  restart policy for a clean retry.
- A migration failure prevents backend rollout.
- A Qdrant schema mismatch prevents collection mutation and feature rollout.
- DashScope or Qdrant operation failures degrade only sharing/RAG boundaries;
  ordinary guide generation remains available where current fail-open behavior
  permits it.
- Logs contain sanitized error classes and state, not credentials, complete
  prompts, retrieval text, or provider response bodies.
- Operators inspect services independently with `docker compose ps`, `logs`,
  and `exec` even though lifecycle management is unified.

## Verification and acceptance

The required deployment checks are:

1. `docker compose config --quiet` succeeds.
2. The backend image builds from the pinned runtime.
3. Container Python imports `ssl`, reports Python 3.12 and OpenSSL 3, and can
   import the installed application dependencies.
4. MySQL reports healthy and Alembic reports revision `f4c2a81d9e30`.
5. Qdrant reports ready and the configured collection is 768-dimensional with
   cosine distance.
6. `GET http://127.0.0.1:8000/api/health` succeeds with explicit Qdrant, RAG,
   and embedding-configuration states.
7. The host listener for port 8000 is restricted to `127.0.0.1`.
8. Reindex and reconcile dry-runs complete without schema mismatch.
9. Sharing is enabled first and a publication/read flow is verified.
10. RAG is enabled second and retrieval is verified against an indexed public
    guide.

Per the deployment constraint, implementation does not automatically run the
full pytest suite. Verification is limited to static/build checks and the
server-side migration and runtime smoke checks above.

## Rollback

1. Set `RAG_ENABLED=false` and `SHARE_SQUARE_ENABLED=false` in `backend.env`.
2. Recreate only the backend container so it receives the new environment.
3. If code rollback is required, check out the previous approved commit and
   rebuild only the backend image/container.
4. Preserve MySQL rows, the Alembic revision, the Qdrant collection, and all
   named volumes.
5. Never use `docker compose down -v` as an application rollback operation.

## Acceptance criteria

The design is complete when the implementation provides a reproducible backend
image, one Compose-managed internal network, server-only secret injection,
bounded dependency readiness, explicit Alembic and Qdrant provisioning steps,
loopback-only API exposure, staged feature enablement, sanitized failure
reporting, and a non-destructive rollback path.
