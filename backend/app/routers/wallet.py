"""Connecting a Freighter wallet."""

import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app import stellar
from app.common import MANAGERS, audit, new_id, now_iso
from app.database import NO_ID, db
from app.dependencies import require_roles

router = APIRouter(prefix="/api")


class ChallengeIn(BaseModel):
    public_key: str = Field(min_length=56, max_length=56)


class ConnectIn(BaseModel):
    challenge_id: str
    signed_xdr: str = Field(min_length=50, max_length=20000)


@router.post("/stellar/wallet/challenge")
async def wallet_challenge(body: ChallengeIn, ctx: dict = Depends(require_roles(*MANAGERS))):
    if not stellar.valid_address(body.public_key):
        raise HTTPException(status_code=422, detail="Invalid Stellar public key")
    if await db.stellar_accounts.find_one({"workspace_id": ctx["ws_id"]}):
        raise HTTPException(status_code=409, detail="Disconnect the current Stellar account first")
    tx_hash, xdr = stellar.challenge_tx(body.public_key, secrets.token_hex(16))
    cid = new_id()
    await db.stellar_challenges.insert_one(
        {
            "id": cid,
            "workspace_id": ctx["ws_id"],
            "public_key": body.public_key,
            "hash": tx_hash,
            "used": False,
            "created_at": now_iso(),
        }
    )
    return {"challenge_id": cid, "xdr": xdr, "network_passphrase": stellar.PASSPHRASE}


@router.post("/stellar/wallet/connect")
async def wallet_connect(body: ConnectIn, ctx: dict = Depends(require_roles(*MANAGERS))):
    ch = await db.stellar_challenges.find_one(
        {"id": body.challenge_id, "workspace_id": ctx["ws_id"], "used": False}, NO_ID
    )
    if not ch or datetime.now(UTC) - datetime.fromisoformat(ch["created_at"]) > timedelta(minutes=5):
        raise HTTPException(status_code=410, detail="Challenge expired; try connecting again")
    try:
        env = stellar.parse_signed(body.signed_xdr)
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid signed challenge")
    if env.hash_hex() != ch["hash"] or not stellar.signed_by(env, ch["public_key"]):
        raise HTTPException(status_code=422, detail="Wallet signature could not be verified")
    await db.stellar_challenges.update_one({"id": ch["id"]}, {"$set": {"used": True}})
    if await db.stellar_accounts.find_one({"workspace_id": ctx["ws_id"]}):
        raise HTTPException(status_code=409, detail="Disconnect the current Stellar account first")
    doc = {
        "id": new_id(),
        "workspace_id": ctx["ws_id"],
        "public_key": ch["public_key"],
        "encrypted_secret": None,
        "mode": "wallet",
        "wallet": "freighter",
        "network": stellar.NETWORK,
        "created_at": now_iso(),
        "verified_at": now_iso(),
    }
    await db.stellar_accounts.insert_one(doc)
    await audit(ctx, "stellar.wallet_connected", "stellar_account", doc["id"], ch["public_key"])
    return {"public_key": ch["public_key"], "mode": "wallet"}
