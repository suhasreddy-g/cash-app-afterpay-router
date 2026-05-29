"""Square webhook handler.

Verifies the ``x-square-hmacsha256-signature`` header using the Square SDK's
webhook helper, then records ``payment.created`` / ``payment.updated`` events to
the shared transaction store.

Config:
    SQUARE_WEBHOOK_SIGNATURE_KEY  (required for verification) signature key from
        the Square webhook subscription
    SQUARE_WEBHOOK_NOTIFICATION_URL  (required for verification) the exact URL
        Square is configured to POST to (must match byte-for-byte)
"""

import json
import logging
import os

from fastapi import Request
from fastapi.responses import JSONResponse
from square.utils.webhooks_helper import verify_signature

from utils.transaction_store import append_transaction

logger = logging.getLogger("payment-router.webhook.square")

HANDLED_EVENTS = {"payment.created", "payment.updated"}
SIGNATURE_HEADER = "x-square-hmacsha256-signature"


def _verify(body: str, signature: str) -> bool:
    """Validate the Square signature. Returns True if valid (or skipped)."""
    signature_key = os.getenv("SQUARE_WEBHOOK_SIGNATURE_KEY")
    notification_url = os.getenv("SQUARE_WEBHOOK_NOTIFICATION_URL")

    if not signature_key or not notification_url:
        # No verification material configured — accept but warn loudly. This
        # keeps the sandbox usable before keys are wired up; production should
        # always set both.
        logger.warning(
            "Square webhook signature NOT verified "
            "(SQUARE_WEBHOOK_SIGNATURE_KEY / SQUARE_WEBHOOK_NOTIFICATION_URL unset)"
        )
        return True

    return verify_signature(
        request_body=body,
        signature_header=signature,
        signature_key=signature_key,
        notification_url=notification_url,
    )


async def handle_square_webhook(request: Request) -> JSONResponse:
    """Process an incoming Square webhook.

    Returns ``{"status": "success"}`` with 200 on handled events, 401 on a bad
    signature, and 400 on malformed payloads.
    """
    raw_body = await request.body()
    body_str = raw_body.decode("utf-8")
    signature = request.headers.get(SIGNATURE_HEADER, "")

    if not _verify(body_str, signature):
        logger.warning("Rejected Square webhook: invalid signature")
        return JSONResponse(status_code=401, content={"status": "invalid signature"})

    try:
        event = json.loads(body_str)
    except json.JSONDecodeError:
        logger.warning("Rejected Square webhook: malformed JSON")
        return JSONResponse(status_code=400, content={"status": "invalid payload"})

    event_type = event.get("type", "")
    if event_type not in HANDLED_EVENTS:
        # Acknowledge unhandled events so Square doesn't retry them.
        logger.info("Ignoring unhandled Square event: %s", event_type)
        return JSONResponse(status_code=200, content={"status": "ignored"})

    payment = (event.get("data", {}).get("object", {}) or {}).get("payment", {}) or {}
    amount_money = payment.get("amount_money", {}) or {}
    amount_cents = amount_money.get("amount")

    record = {
        "source": "square",
        "event": event_type,
        "payment_id": payment.get("id"),
        "status": payment.get("status"),
        "amount": round(amount_cents / 100, 2) if amount_cents is not None else None,
        "currency": amount_money.get("currency"),
        "email": payment.get("buyer_email_address"),
        "timestamp": payment.get("updated_at")
        or payment.get("created_at")
        or event.get("created_at"),
    }

    await append_transaction(record)
    return JSONResponse(status_code=200, content={"status": "success"})
