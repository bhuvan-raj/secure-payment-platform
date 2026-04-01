"""
Transaction Service - Immutable ledger for all financial transactions.
Processes payments from SQS queue and maintains double-entry bookkeeping.
"""

import os
import json
import time
import uuid
import logging
import decimal
import signal
import threading
from datetime import datetime, timezone

import boto3
from prometheus_client import Counter, Histogram, Gauge, start_http_server
from pythonjsonlogger import jsonlogger
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import pool

# ── Logging ───────────────────────────────────────────────────────────────────
logger = logging.getLogger(__name__)
handler = logging.StreamHandler()
handler.setFormatter(jsonlogger.JsonFormatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
logger.addHandler(handler)
logger.setLevel(logging.INFO)

# ── Metrics ───────────────────────────────────────────────────────────────────
TRANSACTIONS_PROCESSED = Counter("txn_processed_total", "Transactions processed", ["status"])
PROCESSING_LATENCY = Histogram("txn_processing_seconds", "Transaction processing time")
QUEUE_DEPTH = Gauge("txn_queue_depth", "Approximate SQS queue depth")

# ── AWS clients ───────────────────────────────────────────────────────────────
secrets_client = boto3.client("secretsmanager", region_name=os.environ["AWS_REGION"])
sqs_client = boto3.client("sqs", region_name=os.environ["AWS_REGION"])

QUEUE_URL = os.environ["PAYMENT_QUEUE_URL"]


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
        logger.info("DB pool initialized")
    return _db_pool


def get_db():
    return get_db_pool().getconn()


def release_db(conn):
    get_db_pool().putconn(conn)


# ── Transaction logic ─────────────────────────────────────────────────────────
def process_payment(payment_data: dict) -> bool:
    """
    Process a payment using double-entry bookkeeping.
    Debits sender's account, credits recipient's account atomically.
    Returns True on success, False on failure.
    """
    payment_id = payment_data["payment_id"]
    sender_id = payment_data["sender_id"]
    recipient_id = payment_data["recipient_id"]
    amount = decimal.Decimal(payment_data["amount"])
    currency = payment_data["currency"]

    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Lock payment row
            cur.execute(
                "SELECT status FROM payments WHERE id = %s FOR UPDATE",
                (payment_id,),
            )
            payment = cur.fetchone()
            if not payment:
                logger.error("Payment not found", extra={"payment_id": payment_id})
                return False
            if payment["status"] != "pending":
                logger.warning("Payment not in pending state", extra={"payment_id": payment_id, "status": payment["status"]})
                return True  # Already processed

            # Mark as processing
            cur.execute(
                "UPDATE payments SET status = 'processing', updated_at = NOW() WHERE id = %s",
                (payment_id,),
            )

            # Check sender balance
            cur.execute(
                "SELECT balance FROM accounts WHERE user_id = %s AND currency = %s FOR UPDATE",
                (sender_id, currency),
            )
            sender_acct = cur.fetchone()
            if not sender_acct or sender_acct["balance"] < amount:
                cur.execute(
                    "UPDATE payments SET status = 'failed', failure_reason = 'Insufficient funds', updated_at = NOW() WHERE id = %s",
                    (payment_id,),
                )
                conn.commit()
                logger.warning("Insufficient funds", extra={"payment_id": payment_id, "sender_id": sender_id})
                return True  # Business failure, not a processing error

            txn_id = str(uuid.uuid4())

            # Debit sender
            cur.execute(
                """
                UPDATE accounts SET balance = balance - %s, updated_at = NOW()
                WHERE user_id = %s AND currency = %s
                """,
                (amount, sender_id, currency),
            )
            cur.execute(
                """
                INSERT INTO transactions (id, payment_id, account_id, type, amount, currency,
                                          running_balance, created_at)
                SELECT %s, %s, id, 'debit', %s, %s, balance, NOW()
                FROM accounts WHERE user_id = %s AND currency = %s
                """,
                (txn_id, payment_id, amount, currency, sender_id, currency),
            )

            # Credit recipient (create account if not exists)
            cur.execute(
                """
                INSERT INTO accounts (id, user_id, currency, balance, created_at, updated_at)
                VALUES (uuid_generate_v4(), %s, %s, 0, NOW(), NOW())
                ON CONFLICT (user_id, currency) DO NOTHING
                """,
                (recipient_id, currency),
            )
            cur.execute(
                """
                UPDATE accounts SET balance = balance + %s, updated_at = NOW()
                WHERE user_id = %s AND currency = %s
                """,
                (amount, recipient_id, currency),
            )
            credit_txn_id = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO transactions (id, payment_id, account_id, type, amount, currency,
                                          running_balance, created_at)
                SELECT %s, %s, id, 'credit', %s, %s, balance, NOW()
                FROM accounts WHERE user_id = %s AND currency = %s
                """,
                (credit_txn_id, payment_id, amount, currency, recipient_id, currency),
            )

            # Mark payment complete
            cur.execute(
                "UPDATE payments SET status = 'completed', processed_at = NOW(), updated_at = NOW() WHERE id = %s",
                (payment_id,),
            )
            conn.commit()

        logger.info("Payment processed", extra={"payment_id": payment_id, "amount": str(amount), "currency": currency})
        return True

    except Exception as e:
        conn.rollback()
        logger.error("Payment processing error", extra={"payment_id": payment_id, "error": str(e)})
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE payments SET status = 'failed', failure_reason = %s, updated_at = NOW() WHERE id = %s AND status = 'processing'",
                    (str(e)[:500], payment_id),
                )
            conn.commit()
        except Exception:
            pass
        return False
    finally:
        release_db(conn)


# ── SQS consumer ─────────────────────────────────────────────────────────────
shutdown_event = threading.Event()


def handle_shutdown(signum, frame):
    logger.info("Shutdown signal received")
    shutdown_event.set()


signal.signal(signal.SIGTERM, handle_shutdown)
signal.signal(signal.SIGINT, handle_shutdown)


def update_queue_depth():
    try:
        resp = sqs_client.get_queue_attributes(
            QueueUrl=QUEUE_URL,
            AttributeNames=["ApproximateNumberOfMessages"],
        )
        depth = int(resp["Attributes"].get("ApproximateNumberOfMessages", 0))
        QUEUE_DEPTH.set(depth)
    except Exception:
        pass


def consume_loop():
    logger.info("SQS consumer starting", extra={"queue_url": QUEUE_URL})
    while not shutdown_event.is_set():
        try:
            update_queue_depth()
            response = sqs_client.receive_message(
                QueueUrl=QUEUE_URL,
                MaxNumberOfMessages=10,
                WaitTimeSeconds=20,  # Long polling
                VisibilityTimeout=60,
            )
            messages = response.get("Messages", [])
            for msg in messages:
                start = time.time()
                try:
                    payment_data = json.loads(msg["Body"])
                    success = process_payment(payment_data)
                    status = "success" if success else "failed"
                    TRANSACTIONS_PROCESSED.labels(status=status).inc()
                    PROCESSING_LATENCY.observe(time.time() - start)
                    # Always delete — failures are handled in DB
                    sqs_client.delete_message(
                        QueueUrl=QUEUE_URL,
                        ReceiptHandle=msg["ReceiptHandle"],
                    )
                except json.JSONDecodeError:
                    logger.error("Invalid JSON in SQS message", extra={"message_id": msg.get("MessageId")})
                    sqs_client.delete_message(QueueUrl=QUEUE_URL, ReceiptHandle=msg["ReceiptHandle"])
                except Exception as e:
                    logger.error("Unhandled error processing message", extra={"error": str(e)})
        except Exception as e:
            logger.error("SQS receive error", extra={"error": str(e)})
            time.sleep(5)

    logger.info("SQS consumer stopped")


if __name__ == "__main__":
    # Expose Prometheus metrics on port 8002
    start_http_server(8002)
    logger.info("Metrics server started on :8002")
    consume_loop()
