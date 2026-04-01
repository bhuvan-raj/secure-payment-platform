FROM python:3.12-slim AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip install --upgrade pip && pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim AS runtime
RUN groupadd -r appuser && useradd -r -g appuser -u 1001 appuser
RUN apt-get update && apt-get install -y --no-install-recommends curl && apt-get clean && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=builder /install /usr/local
COPY app/ .
RUN chown -R appuser:appuser /app
USER 1001
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
EXPOSE 8001
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8001/health || exit 1
CMD ["gunicorn", "main:app", "--bind", "0.0.0.0:8001", "--workers", "4", "--timeout", "30", "--access-logfile", "-", "--error-logfile", "-"]
