import pytest

EXPENSE = {"merchant": "Shop", "category": "Misc", "amount": 100, "date": "2026-01-05", "submit": True}
DEST = "GAIH3ULLFQ4DGSECF2AR555KZ4KNDGEKN4AFI4SU2M7B43MGK3QJZNSR"


def new_payment(client, headers, **over):
    body = {"type": "international", "recipient_name": "Vendor", "destination_address": DEST,
            "amount": "5", "purpose": "Invoice", **over}
    r = client.post("/api/payments", headers=headers, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def approved(client, ws):
    client.post("/api/stellar/account", headers=ws.owner)
    p = new_payment(client, ws.employee)
    r = client.post(f"/api/payments/{p['id']}/decision", headers=ws.fm, json={"action": "approve"})
    assert r.json()["status"] == "approved"
    return p["id"]


def submit(client, ws, pid, key="key-12345678"):
    return client.post(f"/api/payments/{pid}/stellar/submit", headers=ws.owner,
                       json={"confirm": True, "idempotency_key": key})


def test_health(client):
    assert client.get("/api/health").json() == {"ok": True, "service": "AGOS"}


def test_every_read_endpoint_responds(client, ws):
    for path in ("me", "workspace", "team", "audit", "roles", "expenses", "expenses/export", "budgets", "approvals",
                 "cards", "payments", "dashboard", "reports", "reports/export/monthly", "reports/export/payments",
                 "reconciliation", "reconciliation/export", "stellar/account", "stellar/transactions"):
        r = client.get(f"/api/{path}", headers=ws.owner)
        assert r.status_code == 200, (path, r.text)


def test_workspace_isolation(client, ws):
    expense = client.get("/api/expenses", headers=ws.owner).json()[0]["id"]
    payment = client.get("/api/payments", headers=ws.owner).json()[0]["id"]
    client.sign_in("u2")
    assert client.get("/api/expenses", headers=ws.owner).status_code == 404
    other = {"X-Workspace-Id": client.post("/api/workspaces/demo").json()["id"]}
    assert client.get(f"/api/payments/{payment}", headers=other).status_code == 404
    assert client.post(f"/api/expenses/{expense}/submit", headers=other).status_code == 404


def test_workspace_header_is_required(client):
    assert client.get("/api/expenses").status_code == 400


@pytest.mark.parametrize("path", ["audit", "reports", "reconciliation", "reports/export/stellar"])
def test_employees_cannot_read_manager_data(client, ws, path):
    assert client.get(f"/api/{path}", headers=ws.employee).status_code == 403


def test_employees_only_see_their_own_records(client, ws):
    me = ws.members["employee"]
    assert {e["member_id"] for e in client.get("/api/expenses", headers=ws.employee).json()} == {me}
    assert {p["requested_by"] for p in client.get("/api/payments", headers=ws.employee).json()} == {me}


def test_expense_lifecycle(client, ws):
    e = client.post("/api/expenses", headers=ws.employee, json=EXPENSE).json()
    assert e["status"] == "pending_approval"
    url = f"/api/expenses/{e['id']}"
    assert client.post(f"{url}/decision", headers=ws.employee, json={"action": "approve"}).status_code == 403
    assert client.post(f"{url}/decision", headers=ws.fm, json={"action": "reject"}).status_code == 422
    assert client.post(f"{url}/decision", headers=ws.fm, json={"action": "approve"}).json()["status"] == "approved"
    assert client.post(f"{url}/decision", headers=ws.fm, json={"action": "approve"}).status_code == 409
    assert client.post(f"{url}/mark-paid", headers=ws.fm).json()["status"] == "paid"
    assert client.patch(url, headers=ws.employee, json={"amount": 1}).status_code == 409


def test_approval_limits(client, ws):
    own = client.post("/api/expenses", headers=ws.fm, json=EXPENSE).json()["id"]
    assert client.post(f"/api/expenses/{own}/decision", headers=ws.fm, json={"action": "approve"}).status_code == 403
    big = client.post("/api/expenses", headers=ws.employee, json={**EXPENSE, "amount": 60000}).json()["id"]
    assert client.post(f"/api/expenses/{big}/decision", headers=ws.fm, json={"action": "approve"}).status_code == 403
    assert client.post(f"/api/expenses/{big}/decision", headers=ws.owner, json={"action": "approve"}).status_code == 200
    xlm = new_payment(client, ws.employee, amount="150")["id"]
    assert client.post(f"/api/payments/{xlm}/decision", headers=ws.fm, json={"action": "approve"}).status_code == 403


def test_budget_usage_and_permissions(client, ws):
    body = {"name": "Legal", "scope": "department", "department": "Legal", "amount": 100, "period": "2026-01"}
    assert client.post("/api/budgets", headers=ws.fm, json=body).status_code == 403
    b = client.post("/api/budgets", headers=ws.owner, json=body).json()
    e = client.post("/api/expenses", headers=ws.owner, json={**EXPENSE, "department": "Legal", "amount": 150}).json()
    assert any("Would exceed budget" in w for w in e["warnings"])
    client.post(f"/api/expenses/{e['id']}/decision", headers=ws.owner, json={"action": "approve"})
    b = client.put(f"/api/budgets/{b['id']}", headers=ws.owner, json=body).json()
    assert (b["used"], b["remaining"], b["state"]) == (150, -50, "over")
    assert client.delete(f"/api/budgets/{b['id']}", headers=ws.owner).json() == {"ok": True}


def test_card_simulation(client, ws):
    card = client.post("/api/cards", headers=ws.fm, json={
        "member_id": ws.members["employee"], "nickname": "Test", "spending_limit": 100}).json()
    url = f"/api/cards/{card['id']}"
    spend = {"merchant": "M", "category": "C", "amount": 80}
    assert client.post(f"{url}/simulate", headers=ws.employee, json=spend).json()["status"] == "posted"
    assert client.post(f"{url}/simulate", headers=ws.employee, json=spend).json()["status"] == "declined"
    assert client.post(f"{url}/status", headers=ws.employee, json={"action": "freeze"}).json()["status"] == "frozen"
    assert client.post(f"{url}/status", headers=ws.employee, json={"action": "unfreeze"}).status_code == 403
    assert len(client.get(f"{url}/transactions", headers=ws.employee).json()) == 2


@pytest.mark.parametrize("over,code", [({"amount": "0"}, 422), ({"amount": "x"}, 422), ({"amount": "1.12345678"}, 422),
                                       ({"amount": "1001"}, 422), ({"destination_address": "bad"}, 422),
                                       ({"asset_code": "USDC"}, 422)])
def test_payment_validation(client, ws, over, code):
    body = {"type": "international", "recipient_name": "Vendor", "destination_address": DEST, "amount": "5",
            "purpose": "Invoice", **over}
    assert client.post("/api/payments", headers=ws.owner, json=body).status_code == code


def test_stellar_payment_is_confirmed_once(client, ws, ledger):
    pid = approved(client, ws)
    p = submit(client, ws, pid).json()["payment"]
    assert p["status"] == "confirmed" and p["stellar"]["ledger"] == 42
    assert p["stellar"]["verification"]["matches_request"] is True
    assert submit(client, ws, pid).json()["idempotent"] is True
    assert ledger.submitted == 1
    assert client.post(f"/api/payments/{pid}/stellar/verify", headers=ws.owner).json()["status"] == "confirmed"
    rows = client.get("/api/reconciliation", headers=ws.owner).json()["rows"]
    assert next(r for r in rows if r["id"] == pid)["reconciliation_status"] == "verified_on_chain"
    r = client.post(f"/api/reconciliation/{pid}", headers=ws.owner, json={"kind": "payment", "reconciled": True})
    assert r.status_code == 200
    pdf = client.get(f"/api/payments/{pid}/pitch-summary", headers=ws.owner)
    assert pdf.content.startswith(b"%PDF")


def test_unapproved_payment_cannot_be_submitted(client, ws, ledger):
    client.post("/api/stellar/account", headers=ws.owner)
    pid = new_payment(client, ws.employee)["id"]
    assert submit(client, ws, pid).status_code == 409
    assert ledger.submitted == 0


def test_uncertain_submission_waits_for_verification(client, ws, ledger):
    ledger.submit_error = TimeoutError()
    pid = approved(client, ws)
    assert submit(client, ws, pid).json()["payment"]["status"] == "pending_verification"
    assert submit(client, ws, pid).json()["idempotent"] is True
    assert ledger.submitted == 1


def test_mismatched_payment_is_not_verified(client, ws, ledger):
    pid = approved(client, ws)
    submit(client, ws, pid)
    ledger.transactions["ab" * 32]["operations"][0]["amount"] = "4"
    p = client.post(f"/api/payments/{pid}/stellar/verify", headers=ws.owner).json()
    assert p["stellar"]["verification"]["matches_request"] is False
    rows = client.get("/api/reconciliation", headers=ws.owner).json()["rows"]
    assert next(r for r in rows if r["id"] == pid)["reconciliation_status"] == "confirmed_unverified"


def test_wallet_connection_requires_ownership(client, ws):
    from stellar_sdk import Keypair, TransactionEnvelope

    kp, stranger = Keypair.random(), Keypair.random()
    ch = client.post("/api/stellar/wallet/challenge", headers=ws.owner, json={"public_key": kp.public_key}).json()

    def connect(signer):
        env = TransactionEnvelope.from_xdr(ch["xdr"], ch["network_passphrase"])
        env.sign(signer)
        return client.post("/api/stellar/wallet/connect", headers=ws.owner,
                           json={"challenge_id": ch["challenge_id"], "signed_xdr": env.to_xdr()})

    assert connect(stranger).status_code == 422
    assert connect(kp).json() == {"public_key": kp.public_key, "mode": "wallet"}
    assert connect(kp).status_code == 410
    assert client.post("/api/stellar/account/disconnect", headers=ws.owner).json() == {"ok": True}


def test_last_owner_cannot_be_removed(client, ws):
    r = client.patch(f"/api/team/{ws.members['owner']}", headers=ws.owner, json={"status": "removed"})
    assert r.status_code == 409


def test_waitlist_and_admin(client):
    body = {"name": "Ana", "business_name": "Biz", "email": "a@example.com", "category": "Retail", "challenge": "cash flow"}
    assert client.post("/api/waitlist", json={**body, "consent": False}).status_code == 422
    assert client.post("/api/waitlist", json={**body, "consent": True}).json() == {"ok": True}
    assert client.post("/api/waitlist", json={**body, "consent": True}).json()["already"]
    assert client.get("/api/admin/waitlist").status_code == 403
    client.sign_in("super")
    assert len(client.get("/api/admin/waitlist").json()) == 1
    assert client.get("/api/admin/overview").status_code == 200
