"""MongoDB client and indexes."""

import logging

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.config import DB_NAME, MONGO_URL

logger = logging.getLogger("agos")
client = AsyncIOMotorClient(MONGO_URL)
db = client[DB_NAME]
NO_ID = {"_id": 0}

INDEXES = {
    "users": [IndexModel([("user_id", ASCENDING)], name="user_id", unique=True)],
    "workspaces": [
        IndexModel([("id", ASCENDING)], name="id", unique=True),
        IndexModel([("owner_user_id", ASCENDING)], name="owner"),
    ],
    "members": [
        IndexModel([("id", ASCENDING)], name="id", unique=True),
        IndexModel([("workspace_id", ASCENDING), ("user_id", ASCENDING)], name="ws_user"),
        IndexModel([("email", ASCENDING)], name="email"),
    ],
    "expenses": [
        IndexModel([("id", ASCENDING)], name="id", unique=True),
        IndexModel([("workspace_id", ASCENDING), ("date", DESCENDING)], name="ws_date"),
    ],
    "budgets": [IndexModel([("workspace_id", ASCENDING), ("period", DESCENDING)], name="ws_period")],
    "cards": [
        IndexModel([("id", ASCENDING)], name="id", unique=True),
        IndexModel([("workspace_id", ASCENDING)], name="ws"),
    ],
    "card_transactions": [
        IndexModel([("card_id", ASCENDING), ("date", DESCENDING)], name="card_date"),
        IndexModel([("workspace_id", ASCENDING)], name="ws"),
    ],
    "payments": [
        IndexModel([("id", ASCENDING)], name="id", unique=True),
        IndexModel([("workspace_id", ASCENDING), ("created_at", DESCENDING)], name="ws_created"),
        IndexModel([("stellar.tx_hash", ASCENDING)], name="tx_hash", sparse=True),
    ],
    "stellar_accounts": [IndexModel([("workspace_id", ASCENDING)], name="ws", unique=True)],
    "files": [IndexModel([("id", ASCENDING)], name="id", unique=True)],
    "audit_logs": [IndexModel([("workspace_id", ASCENDING), ("at", DESCENDING)], name="ws_at")],
    "waitlist": [IndexModel([("email", ASCENDING)], name="email", unique=True)],
}


async def ensure_indexes() -> None:
    for coll, models in INDEXES.items():
        for m in models:
            try:
                await db[coll].create_indexes([m])
            except Exception as exc:
                logger.error("ensure_indexes(%s.%s): %s", coll, m.document["name"], exc)
