import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from bookscout.api import create_app
from bookscout.core.errors import IdempotencyConflict
from bookscout.core.job import Job, JobState
from bookscout.sqlite_repository import SQLiteJobRepository, migrate

TOKEN = "test-api-token-which-is-long-enough"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": "request-key-0001"}
PAYLOAD = {"operation": "books.search", "parameters": {"isbn": "9785170900008"}}


@pytest.fixture
def migrated_db(tmp_path):
    path = tmp_path / "bookscout.sqlite3"
    assert migrate(path) == ["0001_initial.sql"]
    return path


def test_api_happy_path_and_persistence_after_service_restart(migrated_db):
    client = TestClient(create_app(database_path=str(migrated_db), token=TOKEN))
    created = client.post("/jobs", json=PAYLOAD, headers=HEADERS)
    assert created.status_code == 201
    job_id = created.json()["job_id"]
    assert created.json()["state"] == "SUCCEEDED"

    state = client.get(f"/jobs/{job_id}", headers=HEADERS)
    assert state.status_code == 200
    assert state.json()["state"] == "SUCCEEDED"

    result = client.get(f"/jobs/{job_id}/result", headers=HEADERS)
    assert result.status_code == 200
    assert result.json()["result"]["offers"]

    # New repository and new API app simulate a process restart.
    restarted = TestClient(create_app(database_path=str(migrated_db), token=TOKEN))
    assert restarted.get(f"/jobs/{job_id}", headers=HEADERS).json()["state"] == "SUCCEEDED"
    assert restarted.get(f"/jobs/{job_id}/result", headers=HEADERS).json() == result.json()


def test_idempotency_same_key_same_body_returns_same_job(migrated_db):
    client = TestClient(create_app(database_path=str(migrated_db), token=TOKEN))
    first = client.post("/jobs", json=PAYLOAD, headers=HEADERS)
    second = client.post("/jobs", json=PAYLOAD, headers=HEADERS)
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    repo = SQLiteJobRepository(migrated_db)
    assert len(repo.list_all()) == 1


def test_idempotency_same_key_different_body_is_conflict_even_if_operation_unknown(migrated_db):
    client = TestClient(create_app(database_path=str(migrated_db), token=TOKEN))
    assert client.post("/jobs", json=PAYLOAD, headers=HEADERS).status_code == 201
    changed = {"operation": "not.registered", "parameters": {"anything": True}}
    response = client.post("/jobs", json=changed, headers=HEADERS)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "idempotency_key_reused"
    assert len(SQLiteJobRepository(migrated_db).list_all()) == 1


@pytest.mark.parametrize(
    ("payload", "expected_status", "expected_code"),
    [
        ({"operation": "unknown.operation", "parameters": {}}, 422, "unknown_operation"),
        ({"operation": "books.search", "parameters": {"isbn": 123}}, 422, "validation_error"),
        ({"operation": "books.search", "parameters": {"isbn": "9785170900001"}}, 422, "isbn_checksum_invalid"),
    ],
)
def test_api_error_contract_for_client_errors(migrated_db, payload, expected_status, expected_code):
    client = TestClient(create_app(database_path=str(migrated_db), token=TOKEN))
    response = client.post("/jobs", json=payload, headers=HEADERS)
    assert response.status_code == expected_status
    if expected_code:
        body = response.json()
        assert set(body) == {"error"}
        assert set(body["error"]) == {"code", "message", "details"}
        assert body["error"]["code"] == expected_code
        assert "traceback" not in response.text.lower()
        assert "sqlite" not in response.text.lower()


def test_unknown_job_has_unified_404_error(migrated_db):
    client = TestClient(create_app(database_path=str(migrated_db), token=TOKEN))
    response = client.get("/jobs/missing", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "job_not_found"


def test_authentication_is_required(migrated_db):
    client = TestClient(create_app(database_path=str(migrated_db), token=TOKEN))
    response = client.get("/jobs/nope")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_migration_is_idempotent_and_creates_schema_on_empty_database(tmp_path):
    path = tmp_path / "empty.sqlite3"
    assert not path.exists()
    assert migrate(path) == ["0001_initial.sql"]
    assert migrate(path) == []
    with sqlite3.connect(path) as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    assert {"schema_migrations", "jobs", "job_events"} <= tables


def test_job_state_result_and_history_are_committed_atomically(migrated_db):
    repo = SQLiteJobRepository(migrated_db)
    job = Job(request_id="atomic-test")
    repo.add(job)
    job.transition(JobState.VALIDATING)
    repo.save(job)

    with sqlite3.connect(migrated_db) as conn:
        conn.execute("""CREATE TRIGGER reject_succeeded_event BEFORE INSERT ON job_events
                       WHEN NEW.to_state = 'SUCCEEDED' BEGIN SELECT RAISE(ABORT, 'test failure'); END""")

    job.transition(JobState.RESOLVING_BOOK)
    job.transition(JobState.FETCHING_OFFERS)
    job.transition(JobState.NORMALIZING)
    job.transition(JobState.SUCCEEDED, result={"offers": [{"price_minor": 100}]})
    with pytest.raises(sqlite3.IntegrityError):
        repo.save(job)

    persisted = repo.get(job.id)
    assert persisted.state is JobState.VALIDATING
    assert persisted.result is None
    assert repo.history(job.id)[-1]["to_state"] == "VALIDATING"


def test_openapi_contract_is_machine_readable_and_matches_runtime(migrated_db):
    app = create_app(database_path=str(migrated_db), token=TOKEN)
    contract_path = __import__("pathlib").Path(__file__).parents[1] / "api" / "openapi.json"
    assert json.loads(contract_path.read_text(encoding="utf-8")) == app.openapi()


def test_extra_parameter_quantity_is_rejected_not_ignored(migrated_db):
    client = TestClient(create_app(database_path=str(migrated_db), token=TOKEN))
    payload = {"operation": "books.search", "parameters": {"isbn": "9785170900008", "quantity": -1}}
    response = client.post("/jobs", json=payload, headers=HEADERS)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert response.json()["error"]["details"]["field"] == "quantity"
    assert SQLiteJobRepository(migrated_db).list_all() == []


def test_api_does_not_expose_job_state_mutation(migrated_db):
    client = TestClient(create_app(database_path=str(migrated_db), token=TOKEN))
    created = client.post("/jobs", json=PAYLOAD, headers=HEADERS)
    job_id = created.json()["job_id"]
    response = client.patch(f"/jobs/{job_id}", json={"state": "SUCCEEDED"},
                            headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"


def test_api_has_no_support_only_or_unregistered_operation(migrated_db):
    client = TestClient(create_app(database_path=str(migrated_db), token=TOKEN))
    payload = {"operation": "support.refund", "parameters": {}}
    response = client.post("/jobs", json=payload, headers=HEADERS)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unknown_operation"


def test_api_binds_access_to_single_local_principal(migrated_db):
    client = TestClient(create_app(database_path=str(migrated_db), token=TOKEN))
    response = client.get("/jobs/any-id", headers={"Authorization": "Bearer another-invalid-token"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_offer_search_never_returns_more_than_1000_records():
    from threading import Event
    from bookscout.core.contracts import ToolContext
    from bookscout.tools.builtin import SearchStoreOffersTool

    isbn = "9785170900008"
    edition = {"edition_id": f"isbn:{isbn}", "title": "Book", "isbn13": isbn}

    class LargeAdapter:
        source_id = "large-store"
        def search(self, edition, context):
            return [{"source_id": self.source_id, "title": "Book", "isbn13": isbn,
                     "price_minor": i, "currency": "RUB", "delivery_minor": None,
                     "availability": "in_stock", "product_url": f"https://store.example/{i}"}
                    for i in range(1005)]

    context = ToolContext("job", "request", None, Event())
    result = SearchStoreOffersTool([LargeAdapter()]).execute({"edition": edition}, context)
    assert len(result.value["offers"]) == 1000
    assert result.value["source_results"][0]["truncated"] is True


def test_api_invalid_transition_has_conflict_status(migrated_db):
    client = TestClient(create_app(database_path=str(migrated_db), token=TOKEN))
    created = client.post("/jobs", json=PAYLOAD, headers=HEADERS)
    job_id = created.json()["job_id"]
    response = client.post(f"/jobs/{job_id}/cancel",
                           headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "job_invalid_transition"
    assert set(response.json()["error"]) == {"code", "message", "details"}


def test_idempotency_key_survives_api_restart(migrated_db):
    first_app = TestClient(create_app(database_path=str(migrated_db), token=TOKEN))
    first = first_app.post("/jobs", json=PAYLOAD, headers=HEADERS)
    restarted_app = TestClient(create_app(database_path=str(migrated_db), token=TOKEN))
    second = restarted_app.post("/jobs", json=PAYLOAD, headers=HEADERS)
    assert second.status_code == 201
    assert second.json() == first.json()
    assert len(SQLiteJobRepository(migrated_db).list_all()) == 1


def test_inflight_job_is_marked_failed_and_persisted_after_restart(migrated_db):
    repository = SQLiteJobRepository(migrated_db)
    job = Job(request_id="interrupted-job")
    repository.add(job)
    job.transition(JobState.VALIDATING)
    repository.save(job)

    # Constructing the service's Dispatcher performs startup recovery.
    create_app(database_path=str(migrated_db), token=TOKEN)
    recovered = SQLiteJobRepository(migrated_db).get(job.id)
    assert recovered.state is JobState.FAILED
    assert recovered.error_code == "APP_RESTARTED"
    assert SQLiteJobRepository(migrated_db).history(job.id)[-1]["to_state"] == "FAILED"
