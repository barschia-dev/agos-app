# AGOS

[![Backend](https://github.com/barschia-dev/agos-app/actions/workflows/backend.yml/badge.svg)](https://github.com/barschia-dev/agos-app/actions/workflows/backend.yml)
[![Frontend](https://github.com/barschia-dev/agos-app/actions/workflows/frontend.yml/badge.svg)](https://github.com/barschia-dev/agos-app/actions/workflows/frontend.yml)

AGOS is a finance workspace for startups and small businesses. Teams request, approve, pay and reconcile spending, and international payments settle on Stellar Testnet.

> **Testnet only.** Stellar payments use test assets. Cards and domestic transfers are simulated, and AGOS does not move real money.

## Features

- Workspaces with owner, finance manager and employee roles
- Expenses with receipts, budgets and approval limits
- Simulated corporate cards
- Stellar Testnet payments, signed by the server or a Freighter wallet
- Ledger verification, reconciliation, reports and CSV exports
- Audit log and synthetic demo workspaces

## Stack

| Part | Technology |
| --- | --- |
| Backend | Python 3.11+, FastAPI, MongoDB, Stellar Python SDK |
| Frontend | React 19, Tailwind CSS, shadcn/ui |
| Sign-in | Clerk |

## Run locally

You need Python 3.11+, Node.js 22, Yarn 1, MongoDB and a Clerk application.

**Backend**

```bash
cd backend
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env             # then fill in the values
uvicorn server:app --reload --port 8001
```

**Frontend**

```bash
cd frontend
cp .env.example .env
yarn install
yarn start
```

Open http://localhost:3000. The API reference is at http://localhost:8001/docs.

## Tests

```bash
cd backend
pytest
ruff check .
```

These tests run in memory, with no Clerk or Stellar access. The scripts in `backend/tests/live` need a running server and Clerk test keys.

## Layout

```
backend/
  server.py          entry point
  app/
    config.py        settings from the environment
    database.py      MongoDB client and indexes
    dependencies.py  signed-in user, workspace and role checks
    stellar.py       Horizon access and transaction building
    routers/         API endpoints
    services/        approvals, budgets, settlement and reporting
  tests/
frontend/            React application
docs/                product requirements and design guidelines
```

## How a Stellar payment works

1. A member requests a payment.
2. An owner or finance manager approves it, within their limit.
3. The workspace account or connected wallet signs it.
4. AGOS submits it once and records the transaction hash.
5. AGOS checks the ledger for success, destination and amount, then marks it ready to reconcile.

If the network times out, the payment waits for verification instead of being sent again.
