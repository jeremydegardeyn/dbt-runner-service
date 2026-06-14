"""
dbt runner service (Repo A)
===========================

A thin FastAPI wrapper that runs dbt commands via dbt's in-process dbtRunner.
This repo contains NO dbt models -- it builds a reusable *base image*
(dbt-runner-base). The dbt-analytics repo layers its models on top via
`FROM dbt-runner-base` and is what actually gets deployed to Cloud Run.

The dbt project is expected at $DBT_PROJECT_DIR (default /app/dbt_project),
which the downstream image populates.
"""

from __future__ import annotations

import os
import json
import logging
from typing import Any

from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from dbt.cli.main import dbtRunner, dbtRunnerResult

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("dbt-api")

DBT_PROJECT_DIR = os.environ.get("DBT_PROJECT_DIR", "/app/dbt_project")
DBT_PROFILES_DIR = os.environ.get("DBT_PROFILES_DIR", DBT_PROJECT_DIR)
API_TOKEN = os.environ.get("API_TOKEN")  # optional bearer token; IAM is primary auth

app = FastAPI(title="dbt runner", version="1.0.0")


def require_token(authorization: str | None) -> None:
    if not API_TOKEN:
        return
    if authorization != f"Bearer {API_TOKEN}":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bad token")


class InvokeRequest(BaseModel):
    select: str | None = Field(default=None)
    exclude: str | None = Field(default=None)
    vars: dict[str, Any] | None = Field(default=None)
    full_refresh: bool = Field(default=False)
    target: str | None = Field(default=None)


class InvokeResponse(BaseModel):
    success: bool
    command: list[str]
    results: list[dict[str, Any]]
    elapsed_seconds: float | None = None
    error: str | None = None


def run_dbt(command: str, req: InvokeRequest) -> InvokeResponse:
    args: list[str] = [
        command,
        "--project-dir", DBT_PROJECT_DIR,
        "--profiles-dir", DBT_PROFILES_DIR,
    ]
    if req.select:
        args += ["--select", req.select]
    if req.exclude:
        args += ["--exclude", req.exclude]
    if req.full_refresh:
        args += ["--full-refresh"]
    if req.target:
        args += ["--target", req.target]
    if req.vars:
        args += ["--vars", json.dumps(req.vars)]

    log.info("dbt %s", " ".join(args))
    res: dbtRunnerResult = dbtRunner().invoke(args)

    node_results: list[dict[str, Any]] = []
    elapsed: float | None = None
    if res.result is not None and hasattr(res.result, "results"):
        for r in res.result.results:
            node_results.append(
                {
                    "unique_id": getattr(r.node, "unique_id", None),
                    "status": str(r.status),
                    "execution_time": round(r.execution_time, 3),
                    "message": r.message,
                }
            )
        elapsed = getattr(res.result, "elapsed_time", None)

    return InvokeResponse(
        success=res.success,
        command=args,
        results=node_results,
        elapsed_seconds=round(elapsed, 3) if elapsed else None,
        error=None if res.success else (str(res.exception) if res.exception else "dbt failed"),
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/run", response_model=InvokeResponse)
def dbt_run(req: InvokeRequest, authorization: str | None = Header(default=None)) -> InvokeResponse:
    require_token(authorization)
    return run_dbt("run", req)


@app.post("/build", response_model=InvokeResponse)
def dbt_build(req: InvokeRequest, authorization: str | None = Header(default=None)) -> InvokeResponse:
    require_token(authorization)
    return run_dbt("build", req)


@app.post("/test", response_model=InvokeResponse)
def dbt_test(req: InvokeRequest, authorization: str | None = Header(default=None)) -> InvokeResponse:
    require_token(authorization)
    return run_dbt("test", req)


@app.post("/seed", response_model=InvokeResponse)
def dbt_seed(req: InvokeRequest, authorization: str | None = Header(default=None)) -> InvokeResponse:
    require_token(authorization)
    return run_dbt("seed", req)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
