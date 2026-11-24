"""Settling approved payments on Stellar Testnet."""

import logging
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app import stellar
from app.common import MANAGERS, audit, history_entry, now_iso
from app.database import NO_ID, db
from app.dependencies import require_roles, ws_ctx
from app.services.payments import find_payment
from app.services.settlement import (
    already_settled,
    claim_payment,
    refresh_payment,
    revert_signature,
    send_transaction,
    set_stellar_status,
)

router = APIRouter(prefix="/api")


log = logging.getLogger("agos.stellar")


class SubmitIn(BaseModel):
    confirm: bool
    idempotency_key: str = Field(min_length=8, max_length=64)


@router.post("/payments/{pid}/stellar/submit")
async def stellar_submit(pid: str, body: SubmitIn, ctx: dict = Depends(require_roles(*MANAGERS))):
    if not body.confirm:
        raise HTTPException(status_code=422, detail="Explicit confirmation is required")
    p = await find_payment(ctx, pid)
    if p["type"] != "international":
        raise HTTPException(status_code=409, detail="Only international payments settle on Stellar")
    if p["status"] in ("submitting", "pending_verification", "confirmed"):
        return {"payment": p, "idempotent": True}
    if p["status"] not in ("approved", "failed"):
        raise HTTPException(status_code=409, detail="Payment must be approved before submission")
    acct = await db.stellar_accounts.find_one({"workspace_id": ctx["ws_id"]}, NO_ID)
    if not acct:
        raise HTTPException(status_code=409, detail="Create the workspace Stellar Testnet account first")
    if acct.get("mode") == "wallet":
        raise HTTPException(
            status_code=409, detail="This workspace signs with its connected wallet; use wallet signing"
        )
    claimed = await claim_payment(ctx, pid, ["approved", "failed"], "submitting", body.idempotency_key)
    if not claimed:
        return {"payment": await find_payment(ctx, pid), "idempotent": True}
    prev = claimed.get("stellar") or {}
    if await already_settled(ctx, claimed):
        return {"payment": await find_payment(ctx, pid), "idempotent": True}
    amount = claimed.get("amount_str") or f"{Decimal(str(claimed['amount'])):.7f}"
    try:
        tx, tx_hash, source, op = await stellar.build_signed(
            acct["encrypted_secret"], claimed["destination_address"], amount, f"AGOS {pid[:8]}", claimed["asset_code"]
        )
    except ValueError as e:
        await set_stellar_status(pid, "failed", {"last_error": str(e)}, "Validation failed before submission")
        return {"payment": await find_payment(ctx, pid), "idempotent": False}
    except Exception as e:
        log.warning("stellar build failed: %s", type(e).__name__)
        await set_stellar_status(
            pid,
            "failed",
            {"last_error": "Could not load workspace account from Stellar (unfunded or network unavailable)"},
            "Build failed",
        )
        return {"payment": await find_payment(ctx, pid), "idempotent": False}
    st = {
        "network": stellar.NETWORK,
        "source": source,
        "destination": claimed["destination_address"],
        "asset_code": claimed["asset_code"],
        "asset_issuer": claimed.get("asset_issuer"),
        "amount": amount,
        "tx_hash": tx_hash,
        "operation": op,
        "ledger": None,
        "submitted_at": now_iso(),
        "confirmed_at": None,
        "status": "submitting",
        "attempts": prev.get("attempts", 0) + 1,
        "last_error": None,
        "idempotency_key": body.idempotency_key,
        "verification": None,
        "submitted_by": ctx["actor_name"],
    }
    await db.payments.update_one(
        {"id": pid},
        {
            "$set": {"stellar": st},
            "$push": {"history": history_entry(ctx, "submitting", f"Confirmed and signed; tx {tx_hash[:12]}…")},
        },
    )
    await audit(ctx, "stellar.submitted", "payment", pid, tx_hash)
    await send_transaction(pid, tx)
    return {"payment": await find_payment(ctx, pid), "idempotent": False}


@router.post("/payments/{pid}/stellar/prepare")
async def stellar_prepare(pid: str, body: SubmitIn, ctx: dict = Depends(require_roles(*MANAGERS))):
    if not body.confirm:
        raise HTTPException(status_code=422, detail="Explicit confirmation is required")
    p = await find_payment(ctx, pid)
    if p["type"] != "international":
        raise HTTPException(status_code=409, detail="Only international payments settle on Stellar")
    if p["status"] in ("submitting", "pending_verification", "confirmed"):
        return {"payment": p, "idempotent": True, "xdr": None}
    if p["status"] not in ("approved", "failed", "awaiting_signature"):
        raise HTTPException(status_code=409, detail="Payment must be approved before submission")
    acct = await db.stellar_accounts.find_one({"workspace_id": ctx["ws_id"]}, NO_ID)
    if not acct or acct.get("mode") != "wallet":
        raise HTTPException(status_code=409, detail="Connect a Stellar wallet for this workspace first")
    claimed = await claim_payment(
        ctx, pid, ["approved", "failed", "awaiting_signature"], "awaiting_signature", body.idempotency_key
    )
    if not claimed:
        return {"payment": await find_payment(ctx, pid), "idempotent": True, "xdr": None}
    if await already_settled(ctx, claimed):
        return {"payment": await find_payment(ctx, pid), "idempotent": True, "xdr": None}
    prev = claimed.get("stellar") or {}
    amount = claimed.get("amount_str") or f"{Decimal(str(claimed['amount'])):.7f}"
    try:
        tx, tx_hash, xdr, op = await stellar.build_unsigned(
            acct["public_key"], claimed["destination_address"], amount, f"AGOS {pid[:8]}", claimed["asset_code"]
        )
    except ValueError as e:
        await set_stellar_status(pid, "failed", {"last_error": str(e)}, "Validation failed before signing")
        return {"payment": await find_payment(ctx, pid), "idempotent": False, "xdr": None}
    except Exception as e:
        log.warning("stellar prepare failed: %s", type(e).__name__)
        await set_stellar_status(
            pid,
            "failed",
            {"last_error": "Could not load the wallet account from Stellar (unfunded or network unavailable)"},
            "Prepare failed",
        )
        return {"payment": await find_payment(ctx, pid), "idempotent": False, "xdr": None}
    st = {
        "network": stellar.NETWORK,
        "source": acct["public_key"],
        "destination": claimed["destination_address"],
        "asset_code": claimed["asset_code"],
        "asset_issuer": claimed.get("asset_issuer"),
        "amount": amount,
        "tx_hash": tx_hash,
        "unsigned_xdr": xdr,
        "operation": op,
        "ledger": None,
        "prepared_at": now_iso(),
        "submitted_at": None,
        "confirmed_at": None,
        "status": "awaiting_signature",
        "signing": "wallet",
        "attempts": prev.get("attempts", 0),
        "last_error": None,
        "idempotency_key": body.idempotency_key,
        "verification": None,
        "submitted_by": ctx["actor_name"],
    }
    await db.payments.update_one(
        {"id": pid},
        {
            "$set": {"stellar": st},
            "$push": {"history": history_entry(ctx, "awaiting_signature", "Confirmed; prepared for wallet signature")},
        },
    )
    return {
        "payment": await find_payment(ctx, pid),
        "idempotent": False,
        "xdr": xdr,
        "network_passphrase": stellar.PASSPHRASE,
        "address": acct["public_key"],
    }


class SignedIn(BaseModel):
    signed_xdr: str = Field(min_length=50, max_length=20000)


@router.post("/payments/{pid}/stellar/submit-signed")
async def stellar_submit_signed(pid: str, body: SignedIn, ctx: dict = Depends(require_roles(*MANAGERS))):
    p = await find_payment(ctx, pid)
    if p["status"] in ("submitting", "pending_verification", "confirmed"):
        return {"payment": p, "idempotent": True}
    if p["status"] != "awaiting_signature":
        raise HTTPException(status_code=409, detail="Prepare the payment for signing first")
    acct = await db.stellar_accounts.find_one({"workspace_id": ctx["ws_id"]}, NO_ID)
    if not acct or acct.get("mode") != "wallet":
        raise HTTPException(status_code=409, detail="No connected wallet")
    try:
        env = stellar.parse_signed(body.signed_xdr)
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid signed transaction")
    if env.hash_hex() != p["stellar"]["tx_hash"]:
        raise HTTPException(status_code=422, detail="Signed transaction does not match the prepared payment")
    if not stellar.signed_by(env, acct["public_key"]):
        raise HTTPException(status_code=422, detail="Transaction is not signed by the connected wallet")
    claimed = await db.payments.find_one_and_update(
        {"id": pid, "status": "awaiting_signature"},
        {
            "$set": {
                "status": "submitting",
                "stellar.status": "submitting",
                "stellar.submitted_at": now_iso(),
                "stellar.attempts": (p["stellar"].get("attempts") or 0) + 1,
            },
            "$push": {"history": history_entry(ctx, "submitting", f"Signed in wallet; tx {env.hash_hex()[:12]}…")},
        },
        projection=NO_ID,
    )
    if not claimed:
        return {"payment": await find_payment(ctx, pid), "idempotent": True}
    await audit(ctx, "stellar.submitted_wallet", "payment", pid, env.hash_hex())
    await send_transaction(pid, env)
    return {"payment": await find_payment(ctx, pid), "idempotent": False}


@router.post("/payments/{pid}/stellar/verify")
async def stellar_verify(pid: str, ctx: dict = Depends(ws_ctx)):
    p = await find_payment(ctx, pid)
    if not (p.get("stellar") or {}).get("tx_hash") or p["status"] == "awaiting_signature":
        raise HTTPException(status_code=409, detail="No submitted Stellar transaction to verify")
    try:
        await refresh_payment(p)
    except Exception:
        raise HTTPException(status_code=504, detail="Stellar network unreachable; try again")
    await audit(ctx, "stellar.verified", "payment", pid, p["stellar"]["tx_hash"])
    return await find_payment(ctx, pid)


@router.post("/payments/{pid}/stellar/cancel-signature")
async def cancel_signature(pid: str, ctx: dict = Depends(require_roles(*MANAGERS))):
    await find_payment(ctx, pid)
    if not await revert_signature(pid, f"Wallet signing cancelled by {ctx['actor_name']}"):
        raise HTTPException(status_code=409, detail="Payment is not awaiting a wallet signature")
    await audit(ctx, "stellar.signature_cancelled", "payment", pid)
    return await find_payment(ctx, pid)
