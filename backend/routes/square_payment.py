"""Square payment integration (used for the Cash App Pay flow).

We create a Square hosted **Payment Link** restricted to Cash App Pay. That
gives us a ``checkout_url`` we can redirect the buyer to, plus a payment-link
id we can correlate with the eventual ``payment.created`` webhook.

Credentials come from the environment (loaded by ``main.py`` via python-dotenv):

    SQUARE_ACCESS_TOKEN   (required) sandbox access token
    SQUARE_LOCATION_ID    (optional) location to attribute the sale to; if unset
                          we look up the first location on the account
    SQUARE_ENVIRONMENT    (optional) "sandbox" (default) or "production"
    SQUARE_REDIRECT_URL   (optional) where Square sends the buyer after paying
    SQUARE_SUPPORT_EMAIL  (optional) merchant support email shown at checkout
"""

import logging
import os
import uuid

from square import Square
from square.environment import SquareEnvironment
from square.core.api_error import ApiError

logger = logging.getLogger("payment-router.square")

# Cash App Pay (and Square in general) deals in the smallest currency unit.
CURRENCY = os.getenv("SQUARE_CURRENCY", "USD")

# Cache the resolved location id across calls so we don't hit the Locations API
# on every checkout.
_cached_location_id: str | None = None


class SquarePaymentError(Exception):
    """Raised when a Square payment link can't be created."""


def _build_client() -> Square:
    token = os.getenv("SQUARE_ACCESS_TOKEN")
    if not token:
        raise SquarePaymentError("SQUARE_ACCESS_TOKEN is not configured")

    env_name = os.getenv("SQUARE_ENVIRONMENT", "sandbox").lower()
    environment = (
        SquareEnvironment.PRODUCTION
        if env_name == "production"
        else SquareEnvironment.SANDBOX
    )
    return Square(token=token, environment=environment)


def _resolve_location_id(client: Square) -> str:
    """Return the configured location id, or look up the first one available."""
    global _cached_location_id

    configured = os.getenv("SQUARE_LOCATION_ID")
    if configured:
        return configured

    if _cached_location_id:
        return _cached_location_id

    try:
        response = client.locations.list()
    except ApiError as exc:
        raise SquarePaymentError(f"Could not list Square locations: {exc.body}") from exc

    locations = response.locations or []
    if not locations:
        raise SquarePaymentError(
            "No Square locations found. Set SQUARE_LOCATION_ID in the environment."
        )
    _cached_location_id = locations[0].id
    logger.info("Resolved Square location id: %s", _cached_location_id)
    return _cached_location_id


def create_square_payment(amount: float, email: str, item_name: str) -> dict:
    """Create a Cash App Pay checkout via a Square payment link.

    Args:
        amount: Charge amount in dollars (e.g. 49.99).
        email: Buyer email, pre-populated on the checkout page.
        item_name: Human-readable description of what's being purchased.

    Returns:
        ``{"payment_id", "checkout_url", "status"}`` where ``payment_id`` is the
        Square payment-link id (correlate with webhooks) and ``status`` is
        ``"created"``.

    Raises:
        SquarePaymentError: on invalid input or any Square API failure.
    """
    if amount <= 0:
        raise SquarePaymentError("amount must be greater than 0")

    amount_cents = int(round(amount * 100))
    logger.info(
        "Creating Square (Cash App Pay) link: %s for $%.2f (%s)",
        item_name,
        amount,
        email,
    )

    client = _build_client()
    location_id = _resolve_location_id(client)

    checkout_options = {
        # Restrict the hosted checkout to Cash App Pay only.
        "accepted_payment_methods": {"cash_app_pay": True},
    }
    redirect_url = os.getenv("SQUARE_REDIRECT_URL")
    if redirect_url:
        checkout_options["redirect_url"] = redirect_url
    support_email = os.getenv("SQUARE_SUPPORT_EMAIL")
    if support_email:
        checkout_options["merchant_support_email"] = support_email

    try:
        response = client.checkout.payment_links.create(
            idempotency_key=str(uuid.uuid4()),
            quick_pay={
                "name": item_name,
                "price_money": {"amount": amount_cents, "currency": CURRENCY},
                "location_id": location_id,
            },
            checkout_options=checkout_options,
            pre_populated_data={"buyer_email": email},
        )
    except ApiError as exc:
        logger.error("Square API error (status=%s): %s", exc.status_code, exc.body)
        raise SquarePaymentError(f"Square API error: {exc.body}") from exc

    if response.errors:
        logger.error("Square returned errors: %s", response.errors)
        raise SquarePaymentError(f"Square returned errors: {response.errors}")

    link = response.payment_link
    result = {
        "payment_id": link.id,
        "checkout_url": link.url,
        "status": "created",
    }
    logger.info("Square payment link created: %s", link.id)
    return result
