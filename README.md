# dbt-runner-service (Repo A)

The Cloud Run runner: FastAPI + dbt's in-process `dbtRunner`. Contains **no models**.
It builds a reusable **base image** that the [`dbt-analytics`](../dbt-analytics) repo
layers models onto and deploys.

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
```

## Build & push the base image

```bash
gcloud builds submit --config cloudbuild.yaml --project=$PROJECT
```

That's the whole job of this repo. You rebuild it only when you change the API,
bump dbt, or change the warehouse adapter. Day-to-day model work happens in
`dbt-analytics`, which is what deploys the actual Cloud Run service.
