"""
dbt runner service (Repo A) -- current-standards build
======================================================

One image, two roles (deployed from dbt-analytics):
  * Cloud Run SERVICE  -> this FastAPI app. Handles fast, selective dbt runs
    INLINE via dbt's in-process dbtRunner, and TRIGGERS the Job for heavy work.
  * Cloud Run JOB      -> same image, entrypoint overridden to run dbt directly
    (no web server). For full `dbt build` / long DAGs.

Why a Job for heavy runs: a Cloud Run Service request maxes out at 60 min and you
don't want a batch workload holding an HTTP connection. The Service fires the Job
and returns an execution id you poll. Both scale to zero -> near-zero idle cost.

Modern idioms used here: lifespan handler (not @app.on_event), Annotated/Security
dependency injection for auth, Pydantic v2, structured JSON logging for Cloud Logging.
"""

from __future__ import annotations

import os
import json
import logging
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from dbt.cli.main import dbtRunner, dbtRunnerResult


# --------------------------------------------------------------------------- #
# Structured logging -> Cloud Logging picks up `severity` + `message` from JSON
# --------------------------------------------------------------------------- #
class CloudLoggingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


_handler = logging.StreamHandler()
_handler.setFormatter(CloudLoggingFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_handler], force=True)
log = logging.getLogger("dbt-api")


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
DBT_PROJECT_DIR = os.environ.get("DBT_PROJECT_DIR", "/app/dbt_project")
DBT_PROFILES_DIR = os.environ.get("DBT_PROFILES_DIR", DBT_PROJECT_DIR)
API_TOKEN = os.environ.get("API_TOKEN")  # optional; IAM is the primary auth layer

# For triggering the Cloud Run Job (heavy runs). Set on the Service at deploy time.
GCP_PROJECT = os.environ.get("GCP_PROJECT")
GCP_REGION = os.environ.get("GCP_REGION", "us-central1")
DBT_JOB_NAME = os.environ.get("DBT_JOB_NAME", "dbt-job")


# --------------------------------------------------------------------------- #
# Lifespan
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("dbt runner starting | project_dir=%s job=%s", DBT_PROJECT_DIR, DBT_JOB_NAME)
    yield
    log.info("dbt runner shutting down")


app = FastAPI(title="dbt runner", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Auth (Annotated dependency -- the current FastAPI idiom)
# --------------------------------------------------------------------------- #
_bearer = HTTPBearer(auto_error=False)


def verify_token(
    creds: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer)],
) -> None:
    """No-op when API_TOKEN is unset (rely on Cloud Run IAM)."""
    if not API_TOKEN:
        return
    if creds is None or creds.credentials != API_TOKEN:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing bearer token")


Auth = Annotated[None, Depends(verify_token)]


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class InvokeRequest(BaseModel):
    select: str | None = None
    exclude: str | None = None
    vars: dict[str, Any] | None = None
    full_refresh: bool = False
    target: str | None = None


class InvokeResponse(BaseModel):
    success: bool
    command: list[str]
    results: list[dict[str, Any]] = Field(default_factory=list)
    elapsed_seconds: float | None = None
    error: str | None = None


class JobAck(BaseModel):
    triggered: bool
    execution: str  # short execution id
    execution_name: str  # full resource name
    poll: str  # convenience URL path to check status


class JobStatus(BaseModel):
    execution: str
    running: int
    succeeded: int
    failed: int
    completed: bool


# --------------------------------------------------------------------------- #
# Build dbt arg list (shared by inline + job paths)
# --------------------------------------------------------------------------- #
def _dbt_args(command: str, req: InvokeRequest) -> list[str]:
    args = [
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
    return args


# --------------------------------------------------------------------------- #
# INLINE execution (fast, selective runs) -- runs in Starlette's threadpool
# because the endpoints are plain `def`, so the blocking dbtRunner call doesn't
# stall the event loop.
# --------------------------------------------------------------------------- #
def run_dbt_inline(command: str, req: InvokeRequest) -> InvokeResponse:
    args = _dbt_args(command, req)
    log.info("inline dbt: %s", " ".join(args))
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


# --------------------------------------------------------------------------- #
# JOB execution (heavy runs) -- trigger the Cloud Run Job with arg overrides
# --------------------------------------------------------------------------- #
def trigger_job(command: str, req: InvokeRequest) -> JobAck:
    if not GCP_PROJECT:
        raise HTTPException(500, "GCP_PROJECT not configured; cannot trigger Cloud Run Job")

    # Lazy import keeps the inline path's cold start light.
    from google.cloud import run_v2

    args = _dbt_args(command, req)
    job_path = f"projects/{GCP_PROJECT}/locations/{GCP_REGION}/jobs/{DBT_JOB_NAME}"

    client = run_v2.JobsClient()
    request = run_v2.RunJobRequest(
        name=job_path,
        overrides=run_v2.RunJobRequest.Overrides(
            container_overrides=[
                run_v2.RunJobRequest.Overrides.ContainerOverride(args=args)
            ]
        ),
    )
    operation = client.run_job(request=request)  # returns immediately (LRO)
    execution_name = operation.metadata.name  # projects/.../executions/<id>
    exec_id = execution_name.rsplit("/", 1)[-1]
    log.info("triggered job execution=%s args=%s", exec_id, " ".join(args))

    return JobAck(
        triggered=True,
        execution=exec_id,
        execution_name=execution_name,
        poll=f"/jobs/executions/{exec_id}",
    )


def job_status(exec_id: str) -> JobStatus:
    if not GCP_PROJECT:
        raise HTTPException(500, "GCP_PROJECT not configured")
    from google.cloud import run_v2

    name = f"projects/{GCP_PROJECT}/locations/{GCP_REGION}/jobs/{DBT_JOB_NAME}/executions/{exec_id}"
    execution = run_v2.ExecutionsClient().get_execution(name=name)
    running = execution.running_count
    succeeded = execution.succeeded_count
    failed = execution.failed_count
    completed = execution.completion_time is not None and execution.completion_time.seconds > 0
    return JobStatus(
        execution=exec_id,
        running=running,
        succeeded=succeeded,
        failed=failed,
        completed=completed,
    )


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---- inline (fast) -------------------------------------------------------- #
@app.post("/run", response_model=InvokeResponse)
def dbt_run(req: InvokeRequest, _: Auth) -> InvokeResponse:
    return run_dbt_inline("run", req)


@app.post("/build", response_model=InvokeResponse)
def dbt_build(req: InvokeRequest, _: Auth) -> InvokeResponse:
    return run_dbt_inline("build", req)


@app.post("/test", response_model=InvokeResponse)
def dbt_test(req: InvokeRequest, _: Auth) -> InvokeResponse:
    return run_dbt_inline("test", req)


@app.post("/seed", response_model=InvokeResponse)
def dbt_seed(req: InvokeRequest, _: Auth) -> InvokeResponse:
    return run_dbt_inline("seed", req)


# ---- job (heavy, async) --------------------------------------------------- #
@app.post("/jobs/run", response_model=JobAck, status_code=status.HTTP_202_ACCEPTED)
def jobs_run(req: InvokeRequest, _: Auth) -> JobAck:
    """Fire a full `dbt run` as a Cloud Run Job; returns an execution id to poll."""
    return trigger_job("run", req)


@app.post("/jobs/build", response_model=JobAck, status_code=status.HTTP_202_ACCEPTED)
def jobs_build(req: InvokeRequest, _: Auth) -> JobAck:
    """Fire a full `dbt build` as a Cloud Run Job; returns an execution id to poll."""
    return trigger_job("build", req)


@app.get("/jobs/executions/{exec_id}", response_model=JobStatus)
def jobs_status(exec_id: str, _: Auth) -> JobStatus:
    return job_status(exec_id)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
