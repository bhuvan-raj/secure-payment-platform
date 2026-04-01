FROM python:3.12-slim AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip install --upgrade pip && pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim AS runtime
RUN groupadd -r appuser && useradd -r -g appuser -u 1001 appuser
WORKDIR /app
COPY --from=builder /install /usr/local
COPY app/ .
RUN chown -R appuser:appuser /app
USER 1001
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
EXPOSE 8002
CMD ["python", "main.py"]
