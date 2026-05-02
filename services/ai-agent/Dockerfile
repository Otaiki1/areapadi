FROM python:3.11-slim

WORKDIR /app

# Install system deps for asyncpg + geoalchemy2
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

COPY services/ai-agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared utilities and service code
COPY shared/ ./shared/
COPY services/ai-agent/ ./

ENV PYTHONPATH=/app

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
