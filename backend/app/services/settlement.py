"""Stellar payment state: claiming, submitting and verifying against the ledger."""

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import HTTPException
from pymongo import ReturnDocument
from stellar_sdk.exceptions import BadRequestError, BaseHorizonError

from app import stellar
from app.common import (
    now_iso,
)
from app.database import NO_ID, db

log = logging.getLogger("agos.stellar")


async def set_stellar_status(pid: str, status: str, fields: dict, note: str, actor: str = "AGOS Workflow") -> None:
    upd = {"status": status, **{f"stellar.{k}": v for k, v in fields.items()}}
    await db.payments.update_one(
        {"id": pid},
        {
            "$set": upd,
            "$push": {
                "history": {
                    "status": status,
                    "actor_name": actor,
                    "actor_member_id": None,
                    "at": now_iso(),
                    "note": note,
                }
            },
        },
    )


def _ops_match(p: dict, found: dict) -> bool:
    return any(
        o.get("to") == p["destination_address"]
        and o.get("amount") is not None
        and Decimal(o["amount"]) == Decimal(str(p["amount"]))
        for o in found["operations"]
    )


async def refresh_payment(p: dict) -> None:
    st = p.get("stellar") or {}
    h = st.get("tx_hash")
    if not h or not st.get("submitted_at"):
        return
    found = await stellar.get_transaction(h)
    verification = {"checked_at": now_iso(), "found": found is not None}
    if found is None:
        submitted = datetime.fromisoformat(st["submitted_at"])
        expired = datetime.now(UTC) - submitted > timedelta(seconds=stellar.TX_VALIDITY_SECONDS + 30)
        if p["status"] in ("submitting", "pending_verification") and expired:
            await set_stellar_status(
                p["id"],
                "failed",
                {
                    "status": "not_found",
                    "verification": verification,
                    "last_error": "Not found on ledger and validity window expired — safe to retry",
                },
                "Transaction never reached the ledger",
            )
        else:
            await db.payments.update_one({"id": p["id"]}, {"$set": {"stellar.verification": verification}})
        return
    verification.update(
        {
            "successful": found["successful"],
            "ledger": found["ledger"],
            "source": found["source"],
            "operations": found["operations"],
            "fee_charged": found["fee_charged"],
            "matches_request": _ops_match(p, found),
        }
    )
    if found["successful"]:
        if p["status"] != "confirmed":
            await set_stellar_status(
                p["id"],
                "confirmed",
                {
                    "status": "confirmed",
                    "ledger": found["ledger"],
                    "confirmed_at": found["created_at"],
                    "verification": verification,
                },
                f"Confirmed on Stellar Testnet ledger {found['ledger']}",
            )
        else:
            await db.payments.update_one({"id": p["id"]}, {"$set": {"stellar.verification": verification}})
    else:
        await set_stellar_status(
            p["id"],
            "failed",
            {
                "status": "failed_on_ledger",
                "ledger": found["ledger"],
                "verification": verification,
                "last_error": "Transaction failed on ledger",
            },
            "Failed on ledger",
        )


async def claim_payment(ctx: dict, pid: str, from_statuses: list, to_status: str, key: str):
    await db.payments.update_one({"id": pid, "stellar": None}, {"$set": {"stellar": {}}})
    return await db.payments.find_one_and_update(
        {"id": pid, "workspace_id": ctx["ws_id"], "status": {"$in": from_statuses}},
        {"$set": {"status": to_status, "stellar.idempotency_key": key}},
        projection=NO_ID,
        return_document=ReturnDocument.AFTER,
    )


async def already_settled(ctx: dict, claimed: dict) -> bool:
    prev = claimed.get("stellar") or {}
    if not prev.get("tx_hash"):
        return False
    try:
        found = await stellar.get_transaction(prev["tx_hash"])
    except Exception:
        await set_stellar_status(
            claimed["id"],
            "failed",
            {"last_error": "Could not check previous attempt; network unavailable"},
            "Retry blocked",
        )
        raise HTTPException(status_code=504, detail="Stellar network unreachable; try again")
    if found and found["successful"]:
        await refresh_payment({**claimed, "status": "submitting"})
        return True
    return False


async def send_transaction(pid: str, tx) -> None:
    try:
        res = await stellar.submit(tx)
        if res.get("successful"):
            p2 = await db.payments.find_one({"id": pid}, NO_ID)
            try:
                await refresh_payment(p2)
            except Exception:
                await set_stellar_status(
                    pid,
                    "pending_verification",
                    {"status": "submitted", "ledger": res.get("ledger")},
                    "Accepted by Horizon; verification pending",
                )
        else:
            await set_stellar_status(
                pid, "failed", {"status": "failed", "last_error": "Horizon reported unsuccessful"}, "Failed"
            )
    except BadRequestError as e:
        codes = (e.extras or {}).get("result_codes") if hasattr(e, "extras") else None
        await set_stellar_status(
            pid,
            "failed",
            {"status": "rejected_by_network", "last_error": f"Rejected by network: {codes or e.status}"},
            "Rejected by Stellar network",
        )
    except (TimeoutError, BaseHorizonError, Exception) as e:
        log.warning("stellar submit uncertain: %s", type(e).__name__)
        await set_stellar_status(
            pid,
            "pending_verification",
            {"status": "unknown", "last_error": "Network timeout — use Verify to reconcile"},
            "Submission outcome unknown; awaiting verification",
        )


async def refresh_pending_payments() -> None:
    docs = await db.payments.find(
        {"status": {"$in": ["submitting", "pending_verification"]}, "stellar.tx_hash": {"$ne": None}}, NO_ID
    ).to_list(200)
    for p in docs:
        try:
            await refresh_payment(p)
        except Exception as e:
            log.warning("startup refresh failed: %s", type(e).__name__)


async def revert_signature(pid: str, note: str) -> bool:
    res = await db.payments.update_one(
        {"id": pid, "status": "awaiting_signature"},
        {
            "$set": {
                "status": "approved",
                "stellar.status": "cancelled",
                "stellar.tx_hash": None,
                "stellar.unsigned_xdr": None,
            },
            "$push": {
                "history": {
                    "status": "approved",
                    "actor_name": "AGOS Workflow",
                    "actor_member_id": None,
                    "at": now_iso(),
                    "note": note,
                }
            },
        },
    )
    return res.modified_count == 1
