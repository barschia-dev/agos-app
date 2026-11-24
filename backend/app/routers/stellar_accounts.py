"""Workspace Stellar accounts."""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException

from app import stellar
from app.common import MANAGERS, audit, new_id, now_iso
from app.database import NO_ID, db
from app.dependencies import require_roles, ws_ctx
from app.services.settlement import revert_signature

router = APIRouter(prefix="/api")


def _public_acct(a: dict) -> dict:
    return {
        "public_key": a["public_key"],
        "network": a["network"],
        "created_at": a["created_at"],
        "mode": a.get("mode", "managed"),
        "wallet": a.get("wallet"),
        "explorer": f"{stellar.EXPLORER}/account/{a['public_key']}",
    }


@router.get("/stellar/account")
async def get_account(ctx: dict = Depends(ws_ctx)):
    a = await db.stellar_accounts.find_one({"workspace_id": ctx["ws_id"]}, {"_id": 0, "encrypted_secret": 0})
    if not a:
        return {"account": None, "assets": list(stellar.ASSETS.values()), "network": stellar.NETWORK}
    try:
        info = await stellar.account_info(a["public_key"])
    except Exception:
        info = {"exists": None, "balances": [], "error": "Stellar network unreachable"}
    return {"account": {**_public_acct(a), **info}, "assets": list(stellar.ASSETS.values()), "network": stellar.NETWORK}


@router.post("/stellar/account")
async def create_account(ctx: dict = Depends(require_roles(*MANAGERS))):
    if await db.stellar_accounts.find_one({"workspace_id": ctx["ws_id"]}):
        raise HTTPException(status_code=409, detail="This workspace already has a Stellar Testnet account")
    pub, enc = stellar.new_keypair()
    doc = {
        "id": new_id(),
        "workspace_id": ctx["ws_id"],
        "public_key": pub,
        "encrypted_secret": enc,
        "network": stellar.NETWORK,
        "created_at": now_iso(),
    }
    await db.stellar_accounts.insert_one(doc)
    try:
        fb = await stellar.friendbot(pub)
    except Exception:
        fb = {"ok": False}
    await audit(ctx, "stellar.account_created", "stellar_account", doc["id"], pub)
    return {"public_key": pub, "funded": fb["ok"]}


@router.post("/stellar/account/fund")
async def fund_account(ctx: dict = Depends(require_roles(*MANAGERS))):
    a = await db.stellar_accounts.find_one({"workspace_id": ctx["ws_id"]}, {"_id": 0, "public_key": 1})
    if not a:
        raise HTTPException(status_code=404, detail="No Stellar account")
    try:
        fb = await stellar.friendbot(a["public_key"])
    except Exception:
        raise HTTPException(status_code=504, detail="Friendbot unreachable; try again")
    if not fb["ok"]:
        raise HTTPException(status_code=409, detail="Friendbot declined (account is likely already funded)")
    return {"ok": True}


@router.get("/stellar/transactions")
async def account_transactions(ctx: dict = Depends(ws_ctx)):
    a = await db.stellar_accounts.find_one({"workspace_id": ctx["ws_id"]}, {"_id": 0, "public_key": 1})
    if not a:
        return []
    try:
        return await stellar.recent_payments(a["public_key"])
    except Exception:
        raise HTTPException(status_code=504, detail="Stellar network unreachable")


@router.post("/stellar/demo-recipient")
async def demo_recipient(ctx: dict = Depends(require_roles(*MANAGERS))):
    pub, _ = stellar.new_keypair()  # secret is discarded; this address is receive-only for demos
    try:
        fb = await stellar.friendbot(pub)
    except Exception:
        fb = {"ok": False}
    await db.stellar_recipients.insert_one(
        {"id": new_id(), "workspace_id": ctx["ws_id"], "public_key": pub, "funded": fb["ok"], "created_at": now_iso()}
    )
    return {"public_key": pub, "funded": fb["ok"]}


@router.post("/stellar/account/disconnect")
async def disconnect_account(ctx: dict = Depends(require_roles("owner"))):
    a = await db.stellar_accounts.find_one({"workspace_id": ctx["ws_id"]}, NO_ID)
    if not a:
        raise HTTPException(status_code=404, detail="No Stellar account")
    expired = (datetime.now(UTC) - timedelta(seconds=stellar.TX_VALIDITY_SECONDS + 30)).isoformat()
    for p in await db.payments.find(
        {"workspace_id": ctx["ws_id"], "status": "awaiting_signature", "stellar.prepared_at": {"$lt": expired}}, NO_ID
    ).to_list(200):
        await revert_signature(p["id"], "Signing window expired; reverted on disconnect")
    if await db.payments.count_documents(
        {"workspace_id": ctx["ws_id"], "status": {"$in": ["submitting", "awaiting_signature", "pending_verification"]}}
    ):
        raise HTTPException(status_code=409, detail="Finish or verify in-flight Stellar payments first")
    await db.stellar_accounts_archive.insert_one({**a, "archived_at": now_iso(), "archived_by": ctx["actor_name"]})
    await db.stellar_accounts.delete_one({"workspace_id": ctx["ws_id"], "id": a["id"]})
    await audit(ctx, "stellar.account_disconnected", "stellar_account", a["id"], a["public_key"])
    return {"ok": True}
