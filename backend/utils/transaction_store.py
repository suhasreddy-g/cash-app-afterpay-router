"""Shared transaction store backed by ``webhooks/transactions.json``.

Both webhook handlers append to the same JSON array, and ``/api/transactions``
reads it back. Access is serialized with an asyncio lock so concurrent webhook
deliveries don't clobber each other's writes (last-read-wins on a shared file).
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger("payment-router.transactions")

# Resolve the path relative to the backend package root so it works no matter
# what the process working directory is.
TRANSACTIONS_FILE = Path(__file__).resolve().parent.parent / "webhooks" / "transactions.json"

_lock = asyncio.Lock()


def _read_sync() -> List[Dict[str, Any]]:
    """Read and parse the transactions file. Returns [] if missing/corrupt."""
    try:
        raw = TRANSACTIONS_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return []
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("transactions.json is not valid JSON; treating as empty")
        return []
    return data if isinstance(data, list) else []


def _write_sync(records: List[Dict[str, Any]]) -> None:
    TRANSACTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp file then replace, so a crash mid-write can't truncate
    # the existing transactions.
    tmp = TRANSACTIONS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(records, indent=2), encoding="utf-8")
    tmp.replace(TRANSACTIONS_FILE)


async def read_transactions() -> List[Dict[str, Any]]:
    """Return every logged transaction (newest entries last)."""
    async with _lock:
        return _read_sync()


async def append_transaction(record: Dict[str, Any]) -> Dict[str, Any]:
    """Append a single transaction record and return it."""
    async with _lock:
        records = _read_sync()
        records.append(record)
        _write_sync(records)
    logger.info(
        "Logged transaction: source=%s status=%s id=%s",
        record.get("source"),
        record.get("status"),
        record.get("payment_id") or record.get("afterpay_id"),
    )
    return record
