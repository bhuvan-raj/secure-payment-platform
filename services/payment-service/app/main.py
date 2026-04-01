"""
Payment Service - Handles payment initiation, validation, and processing.
Integrates with Transaction Service for ledger entries.
"""

import os
import time
import uuid
import logging
import decimal
import json
from functools import wraps

import boto3
import requests
from flask import Flask, request, jsonify, g
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from pythonjsonlogger import jsonlogger
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import pool

# ── Logging ───────────────────────────────────────────────────────────────────
logger = logging.getLogger(__name__)
handler = logging.StreamHandler()
handler.setFormatter(jsonlogger.JsonFormatter(
    "%(asctime)s %(name)s %(levelname)s %(message)s"
))
logger.addHandler(handler)
logger.setLevel(logging.INFO)

# ── App ───────────────────────────────────────────────────────────────────────
app = Flask(__name__)
limiter = Limiter(get_remote_address, app=app, default_limits=["100 per minute"], storage_uri="memory://")

# ── Metrics ───────────────────────────────────────────────────────────────────
PAYMENT_REQUESTS = Counter("payment_requests_total", "Total payment requests", ["status"])
PAYMENT_LATENCY = Histogram("payment_duration_seconds", "Payment processing latency")
PAYMENT_AMOUNT = Histogram("payment_amount_usd", "Payment amounts in USD",
                           buckets=[1, 10, 50, 100, 500, 1000, 5000, 10000])

# ── AWS ───────────────────────────────────────────────────────────────────────
secrets_client = boto3.client("secretsmanager", region_name=os.environ["AWS_REGION"])
sqs_client = boto3.client("sqs", region_name=os.environ["AWS_REGION"])


def get_secret(arn: str) -> dict:
    response = secrets_client.get_secret_value(SecretId=arn)
    return json.loads(response["SecretString"])


# ── DB Pool ───────────────────────────────────────────────────────────────────
_db_pool = None


def get_db_pool():
    global _db_pool
    if _db_pool is None:
        secret = get_secret(os.environ["DB_SECRET_ARN"])
        _db_pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2, maxconn=10,
            host=secret["host"], port=secret.get("port", 5432),
            dbname=secret["dbname"], user=secret["username"],
            password=secret["password"], sslmode="require",
        )
    return _db_pool


def get_db():
    return get_db_pool().getconn()


def release_db(conn):
    get_db_pool().putconn(conn)


# ── Auth verification ─────────────────────────────────────────────────────────
AUTH_SERVICE_URL = os.environ.get("AUTH_SERVICE_URL", "http://auth-service:8000")


def verify_token(token: str) -> dict:
    resp = requests.post(
        f"{AUTH_SERVICE_URL}/v1/auth/verify",
        json={"token": token},
        timeout=5,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("valid"):
        raise ValueError(data.get("error", "Invalid token"))
    return data["payload"]


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Missing or invalid Authorization header"}), 401
        try:
            g.current_user = verify_token(auth_header.split(" ", 1)[1])
        except Exception as e:
            return jsonify({"error": "Unauthorized", "detail": str(e)}), 401
        return f(*args, **kwargs)
    return decorated


# ── Payment validation ────────────────────────────────────────────────────────
ALLOWED_CURRENCIES = {"USD", "EUR", "GBP", "INR", "SGD"}
MAX_PAYMENT_AMOUNT = decimal.Decimal("100000.00")
MIN_PAYMENT_AMOUNT = decimal.Decimal("0.01")


def validate_payment(data: dict) -> tuple:
    """Returns (cleaned_data, error_message)."""
    amount_raw = data.get("amount")
    currency = data.get("currency", "").upper()
    recipient_id = data.get("recipient_id", "").strip()
    description = data.get("description", "").strip()

    if not amount_raw or not currency or not recipient_id:
        return None, "amount, currency, and recipient_id are required"

    try:
        amount = decimal.Decimal(str(amount_raw)).quantize(decimal.Decimal("0.01"))
    except decimal.InvalidOperation:
        return None, "Invalid amount format"

    if amount < MIN_PAYMENT_AMOUNT:
        return None, f"Minimum payment is {MIN_PAYMENT_AMOUNT}"
    if amount > MAX_PAYMENT_AMOUNT:
        return None, f"Maximum payment is {MAX_PAYMENT_AMOUNT}"
    if currency not in ALLOWED_CURRENCIES:
        return None, f"Currency must be one of: {', '.join(ALLOWED_CURRENCIES)}"

    return {
        "amount": amount,
        "currency": currency,
        "recipient_id": recipient_id,
        "description": description[:500],
    }, None


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy", "service": "payment-service"}), 200


@app.route("/ready", methods=["GET"])
def ready():
    try:
        conn = get_db()
        conn.cursor().execute("SELECT 1")
        release_db(conn)
        return jsonify({"status": "ready"}), 200
    except Exception:
        return jsonify({"status": "not ready"}), 503


@app.route("/metrics", methods=["GET"])
def metrics():
    return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}


@app.route("/v1/payments", methods=["POST"])
@require_auth
@limiter.limit("30 per minute")
def create_payment():
    start = time.time()
    data = request.get_json(force=True, silent=True) or {}
    cleaned, error = validate_payment(data)
    if error:
        PAYMENT_REQUESTS.labels(status="validation_error").inc()
        return jsonify({"error": error}), 400

    sender_id = g.current_user["sub"]
    payment_id = str(uuid.uuid4())
    idempotency_key = request.headers.get("Idempotency-Key", str(uuid.uuid4()))

    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Idempotency check
            cur.execute(
                "SELECT id, status FROM payments WHERE idempotency_key = %s AND sender_id = %s",
                (idempotency_key, sender_id),
            )
            existing = cur.fetchone()
            if existing:
                return jsonify({"payment_id": str(existing["id"]), "status": existing["status"], "idempotent": True}), 200

            # Insert payment
            cur.execute(
                """
                INSERT INTO payments (id, sender_id, recipient_id, amount, currency,
                                      description, status, idempotency_key, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s, NOW())
                """,
                (payment_id, sender_id, cleaned["recipient_id"], str(cleaned["amount"]),
                 cleaned["currency"], cleaned["description"], idempotency_key),
            )
            conn.commit()

        # Enqueue to SQS for async processing
        sqs_client.send_message(
            QueueUrl=os.environ["PAYMENT_QUEUE_URL"],
            MessageBody=json.dumps({
                "payment_id": payment_id,
                "sender_id": sender_id,
                "recipient_id": cleaned["recipient_id"],
                "amount": str(cleaned["amount"]),
                "currency": cleaned["currency"],
            }),
            MessageDeduplicationId=idempotency_key,
            MessageGroupId=sender_id,
        )

        PAYMENT_REQUESTS.labels(status="created").inc()
        PAYMENT_AMOUNT.observe(float(cleaned["amount"]))
        PAYMENT_LATENCY.observe(time.time() - start)
        logger.info("Payment created", extra={
            "payment_id": payment_id, "amount": str(cleaned["amount"]),
            "currency": cleaned["currency"], "request_id": g.request_id,
        })
        return jsonify({"payment_id": payment_id, "status": "pending"}), 202

    except Exception as e:
        conn.rollback()
        logger.error("Payment creation failed", extra={"error": str(e)})
        PAYMENT_REQUESTS.labels(status="error").inc()
        return jsonify({"error": "Internal server error"}), 500
    finally:
        release_db(conn)


@app.route("/v1/payments/<payment_id>", methods=["GET"])
@require_auth
def get_payment(payment_id):
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM payments WHERE id = %s AND sender_id = %s",
                (payment_id, g.current_user["sub"]),
            )
            payment = cur.fetchone()
    finally:
        release_db(conn)

    if not payment:
        return jsonify({"error": "Payment not found"}), 404
    return jsonify(dict(payment)), 200


@app.route("/v1/payments", methods=["GET"])
@require_auth
def list_payments():
    page = max(1, int(request.args.get("page", 1)))
    per_page = min(100, int(request.args.get("per_page", 20)))
    offset = (page - 1) * per_page

    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, recipient_id, amount, currency, status, created_at
                FROM payments WHERE sender_id = %s
                ORDER BY created_at DESC LIMIT %s OFFSET %s
                """,
                (g.current_user["sub"], per_page, offset),
            )
            payments = cur.fetchall()
            cur.execute("SELECT COUNT(*) FROM payments WHERE sender_id = %s", (g.current_user["sub"],))
            total = cur.fetchone()["count"]
    finally:
        release_db(conn)

    return jsonify({
        "payments": [dict(p) for p in payments],
        "pagination": {"page": page, "per_page": per_page, "total": total},
    }), 200


@app.before_request
def before_request():
    g.start_time = time.time()
    g.request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8001, debug=False)
