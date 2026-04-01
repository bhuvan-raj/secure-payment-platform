"""
Auth Service - JWT-based authentication for the payment platform.
Production-grade with structured logging, health checks, and metrics.
"""

import os
import time
import logging
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps

import boto3
import jwt
import bcrypt
from flask import Flask, request, jsonify, g
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from pythonjsonlogger import jsonlogger
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import pool

# ── Logging setup ────────────────────────────────────────────────────────────
logger = logging.getLogger(__name__)
handler = logging.StreamHandler()
handler.setFormatter(jsonlogger.JsonFormatter(
    "%(asctime)s %(name)s %(levelname)s %(message)s"
))
logger.addHandler(handler)
logger.setLevel(logging.INFO)

# ── App init ─────────────────────────────────────────────────────────────────
app = Flask(__name__)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per minute"],
    storage_uri="memory://",
)

# ── Prometheus metrics ────────────────────────────────────────────────────────
REQUEST_COUNT = Counter(
    "auth_requests_total", "Total auth requests", ["method", "endpoint", "status"]
)
REQUEST_LATENCY = Histogram(
    "auth_request_duration_seconds", "Auth request latency", ["endpoint"]
)
LOGIN_ATTEMPTS = Counter(
    "auth_login_attempts_total", "Login attempts", ["status"]
)
TOKEN_ISSUED = Counter("auth_tokens_issued_total", "JWT tokens issued")

# ── AWS clients ───────────────────────────────────────────────────────────────
secrets_client = boto3.client("secretsmanager", region_name=os.environ["AWS_REGION"])


def get_secret(secret_name: str) -> dict:
    """Fetch secret from AWS Secrets Manager with caching."""
    import json
    response = secrets_client.get_secret_value(SecretId=secret_name)
    return json.loads(response["SecretString"])


# ── DB connection pool ────────────────────────────────────────────────────────
_db_pool = None


def get_db_pool():
    global _db_pool
    if _db_pool is None:
        secret = get_secret(os.environ["DB_SECRET_ARN"])
        _db_pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=10,
            host=secret["host"],
            port=secret.get("port", 5432),
            dbname=secret["dbname"],
            user=secret["username"],
            password=secret["password"],
            sslmode="require",
        )
        logger.info("Database connection pool initialized")
    return _db_pool


def get_db():
    return get_db_pool().getconn()


def release_db(conn):
    get_db_pool().putconn(conn)


# ── JWT helpers ───────────────────────────────────────────────────────────────
def get_jwt_secret() -> str:
    secret = get_secret(os.environ["JWT_SECRET_ARN"])
    return secret["jwt_secret"]


def generate_token(user_id: str, email: str, roles: list) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "roles": roles,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        "jti": str(uuid.uuid4()),
    }
    token = jwt.encode(payload, get_jwt_secret(), algorithm="HS256")
    TOKEN_ISSUED.inc()
    return token


def verify_token(token: str) -> dict:
    return jwt.decode(token, get_jwt_secret(), algorithms=["HS256"])


# ── Middleware ────────────────────────────────────────────────────────────────
@app.before_request
def before_request():
    g.start_time = time.time()
    g.request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))


@app.after_request
def after_request(response):
    duration = time.time() - g.start_time
    REQUEST_COUNT.labels(
        method=request.method,
        endpoint=request.endpoint or "unknown",
        status=response.status_code,
    ).inc()
    REQUEST_LATENCY.labels(endpoint=request.endpoint or "unknown").observe(duration)
    response.headers["X-Request-ID"] = g.request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Missing or invalid Authorization header"}), 401
        token = auth_header.split(" ", 1)[1]
        try:
            g.current_user = verify_token(token)
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "Token expired"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"error": "Invalid token"}), 401
        return f(*args, **kwargs)
    return decorated


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    """Kubernetes liveness probe."""
    return jsonify({"status": "healthy", "service": "auth-service", "version": os.environ.get("APP_VERSION", "1.0.0")}), 200


@app.route("/ready", methods=["GET"])
def ready():
    """Kubernetes readiness probe — checks DB connectivity."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        release_db(conn)
        return jsonify({"status": "ready"}), 200
    except Exception as e:
        logger.error("Readiness check failed", extra={"error": str(e)})
        return jsonify({"status": "not ready", "error": "DB unavailable"}), 503


@app.route("/metrics", methods=["GET"])
def metrics():
    """Prometheus metrics endpoint."""
    return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}


@app.route("/v1/auth/register", methods=["POST"])
@limiter.limit("10 per minute")
def register():
    data = request.get_json(force=True, silent=True) or {}
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")
    full_name = data.get("full_name", "").strip()

    if not email or not password or not full_name:
        return jsonify({"error": "email, password, and full_name are required"}), 400
    if len(password) < 12:
        return jsonify({"error": "Password must be at least 12 characters"}), 400

    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
    user_id = str(uuid.uuid4())

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (id, email, password_hash, full_name, roles, created_at)
                VALUES (%s, %s, %s, %s, %s, NOW())
                """,
                (user_id, email, password_hash, full_name, ["user"]),
            )
            conn.commit()
        logger.info("User registered", extra={"user_id": user_id, "request_id": g.request_id})
        return jsonify({"message": "User registered successfully", "user_id": user_id}), 201
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        return jsonify({"error": "Email already registered"}), 409
    except Exception as e:
        conn.rollback()
        logger.error("Registration failed", extra={"error": str(e)})
        return jsonify({"error": "Internal server error"}), 500
    finally:
        release_db(conn)


@app.route("/v1/auth/login", methods=["POST"])
@limiter.limit("20 per minute")
def login():
    data = request.get_json(force=True, silent=True) or {}
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")

    if not email or not password:
        return jsonify({"error": "email and password are required"}), 400

    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, email, password_hash, roles, is_active FROM users WHERE email = %s",
                (email,),
            )
            user = cur.fetchone()
    finally:
        release_db(conn)

    if not user or not bcrypt.checkpw(password.encode(), user["password_hash"].encode()):
        LOGIN_ATTEMPTS.labels(status="failed").inc()
        logger.warning("Failed login attempt", extra={"email": email, "request_id": g.request_id})
        return jsonify({"error": "Invalid credentials"}), 401

    if not user["is_active"]:
        return jsonify({"error": "Account disabled"}), 403

    token = generate_token(str(user["id"]), user["email"], user["roles"])
    LOGIN_ATTEMPTS.labels(status="success").inc()
    logger.info("Successful login", extra={"user_id": str(user["id"]), "request_id": g.request_id})
    return jsonify({"access_token": token, "token_type": "Bearer", "expires_in": 3600}), 200


@app.route("/v1/auth/verify", methods=["POST"])
def verify():
    """Internal endpoint for other services to validate tokens."""
    data = request.get_json(force=True, silent=True) or {}
    token = data.get("token", "")
    try:
        payload = verify_token(token)
        return jsonify({"valid": True, "payload": payload}), 200
    except jwt.ExpiredSignatureError:
        return jsonify({"valid": False, "error": "Token expired"}), 200
    except jwt.InvalidTokenError:
        return jsonify({"valid": False, "error": "Invalid token"}), 200


@app.route("/v1/auth/me", methods=["GET"])
@require_auth
def me():
    return jsonify({"user": g.current_user}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
