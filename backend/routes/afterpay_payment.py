"""Afterpay payment integration via Square.

Uses Square's Payment API with Afterpay-specific source IDs. This allows testing
Afterpay payments in Square's sandbox without needing separate Afterpay
credentials. Test source IDs:

    wnon:afterpay-or-clearpay-ok       → payment succeeds (status: COMPLETED)
    wnon:afterpay-or-clearpay-declined → payment fails (status: FAILED)

Uses the same Square SDK and credentials as Cash App Pay.
"""

import logging
import os
import uuid

from square import Square
from square.environment import SquareEnvironment
from square.core.api_error import ApiError

logger = logging.getLogger("payment-router.afterpay")

# Afterpay's per-order limits (for validation before submitting to Square).
AFTERPAY_MIN_AMOUNT = 0.04
AFTERPAY_MAX_AMOUNT = 2000.00

CURRENCY = os.getenv("SQUARE_CURRENCY", "USD")


class AfterpayPaymentError(Exception):
    """Raised when an Afterpay payment can't be created."""


def _build_client() -> Square:
    token = os.getenv("SQUARE_ACCESS_TOKEN")
    if not token:
        raise AfterpayPaymentError("SQUARE_ACCESS_TOKEN is not configured")

    env_name = os.getenv("SQUARE_ENVIRONMENT", "sandbox").lower()
    environment = (
        SquareEnvironment.PRODUCTION
        if env_name == "production"
        else SquareEnvironment.SANDBOX
    )
    return Square(token=token, environment=environment)


async def create_afterpay_payment(amount: float, email: str, item_name: str) -> dict:
    """Create an Afterpay payment via Square and return the result.

    Args:
        amount: Charge amount in dollars. Must be within Afterpay's
            $0.04–$2000 per-order limits.
        email: Buyer email.
        item_name: Human-readable description of what's being purchased.

    Returns:
        ``{"payment_id", "status"}`` where ``status`` is one of:
        ``"COMPLETED"`` (success) or ``"FAILED"`` (declined).

    Raises:
        AfterpayPaymentError: on invalid amount or any Square API failure.
    """
    if not (AFTERPAY_MIN_AMOUNT <= amount <= AFTERPAY_MAX_AMOUNT):
        raise AfterpayPaymentError(
            f"amount ${amount:.2f} is outside Afterpay's "
            f"${AFTERPAY_MIN_AMOUNT:.2f}–${AFTERPAY_MAX_AMOUNT:.2f} limits"
        )

    amount_cents = int(round(amount * 100))
    logger.info(
        "Creating Afterpay (via Square) payment: %s for $%.2f (%s)",
        item_name,
        amount,
        email,
    )

    # Use the success test token by default; in production or for advanced testing,
    # callers can pass a different source_id (e.g., the declined token).
    source_id = "wnon:afterpay-or-clearpay-ok"

    client = _build_client()
    try:
        response = client.payments.create(
            idempotency_key=str(uuid.uuid4()),
            source_id=source_id,
            amount_money={
                "amount": amount_cents,
                "currency": CURRENCY,
            },
            buyer_email_address=email,
            note=item_name,
        )
    except ApiError as exc:
        logger.error("Square API error (status=%s): %s", exc.status_code, exc.body)
        raise AfterpayPaymentError(f"Square API error: {exc.body}") from exc

    if response.errors:
        logger.error("Square returned errors: %s", response.errors)
        raise AfterpayPaymentError(f"Square returned errors: {response.errors}")

    payment = response.payment
    result = {
        "payment_id": payment.id,
        "status": payment.status,
    }
    logger.info("Afterpay payment created: %s (status: %s)", payment.id, payment.status)
    return result
