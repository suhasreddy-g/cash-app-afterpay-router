"""Afterpay webhook handler.

Afterpay signs webhook deliveries with an HMAC-SHA256 of the raw request body
using a shared secret. We verify it in constant time, then record
``payment.approved`` / ``payment.cancelled`` / ``payment.declined`` events to the
shared transaction store.

Config:
    AFTERPAY_WEBHOOK_SECRET  (required for verification) shared HMAC secret
"""

import base64
import hashlib
import hmac
import json
import logging
import os

from fastapi import Request
from fastapi.responses import JSONResponse

from utils.transaction_store import append_transaction

logger = logging.getLogger("payment-router.webhook.afterpay")

HANDLED_EVENTS = {"payment.approved", "payment.cancelled", "payment.declined"}
SIGNATURE_HEADER = "x-afterpay-signature"


def _verify(raw_body: bytes, signature: str) -> bool:
    """Validate the Afterpay HMAC signature. Returns True if valid (or skipped)."""
    secret = os.getenv("AFTERPAY_WEBHOOK_SECRET")
    if not secret:
        # No secret configured — accept but warn. Production should always set it.
        logger.warning(
            "Afterpay webhook signature NOT verified (AFTERPAY_WEBHOOK_SECRET unset)"
        )
        return True

    if not signature:
        return False

    expected = base64.b64encode(
        hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    ).decode("utf-8")
    # Constant-time comparison to avoid leaking the signature via timing.
    return hmac.compare_digest(expected, signature)


async def handle_afterpay_webhook(request: Request) -> JSONResponse:
    """Process an incoming Afterpay webhook.

    Returns ``{"status": "success"}`` with 200 on handled events, 401 on a bad
    signature, and 400 on malformed payloads.
    """
    raw_body = await request.body()
    signature = request.headers.get(SIGNATURE_HEADER, "")

    if not _verify(raw_body, signature):
        logger.warning("Rejected Afterpay webhook: invalid signature")
        return JSONResponse(status_code=401, content={"status": "invalid signature"})

    try:
        event = json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError:
        logger.warning("Rejected Afterpay webhook: malformed JSON")
        return JSONResponse(status_code=400, content={"status": "invalid payload"})

    event_type = event.get("type") or event.get("eventType", "")
    if event_type not in HANDLED_EVENTS:
        logger.info("Ignoring unhandled Afterpay event: %s", event_type)
        return JSONResponse(status_code=200, content={"status": "ignored"})

    # Afterpay nests the payment under "data"; fall back to the top level.
    data = event.get("data", event) or {}
    amount = data.get("amount", {}) or {}

    record = {
        "source": "afterpay",
        "event": event_type,
        "afterpay_id": data.get("id") or data.get("orderId") or data.get("token"),
        "status": data.get("status") or event_type.split(".")[-1],
        "amount": float(amount["amount"]) if amount.get("amount") is not None else None,
        "currency": amount.get("currency"),
        "email": (data.get("consumer", {}) or {}).get("email") or data.get("email"),
        "timestamp": event.get("created")
        or event.get("createdAt")
        or data.get("created"),
    }

    await append_transaction(record)
    return JSONResponse(status_code=200, content={"status": "success"})
