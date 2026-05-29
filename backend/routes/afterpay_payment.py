"""Afterpay payment integration.

Talks to the Afterpay (Clearpay) sandbox Online Checkout API directly over
HTTPS with ``httpx``. We create a checkout and return the redirect URL the
buyer is sent to in order to complete a "Pay in 4" plan.

Credentials / config come from the environment:

    AFTERPAY_MERCHANT_ID   (required) sandbox merchant id (Basic-auth username)
    AFTERPAY_SECRET_KEY    (required) sandbox secret key  (Basic-auth password)
    AFTERPAY_BASE_URL      (optional) defaults to the global sandbox host
    AFTERPAY_REDIRECT_CONFIRM_URL / AFTERPAY_REDIRECT_CANCEL_URL (optional)
    AFTERPAY_CURRENCY      (optional) defaults to USD

Reference: https://developers.afterpay.com/afterpay-online/reference (Checkouts).
"""

import logging
import os

import httpx

logger = logging.getLogger("payment-router.afterpay")

# Afterpay's per-order limits. The spec pins these explicitly.
AFTERPAY_MIN_AMOUNT = 0.04
AFTERPAY_MAX_AMOUNT = 2000.00

DEFAULT_BASE_URL = "https://global-api-sandbox.afterpay.com"
CURRENCY = os.getenv("AFTERPAY_CURRENCY", "USD")
REQUEST_TIMEOUT = 20.0


class AfterpayPaymentError(Exception):
    """Raised when an Afterpay checkout can't be created."""


async def create_afterpay_payment(amount: float, email: str, item_name: str) -> dict:
    """Create an Afterpay checkout and return the redirect details.

    Args:
        amount: Charge amount in dollars. Must be within Afterpay's
            $0.04–$2000 per-order limits.
        email: Buyer email, attached to the checkout consumer.
        item_name: Human-readable description of what's being purchased.

    Returns:
        ``{"checkout_token", "checkout_url", "status"}`` where ``checkout_url``
        is Afterpay's hosted ``redirectCheckoutUrl`` and ``status`` is
        ``"created"``.

    Raises:
        AfterpayPaymentError: on invalid amount, missing credentials, or any
            Afterpay API / network failure.
    """
    if not (AFTERPAY_MIN_AMOUNT <= amount <= AFTERPAY_MAX_AMOUNT):
        raise AfterpayPaymentError(
            f"amount ${amount:.2f} is outside Afterpay's "
            f"${AFTERPAY_MIN_AMOUNT:.2f}–${AFTERPAY_MAX_AMOUNT:.2f} limits"
        )

    merchant_id = os.getenv("AFTERPAY_MERCHANT_ID")
    secret_key = os.getenv("AFTERPAY_SECRET_KEY")
    if not merchant_id or not secret_key:
        raise AfterpayPaymentError(
            "AFTERPAY_MERCHANT_ID and AFTERPAY_SECRET_KEY must be configured"
        )

    base_url = os.getenv("AFTERPAY_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    amount_str = f"{amount:.2f}"
    money = {"amount": amount_str, "currency": CURRENCY}

    payload = {
        "amount": money,
        "consumer": {"email": email},
        "merchant": {
            "redirectConfirmUrl": os.getenv(
                "AFTERPAY_REDIRECT_CONFIRM_URL", "https://example.com/confirm"
            ),
            "redirectCancelUrl": os.getenv(
                "AFTERPAY_REDIRECT_CANCEL_URL", "https://example.com/cancel"
            ),
        },
        "items": [
            {
                "name": item_name,
                "quantity": 1,
                "price": money,
            }
        ],
    }

    logger.info(
        "Creating Afterpay checkout: %s for $%s (%s)", item_name, amount_str, email
    )

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.post(
                f"{base_url}/v2/checkouts",
                json=payload,
                auth=(merchant_id, secret_key),
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "cash-app-afterpay-router/1.0",
                },
            )
    except httpx.RequestError as exc:
        logger.error("Afterpay request failed: %s", exc)
        raise AfterpayPaymentError(f"Afterpay request failed: {exc}") from exc

    if response.status_code >= 400:
        logger.error(
            "Afterpay API error (status=%s): %s", response.status_code, response.text
        )
        raise AfterpayPaymentError(
            f"Afterpay API error {response.status_code}: {response.text}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise AfterpayPaymentError("Afterpay returned a non-JSON response") from exc

    token = data.get("token")
    checkout_url = data.get("redirectCheckoutUrl")
    if not token or not checkout_url:
        raise AfterpayPaymentError(f"Unexpected Afterpay response: {data}")

    result = {
        "checkout_token": token,
        "checkout_url": checkout_url,
        "status": "created",
    }
    logger.info("Afterpay checkout created: token=%s", token)
    return result
