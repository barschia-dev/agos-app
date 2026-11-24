"""Dashboard, report and reconciliation data."""

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Optional

from app import stellar
from app.common import MANAGERS, this_month
from app.database import NO_ID, db
from app.services.budgets import COUNTED, budgets_with_usage

DISCLAIMER = (
    "Stellar confirmation verifies Testnet ledger settlement only; it does not prove fiat delivery to a beneficiary."
)


def recent_months(n: int) -> list:
    now = datetime.now(UTC).replace(day=1)
    out = []
    for _ in range(n):
        out.append(now.strftime("%Y-%m"))
        now = (now - timedelta(days=1)).replace(day=1)
    return out[::-1]


def date_range(q: dict, date_from, date_to) -> dict:
    if date_from or date_to:
        q["date"] = {k: v for k, v in (("$gte", date_from), ("$lte", date_to)) if v}
    return q


def group_by(exps, key) -> list:
    agg = defaultdict(float)
    for e in exps:
        agg[e.get(key) or "Unassigned"] += e["amount_php"]
    return sorted([{"name": k, "amount": round(v, 2)} for k, v in agg.items()], key=lambda x: -x["amount"])


def monthly_totals(exps, months) -> list:
    agg = defaultdict(float)
    for e in exps:
        agg[e["date"][:7]] += e["amount_php"]
    return [{"month": m, "amount": round(agg.get(m, 0), 2)} for m in months]


async def dashboard_data(ctx: dict, months: int = 6, department: Optional[str] = None) -> dict:
    months = max(1, min(months, 12))
    mlist = recent_months(months)
    scope = {} if ctx["role"] in MANAGERS else {"member_id": ctx["member"]["id"]}
    q = {"workspace_id": ctx["ws_id"], **scope}
    if department:
        q["department"] = department
    all_exp = await db.expenses.find(q, NO_ID).sort("date", -1).to_list(10000)
    recorded = [e for e in all_exp if e["status"] in COUNTED]
    in_window = [e for e in recorded if e["date"][:7] >= mlist[0]]
    month = this_month()
    pay_scope = {} if ctx["role"] in MANAGERS else {"requested_by": ctx["member"]["id"]}
    pays = (
        await db.payments.find({"workspace_id": ctx["ws_id"], **pay_scope}, NO_ID).sort("created_at", -1).to_list(2000)
    )
    bq = {"period": month}
    if department:
        bq["department"] = department
    if ctx["role"] not in MANAGERS:
        bq["$or"] = [{"assigned_member_ids": ctx["member"]["id"]}, {"member_id": ctx["member"]["id"]}]
    budgets = await budgets_with_usage(ctx["ws_id"], bq)
    biz = [b for b in budgets if b["scope"] != "employee"]
    card_q = {"workspace_id": ctx["ws_id"], "status": "posted", **scope}
    card_txns = await db.card_transactions.find(card_q, NO_ID).sort("date", -1).to_list(5000)
    recent = sorted(
        [
            {
                "kind": "expense",
                "id": e["id"],
                "title": e["merchant"],
                "who": e["member_name"],
                "date": e["date"],
                "amount": e["amount"],
                "currency": e["currency"],
                "status": e["status"],
                "is_synthetic": e.get("is_synthetic"),
            }
            for e in all_exp[:10]
        ]
        + [
            {
                "kind": "payment",
                "id": p["id"],
                "title": p["recipient_name"],
                "who": p["requested_by_name"],
                "date": p["created_at"][:10],
                "amount": p["amount"],
                "currency": p["currency"],
                "status": p["status"],
                "rail": p["rail"],
                "is_synthetic": p.get("is_synthetic"),
            }
            for p in pays[:10]
        ],
        key=lambda r: r["date"],
        reverse=True,
    )[:10]
    dom = [p for p in pays if p["type"] == "domestic"]
    intl = [p for p in pays if p["type"] == "international"]
    confirmed = [p for p in intl if p["status"] == "confirmed"]
    pending_e = sum(1 for e in all_exp if e["status"] == "pending_approval")
    pending_p = sum(1 for p in pays if p["status"] == "pending_approval")
    return {
        "kpis": {
            "total_recorded": round(sum(e["amount_php"] for e in recorded), 2),
            "month_spend": round(sum(e["amount_php"] for e in recorded if e["date"].startswith(month)), 2),
            "budget_total": round(
                sum(b["amount"] for b in biz if b["scope"] == "department") or sum(b["amount"] for b in biz), 2
            ),
            "budget_used": round(
                sum(b["used"] for b in biz if b["scope"] == "department") or sum(b["used"] for b in biz), 2
            ),
            "pending_approvals": pending_e + pending_p,
            "pending_expenses": pending_e,
            "pending_payments": pending_p,
            "card_spend_month": round(sum(t["amount_php"] for t in card_txns if t["date"].startswith(month)), 2),
        },
        "monthly": monthly_totals(in_window, mlist),
        "departments": group_by(in_window, "department"),
        "employees": group_by(in_window, "member_name")[:8],
        "budgets": budgets,
        "recent": recent,
        "domestic": {
            "count": len(dom),
            "recorded_demo": sum(1 for p in dom if p["status"] == "recorded_as_paid"),
            "pending": sum(1 for p in dom if p["status"] == "pending_approval"),
            "total_php": round(sum(p["amount"] for p in dom if p["status"] == "recorded_as_paid"), 2),
        },
        "stellar": {
            "count": len(intl),
            "confirmed": len(confirmed),
            "xlm_settled": round(sum(p["amount"] for p in confirmed), 7),
            "latest": [
                {
                    "id": p["id"],
                    "recipient": p["recipient_name"],
                    "amount": p["amount"],
                    "status": p["status"],
                    "tx_hash": (p.get("stellar") or {}).get("tx_hash"),
                    "ledger": (p.get("stellar") or {}).get("ledger"),
                }
                for p in intl[:5]
            ],
        },
        "synthetic_records": sum(1 for e in all_exp if e.get("is_synthetic")),
        "departments_list": ctx["ws"].get("departments", []),
        "month": month,
    }


async def report_data(ctx, date_from, date_to) -> dict:
    exps = await db.expenses.find(
        date_range({"workspace_id": ctx["ws_id"], "status": {"$in": COUNTED}}, date_from, date_to), NO_ID
    ).to_list(20000)
    months = sorted({e["date"][:7] for e in exps}) or [this_month()]
    pq = {"workspace_id": ctx["ws_id"]}
    if date_from or date_to:
        pq["created_at"] = {
            k: v for k, v in (("$gte", date_from), ("$lte", (date_to or "") + "T23:59:59")) if v and v != "T23:59:59"
        }
    pays = await db.payments.find(pq, NO_ID).sort("created_at", -1).to_list(5000)
    bq = {}
    if date_from or date_to:
        bq["period"] = {k: v for k, v in (("$gte", (date_from or "")[:7]), ("$lte", (date_to or "")[:7])) if v}
    budgets = await budgets_with_usage(ctx["ws_id"], bq)
    pending = await db.expenses.find({"workspace_id": ctx["ws_id"], "status": "pending_approval"}, NO_ID).to_list(1000)
    stellar_pays = [p for p in pays if p["type"] == "international" and (p.get("stellar") or {}).get("tx_hash")]
    return {
        "monthly": monthly_totals(exps, [m for m in recent_months(24) if months[0] <= m <= months[-1]] or months),
        "departments": group_by(exps, "department"),
        "employees": group_by(exps, "member_name"),
        "budget_vs_actual": [
            {
                "name": b["name"],
                "period": b["period"],
                "scope": b["scope"],
                "budget": b["amount"],
                "actual": b["used"],
                "variance": b["remaining"],
                "utilization": b["utilization"],
            }
            for b in budgets
        ],
        "pending": [
            {
                "id": e["id"],
                "merchant": e["merchant"],
                "member_name": e["member_name"],
                "amount_php": e["amount_php"],
                "date": e["date"],
            }
            for e in pending
        ]
        + [
            {
                "id": p["id"],
                "merchant": p["recipient_name"],
                "member_name": p["requested_by_name"],
                "amount_php": p["amount_php"],
                "date": p["created_at"][:10],
                "currency": p["currency"],
                "amount": p["amount"],
            }
            for p in pays
            if p["status"] == "pending_approval"
        ],
        "payments": [
            {
                "id": p["id"],
                "date": p["created_at"][:10],
                "type": p["type"],
                "rail": p["rail"],
                "recipient": p["recipient_name"],
                "amount": p["amount"],
                "currency": p["currency"],
                "status": p["status"],
                "is_synthetic": p.get("is_synthetic"),
            }
            for p in pays
        ],
        "stellar": [
            {
                "id": p["id"],
                "recipient": p["recipient_name"],
                "destination": p["destination_address"],
                "amount": p["amount"],
                "asset": p["asset_code"],
                "status": p["status"],
                **{
                    k: p["stellar"].get(k)
                    for k in ("tx_hash", "ledger", "network", "source", "submitted_at", "confirmed_at")
                },
                "verified_at": (p["stellar"].get("verification") or {}).get("checked_at"),
                "matches_request": (p["stellar"].get("verification") or {}).get("matches_request"),
                "explorer": stellar.tx_links(p["stellar"]["tx_hash"])["explorer"],
            }
            for p in stellar_pays
        ],
        "totals": {
            "recorded_php": round(sum(e["amount_php"] for e in exps), 2),
            "expense_count": len(exps),
            "payment_count": len(pays),
            "stellar_count": len(stellar_pays),
        },
        "disclaimer": DISCLAIMER,
    }


async def reconciliation_rows(ctx) -> list:
    ws = ctx["ws_id"]
    rows = []
    for e in (
        await db.expenses.find({"workspace_id": ws, "status": {"$ne": "draft"}}, NO_ID).sort("date", -1).to_list(5000)
    ):
        has_receipt = bool(e.get("receipts"))
        if e.get("reconciled"):
            rs = "reconciled"
        elif e["status"] in ("rejected", "pending_approval", "submitted"):
            rs = "not_applicable" if e["status"] == "rejected" else "awaiting_approval"
        elif not has_receipt:
            rs = "missing_receipt"
        else:
            rs = "ready_to_reconcile"
        rows.append(
            {
                "kind": "expense",
                "id": e["id"],
                "reference": "EXP-" + e["id"][:8].upper(),
                "date": e["date"],
                "description": e["merchant"],
                "method": "Simulated card" if e.get("card_id") else "Employee expense",
                "amount": e["amount"],
                "currency": e["currency"],
                "category": e["category"],
                "approval_status": e["status"] if e["status"] != "paid" else "approved",
                "payment_status": "recorded_as_paid"
                if e["status"] == "paid"
                else ("card_settled_simulated" if e.get("card_id") else "unpaid"),
                "reconciliation_status": rs,
                "evidence": f"{len(e.get('receipts', []))} receipt(s)",
                "is_synthetic": e.get("is_synthetic"),
                "can_toggle": e["status"] in COUNTED,
            }
        )
    for t in await db.card_transactions.find({"workspace_id": ws}, NO_ID).sort("date", -1).to_list(5000):
        rows.append(
            {
                "kind": "card_txn",
                "id": t["id"],
                "reference": "CTX-" + t["id"][:8].upper(),
                "date": t["date"],
                "description": t["merchant"],
                "method": "Simulated card",
                "amount": t["amount"],
                "currency": t["currency"],
                "category": t["category"],
                "approval_status": "n/a",
                "payment_status": t["status"],
                "reconciliation_status": "declined"
                if t["status"] == "declined"
                else ("matched_to_expense" if t.get("expense_id") else "unmatched"),
                "evidence": ("Expense EXP-" + t["expense_id"][:8].upper())
                if t.get("expense_id")
                else (t.get("decline_reason") or ""),
                "is_synthetic": True,
                "can_toggle": False,
            }
        )
    for p in await db.payments.find({"workspace_id": ws}, NO_ID).sort("created_at", -1).to_list(5000):
        st = p.get("stellar") or {}
        if p["type"] == "domestic":
            rs = (
                "reconciled"
                if p.get("reconciled")
                else ("demo_record" if p["status"] == "recorded_as_paid" else "open")
            )
            method, evidence = (
                "Domestic transfer (demo record)",
                f"{p.get('recipient_bank') or ''} ••{p.get('recipient_account_last4') or ''}",
            )
        else:
            v = st.get("verification") or {}
            if p["status"] == "confirmed":
                rs = (
                    "reconciled"
                    if p.get("reconciled")
                    else ("verified_on_chain" if v.get("matches_request") else "confirmed_unverified")
                )
            else:
                rs = (
                    "awaiting_settlement"
                    if p["status"] in ("approved", "submitting", "pending_verification")
                    else "open"
                )
            method, evidence = "Stellar Testnet", st.get("tx_hash") or ""
        rows.append(
            {
                "kind": "payment",
                "id": p["id"],
                "reference": "PAY-" + p["id"][:8].upper(),
                "date": p["created_at"][:10],
                "description": p["recipient_name"],
                "method": method,
                "amount": p["amount"],
                "currency": p["currency"],
                "category": p["purpose"],
                "approval_status": "approved" if p["status"] not in ("pending_approval", "rejected") else p["status"],
                "payment_status": p["status"],
                "reconciliation_status": rs,
                "evidence": evidence,
                "ledger": st.get("ledger"),
                "explorer": stellar.tx_links(st["tx_hash"])["explorer"] if st.get("tx_hash") else None,
                "is_synthetic": p.get("is_synthetic"),
                "can_toggle": p["status"] in ("recorded_as_paid", "confirmed"),
            }
        )
    rows.sort(key=lambda r: r["date"], reverse=True)
    return rows
