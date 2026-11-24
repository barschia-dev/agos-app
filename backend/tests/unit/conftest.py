"""In-process API tests: no Clerk or Stellar access is needed.

Set MONGO_URL to use a real MongoDB server; otherwise an in-memory one is used.
"""
import importlib
import os
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
USE_REAL_MONGO = "MONGO_URL" in os.environ
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ["DB_NAME"] = "agos_unit_tests"
os.environ["APP_NAME"] = "agos-test"
os.environ["STELLAR_HORIZON_URL"] = "https://horizon-testnet.stellar.org"
os.environ["STELLAR_NETWORK"] = "TESTNET"
os.environ["STELLAR_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
os.environ["SUPER_ADMIN_EMAIL"] = "super@example.com"

if not USE_REAL_MONGO:
    import motor.motor_asyncio
    import mongomock.collection
    from mongomock_motor import AsyncMongoMockClient

    _find_one_and_update = mongomock.collection.Collection.find_one_and_update

    def _without_projection(self, *args, projection=None, **kwargs):
        # mongomock loses the document when the projection excludes _id and the filter no longer matches.
        doc = _find_one_and_update(self, *args, **kwargs)
        return {k: v for k, v in doc.items() if k != "_id"} if doc else doc

    mongomock.collection.Collection.find_one_and_update = _without_projection

    motor.motor_asyncio.AsyncIOMotorClient = AsyncMongoMockClient

from fastapi.testclient import TestClient  # noqa: E402

from server import app  # noqa: E402


def load(*names):
    for name in names:
        try:
            return importlib.import_module(name)
        except ModuleNotFoundError:
            continue
    raise ModuleNotFoundError(names)


core = load("app.dependencies", "core")
stellar = load("app.stellar", "stellar_service")
storage = load("app.storage", "storage")


class Ledger:
    """Stands in for Horizon. Nothing reaches the network."""

    def __init__(self):
        self.transactions = {}
        self.submitted = 0
        self.submit_error = None
        self.amount = None
        self.to = None

    async def build_signed(self, secret, destination, amount, memo, asset):
        self.to, self.amount = destination, amount
        return "tx", "ab" * 32, "GSOURCE", "payment"

    async def submit(self, tx):
        self.submitted += 1
        if self.submit_error:
            raise self.submit_error
        self.transactions["ab" * 32] = {
            "successful": True, "ledger": 42, "created_at": "2026-01-01T00:00:00Z", "source": "GSOURCE",
            "fee_charged": "100", "memo": "m",
            "operations": [{"type": "payment", "to": self.to, "amount": self.amount}],
        }
        return {"successful": True, "ledger": 42}

    async def get_transaction(self, tx_hash):
        return self.transactions.get(tx_hash)

    async def friendbot(self, key):
        return {"ok": True, "status": 200}


@pytest.fixture
def ledger(monkeypatch):
    fake = Ledger()
    for name in ("build_signed", "submit", "get_transaction", "friendbot"):
        monkeypatch.setattr(stellar, name, getattr(fake, name))
    return fake


@pytest.fixture
def client():
    identity = {"user_id": "u1"}

    def who():
        return dict(identity)

    app.dependency_overrides[core.require_user] = who
    core._clerk_profile = lambda uid: {"emails": [f"{uid}@example.com"], "name": uid.title()}
    with TestClient(app) as c:
        c.sign_in = lambda uid: identity.update(user_id=uid)
        db = core.db
        c.portal.call(lambda: db.client.drop_database(db.name)) if USE_REAL_MONGO else [
            c.portal.call(db.drop_collection, n) for n in c.portal.call(db.list_collection_names)]
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def ws(client):
    """A demo workspace; returns helpers for acting as each role."""
    ws_id = client.post("/api/workspaces/demo").json()["id"]
    base = {"X-Workspace-Id": ws_id}
    info = client.get("/api/workspace", headers=base).json()
    ids = {}
    for p in info["personas"]:
        ids.setdefault(p["role"], p["id"])

    class W:
        id = ws_id
        members = ids
        owner = base
        fm = {**base, "X-Act-As": ids["finance_manager"]}
        employee = {**base, "X-Act-As": ids["employee"]}
    return W
