# dbt-runner-service (Repo A)

The Cloud Run runner: FastAPI + dbt's in-process `dbtRunner`. Contains **no models**.
It builds a reusable **base image** that the [`dbt-analytics`](../dbt-analytics) repo
layers models onto and deploys.

## Architecture (current-standards)

One image, deployed twice from `dbt-analytics`:

- **Cloud Run Service** (`dbt-api`) — the FastAPI HTTP front door.
  - `POST /run|/build|/test|/seed` → runs dbt **inline** (fast, selective; finishes in the request). Good for seconds-to-minutes runs.
  - `POST /jobs/run|/jobs/build` → **triggers the Cloud Run Job** and returns a `202` with an execution id. Use for heavy/full builds that would blow the 60-min request cap.
  - `GET /jobs/executions/{id}` → poll execution status.
- **Cloud Run Job** (`dbt-job`) — the *same image* with the entrypoint overridden to run `dbt` directly (no web server). This is where heavy runs actually execute.

Both scale to zero, so idle cost is ~$0. The Service runs with `--concurrency 1`
(dbt is heavy; scale out with instances, not threads).

```
dbt-runner-service/        <-- you are here (stable infra, rarely changes)
└── builds  ->  dbt-runner-base:latest  (Artifact Registry)
                      ^
                      |  FROM
dbt-analytics/         (your models; builds the real deployable image)
```

## One-time GCP setup

```bash
PROJECT=your-gcp-project
REGION=us-central1

# Artifact Registry repo to hold images
gcloud artifacts repositories create dbt \
  --repository-format=docker --location=$REGION --project=$PROJECT

# Service account dbt runs as (needs BigQuery access)
gcloud iam service-accounts create dbt-runner --project=$PROJECT
SA=dbt-runner@${PROJECT}.iam.gserviceaccount.com
gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:${SA}" --role="roles/bigquery.dataEditor"
gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:${SA}" --role="roles/bigquery.jobUser"

# Let the SERVICE trigger the Cloud Run JOB (heavy runs) and read execution status
gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:${SA}" --role="roles/run.developer"
```

## Build & push the base image

```bash
gcloud builds submit --config cloudbuild.yaml --project=$PROJECT
```

That's the whole job of this repo. You rebuild it only when you change the API,
bump dbt, or change the warehouse adapter. Day-to-day model work happens in
`dbt-analytics`, which is what deploys the actual Cloud Run service.
