# BookScout — persistent core and HTTP API

BookScout is a single-user desktop-oriented book price search prototype. This iteration adds a thin authenticated HTTP API and persistent SQLite storage without moving domain rules out of the core.

## Requirements

- Python 3.11+
- SQLite 3

## Install

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

## Database migrations

Migrations are numbered SQL files in `bookscout/migrations/`. The application does **not** create or alter tables on startup.

```bash
export BOOKSCOUT_DB_PATH=./bookscout.sqlite3
python -m bookscout.migrate --database "$BOOKSCOUT_DB_PATH"
```

The migration runner records applied filenames in `schema_migrations`; running it again is safe and applies only pending migrations.

## Start the API

Set a long random token before starting the service. The default bind is loopback (`127.0.0.1`), not a public interface.

```bash
export BOOKSCOUT_API_TOKEN='replace-with-a-long-random-secret'
export BOOKSCOUT_DB_PATH=./bookscout.sqlite3
python -m bookscout.api
```

The API is available at `http://127.0.0.1:8000`; interactive docs at `/docs`.

All endpoints require `Authorization: Bearer $BOOKSCOUT_API_TOKEN`.

### Create a Job

`POST /jobs` requires `Idempotency-Key` (8–128 characters):

```bash
curl -X POST http://127.0.0.1:8000/jobs \
  -H "Authorization: Bearer $BOOKSCOUT_API_TOKEN" \
  -H 'Idempotency-Key: book-search-0001' \
  -H 'Content-Type: application/json' \
  -d '{"operation":"books.search","parameters":{"isbn":"9785170900008"}}'
```

The current prototype executes the configured workflow synchronously and returns the created Job representation. Repeating the same request with the same key returns the existing Job. Reusing the key with a different request body returns `409 idempotency_key_reused`.

### Read Job state

```bash
curl http://127.0.0.1:8000/jobs/JOB_ID \
  -H "Authorization: Bearer $BOOKSCOUT_API_TOKEN"
```

### Cancel a Job

```bash
curl -X POST http://127.0.0.1:8000/jobs/JOB_ID/cancel \
  -H "Authorization: Bearer $BOOKSCOUT_API_TOKEN"
```

Cancellation is a domain operation: `Job.cancel()` decides whether the current state can be cancelled. The API does not accept arbitrary status updates.

### Read Job result

```bash
curl http://127.0.0.1:8000/jobs/JOB_ID/result \
  -H "Authorization: Bearer $BOOKSCOUT_API_TOKEN"
```

## API contract

`api/openapi.json` is the committed OpenAPI 3 contract. The test `test_openapi_contract_is_machine_readable_and_matches_runtime` compares it with `app.openapi()` so the checked-in contract cannot silently drift from FastAPI routes, request/response models, and headers.

## Error format

Every API error uses the same envelope:

```json
{
  "error": {
    "code": "validation_error",
    "message": "Запрос не соответствует контракту",
    "details": {
      "field": "parameters.isbn"
    }
  }
}
```

Client errors use 4xx status codes; unexpected service failures use 5xx. Responses never include stack traces, SQL, local file paths, or driver diagnostics.

## Persistence and transaction boundary

- `JobRepository` remains the core-facing storage interface.
- `InMemoryJobRepository` is retained for isolated unit tests.
- `SQLiteJobRepository` is the persistent adapter and stores Job, result, idempotency key/hash, and state history.
- Every `save(job)` writes the current state/result and the corresponding history event in one SQLite transaction. A failure rolls back both.
- Idempotency-key uniqueness and request hash are stored in SQLite. The key lookup and insertion are protected by a write transaction.
- On service restart, non-terminal persisted Jobs are moved to `FAILED / APP_RESTARTED` and that transition is persisted.

## Core architecture

- `bookscout/core/job.py` — Job state machine; legal transitions are enforced here.
- `bookscout/core/dispatcher.py` — workflow orchestration; saves state changes through `JobRepository`.
- `bookscout/core/registry.py` — Tool/workflow registry and input schema validation.
- `bookscout/core/repository.py` — storage Protocol and in-memory adapter.
- `bookscout/sqlite_repository.py` — SQLite adapter and numbered SQL migration runner.
- `bookscout/migrations/0001_initial.sql` — initial schema migration for migration history, Jobs, and Job events.
- `bookscout/api.py` — HTTP transport, authentication, status mapping, and unified error responses only.
- `api/openapi.json` — machine-readable API contract.
- `docs/api-security-review.md` — review against violations N40–N49.

## Tests

One command runs unit, API, persistence, idempotency, migration, transaction, and OpenAPI contract tests:

```bash
python -m pytest -q
```

## Known MVP limits

- The API is single-user and intended for loopback access. Multi-user ownership checks are not implemented.
- Workflow execution is synchronous in the HTTP request. A future iteration may move it to a background worker while preserving the same Job contract.
- Bibliographic lookup and store adapters are demo implementations; `example.org` is not a real store offer.
- Payment, order management, performer accounts, and support-only operations do not exist in BookScout.
