"""Payment router backend.

Routes a checkout to Cash App Pay (via Square) and/or Afterpay based on the
order amount, exposes the per-provider payment endpoints, receives provider
webhooks, and serves the logged transaction history.

Run with:
    uvicorn main:app --reload
"""

import logging
import os
import time

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from routes.square_payment import create_square_payment, SquarePaymentError
from routes.afterpay_payment import create_afterpay_payment, AfterpayPaymentError
from webhooks.square_webhook import handle_square_webhook
from webhooks.afterpay_webhook import handle_afterpay_webhook
from utils.transaction_store import read_transactions

# --- Environment ---------------------------------------------------------

load_dotenv()

REQUIRED_ENV_VARS = ["SQUARE_ACCESS_TOKEN", "SQUARE_APP_ID"]

missing = [name for name in REQUIRED_ENV_VARS if not os.getenv(name)]
if missing:
    raise RuntimeError(
        "Missing required environment variable(s): "
        + ", ".join(missing)
        + ". Add them to backend/.env before starting the server."
    )

SQUARE_ACCESS_TOKEN = os.getenv("SQUARE_ACCESS_TOKEN")
SQUARE_APP_ID = os.getenv("SQUARE_APP_ID")

# Amount thresholds (USD) that decide which payment method(s) to offer.
CASH_APP_MAX = 50.0    # below this -> Cash App Pay only
AFTERPAY_MIN = 500.0   # above this -> Afterpay only; in between -> both

# --- Logging -------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("payment-router")

# --- App -----------------------------------------------------------------

app = FastAPI(title="Cash App / Afterpay Payment Router")

# Allowed origins:
#   - http://localhost:8080, http://localhost:3000  (local frontend dev)
#   - https://<anything>.vercel.app                 (Vercel-deployed frontend)
#   - https://<anything>.onrender.com               (Render-deployed frontend)
# Wildcard subdomains can't go in allow_origins (it does exact matches), so
# Vercel/Render are matched via allow_origin_regex instead.
ALLOWED_ORIGINS = [
    "https://cash-app-afterpay-router.vercel.app",
    "https://cash-app-afterpay-router.onrender.com",
    "http://localhost:3000",
    "http://localhost:8080",
]
ALLOWED_ORIGIN_REGEX = r"https://([a-z0-9-]+\.)*(vercel\.app|onrender\.com)$"

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=ALLOWED_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log every request with a timestamp and how long it took."""
    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - start) * 1000
    logger.info(
        "%s %s -> %s (%.1f ms)",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response


# --- Schemas -------------------------------------------------------------

class CheckoutRequest(BaseModel):
    amount: float = Field(..., gt=0, description="Charge amount in dollars")
    email: str = Field(..., pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    itemName: str = Field(..., min_length=1)


# --- Payment helpers -----------------------------------------------------

async def _square_option(req: CheckoutRequest) -> dict:
    """Create a Cash App Pay option. Square's SDK is sync -> run off-loop."""
    result = await run_in_threadpool(
        create_square_payment, req.amount, req.email, req.itemName
    )
    return {
        "payment_method": "cash_app_pay",
        "checkout_url": result["checkout_url"],
        "payment_id": result["payment_id"],
    }


async def _afterpay_option(req: CheckoutRequest) -> dict:
    """Create an Afterpay option."""
    result = await create_afterpay_payment(req.amount, req.email, req.itemName)
    return {
        "payment_method": "afterpay",
        "checkout_url": result["checkout_url"],
        "payment_id": result["checkout_token"],
    }


# --- Routes --------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/api/checkout")
async def checkout(payload: CheckoutRequest):
    """Route a checkout to the appropriate payment method(s) by amount.

        amount < $50      -> Cash App Pay only
        $50 <= amt <= $500 -> both options returned (buyer picks)
        amount > $500     -> Afterpay only
    """
    logger.info(
        "Checkout: %s for $%.2f (%s)",
        payload.itemName,
        payload.amount,
        payload.email,
    )

    try:
        if payload.amount < CASH_APP_MAX:
            return await _square_option(payload)

        if payload.amount > AFTERPAY_MIN:
            return await _afterpay_option(payload)

        # Mid-range: offer both and let the buyer choose.
        return {
            "payment_method": "both",
            "options": [
                await _square_option(payload),
                await _afterpay_option(payload),
            ],
        }
    except (SquarePaymentError, AfterpayPaymentError) as exc:
        logger.error("Checkout failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/api/payments/square")
async def payments_square(payload: CheckoutRequest):
    """Create a Cash App Pay (Square) checkout directly."""
    try:
        return await run_in_threadpool(
            create_square_payment, payload.amount, payload.email, payload.itemName
        )
    except SquarePaymentError as exc:
        logger.error("Square payment failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/api/payments/afterpay")
async def payments_afterpay(payload: CheckoutRequest):
    """Create an Afterpay checkout directly."""
    try:
        return await create_afterpay_payment(
            payload.amount, payload.email, payload.itemName
        )
    except AfterpayPaymentError as exc:
        logger.error("Afterpay payment failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/webhooks/square")
async def webhooks_square(request: Request):
    """Receive Square payment webhooks."""
    return await handle_square_webhook(request)


@app.post("/webhooks/afterpay")
async def webhooks_afterpay(request: Request):
    """Receive Afterpay payment webhooks."""
    return await handle_afterpay_webhook(request)


@app.get("/api/transactions")
async def transactions():
    """Return every webhook-logged transaction."""
    records = await read_transactions()
    return {"count": len(records), "transactions": records}


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
