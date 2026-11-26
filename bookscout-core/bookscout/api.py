"""Thin authenticated HTTP API over the application core."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from bookscout.app import build_dispatcher
from bookscout.core.dispatcher import Dispatcher
from bookscout.core.errors import (
    BookScoutError, IdempotencyConflict, InvalidTransition, JobNotFound,
    ToolContractViolation, UnknownOperation, ValidationError,
)
from bookscout.core.job import Job, JobState
from bookscout.core.registry import ToolRegistry
from bookscout.sqlite_repository import SQLiteJobRepository


class CreateJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: str = Field(min_length=1, max_length=100, examples=["books.search"])
    parameters: dict[str, Any]


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorBody


class JobResponse(BaseModel):
    job_id: str
    state: str
    created_at: str
    updated_at: str
    result_available: bool
    failure: ErrorBody | None = None


class JobResultResponse(BaseModel):
    job_id: str
    result: dict[str, Any]


bearer = HTTPBearer(auto_error=False)


def _error(code: str, message: str, details: dict[str, Any] | None = None):
    return {"error": {"code": code, "message": message, "details": details or {}}}


def _job_view(job: Job) -> dict[str, Any]:
    failure = None
    if job.error_code:
        failure = {"code": job.error_code, "message": job.error_message or "Job завершилась с ошибкой",
                   "details": {}}
    return {
        "job_id": job.id,
        "state": job.state.value,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
        "result_available": job.result is not None,
        "failure": failure,
    }


def create_app(*, database_path: str | None = None, token: str | None = None,
               dispatcher: Dispatcher | None = None) -> FastAPI:
    api_token = token or os.getenv("BOOKSCOUT_API_TOKEN")
    if not api_token:
        raise RuntimeError("Set BOOKSCOUT_API_TOKEN before starting the API")

    if dispatcher is None:
        db_path = database_path or os.getenv("BOOKSCOUT_DB_PATH", "bookscout.sqlite3")
        repository = SQLiteJobRepository(db_path)
        # Startup recovery is persisted through the same repository adapter.
        base = build_dispatcher(repository=repository, recover_interrupted=True)
        dispatcher = base

    app = FastAPI(
        title="BookScout API", version="1.0.0",
        description="HTTP API for creating book-search Jobs and reading their state/results.",
    )
    app.state.dispatcher = dispatcher

    async def authenticate(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if credentials is None or credentials.scheme.lower() != "bearer" or not hmac.compare_digest(
            credentials.credentials, api_token
        ):
            raise HTTPException(status_code=401, detail="Требуется действительный Bearer token",
                                headers={"WWW-Authenticate": "Bearer"})
        return True

    @app.exception_handler(BookScoutError)
    async def bookscout_error_handler(_: Request, exc: BookScoutError):
        if isinstance(exc, JobNotFound):
            status = 404
        elif isinstance(exc, (InvalidTransition, IdempotencyConflict)):
            status = 409
        elif isinstance(exc, (UnknownOperation, ValidationError)):
            status = 422
        elif isinstance(exc, ToolContractViolation):
            status = 500
        else:
            status = 400
        details = dict(exc.details)
        if exc.field:
            details.setdefault("field", exc.field)
        return JSONResponse(status_code=status, content=_error(exc.code, exc.message, details))

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(_: Request, exc: RequestValidationError):
        violations = []
        for item in exc.errors():
            loc = [str(part) for part in item.get("loc", ()) if part != "body"]
            violations.append({"field": ".".join(loc) or "$", "rule": item.get("type", "invalid"),
                               "message": item.get("msg", "Некорректное значение")})
        return JSONResponse(status_code=422, content=_error(
            "validation_error", "Запрос не соответствует контракту", {"violations": violations}
        ))

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(_: Request, exc: StarletteHTTPException):
        if exc.status_code == 401:
            code, message = "unauthorized", "Требуется действительный Bearer token"
        elif exc.status_code == 405:
            code, message = "method_not_allowed", "HTTP-метод для этого ресурса не поддерживается"
        else:
            code, message = "http_error", str(exc.detail)
        return JSONResponse(status_code=exc.status_code, content=_error(code, message),
                            headers=exc.headers)

    @app.post("/jobs", status_code=201, response_model=JobResponse,
              responses={401: {"model": ErrorResponse}, 409: {"model": ErrorResponse},
                         422: {"model": ErrorResponse}, 500: {"model": ErrorResponse},
                         502: {"model": ErrorResponse}, 503: {"model": ErrorResponse},
                         504: {"model": ErrorResponse}})
    def create_job(payload: CreateJobRequest,
                   idempotency_key: str = Header(..., alias="Idempotency-Key",
                                                 min_length=8, max_length=128),
                   _: bool = Depends(authenticate)):
        canonical = json.dumps(payload.model_dump(), ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"))
        request_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        job = dispatcher.dispatch(
            payload.operation, payload.parameters,
            idempotency_key=idempotency_key, request_hash=request_hash,
        )
        if job.state is JobState.FAILED and job.error_code:
            client_errors = {"ISBN_FORMAT_INVALID", "ISBN_CHECKSUM_INVALID", "BOOK_QUERY_INVALID"}
            upstream_status = {"STORE_TIMEOUT": 504, "STORE_UNAVAILABLE": 503,
                               "STORE_RESPONSE_INVALID": 502, "STORE_SOURCES_FAILED": 502}
            if job.error_code in client_errors:
                status = 422
            else:
                status = upstream_status.get(job.error_code, 500)
            return JSONResponse(status_code=status, content=_error(
                job.error_code.lower(), job.error_message or "Не удалось выполнить поиск",
                {"job_id": job.id},
            ))
        if job.state is JobState.TIMED_OUT:
            return JSONResponse(status_code=504, content=_error(
                (job.error_code or "store_timeout").lower(),
                job.error_message or "Истекло время ожидания", {"job_id": job.id},
            ))
        return _job_view(job)

    @app.get("/jobs/{job_id}", response_model=JobResponse,
             responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse},
                        422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
    def get_job(job_id: str, _: bool = Depends(authenticate)):
        return _job_view(dispatcher.get_job(job_id))

    @app.post("/jobs/{job_id}/cancel", response_model=JobResponse,
              responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse},
                         409: {"model": ErrorResponse}, 422: {"model": ErrorResponse},
                         500: {"model": ErrorResponse}})
    def cancel_job(job_id: str, _: bool = Depends(authenticate)):
        # The transport cannot assign a state; Job.cancel() enforces the domain transition.
        return _job_view(dispatcher.cancel(job_id))

    @app.get("/jobs/{job_id}/result", response_model=JobResultResponse,
             responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse},
                        409: {"model": ErrorResponse}, 422: {"model": ErrorResponse},
                        500: {"model": ErrorResponse}})
    def get_job_result(job_id: str, _: bool = Depends(authenticate)):
        job = dispatcher.get_job(job_id)
        if job.result is None:
            details = {"job_id": job.id, "state": job.state.value}
            if job.error_code:
                details["cause_code"] = job.error_code
            return JSONResponse(status_code=409, content=_error(
                "job_result_unavailable", "Результат Job пока недоступен", details
            ))
        return {"job_id": job.id, "result": job.result}


    @app.exception_handler(Exception)
    async def unexpected_error_handler(_: Request, exc: Exception):
        # Never leak traceback, filesystem paths, SQL or driver messages to clients.
        return JSONResponse(status_code=500, content=_error(
            "internal_error", "Внутренняя ошибка сервиса"
        ))

    return app


def main():
    import uvicorn
    host = os.getenv("BOOKSCOUT_API_HOST", "127.0.0.1")
    port = int(os.getenv("BOOKSCOUT_API_PORT", "8000"))
    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":
    main()
