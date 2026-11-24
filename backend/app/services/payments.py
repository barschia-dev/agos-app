"""Payment lookup."""

from fastapi import HTTPException

from app import stellar
from app.common import MANAGERS
from app.database import NO_ID, db


async def find_payment(ctx: dict, pid: str) -> dict:
    scope = {} if ctx["role"] in MANAGERS else {"requested_by": ctx["member"]["id"]}
    p = await db.payments.find_one({"id": pid, "workspace_id": ctx["ws_id"], **scope}, NO_ID)
    if not p:
        raise HTTPException(status_code=404, detail="Not found")
    if p.get("stellar") and p["stellar"].get("tx_hash"):
        p["stellar"]["links"] = stellar.tx_links(p["stellar"]["tx_hash"])
    return p
