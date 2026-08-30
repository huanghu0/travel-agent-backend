# Shared-guide RAG operations

This runbook treats MySQL as the business source of truth and Qdrant as a disposable, rebuildable index. Maintenance commands only operate on the configured `QDRANT_COLLECTION`. They reject blank, placeholder, and non-versioned collection names, and they never delete or recreate a collection.

Both commands validate the existing collection schema through Qdrant's public
API before reporting or applying work: v1 requires one unnamed 768-dimensional
vector with cosine distance. A mismatch is reported in dry-run and aborts apply
before any point or MySQL mutation.

## Local setup and readiness

Install the pinned Python dependencies and start the pinned Qdrant container:

```powershell
python -m pip install -r requirements.txt
docker compose -f docker-compose.qdrant.yml up -d
Invoke-WebRequest http://127.0.0.1:6333/readyz
```

Stop it without deleting its named volume:

```powershell
docker compose -f docker-compose.qdrant.yml stop
```

## CentOS 7 Compose deployment

The production host keeps its active Compose and environment files outside this
repository in `/home/aicreator/apps/travel-agent-infra`. Do not copy those files
into the backend checkout or add them to Git. Keep `infra.env` and `backend.env`
at mode `0600`.

Add only this `backend` service to the existing `services:` mapping in the
server's `compose.yaml`; leave the existing MySQL/Qdrant services and named
volumes unchanged:

```yaml
  backend:
    build:
      context: ../travel-agent-backend
      dockerfile: Dockerfile
    image: travel-agent-backend:rag
    container_name: travel-agent-backend
    restart: unless-stopped
    env_file:
      - ./backend.env
    depends_on:
      mysql:
        condition: service_healthy
      qdrant:
        condition: service_started
    ports:
      - "127.0.0.1:8000:8000"
    mem_limit: 768m
    healthcheck:
      test:
        - CMD
        - python
        - -c
        - import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3).read(1)
      interval: 10s
      timeout: 5s
      retries: 6
      start_period: 30s
```

`backend.env` must retain the approved LLM, map-provider, authentication, and
other application settings and must include these deployment values:

```text
DATABASE_BACKEND=mysql
MYSQL_HOST=mysql
MYSQL_PORT=3306
MYSQL_DATABASE=travel_agent
MYSQL_USER=travel_agent_app

QDRANT_URL=http://qdrant:6333
QDRANT_COLLECTION=shared_guide_embeddings_v1
QDRANT_TIMEOUT_SECONDS=5

EMBEDDING_MODEL=qwen3.7-text-embedding
EMBEDDING_DIMENSION=768

REDIS_ENABLED=false
SHARE_SQUARE_ENABLED=false
RAG_ENABLED=false
DEPENDENCY_WAIT_TIMEOUT_SECONDS=60
```

Populate `MYSQL_PASSWORD`, `JWT_SECRET_KEY`, `DASHSCOPE_API_KEY`,
`DASHSCOPE_BASE_URL`, and the active LLM/map-provider credentials only in the
server file. `JWT_SECRET_KEY` must contain at least 32 characters. The current
host-local Qdrant deployment may keep `QDRANT_API_KEY` empty.

Validate, build, and start dependencies without printing resolved environment
values:

```bash
cd /home/aicreator/apps/travel-agent-infra
chmod 600 infra.env backend.env
sudo docker compose --env-file ./infra.env -f ./compose.yaml config --quiet
sudo docker compose --env-file ./infra.env -f ./compose.yaml build backend
sudo docker compose --env-file ./infra.env -f ./compose.yaml up -d mysql qdrant
sudo docker compose --env-file ./infra.env -f ./compose.yaml ps
curl -fsS http://127.0.0.1:6333/readyz
```

Run migrations and provision Qdrant before starting the backend:

```bash
sudo docker compose --env-file ./infra.env -f ./compose.yaml run --rm backend alembic upgrade head
sudo docker compose --env-file ./infra.env -f ./compose.yaml run --rm backend alembic current
sudo docker compose --env-file ./infra.env -f ./compose.yaml run --rm backend python scripts/ensure_shared_guide_collection.py
```

`alembic current` must report `f4c2a81d9e30 (head)`. Provisioning must report
`collection=shared_guide_embeddings_v1 dimension=768 distance=Cosine` and must
stop without mutation on an incompatible existing collection.

Start with both features disabled, then verify health and binding:

```bash
sudo docker compose --env-file ./infra.env -f ./compose.yaml up -d backend
sudo docker compose --env-file ./infra.env -f ./compose.yaml ps
curl -fsS http://127.0.0.1:8000/api/health
sudo ss -lntp | grep -E '127\.0\.0\.1:8000\b'
sudo docker compose --env-file ./infra.env -f ./compose.yaml run --rm backend python scripts/reindex_shared_guides.py
sudo docker compose --env-file ./infra.env -f ./compose.yaml run --rm backend python scripts/reconcile_shared_guide_index.py
```

Enable sharing first by changing only `SHARE_SQUARE_ENABLED=true`, then recreate
only the backend so it receives the updated environment:

```bash
sed -i 's/^SHARE_SQUARE_ENABLED=.*/SHARE_SQUARE_ENABLED=true/' backend.env
sudo docker compose --env-file ./infra.env -f ./compose.yaml up -d --force-recreate backend
curl -fsS http://127.0.0.1:8000/api/health
curl -fsS 'http://127.0.0.1:8000/api/shared-guides?limit=1'
```

Use an authenticated existing account and one owned completed trip session to
publish a guide through `POST /api/trip/sessions/{session_id}/share`; wait until
its owned projection reports `publication_status=PUBLIC` and
`index_status=READY`, then confirm it appears in the public list.

Enable RAG only after that publication is indexed:

```bash
sed -i 's/^RAG_ENABLED=.*/RAG_ENABLED=true/' backend.env
sudo docker compose --env-file ./infra.env -f ./compose.yaml up -d --force-recreate backend
curl -fsS http://127.0.0.1:8000/api/health
```

The health response must report Qdrant and RAG ready with
`embedding_configured=true`. Generate one same-city trip and confirm retrieval
uses the indexed public guide through the RAG metrics/response metadata.

After first-deployment acceptance, normal host startup uses:

```bash
sudo docker compose --env-file ./infra.env -f ./compose.yaml up -d
```

Continue to use `up -d --force-recreate backend` whenever `backend.env`
changes; a plain `restart backend` can retain the old container environment.

For rollback, disable both feature flags and recreate only the backend:

```bash
sed -i 's/^RAG_ENABLED=.*/RAG_ENABLED=false/' backend.env
sed -i 's/^SHARE_SQUARE_ENABLED=.*/SHARE_SQUARE_ENABLED=false/' backend.env
sudo docker compose --env-file ./infra.env -f ./compose.yaml up -d --force-recreate backend
curl -fsS http://127.0.0.1:8000/api/health
```

Keep the MySQL revision, Qdrant collection, and named volumes. If code rollback
is also required, check out the previous approved commit, rebuild only
`backend`, and recreate only that service. Do not run `alembic downgrade`, and
never use `docker compose down -v` as application rollback.

Production Qdrant must be reachable only on an internal network. Set the server-side `QDRANT__SERVICE__API_KEY` and pass the matching value to the backend as `QDRANT_API_KEY`. Keep both out of Compose files, source control, shell history, screenshots, and logs.

Before either feature flag is enabled, configure MySQL, Qdrant, and DashScope, then run:

```powershell
alembic upgrade head
alembic current
```

The expected migration revision is `f4c2a81d9e30`. `/api/health` must report explicit `qdrant`, `rag`, and `embedding_configured` states; the health probe does not call DashScope.

Use this readiness sequence:

1. Start Qdrant and verify `/readyz`.
2. Apply the Alembic migration and verify the current revision.
3. Keep `SHARE_SQUARE_ENABLED=false` and `RAG_ENABLED=false`.
4. Run both maintenance commands in their default dry-run mode.
5. Run `reindex_shared_guides.py --apply` and inspect its non-secret summary.
6. Run reconcile in dry-run, then use `--apply` if missing or stale points remain.
7. Enable `SHARE_SQUARE_ENABLED` and verify a test publication.
8. Enable `RAG_ENABLED` and verify health and retrieval metrics.

## Reindex and reconcile

Reindex is dry-run unless `--apply` is present:

```powershell
python scripts/reindex_shared_guides.py
python scripts/reindex_shared_guides.py --share-id 00000000-0000-0000-0000-000000000001
python scripts/reindex_shared_guides.py --apply --batch-size 100
```

Dry-run reads bounded `PUBLIC + READY` MySQL rows and regenerates canonical text/hash locally. It reads Qdrant only for schema readiness, does not call DashScope, and does not write MySQL or Qdrant. In apply mode, an unchanged hash rebuilds the point with the same `index_version` and leaves visibility unchanged. A changed hash goes through the existing transactional update/job state machine, increments `index_version`, hides the record while indexing, and republishes only after the compare-and-set completion succeeds. Processing continues after a per-record failure, and apply exits nonzero if any record failed.

Every maintenance upsert re-reads the exact MySQL record immediately before
the external write and after it. It must still be `PUBLIC + READY` with the
same share ID, version, content hash, and publication/index timestamps. A race
is skipped or reported; a stale point written before the race is removed with a
version-filtered delete and the current row is requeued when the durable store
can accept it. Changed-hash reindex additionally passes that identity into the
transactional `stage_update` compare-and-set; a newer authoritative row is
never overwritten. An authoritative DB read, guard, or recovery failure is a
real apply failure and produces a nonzero exit, rather than being counted as a
safe concurrent skip. Rows that are `PUBLIC + READY` but lack `published_at`
or `indexed_at` are reported as inconsistent failures rather than omitted.

Reconcile uses paginated MySQL reads and Qdrant scroll and reports missing, stale, and extra points separately:

```powershell
python scripts/reconcile_shared_guide_index.py
python scripts/reconcile_shared_guide_index.py --apply --batch-size 100
python scripts/reconcile_shared_guide_index.py --apply --delete-extra --batch-size 100
```

Missing and stale points are repaired only with `--apply`. Extra points are retained unless both `--apply` and `--delete-extra` are supplied. Reconcile preserves the physical Qdrant point ID returned by scroll, reports malformed payloads and payload/point-ID mismatches separately, and re-reads that physical point immediately before deletion. The delete request is also a server-side `FilterSelector` requiring that physical ID and the observed payload identity (`share_id`, `index_version`, `content_hash`, and observed `visibility` when present); it is never an unconditional point-ID delete. After the request, the adapter re-reads the physical ID again: absent means `deleted`, unchanged means a failed/unconfirmed delete, and a changed or replacement point is skipped/reported and never counted as deleted. If the adapter cannot express the condition, the point is skipped/reported. An explicitly present non-string `visibility` (including `null` or an integer) is malformed, retains its raw value for reporting, and is conservatively skipped without omitting the visibility guard. Exact `UNPUBLISHED + DELETED` rows may be cleaned by matching share ID, version, and hash; `PUBLISHING` and other non-terminal states are never republished or deleted. Review all extra, malformed, and wrong-ID reports before deletion. Reconcile never creates, deletes, or recreates the collection.

## Worker backlog and safe retry

Monitor pending, running, failed, and due job counts, oldest due age, attempt count, publication failures, embedding outcomes, and Qdrant operation failures. Diagnose in this order:

1. Confirm Qdrant `/readyz` and backend-to-Qdrant connectivity.
2. Confirm the configured collection exists with 768-dimensional cosine vectors.
3. Confirm DashScope quota/credentials without printing the key or provider response body. With free-quota exhaustion stop enabled, an exhausted quota returns HTTP 403.
4. Inspect sanitized `last_index_error`, job status, lease expiry, and attempt count.
5. Wait for an active lease to expire; do not manually race a running worker.
6. Run reindex/reconcile dry-run for scope, then apply only the required repair.

Retries are idempotent and version-filtered. Never edit job versions, force a public status, or blindly delete Qdrant data. A failed DELETE is safe for business visibility because MySQL is already unpublished; a failed UPSERT stays hidden.

## Rollback

Rollback feature behavior before infrastructure:

1. Set `RAG_ENABLED=false`.
2. Set `SHARE_SQUARE_ENABLED=false`.
3. Restart/roll the backend and confirm ordinary generation remains healthy.
4. Keep all MySQL shared-guide snapshots, likes, and jobs for recovery/audit.
5. Stop Qdrant only after the flags are disabled.

Do not downgrade the migration or erase MySQL data during an operational rollback. Qdrant can be rebuilt later from active MySQL records.

## Future model or dimension migration

Never mix incompatible models, dimensions, or retrieval templates in v1. For a future change:

1. Create an explicit `shared_guide_embeddings_v2` collection with the new schema.
2. Deploy code that can write/read v2 while v1 remains available.
3. Backfill v2 from MySQL with a version-specific maintenance run.
4. Reconcile v2 and run the locked offline evaluation plus manual live calibration.
5. Cut over `QDRANT_COLLECTION` (or a controlled alias) only after acceptance.
6. Keep v1 for rollback; delete it only in a separate reviewed operation.

## Evaluation, privacy, and secrets

The quality gate runs the frozen score matrix entirely offline:

```powershell
python scripts/run_rag_retrieval_evaluation.py --fixture-dir tests/fixtures/rag/v1 --summary-only
```

Manual calibration is opt-in and never edits fixture files:

```powershell
# Inject this process environment variable from the approved secret manager or CI
# secret store. Do not paste a real DashScope key or workspace URL into history.
$env:DASHSCOPE_API_KEY = (Get-Secret -Name DashScopeEmbeddingApiKey -AsPlainText)
$env:DASHSCOPE_BASE_URL = (Get-Secret -Name DashScopeEmbeddingBaseUrl -AsPlainText)
python scripts/run_rag_retrieval_evaluation.py --live-dashscope
Remove-Item Env:DASHSCOPE_API_KEY
Remove-Item Env:DASHSCOPE_BASE_URL
```

Prefer a one-shot deployment/CI environment injection when shell history or
process inspection is audited; never echo the variable or put it in a script,
Compose file, ticket, screenshot, or command-line argument. The live mode only
reads the already-injected environment value and does not persist it.

The live report prints a recommended threshold separately for human review. Never copy it into fixtures automatically.

Treat DashScope as an external data processor. Send only the already-approved public retrieval text and the current user's bounded retrieval conditions. Never send or log private free text from shared snapshots, author/user IDs, tokens, passwords, full agent state, likes, internal errors, or prompts. Confirm the selected service tier and data-governance terms before production use.

Secrets belong only in the deployment secret manager or process environment. Redact database passwords, DashScope keys, Qdrant keys, JWTs, provider bodies, full retrieval text, and prompts from command output, metrics, logs, incident tickets, and acceptance reports.
