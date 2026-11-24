"""Budget usage calculations."""

from app.database import NO_ID, db

COUNTED = ["approved", "paid"]  # accounting rule: only approved or recorded-as-paid expenses consume budgets


def budget_matches(b: dict, e: dict) -> bool:
    if not e["date"].startswith(b["period"]):
        return False
    if b["scope"] == "department":
        return e.get("department") == b["department"]
    if b["scope"] == "employee":
        return e.get("member_id") == b["member_id"]
    return True


async def budgets_with_usage(ws_id: str, query: dict | None = None) -> list:
    budgets = await db.budgets.find({"workspace_id": ws_id, **(query or {})}, NO_ID).sort("period", -1).to_list(500)
    periods = {b["period"] for b in budgets}
    exps = await db.expenses.find(
        {"workspace_id": ws_id, "status": {"$in": COUNTED}, "date": {"$regex": "^(" + "|".join(periods) + ")"}}
        if periods
        else {"_id": None},
        {"_id": 0, "date": 1, "department": 1, "member_id": 1, "amount_php": 1},
    ).to_list(10000)
    for b in budgets:
        used = round(sum(e["amount_php"] for e in exps if budget_matches(b, e)), 2)
        b["used"] = used
        b["remaining"] = round(b["amount"] - used, 2)
        b["utilization"] = round(used / b["amount"] * 100, 1) if b["amount"] else 0
        b["state"] = (
            "over" if used > b["amount"] else ("warning" if b["utilization"] >= b.get("alert_pct", 80) else "ok")
        )
    return budgets


async def budget_warnings(ws_id: str, exp: dict) -> list:
    out = []
    for b in await budgets_with_usage(ws_id, {"period": exp["date"][:7]}):
        if budget_matches(b, exp) and b["used"] + exp["amount_php"] > b["amount"]:
            out.append(f"Would exceed budget '{b['name']}' (₱{b['remaining']:,.2f} remaining)")
    return out
