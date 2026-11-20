# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    PYTHONPATH=/app

WORKDIR /app

COPY middleware/requirements.txt /app/middleware/requirements.txt
RUN pip install --no-cache-dir -r /app/middleware/requirements.txt

COPY schemas /app/schemas
COPY middleware /app/middleware

RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

CMD ["sh", "-c", "uvicorn middleware.app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
