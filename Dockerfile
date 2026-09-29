FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY src ./src
COPY fixtures.json .
ENV PYTHONPATH=/app/src DATABASE_PATH=/app/data/rubrica.db FIXTURES_PATH=/app/fixtures.json PORT=8080
VOLUME ["/app/data"]
EXPOSE 8080
HEALTHCHECK --interval=5s --timeout=3s --retries=10 CMD curl -fsS http://localhost:8080/health || exit 1
CMD ["python", "-m", "rubrica"]
