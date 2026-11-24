"""Application setup."""

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from app.clerk_auth import clerk_router
from app.config import CORS_ORIGINS
from app.database import client, ensure_indexes
from app.middleware import rate_limit, safe_errors
from app.routers import (
    admin,
    approvals,
    budgets,
    cards,
    dashboard,
    expenses,
    payments,
    reconciliation,
    reports,
    settlement,
    stellar_accounts,
    team,
    waitlist,
    wallet,
    workspaces,
)
from app.services.settlement import refresh_pending_payments

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

ROUTERS = (
    workspaces.router,
    team.router,
    expenses.router,
    budgets.router,
    approvals.router,
    cards.router,
    payments.router,
    settlement.router,
    stellar_accounts.router,
    wallet.router,
    dashboard.router,
    reports.router,
    reconciliation.router,
    waitlist.router,
    admin.router,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    tasks = [asyncio.create_task(ensure_indexes()), asyncio.create_task(refresh_pending_payments())]
    yield
    for task in tasks:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    client.close()


def create_app() -> FastAPI:
    app = FastAPI(title="AGOS API", lifespan=lifespan)
    app.middleware("http")(rate_limit)
    app.exception_handler(Exception)(safe_errors)
    app.add_middleware(
        CORSMiddleware,
        allow_credentials=True,
        allow_origins=CORS_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    async def health():
        return {"ok": True, "service": "AGOS"}

    app.include_router(clerk_router)
    for router in ROUTERS:
        app.include_router(router)
    return app


app = create_app()
