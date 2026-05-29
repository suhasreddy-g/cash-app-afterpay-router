"""Payment router backend.

Run with:
    uvicorn main:app --reload
"""

import logging
import os
import time

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

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
    "https://cash-app-afterpay-router.vercel.app/",     # ← Add your Vercel URL
    "https://cash-app-afterpay-router.onrender.com",   # ← Remove trailing slash
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


# --- Routes --------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/api/checkout")
async def checkout(payload: CheckoutRequest):
    logger.info(
        "Checkout: %s for $%.2f (%s)",
        payload.itemName,
        payload.amount,
        payload.email,
    )
    # TODO: route to Square / Cash App / Afterpay payment provider.
    return {
        "status": "received",
        "itemName": payload.itemName,
        "amount": payload.amount,
        "email": payload.email,
    }


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
