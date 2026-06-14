# Base image: FastAPI + dbt, NO models.
# The dbt-analytics repo builds FROM this and copies its project into /app/dbt_project.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DBT_PROJECT_DIR=/app/dbt_project \
    DBT_PROFILES_DIR=/app/dbt_project \
    PORT=8080

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

EXPOSE 8080
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
