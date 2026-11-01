"""Farm Input Inventory Tracker - Flask application.

All inventory arithmetic, validation and alert logic live on the server.
The browser only displays data and performs convenience (client-side) validation.
"""
import csv
import io
import math
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from functools import wraps
from urllib.parse import urlparse

from flask import (Flask, Response, abort, flash, g, jsonify, redirect,
                   render_template, request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
UNITS = ["kg", "g", "litre", "ml", "bag", "packet", "pack", "piece", "pair", "roll", "box", "bottle", "set"]


# --------------------------------------------------------------------------- app factory
def create_app(db_path=None):
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path or os.environ.get("FARM_DB", os.path.join(BASE, "farm_inventory.db"))
    app.config["SECRET_KEY"] = _load_secret_key()
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=2),
        MAX_CONTENT_LENGTH=1024 * 1024,
    )
    if not os.path.exists(app.config["DB_PATH"]):
        import init_db
        init_db.DB_PATH = app.config["DB_PATH"]
        conn = init_db.connect()
        init_db.create_schema(conn)
        init_db.seed_base(conn)
        conn.commit()
        conn.close()
    register(app)
    return app


def _load_secret_key():
    env = os.environ.get("FARM_SECRET_KEY")
    if env:
        return env
    inst = os.path.join(BASE, "instance")
    os.makedirs(inst, exist_ok=True)
    path = os.path.join(inst, "secret.key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


# --------------------------------------------------------------------------- database helpers
def get_db():
    if "db" not in g:
        conn = sqlite3.connect(current_app_path(), isolation_level=None)  # explicit transactions
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


def current_app_path():
    from flask import current_app
    return current_app.config["DB_PATH"]


@contextmanager
def tx(conn):
    """Atomic block: BEGIN IMMEDIATE ... COMMIT, or ROLLBACK on any error."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def q(sql, params=()):
    return get_db().execute(sql, params).fetchall()


def q1(sql, params=()):
    return get_db().execute(sql, params).fetchone()


def get_setting(key, default=""):
    row = q1("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))


def audit(conn, action, detail=""):
    conn.execute("INSERT INTO audit_logs(user_id, action, detail) VALUES (?,?,?)",
                 (session.get("user_id"), action, detail[:500]))


def status_sql(alias="i"):
    """SQL CASE expression giving OUT / LOW / OK according to the configured alert basis."""
    op = "<" if get_setting("alert_basis", "at_or_below") == "below" else "<="
    return (f"CASE WHEN {alias}.quantity <= 0 THEN 'OUT' "
            f"WHEN {alias}.quantity {op} {alias}.min_threshold THEN 'LOW' ELSE 'OK' END")


def stock_status(qty, minimum):
    below = get_setting("alert_basis", "at_or_below") == "below"
    if qty <= 0:
        return "OUT"
    if (qty < minimum) if below else (qty <= minimum):
        return "LOW"
    return "OK"


def sync_alert(conn, item_id):
    """Create/clear notifications so they always reflect the item's current stock status.
    Called inside the same DB transaction that changed the stock."""
    it = conn.execute("SELECT id,name,quantity,min_threshold,unit FROM inventory_items WHERE id=?", (item_id,)).fetchone()
    if not it:
        return
    st = stock_status(it["quantity"], it["min_threshold"])
    last = conn.execute(
        "SELECT kind FROM system_notifications WHERE item_id=? AND kind IN ('LOW','OUT') AND is_read=0 "
        "ORDER BY id DESC LIMIT 1", (item_id,)).fetchone()
    if st == "OK":
        conn.execute("UPDATE system_notifications SET is_read=1 WHERE item_id=? AND kind IN ('LOW','OUT')", (item_id,))
        return
    if last and last["kind"] == st:
        return
    conn.execute("UPDATE system_notifications SET is_read=1 WHERE item_id=? AND kind IN ('LOW','OUT')", (item_id,))
    if st == "OUT":
        msg = f"{it['name']} is out of stock."
    else:
        msg = f"{it['name']} is low on stock ({fmt_qty(it['quantity'])} {it['unit']} left, minimum {fmt_qty(it['min_threshold'])})."
    conn.execute("INSERT INTO system_notifications(item_id,kind,message) VALUES (?,?,?)", (item_id, st, msg))


def fmt_qty(v):
    if v is None:
        return "0"
    return f"{v:,.2f}".rstrip("0").rstrip(".") if float(v) != int(v) else f"{int(v):,}"


# --------------------------------------------------------------------------- validation
class ValidationError(Exception):
    def __init__(self, errors):
        super().__init__("; ".join(errors.values()))
        self.errors = errors


def parse_number(raw, label, errors, key, minimum=0.0, allow_zero=True, maximum=1e9):
    raw = (raw or "").strip().replace(",", "")
    if raw == "":
        errors[key] = f"{label} is required."
        return None
    try:
        val = float(raw)
    except ValueError:
        errors[key] = f"{label} must be a number."
        return None
    if val != val or val in (float("inf"), float("-inf")):
        errors[key] = f"{label} must be a valid number."
        return None
    if val < minimum or (not allow_zero and val == 0):
        errors[key] = f"{label} must be {'greater than zero' if not allow_zero else 'zero or more'}."
        return None
    if val > maximum:
        errors[key] = f"{label} is too large."
        return None
    return round(val, 3)


def validate_item(form, editing_id=None):
    errors, data = {}, {}
    name = (form.get("name") or "").strip()
    if not name:
        errors["name"] = "Item name is required."
    elif len(name) > 100:
        errors["name"] = "Item name must be 100 characters or fewer."
    data["name"] = name
    cat = form.get("category_id", "")
    if not cat.isdigit() or not q1("SELECT 1 FROM categories WHERE id=?", (int(cat),)):
        errors["category_id"] = "Choose a valid category."
    data["category_id"] = int(cat) if cat.isdigit() else None
    unit = (form.get("unit") or "").strip()
    if not unit:
        errors["unit"] = "Unit of measurement is required."
    elif len(unit) > 20:
        errors["unit"] = "Unit must be 20 characters or fewer."
    data["unit"] = unit
    desc = (form.get("description") or "").strip()
    if len(desc) > 500:
        errors["description"] = "Description must be 500 characters or fewer."
    data["description"] = desc
    data["min_threshold"] = parse_number(form.get("min_threshold"), "Minimum stock threshold", errors, "min_threshold")
    data["unit_price"] = parse_number(form.get("unit_price"), "Unit purchase price", errors, "unit_price")
    sup = (form.get("supplier") or "").strip()
    if len(sup) > 100:
        errors["supplier"] = "Supplier name must be 100 characters or fewer."
    data["supplier"] = sup
    code = (form.get("item_code") or "").strip().upper()
    if code and not re.fullmatch(r"[A-Z0-9\-]{2,20}", code):
        errors["item_code"] = "Item ID may only contain letters, digits and hyphens (2-20 characters)."
    elif code:
        dup = q1("SELECT id FROM inventory_items WHERE item_code=? AND id<>?", (code, editing_id or 0))
        if dup:
            errors["item_code"] = "That Item ID is already in use."
    data["item_code"] = code
    data["is_returnable"] = 1 if form.get("is_returnable") in ("1", "on") else 0
    if editing_id is None:
        raw = (form.get("opening_qty") or "").strip()
        data["opening_qty"] = 0.0
        if raw:
            v = parse_number(raw, "Opening stock", errors, "opening_qty")
            data["opening_qty"] = v or 0.0
    if errors:
        raise ValidationError(errors)
    return data


def get_or_create_supplier(conn, name):
    if not name:
        return None
    row = conn.execute("SELECT id FROM suppliers WHERE name=?", (name,)).fetchone()
    if row:
        return row["id"]
    return conn.execute("INSERT INTO suppliers(name) VALUES (?)", (name,)).lastrowid


def next_item_code(conn, category_id):
    cat = conn.execute("SELECT name FROM categories WHERE id=?", (category_id,)).fetchone()["name"]
    prefixes = {"Seeds": "SD", "Fertilizers": "FT", "Pesticides": "PS", "Herbicides": "HB",
                "Farming tools": "TL", "Irrigation equipment": "IR", "Other farm supplies": "OT"}
    prefix = prefixes.get(cat) or re.sub(r"[^A-Z]", "", cat.upper())[:2].ljust(2, "X")
    n = 0
    for r in conn.execute("SELECT item_code FROM inventory_items WHERE item_code LIKE ?", (prefix + "-%",)):
        m = re.fullmatch(re.escape(prefix) + r"-(\d+)", r["item_code"])
        if m:
            n = max(n, int(m.group(1)))
    return f"{prefix}-{n + 1:03d}"


# --------------------------------------------------------------------------- core stock operation
TXN_LABEL = {"IN": "Stock in", "OUT": "Stock out", "LOAN": "Lent", "RETURN": "Returned"}
TXN_PREFIX = {"IN": "STI", "OUT": "STO", "LOAN": "LON", "RETURN": "RET"}


def loan_outstanding(conn, item_id, borrower):
    """Quantity of an item that a given borrower currently holds (lent minus returned)."""
    row = conn.execute(
        """SELECT COALESCE(SUM(CASE txn_type WHEN 'LOAN' THEN quantity ELSE -quantity END),0) FROM stock_transactions
           WHERE item_id=? AND txn_type IN ('LOAN','RETURN') AND counterparty=? COLLATE NOCASE""",
        (item_id, borrower)).fetchone()
    return round(row[0], 3)


def record_transaction(item_id, txn_type, quantity, txn_date, responsible, counterparty, notes, unit_price=None):
    """Atomically record a stock movement and update the item's quantity.

    txn_type: IN (received), OUT (issued/consumed/sold), LOAN (tool lent out), RETURN (tool returned).
    Returns the new transaction reference. Raises ValidationError for business-rule violations
    (e.g. issuing more than is available). Nothing is written unless every step succeeds.
    """
    conn = get_db()
    with tx(conn):
        item = conn.execute("SELECT * FROM inventory_items WHERE id=?", (item_id,)).fetchone()
        if not item:
            raise ValidationError({"item_id": "Selected item does not exist."})
        if txn_type in ("LOAN", "RETURN") and not item["is_returnable"]:
            raise ValidationError({"item_id": f"{item['name']} is not marked as a returnable tool, so it cannot be lent or returned."})
        price = item["unit_price"]
        if txn_type in ("OUT", "LOAN"):
            if quantity > item["quantity"]:
                raise ValidationError({"quantity": (
                    f"Insufficient stock: only {fmt_qty(item['quantity'])} {item['unit']} of "
                    f"{item['name']} available, cannot {'lend' if txn_type == 'LOAN' else 'issue'} {fmt_qty(quantity)}.")})
            new_qty = round(item["quantity"] - quantity, 3)
            cur = conn.execute(
                "UPDATE inventory_items SET quantity=?, updated_at=datetime('now') WHERE id=? AND quantity>=?",
                (new_qty, item_id, quantity))
            if cur.rowcount != 1:  # guard against a concurrent change
                raise ValidationError({"quantity": "Stock changed while saving; please retry."})
        elif txn_type == "RETURN":
            held = loan_outstanding(conn, item_id, counterparty)
            if quantity > held + 1e-9:
                raise ValidationError({"quantity": (
                    f"{counterparty} has {fmt_qty(held)} {item['unit']} of {item['name']} on loan, "
                    f"so {fmt_qty(quantity)} cannot be returned.")})
            new_qty = round(item["quantity"] + quantity, 3)
            conn.execute("UPDATE inventory_items SET quantity=?, updated_at=datetime('now') WHERE id=?", (new_qty, item_id))
        else:  # IN
            new_qty = round(item["quantity"] + quantity, 3)
            price = unit_price if unit_price is not None else item["unit_price"]
            conn.execute(
                "UPDATE inventory_items SET quantity=?, unit_price=?, updated_at=datetime('now') WHERE id=?",
                (new_qty, price, item_id))
        next_id = conn.execute("SELECT COALESCE(MAX(id),0)+1 FROM stock_transactions").fetchone()[0]
        ref = f"{TXN_PREFIX[txn_type]}-{txn_date.replace('-', '')}-{next_id:04d}"
        conn.execute(
            """INSERT INTO stock_transactions(reference,item_id,txn_type,quantity,balance_after,unit_price,
               txn_date,responsible,counterparty,notes,created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (ref, item_id, txn_type, quantity, new_qty, price, txn_date, responsible,
             counterparty or None, notes or None, session.get("user_id")))
        sync_alert(conn, item_id)
        audit(conn, f"STOCK_{txn_type}", f"{ref}: {item['item_code']} {fmt_qty(quantity)} {item['unit']}")
    return ref


def validate_transaction(form, txn_type):
    errors, data = {}, {}
    iid = form.get("item_id", "")
    if not iid.isdigit() or not q1("SELECT 1 FROM inventory_items WHERE id=?", (int(iid),)):
        errors["item_id"] = "Choose an item."
    data["item_id"] = int(iid) if iid.isdigit() else None
    data["quantity"] = parse_number(form.get("quantity"), "Quantity", errors, "quantity", allow_zero=False)
    d = (form.get("txn_date") or "").strip()
    try:
        parsed = datetime.strptime(d, "%Y-%m-%d").date()
        if parsed > date.today():
            errors["txn_date"] = "Date cannot be in the future."
        elif parsed < date.today() - timedelta(days=3650):
            errors["txn_date"] = "Date is too far in the past."
    except ValueError:
        errors["txn_date"] = "Enter a valid date."
    data["txn_date"] = d
    resp = (form.get("responsible") or "").strip()
    if not resp:
        errors["responsible"] = "Responsible person is required."
    elif len(resp) > 100:
        errors["responsible"] = "Name must be 100 characters or fewer."
    data["responsible"] = resp
    cp = (form.get("counterparty") or "").strip()
    if txn_type in ("OUT", "LOAN", "RETURN") and not cp:
        errors["counterparty"] = {"OUT": "Recipient / purpose is required for stock issued.",
                                  "LOAN": "Borrower is required.", "RETURN": "Borrower is required."}[txn_type]
    if len(cp) > 100:
        errors["counterparty"] = "Must be 100 characters or fewer."
    data["counterparty"] = cp
    notes = (form.get("notes") or "").strip()
    if len(notes) > 500:
        errors["notes"] = "Notes must be 500 characters or fewer."
    data["notes"] = notes
    data["unit_price"] = None
    if txn_type == "IN" and (form.get("unit_price") or "").strip():
        data["unit_price"] = parse_number(form.get("unit_price"), "Unit price", errors, "unit_price")
    if errors:
        raise ValidationError(errors)
    return data


# --------------------------------------------------------------------------- loans and forecast
def outstanding_loans(item_id=None):
    """Tools currently out: one row per (item, borrower) with a positive balance of lent minus returned."""
    where, params = "", []
    if item_id:
        where, params = "AND t.item_id=?", [item_id]
    rows = q(f"""SELECT i.id item_id, i.item_code, i.name, i.unit, MIN(t.counterparty) borrower,
                 ROUND(SUM(CASE t.txn_type WHEN 'LOAN' THEN t.quantity ELSE -t.quantity END),3) held,
                 MAX(CASE WHEN t.txn_type='LOAN' THEN t.txn_date END) last_loan
                 FROM stock_transactions t JOIN inventory_items i ON i.id=t.item_id
                 WHERE t.txn_type IN ('LOAN','RETURN') {where}
                 GROUP BY i.id, LOWER(t.counterparty) HAVING held > 0 ORDER BY last_loan, i.name""", params)
    today = date.today()
    out = []
    for r in rows:
        d = dict(r)
        d["days_out"] = (today - datetime.strptime(r["last_loan"], "%Y-%m-%d").date()).days if r["last_loan"] else 0
        out.append(d)
    return out


def int_setting(key, default, lo=1, hi=365):
    try:
        return max(lo, min(int(get_setting(key, str(default))), hi))
    except ValueError:
        return default


def forecast_rows():
    """Per-item run-out forecast from recent usage (stock out only; lending is not consumption).

    daily use   = quantity issued in the last `forecast_days` / forecast_days
    days cover  = quantity in store / daily use
    needs order = status LOW/OUT, or cover <= `warn_days`
    suggested   = daily use x `target_cover_days` + minimum threshold - quantity in store (rounded up)
    """
    days, warn, target = int_setting("forecast_days", 30), int_setting("warn_days", 14), int_setting("target_cover_days", 30)
    since = (date.today() - timedelta(days=days - 1)).isoformat()
    st = status_sql("i")
    rows = q(f"""SELECT i.id, i.item_code, i.name, i.quantity, i.unit, i.min_threshold, i.unit_price, i.is_returnable,
                 c.name cat, s.name sup, {st} status,
                 COALESCE((SELECT SUM(quantity) FROM stock_transactions t
                           WHERE t.item_id=i.id AND t.txn_type='OUT' AND t.txn_date>=?),0) used
                 FROM inventory_items i JOIN categories c ON c.id=i.category_id LEFT JOIN suppliers s ON s.id=i.supplier_id""", (since,))
    out = []
    for r in rows:
        d = dict(r)
        daily = r["used"] / days
        d["daily"] = daily
        d["cover"] = (r["quantity"] / daily) if daily > 0 else None
        d["need"] = r["status"] != "OK" or (d["cover"] is not None and d["cover"] <= warn)
        raw = daily * target + r["min_threshold"] - r["quantity"]
        d["suggested"] = int(math.ceil(round(raw, 6))) if d["need"] and raw > 0 else 0
        d["cost"] = d["suggested"] * r["unit_price"]
        out.append(d)
    out.sort(key=lambda d: (d["status"] != "OUT", d["status"] != "LOW", d["cover"] if d["cover"] is not None else 1e9, d["name"]))
    return out


# --------------------------------------------------------------------------- reports
REPORTS = {
    "inventory": "General inventory report",
    "item": "Individual item report",
    "stock_in": "Stock-in report",
    "stock_out": "Stock-out report",
    "usage": "Usage report",
    "low_stock": "Low-stock report",
    "valuation": "Inventory valuation report",
    "reorder": "Reorder planner (purchase list by supplier)",
    "loans": "Tool loans report (items currently out)",
}
DATE_REPORTS = {"inventory", "item", "stock_in", "stock_out", "usage"}


def parse_range(args):
    today = date.today()
    f = (args.get("date_from") or "").strip()
    t = (args.get("date_to") or "").strip()
    try:
        d_from = datetime.strptime(f, "%Y-%m-%d").date() if f else today - timedelta(days=29)
        d_to = datetime.strptime(t, "%Y-%m-%d").date() if t else today
    except ValueError:
        raise ValidationError({"date_from": "Enter valid dates for the report period."})
    if d_from > d_to:
        raise ValidationError({"date_from": "'From' date must not be later than 'To' date."})
    return d_from.isoformat(), d_to.isoformat()


def money(v):
    cur = get_setting("currency", "NGN")
    return f"{cur} {v or 0:,.2f}"


def build_report(kind, args):
    if kind not in REPORTS:
        abort(404)
    r = {"kind": kind, "title": REPORTS[kind], "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
         "store": get_setting("store_name"), "period": None, "summary": [], "columns": [], "rows": [],
         "notes": [], "money_cols": set(), "right_cols": set()}
    st = status_sql("i")
    if kind in DATE_REPORTS:
        d_from, d_to = parse_range(args)
        r["period"] = f"{d_from} to {d_to}"
    else:
        r["notes"].append("This report reflects stock levels at the time it was generated; no date range applies.")

    if kind == "inventory":
        rows = q(f"""SELECT i.item_code, i.name, c.name cat, i.quantity, i.unit, i.min_threshold, i.unit_price,
                     {st} status,
                     COALESCE((SELECT SUM(quantity) FROM stock_transactions t WHERE t.item_id=i.id AND t.txn_type='IN' AND t.txn_date BETWEEN ? AND ?),0) rin,
                     COALESCE((SELECT SUM(quantity) FROM stock_transactions t WHERE t.item_id=i.id AND t.txn_type='OUT' AND t.txn_date BETWEEN ? AND ?),0) rout,
                     COALESCE((SELECT SUM(CASE txn_type WHEN 'LOAN' THEN quantity WHEN 'RETURN' THEN -quantity ELSE 0 END) FROM stock_transactions t WHERE t.item_id=i.id),0) loaned
                     FROM inventory_items i JOIN categories c ON c.id=i.category_id ORDER BY c.name, i.name""",
                 (d_from, d_to, d_from, d_to))
        r["columns"] = ["Item ID", "Item", "Category", "In stock", "Unit", "Minimum", "Status", "Received (period)", "Issued (period)", "On loan", "Stock value"]
        r["right_cols"] = {3, 5, 7, 8, 9, 10}
        for x in rows:
            r["rows"].append([x["item_code"], x["name"], x["cat"], fmt_qty(x["quantity"]), x["unit"], fmt_qty(x["min_threshold"]),
                              x["status"].replace("OK", "In stock").replace("LOW", "Low").replace("OUT", "Out of stock"),
                              fmt_qty(x["rin"]), fmt_qty(x["rout"]), fmt_qty(x["loaned"]), money(x["quantity"] * x["unit_price"])])
        r["summary"] = [("Registered items", len(rows)),
                        ("Low-stock items", sum(1 for x in rows if x["status"] == "LOW")),
                        ("Out-of-stock items", sum(1 for x in rows if x["status"] == "OUT")),
                        ("Total stock value", money(sum(x["quantity"] * x["unit_price"] for x in rows)))]

    elif kind == "item":
        iid = args.get("item_id", "")
        item = q1("SELECT i.*, c.name cat FROM inventory_items i JOIN categories c ON c.id=i.category_id WHERE i.id=?",
                  (int(iid),)) if iid.isdigit() else None
        if not item:
            raise ValidationError({"item_id": "Select an item for the individual item report."})
        r["title"] = f"Individual item report: {item['name']} ({item['item_code']})"
        rows = q("""SELECT t.txn_date, t.reference, t.txn_type, t.quantity, t.balance_after, t.counterparty, t.responsible, t.notes
                    FROM stock_transactions t WHERE t.item_id=? AND t.txn_date BETWEEN ? AND ? ORDER BY t.id""",
                 (item["id"], d_from, d_to))
        r["columns"] = ["Date", "Reference", "Type", "Quantity", "Balance after", "Supplier / recipient", "Responsible", "Notes"]
        r["right_cols"] = {3, 4}
        for x in rows:
            r["rows"].append([x["txn_date"], x["reference"], TXN_LABEL[x["txn_type"]],
                              fmt_qty(x["quantity"]), fmt_qty(x["balance_after"]), x["counterparty"] or "-", x["responsible"], x["notes"] or ""])
        tin = sum(x["quantity"] for x in rows if x["txn_type"] == "IN")
        tout = sum(x["quantity"] for x in rows if x["txn_type"] == "OUT")
        on_loan = sum(l["held"] for l in outstanding_loans(item["id"])) if item["is_returnable"] else 0
        r["summary"] = [("Category", item["cat"]), ("Current stock in store", f"{fmt_qty(item['quantity'])} {item['unit']}"),
                        *([("Currently on loan", f"{fmt_qty(on_loan)} {item['unit']}")] if item["is_returnable"] else []),
                        ("Minimum threshold", fmt_qty(item["min_threshold"])),
                        ("Status", stock_status(item["quantity"], item["min_threshold"]).replace("OK", "In stock").replace("LOW", "Low").replace("OUT", "Out of stock")),
                        ("Received in period", fmt_qty(tin)), ("Issued in period", fmt_qty(tout)),
                        ("Movements in period", len(rows))]

    elif kind in ("stock_in", "stock_out"):
        t = "IN" if kind == "stock_in" else "OUT"
        rows = q("""SELECT t.txn_date, t.reference, i.item_code, i.name, i.unit, t.quantity, t.unit_price, t.counterparty, t.responsible
                    FROM stock_transactions t JOIN inventory_items i ON i.id=t.item_id
                    WHERE t.txn_type=? AND t.txn_date BETWEEN ? AND ? ORDER BY t.txn_date, t.id""", (t, d_from, d_to))
        who = "Supplier" if t == "IN" else "Recipient / purpose"
        r["columns"] = ["Date", "Reference", "Item ID", "Item", "Quantity", "Unit", "Value", who, "Responsible"]
        r["right_cols"] = {4, 6}
        for x in rows:
            r["rows"].append([x["txn_date"], x["reference"], x["item_code"], x["name"], fmt_qty(x["quantity"]), x["unit"],
                              money(x["quantity"] * x["unit_price"]), x["counterparty"] or "-", x["responsible"]])
        r["summary"] = [("Transactions", len(rows)), ("Distinct items", len({x["item_code"] for x in rows})),
                        ("Total value", money(sum(x["quantity"] * x["unit_price"] for x in rows)))]
        r["notes"].append("Quantities are in each item's own unit and are therefore not added together.")

    elif kind == "usage":
        rows = q(f"""SELECT i.item_code, i.name, c.name cat, i.unit, i.quantity,
                     COALESCE(SUM(t.quantity),0) used, COUNT(t.id) n
                     FROM inventory_items i JOIN categories c ON c.id=i.category_id
                     LEFT JOIN stock_transactions t ON t.item_id=i.id AND t.txn_type='OUT' AND t.txn_date BETWEEN ? AND ?
                     GROUP BY i.id ORDER BY used DESC, i.name""", (d_from, d_to))
        days = (datetime.strptime(d_to, "%Y-%m-%d") - datetime.strptime(d_from, "%Y-%m-%d")).days + 1
        r["columns"] = ["Item ID", "Item", "Category", "Quantity used", "Unit", "Issues", "Avg per day", "Current stock"]
        r["right_cols"] = {3, 5, 6, 7}
        for x in rows:
            r["rows"].append([x["item_code"], x["name"], x["cat"], fmt_qty(x["used"]), x["unit"], x["n"],
                              f"{x['used'] / days:.2f}", fmt_qty(x["quantity"])])
        used = [x for x in rows if x["used"] > 0]
        r["summary"] = [("Period length", f"{days} days"), ("Items with usage", len(used)),
                        ("Issue transactions", sum(x["n"] for x in rows)),
                        ("Most used item", used[0]["name"] if used else "None")]
        r["notes"].append("Usage is the total quantity recorded as stock out within the period. Tools lent out and returned are not counted as usage.")

    elif kind == "low_stock":
        rows = q(f"""SELECT i.item_code, i.name, c.name cat, i.quantity, i.unit, i.min_threshold, {st} status, s.name sup
                     FROM inventory_items i JOIN categories c ON c.id=i.category_id LEFT JOIN suppliers s ON s.id=i.supplier_id
                     WHERE {st} <> 'OK' ORDER BY (i.quantity - i.min_threshold), i.name""")
        r["columns"] = ["Item ID", "Item", "Category", "In stock", "Unit", "Minimum", "Shortfall", "Status", "Supplier"]
        r["right_cols"] = {3, 5, 6}
        for x in rows:
            r["rows"].append([x["item_code"], x["name"], x["cat"], fmt_qty(x["quantity"]), x["unit"], fmt_qty(x["min_threshold"]),
                              fmt_qty(max(x["min_threshold"] - x["quantity"], 0)),
                              "Out of stock" if x["status"] == "OUT" else "Low", x["sup"] or "-"])
        r["summary"] = [("Items needing attention", len(rows)),
                        ("Out of stock", sum(1 for x in rows if x["status"] == "OUT")),
                        ("Low stock", sum(1 for x in rows if x["status"] == "LOW"))]

    elif kind == "valuation":
        rows = q("""SELECT i.item_code, i.name, c.name cat, i.quantity, i.unit, i.unit_price
                    FROM inventory_items i JOIN categories c ON c.id=i.category_id ORDER BY c.name, i.name""")
        r["columns"] = ["Item ID", "Item", "Category", "In stock", "Unit", "Unit price", "Stock value"]
        r["right_cols"] = {3, 5, 6}
        r["money_cols"] = {5, 6}
        by_cat = {}
        for x in rows:
            val = x["quantity"] * x["unit_price"]
            by_cat[x["cat"]] = by_cat.get(x["cat"], 0) + val
            r["rows"].append([x["item_code"], x["name"], x["cat"], fmt_qty(x["quantity"]), x["unit"], money(x["unit_price"]), money(val)])
        total = sum(by_cat.values())
        r["summary"] = [("Total inventory value", money(total))] + [(f"{k}", money(v)) for k, v in by_cat.items()]
        r["notes"].append("Value = quantity in stock x latest recorded unit purchase price (items on loan are not included).")

    elif kind == "reorder":
        days, warn, target = int_setting("forecast_days", 30), int_setting("warn_days", 14), int_setting("target_cover_days", 30)
        rows = sorted([d for d in forecast_rows() if d["need"]], key=lambda d: ((d["sup"] or "~"), d["name"]))
        r["columns"] = ["Supplier", "Item ID", "Item", "In stock", "Unit", "Avg daily use", "Days of cover", "Suggested order", "Unit price", "Est. cost"]
        r["right_cols"] = {3, 5, 6, 7, 8, 9}
        for d in rows:
            r["rows"].append([d["sup"] or "No supplier", d["item_code"], d["name"], fmt_qty(d["quantity"]), d["unit"], f"{d['daily']:.2f}",
                              "-" if d["cover"] is None else f"{d['cover']:.0f} d", fmt_qty(d["suggested"]), money(d["unit_price"]), money(d["cost"])])
        r["summary"] = [("Items to reorder", len(rows)), ("Suppliers to contact", len({d["sup"] for d in rows})),
                        ("Estimated total cost", money(sum(d["cost"] for d in rows))),
                        ("Forecast basis", f"last {days} days of stock out")]
        r["notes"].append(f"An item is listed when it is low or out of stock, or when its days of cover are {warn} or fewer. "
                          f"Suggested order = average daily use x {target} days + minimum threshold - quantity in store, rounded up. "
                          "Estimates use recorded history only and do not know about future plans or supplier lead times.")

    elif kind == "loans":
        rows = outstanding_loans()
        r["columns"] = ["Item ID", "Tool", "Borrower", "Quantity out", "Unit", "Last lent", "Days out"]
        r["right_cols"] = {3, 6}
        for d in rows:
            r["rows"].append([d["item_code"], d["name"], d["borrower"], fmt_qty(d["held"]), d["unit"], d["last_loan"], d["days_out"]])
        r["summary"] = [("Open loans", len(rows)), ("Units out", fmt_qty(sum(d["held"] for d in rows))),
                        ("Borrowers", len({d["borrower"].lower() for d in rows})),
                        ("Longest out", f"{max((d['days_out'] for d in rows), default=0)} days")]
        r["notes"].append("Days out is counted from the most recent date the tool was lent to that borrower.")
    return r


def report_csv(rep):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([rep["store"]])
    w.writerow([rep["title"]])
    if rep["period"]:
        w.writerow(["Period", rep["period"]])
    w.writerow(["Generated", rep["generated_at"]])
    for k, v in rep["summary"]:
        w.writerow([k, v])
    w.writerow([])
    w.writerow(rep["columns"])
    for row in rep["rows"]:
        w.writerow(["'" + c if isinstance(c, str) and c[:1] in "=+-@" and c != "-" else c for c in row])
    return out.getvalue()


def report_pdf(rep):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    buf = io.BytesIO()
    wide = len(rep["columns"]) > 6
    page = landscape(A4) if wide else A4

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#555555"))
        canvas.drawString(15 * mm, 9 * mm, f"{rep['store']} - Generated {rep['generated_at']}")
        canvas.drawRightString(page[0] - 15 * mm, 9 * mm, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(buf, pagesize=page, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=16 * mm,
                            title=rep["title"], author=rep["store"])
    ss = getSampleStyleSheet()
    cell = ParagraphStyle("c", parent=ss["Normal"], fontSize=8, leading=10)
    head = ParagraphStyle("h", parent=cell, textColor=colors.white, fontName="Helvetica-Bold")
    story = [Paragraph(f"<b>{_x(rep['store'])}</b>", ss["Heading3"]), Paragraph(f"<b>{_x(rep['title'])}</b>", ss["Heading2"])]
    meta = f"Generated: {rep['generated_at']}" + (f" &nbsp;|&nbsp; Period: {rep['period']}" if rep["period"] else "")
    story += [Paragraph(meta, ss["Normal"]), Spacer(1, 6)]
    if rep["summary"]:
        st = Table([[Paragraph(f"<b>{_x(str(k))}</b>", cell), Paragraph(_x(str(v)), cell)] for k, v in rep["summary"]],
                   colWidths=[60 * mm, 60 * mm], hAlign="LEFT")
        st.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#c8d3cc")),
                                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#eef4f0"))]))
        story += [st, Spacer(1, 8)]
    data = [[Paragraph(_x(c), head) for c in rep["columns"]]]
    for row in rep["rows"]:
        data.append([Paragraph(_x(str(c)), cell) for c in row])
    if len(data) == 1:
        story.append(Paragraph("No records for the selected criteria.", ss["Normal"]))
    else:
        t = Table(data, repeatRows=1)
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f6f43")),
                               ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f7f5")]),
                               ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#c8d3cc")),
                               ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        story.append(t)
    for n in rep["notes"]:
        story += [Spacer(1, 6), Paragraph(f"<i>{_x(n)}</i>", cell)]
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


def _x(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --------------------------------------------------------------------------- pagination / sorting
def paginate(total, page, per_page):
    pages = max((total + per_page - 1) // per_page, 1)
    page = min(max(page, 1), pages)
    return {"page": page, "pages": pages, "per_page": per_page, "total": total, "offset": (page - 1) * per_page}


def page_arg():
    try:
        return int(request.args.get("page", 1))
    except ValueError:
        return 1


def per_page():
    try:
        return max(5, min(int(get_setting("page_size", "10")), 100))
    except ValueError:
        return 10


# --------------------------------------------------------------------------- routes
def register(app):
    @app.teardown_appcontext
    def close_db(_exc):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.after_request
    def headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Content-Security-Policy"] = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                                                   "script-src 'self'; frame-ancestors 'none'")
        if "user_id" in session:
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # ---- CSRF
    def csrf_token():
        if "_csrf" not in session:
            session["_csrf"] = secrets.token_hex(16)
        return session["_csrf"]

    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.before_request
    def csrf_protect():
        if request.method == "POST":
            sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token")
            if not sent or not secrets.compare_digest(sent, session.get("_csrf", "")):
                abort(400, "Security token missing or invalid. Please reload the page and try again.")

    # ---- template helpers
    @app.context_processor
    def inject():
        ctx = {"fmt_qty": fmt_qty, "money": money, "UNITS": UNITS, "today": date.today().isoformat()}
        if "user_id" in session:
            ctx["store_name"] = get_setting("store_name", "Farm Store")
            ctx["unread_alerts"] = q1("SELECT COUNT(*) c FROM system_notifications WHERE is_read=0 AND kind IN ('LOW','OUT')")["c"]
            ctx["current_user"] = q1("SELECT id,username,full_name,role FROM users WHERE id=?", (session["user_id"],))
        return ctx

    app.jinja_env.filters["qty"] = fmt_qty
    app.jinja_env.filters["money"] = money

    @app.errorhandler(400)
    def e400(e):
        return render_template("error.html", code=400, message=getattr(e, "description", "Bad request")), 400

    @app.errorhandler(403)
    def e403(e):
        return render_template("error.html", code=403, message="You do not have permission to do that."), 403

    @app.errorhandler(404)
    def e404(e):
        return render_template("error.html", code=404, message="The page you requested could not be found."), 404

    @app.errorhandler(500)
    def e500(e):
        return render_template("error.html", code=500, message="Something went wrong on the server. No changes were saved."), 500

    # ---- auth decorators
    def login_required(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if "user_id" not in session:
                return redirect(url_for("login", next=request.full_path.rstrip("?")))
            u = q1("SELECT is_active FROM users WHERE id=?", (session["user_id"],))
            if not u or not u["is_active"]:
                session.clear()
                return redirect(url_for("login"))
            return fn(*a, **kw)
        return wrapper

    def admin_required(fn):
        @wraps(fn)
        @login_required
        def wrapper(*a, **kw):
            if session.get("role") != "admin":
                abort(403)
            return fn(*a, **kw)
        return wrapper

    # ---- login / logout
    failed = {}

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if "user_id" in session:
            return redirect(url_for("dashboard"))
        error = None
        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            key = (username.lower(), request.remote_addr)
            tries = [t for t in failed.get(key, []) if t > datetime.now() - timedelta(minutes=5)]
            if len(tries) >= 5:
                error = "Too many failed attempts. Please wait 5 minutes and try again."
            else:
                u = q1("SELECT * FROM users WHERE username=? AND is_active=1", (username,))
                if u and check_password_hash(u["password_hash"], password):
                    session.clear()
                    session.permanent = True
                    session.update(user_id=u["id"], role=u["role"])
                    conn = get_db()
                    with tx(conn):
                        conn.execute("UPDATE users SET last_login=datetime('now') WHERE id=?", (u["id"],))
                        audit(conn, "LOGIN", username)
                    failed.pop(key, None)
                    nxt = request.args.get("next", "")
                    if nxt and urlparse(nxt).netloc == "" and nxt.startswith("/") and not nxt.startswith("//"):
                        return redirect(nxt)
                    return redirect(url_for("dashboard"))
                tries.append(datetime.now())
                failed[key] = tries
                error = "Incorrect username or password."
        return render_template("login.html", error=error)

    @app.route("/logout", methods=["POST"])
    @login_required
    def logout():
        conn = get_db()
        with tx(conn):
            audit(conn, "LOGOUT")
        session.clear()
        flash("You have been signed out.", "info")
        return redirect(url_for("login"))

    # ---- dashboard
    @app.route("/")
    @login_required
    def dashboard():
        st = status_sql("i")
        totals = q1(f"""SELECT COUNT(*) items, COALESCE(SUM(quantity),0) stock,
                        SUM(CASE WHEN {st}='LOW' THEN 1 ELSE 0 END) low,
                        SUM(CASE WHEN {st}='OUT' THEN 1 ELSE 0 END) out_,
                        COALESCE(SUM(quantity*unit_price),0) value FROM inventory_items i""")
        mv = q1("""SELECT COALESCE(SUM(CASE WHEN txn_type='IN' THEN quantity END),0) rin,
                   COALESCE(SUM(CASE WHEN txn_type='OUT' THEN quantity END),0) rout FROM stock_transactions""")
        recent = q("""SELECT t.*, i.name item_name, i.unit FROM stock_transactions t JOIN inventory_items i ON i.id=t.item_id
                      ORDER BY t.id DESC LIMIT 8""")
        cats = q("""SELECT c.name, COUNT(i.id) n FROM categories c LEFT JOIN inventory_items i ON i.category_id=c.id
                    GROUP BY c.id HAVING n>0 ORDER BY n DESC""")
        weeks = []
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        for k in range(11, -1, -1):
            ws = monday - timedelta(weeks=k)
            we = ws + timedelta(days=6)
            row = q1("""SELECT COALESCE(SUM(CASE WHEN txn_type='IN' THEN quantity END),0) a,
                        COALESCE(SUM(CASE WHEN txn_type='OUT' THEN quantity END),0) b
                        FROM stock_transactions WHERE txn_date BETWEEN ? AND ?""", (ws.isoformat(), we.isoformat()))
            weeks.append({"label": ws.strftime("%d %b"), "in": row["a"], "out": row["b"]})
        alerts = q(f"""SELECT i.id, i.name, i.quantity, i.unit, i.min_threshold, {st} status FROM inventory_items i
                       WHERE {st}<>'OK' ORDER BY i.quantity - i.min_threshold, i.name LIMIT 6""")
        sample = q1("SELECT (SELECT COUNT(*) FROM inventory_items WHERE is_sample=1) a")["a"]
        chart = {"categories": [{"name": c["name"], "n": c["n"]} for c in cats], "weeks": weeks}
        fc = forecast_rows()
        watch = [d for d in fc if d["need"]]
        warn = int_setting("warn_days", 14)
        soon = sum(1 for d in watch if d["status"] == "OK")
        loans = outstanding_loans()
        return render_template("dashboard.html", totals=totals, mv=mv, recent=recent, alerts=alerts, chart=chart, sample=sample,
                               watch=watch[:6], watch_total=len(watch), soon=soon, warn=warn, loans=loans,
                               loan_units=sum(l["held"] for l in loans), reorder_cost=sum(d["cost"] for d in watch))

    # ---- inventory
    SORTS = {"code": "i.item_code", "name": "i.name", "category": "c.name", "quantity": "i.quantity",
             "price": "i.unit_price", "updated": "i.updated_at"}

    @app.route("/inventory")
    @login_required
    def inventory():
        search = (request.args.get("q") or "").strip()
        cat = request.args.get("category", "")
        status = request.args.get("status", "")
        sort = request.args.get("sort", "name")
        sort = sort if sort in SORTS else "name"
        order = "DESC" if request.args.get("order") == "desc" else "ASC"
        st = status_sql("i")
        where, params = ["1=1"], []
        if search:
            where.append("(i.name LIKE ? OR i.item_code LIKE ? OR i.description LIKE ? OR s.name LIKE ?)")
            params += [f"%{search}%"] * 4
        if cat.isdigit():
            where.append("i.category_id=?")
            params.append(int(cat))
        if status in ("OK", "LOW", "OUT"):
            where.append(f"{st}=?")
            params.append(status)
        base = f"FROM inventory_items i JOIN categories c ON c.id=i.category_id LEFT JOIN suppliers s ON s.id=i.supplier_id WHERE {' AND '.join(where)}"
        total = q1(f"SELECT COUNT(*) c {base}", params)["c"]
        pg = paginate(total, page_arg(), per_page())
        items = q(f"SELECT i.*, c.name category, s.name supplier, {st} status {base} ORDER BY {SORTS[sort]} {order}, i.id LIMIT ? OFFSET ?",
                  params + [pg["per_page"], pg["offset"]])
        return render_template("inventory.html", fcmap={d["id"]: d for d in forecast_rows()}, items=items, pg=pg, categories=q("SELECT * FROM categories ORDER BY name"),
                               args={"q": search, "category": cat, "status": status, "sort": sort, "order": order.lower()})

    def item_form_context(form=None, errors=None, item=None):
        return dict(categories=q("SELECT * FROM categories ORDER BY name"), suppliers=q("SELECT name FROM suppliers ORDER BY name"),
                    form=form or {}, errors=errors or {}, item=item, default_min=get_setting("default_min_threshold", "10"))

    @app.route("/inventory/new", methods=["GET", "POST"])
    @login_required
    def item_new():
        if request.method == "POST":
            try:
                d = validate_item(request.form)
                conn = get_db()
                with tx(conn):
                    code = d["item_code"] or next_item_code(conn, d["category_id"])
                    sid = get_or_create_supplier(conn, d["supplier"])
                    iid = conn.execute(
                        """INSERT INTO inventory_items(item_code,name,category_id,description,quantity,unit,min_threshold,unit_price,supplier_id,is_returnable)
                           VALUES (?,?,?,?,0,?,?,?,?,?)""",
                        (code, d["name"], d["category_id"], d["description"], d["unit"], d["min_threshold"], d["unit_price"], sid,
                         d["is_returnable"])).lastrowid
                    audit(conn, "ITEM_CREATE", f"{code} {d['name']}")
                if d["opening_qty"] > 0:
                    record_transaction(iid, "IN", d["opening_qty"], date.today().isoformat(),
                                       q1("SELECT full_name FROM users WHERE id=?", (session["user_id"],))["full_name"],
                                       d["supplier"], "Opening stock recorded when item was registered", d["unit_price"])
                else:
                    conn2 = get_db()
                    with tx(conn2):
                        sync_alert(conn2, iid)
                flash(f"Item {code} - {d['name']} registered.", "success")
                return redirect(url_for("item_detail", item_id=iid))
            except ValidationError as e:
                flash("Please correct the highlighted fields.", "error")
                return render_template("item_form.html", **item_form_context(request.form, e.errors)), 422
        return render_template("item_form.html", **item_form_context({"min_threshold": get_setting("default_min_threshold", "10")}))

    @app.route("/inventory/<int:item_id>")
    @login_required
    def item_detail(item_id):
        st = status_sql("i")
        item = q1(f"""SELECT i.*, c.name category, s.name supplier, {st} status FROM inventory_items i
                      JOIN categories c ON c.id=i.category_id LEFT JOIN suppliers s ON s.id=i.supplier_id WHERE i.id=?""", (item_id,))
        if not item:
            abort(404)
        total = q1("SELECT COUNT(*) c FROM stock_transactions WHERE item_id=?", (item_id,))["c"]
        pg = paginate(total, page_arg(), per_page())
        txns = q("SELECT * FROM stock_transactions WHERE item_id=? ORDER BY id DESC LIMIT ? OFFSET ?", (item_id, pg["per_page"], pg["offset"]))
        sums = q1("""SELECT COALESCE(SUM(CASE WHEN txn_type='IN' THEN quantity END),0) a,
                     COALESCE(SUM(CASE WHEN txn_type='OUT' THEN quantity END),0) b FROM stock_transactions WHERE item_id=?""", (item_id,))
        fc = next((d for d in forecast_rows() if d["id"] == item_id), None)
        return render_template("item_detail.html", item=item, txns=txns, pg=pg, sums=sums, total_txns=total, fc=fc,
                               loans=outstanding_loans(item_id), warn=int_setting("warn_days", 14))

    @app.route("/inventory/<int:item_id>/edit", methods=["GET", "POST"])
    @login_required
    def item_edit(item_id):
        item = q1("SELECT i.*, s.name supplier FROM inventory_items i LEFT JOIN suppliers s ON s.id=i.supplier_id WHERE i.id=?", (item_id,))
        if not item:
            abort(404)
        if request.method == "POST":
            try:
                d = validate_item(request.form, editing_id=item_id)
                if item["is_returnable"] and not d["is_returnable"] and outstanding_loans(item_id):
                    raise ValidationError({"is_returnable": "This tool still has loans outstanding, so it must stay returnable until they are returned."})
                conn = get_db()
                with tx(conn):
                    code = d["item_code"] or item["item_code"]
                    sid = get_or_create_supplier(conn, d["supplier"])
                    conn.execute(
                        """UPDATE inventory_items SET item_code=?, name=?, category_id=?, description=?, unit=?, min_threshold=?,
                           unit_price=?, supplier_id=?, is_returnable=?, updated_at=datetime('now') WHERE id=?""",
                        (code, d["name"], d["category_id"], d["description"], d["unit"], d["min_threshold"], d["unit_price"], sid,
                         d["is_returnable"], item_id))
                    sync_alert(conn, item_id)  # threshold may have changed
                    audit(conn, "ITEM_UPDATE", f"{code} {d['name']}")
                flash("Item updated.", "success")
                return redirect(url_for("item_detail", item_id=item_id))
            except ValidationError as e:
                flash("Please correct the highlighted fields.", "error")
                return render_template("item_form.html", **item_form_context(request.form, e.errors, item)), 422
        form = dict(item)
        form["category_id"] = str(item["category_id"])
        return render_template("item_form.html", **item_form_context(form, None, item))

    @app.route("/inventory/<int:item_id>/delete", methods=["POST"])
    @login_required
    def item_delete(item_id):
        item = q1("SELECT * FROM inventory_items WHERE id=?", (item_id,))
        if not item:
            abort(404)
        n = q1("SELECT COUNT(*) c FROM stock_transactions WHERE item_id=?", (item_id,))["c"]
        if n and session.get("role") != "admin":
            flash("This item has transaction history. Only an administrator can delete it.", "error")
            return redirect(url_for("item_detail", item_id=item_id))
        if n and request.form.get("with_history") != "yes":
            flash("This item has transaction history. Tick the box to confirm that the history should be deleted too.", "error")
            return redirect(url_for("item_detail", item_id=item_id))
        conn = get_db()
        with tx(conn):
            conn.execute("DELETE FROM stock_transactions WHERE item_id=?", (item_id,))
            conn.execute("DELETE FROM inventory_items WHERE id=?", (item_id,))
            audit(conn, "ITEM_DELETE", f"{item['item_code']} {item['name']} (+{n} transactions)")
        flash(f"Item {item['item_code']} deleted.", "success")
        return redirect(url_for("inventory"))

    # ---- stock movements
    @app.route("/stock")
    @login_required
    def stock_history():
        t = request.args.get("type", "")
        item = request.args.get("item_id", "")
        search = (request.args.get("q") or "").strip()
        try:
            d_from = (request.args.get("date_from") or "").strip()
            d_to = (request.args.get("date_to") or "").strip()
            for dv in (d_from, d_to):
                if dv:
                    datetime.strptime(dv, "%Y-%m-%d")
        except ValueError:
            flash("Enter valid dates for filtering.", "error")
            d_from = d_to = ""
        where, params = ["1=1"], []
        if t in ("IN", "OUT", "LOAN", "RETURN"):
            where.append("t.txn_type=?")
            params.append(t)
        if item.isdigit():
            where.append("t.item_id=?")
            params.append(int(item))
        if d_from:
            where.append("t.txn_date>=?")
            params.append(d_from)
        if d_to:
            where.append("t.txn_date<=?")
            params.append(d_to)
        if search:
            where.append("(t.reference LIKE ? OR i.name LIKE ? OR t.counterparty LIKE ? OR t.responsible LIKE ?)")
            params += [f"%{search}%"] * 4
        base = f"FROM stock_transactions t JOIN inventory_items i ON i.id=t.item_id WHERE {' AND '.join(where)}"
        total = q1(f"SELECT COUNT(*) c {base}", params)["c"]
        pg = paginate(total, page_arg(), per_page())
        rows = q(f"SELECT t.*, i.name item_name, i.item_code, i.unit {base} ORDER BY t.txn_date DESC, t.id DESC LIMIT ? OFFSET ?",
                 params + [pg["per_page"], pg["offset"]])
        return render_template("stock_history.html", rows=rows, pg=pg, items=q("SELECT id,name,item_code FROM inventory_items ORDER BY name"),
                               args={"type": t, "item_id": item, "q": search, "date_from": d_from, "date_to": d_to})

    STOCK_TYPES = {"IN": "Stock in", "OUT": "Stock out", "LOAN": "Lend tool", "RETURN": "Return tool"}

    def stock_form(txn_type):
        label = STOCK_TYPES[txn_type]
        only_ret = "WHERE i.is_returnable=1" if txn_type == "LOAN" else ""
        items = q(f"SELECT i.id,i.name,i.item_code,i.quantity,i.unit,i.unit_price,s.name supplier FROM inventory_items i LEFT JOIN suppliers s ON s.id=i.supplier_id {only_ret} ORDER BY i.name")
        open_loans = outstanding_loans() if txn_type == "RETURN" else []
        borrowers = [r[0] for r in q("SELECT DISTINCT counterparty FROM stock_transactions WHERE txn_type='LOAN' AND counterparty IS NOT NULL ORDER BY 1")]
        me = q1("SELECT full_name FROM users WHERE id=?", (session["user_id"],))["full_name"]
        form, errors = {"txn_date": date.today().isoformat(), "responsible": me}, {}
        if request.args.get("item_id", "").isdigit():
            form["item_id"] = request.args["item_id"]
            if request.args.get("borrower"):
                form["loan"] = f"{request.args['item_id']}::{request.args['borrower']}"
                for l in open_loans:
                    if str(l["item_id"]) == request.args["item_id"] and l["borrower"].lower() == request.args["borrower"].lower():
                        form["quantity"] = fmt_qty(l["held"]).replace(",", "")
        ctx = dict(txn_type=txn_type, label=label, items=items, open_loans=open_loans, borrowers=borrowers)
        if request.method == "POST":
            form = request.form.to_dict()
            if txn_type == "RETURN":
                loan = form.get("loan", "")
                if "::" in loan:
                    form["item_id"], form["counterparty"] = loan.split("::", 1)
            try:
                d = validate_transaction(form, txn_type)
                ref = record_transaction(d["item_id"], txn_type, d["quantity"], d["txn_date"], d["responsible"],
                                         d["counterparty"], d["notes"], d["unit_price"])
                flash(f"{label} recorded as {ref}.", "success")
                return redirect(url_for("loans") if txn_type in ("LOAN", "RETURN") else url_for("stock_history"))
            except ValidationError as e:
                errors = e.errors
                flash(next(iter(errors.values())) if len(errors) == 1 else "Please correct the highlighted fields.", "error")
                return render_template("stock_form.html", form=form, errors=errors, **ctx), 422
        return render_template("stock_form.html", form=form, errors=errors, **ctx)

    @app.route("/stock/in", methods=["GET", "POST"])
    @login_required
    def stock_in():
        return stock_form("IN")

    @app.route("/stock/out", methods=["GET", "POST"])
    @login_required
    def stock_out():
        return stock_form("OUT")

    @app.route("/stock/lend", methods=["GET", "POST"])
    @login_required
    def stock_lend():
        return stock_form("LOAN")

    @app.route("/stock/return", methods=["GET", "POST"])
    @login_required
    def stock_return():
        return stock_form("RETURN")

    @app.route("/loans")
    @login_required
    def loans():
        recent = q("""SELECT t.*, i.name item_name, i.unit FROM stock_transactions t JOIN inventory_items i ON i.id=t.item_id
                      WHERE t.txn_type IN ('LOAN','RETURN') ORDER BY t.id DESC LIMIT 10""")
        return render_template("loans.html", loans=outstanding_loans(), recent=recent,
                               tools=q1("SELECT COUNT(*) c FROM inventory_items WHERE is_returnable=1")["c"])

    # ---- alerts
    @app.route("/alerts")
    @login_required
    def alerts():
        flt = request.args.get("status", "all")
        cat = request.args.get("category", "")
        st = status_sql("i")
        where, params = [f"{st} <> 'OK'"], []
        if flt == "low":
            where.append(f"{st}='LOW'")
        elif flt == "out":
            where.append(f"{st}='OUT'")
        if cat.isdigit():
            where.append("i.category_id=?")
            params.append(int(cat))
        items = q(f"""SELECT i.*, c.name category, s.name supplier, {st} status FROM inventory_items i
                      JOIN categories c ON c.id=i.category_id LEFT JOIN suppliers s ON s.id=i.supplier_id
                      WHERE {' AND '.join(where)} ORDER BY (i.quantity - i.min_threshold), i.name""", params)
        counts = q1(f"""SELECT SUM(CASE WHEN {st}='LOW' THEN 1 ELSE 0 END) low, SUM(CASE WHEN {st}='OUT' THEN 1 ELSE 0 END) out_ FROM inventory_items i""")
        notes = q("""SELECT n.*, i.name item_name FROM system_notifications n LEFT JOIN inventory_items i ON i.id=n.item_id
                     WHERE n.kind IN ('LOW','OUT') ORDER BY n.id DESC LIMIT 15""")
        plan = [d for d in forecast_rows() if d["need"]]
        return render_template("low_stock.html", plan=plan, plan_cost=sum(d["cost"] for d in plan), warn=int_setting("warn_days", 14),
                               target=int_setting("target_cover_days", 30), days=int_setting("forecast_days", 30),
                               items=items, counts=counts, notes=notes, flt=flt, cat=cat,
                               categories=q("SELECT * FROM categories ORDER BY name"))

    @app.route("/alerts/mark-read", methods=["POST"])
    @login_required
    def alerts_mark_read():
        conn = get_db()
        with tx(conn):
            conn.execute("UPDATE system_notifications SET is_read=1")
            audit(conn, "NOTIFICATIONS_READ")
        flash("Notifications marked as read. Items remain listed while stock is low.", "info")
        return redirect(url_for("alerts"))

    # ---- reports
    @app.route("/reports")
    @login_required
    def reports():
        kind = request.args.get("kind", "")
        items = q("SELECT id,name,item_code FROM inventory_items ORDER BY name")
        today = date.today()
        args = {"kind": kind or "inventory", "date_from": request.args.get("date_from") or (today - timedelta(days=29)).isoformat(),
                "date_to": request.args.get("date_to") or today.isoformat(), "item_id": request.args.get("item_id", "")}
        rep, err = None, None
        if kind:
            try:
                rep = build_report(kind, request.args)
                fmt = request.args.get("format")
                fname = f"{kind}_report_{datetime.now():%Y%m%d_%H%M}"
                if fmt == "csv":
                    return Response(report_csv(rep), mimetype="text/csv",
                                    headers={"Content-Disposition": f"attachment; filename={fname}.csv"})
                if fmt == "pdf":
                    return Response(report_pdf(rep), mimetype="application/pdf",
                                    headers={"Content-Disposition": f"attachment; filename={fname}.pdf"})
            except ValidationError as e:
                err = next(iter(e.errors.values()))
                flash(err, "error")
        return render_template("reports.html", rep=rep, args=args, items=items, REPORTS=REPORTS, DATE_REPORTS=DATE_REPORTS,
                               query=request.query_string.decode())

    # ---- settings
    @app.route("/settings", methods=["GET", "POST"])
    @login_required
    def settings():
        if request.method == "POST":
            section = request.form.get("section")
            conn = get_db()
            try:
                if section == "profile":
                    name = (request.form.get("full_name") or "").strip()
                    email = (request.form.get("email") or "").strip()
                    errs = {}
                    if not name:
                        errs["full_name"] = "Full name is required."
                    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                        errs["email"] = "Enter a valid email address."
                    if errs:
                        raise ValidationError(errs)
                    with tx(conn):
                        conn.execute("UPDATE users SET full_name=?, email=? WHERE id=?", (name, email or None, session["user_id"]))
                        audit(conn, "PROFILE_UPDATE")
                    flash("Profile updated.", "success")
                elif section == "password":
                    u = q1("SELECT * FROM users WHERE id=?", (session["user_id"],))
                    cur, new, conf = (request.form.get(k) or "" for k in ("current_password", "new_password", "confirm_password"))
                    errs = {}
                    if not check_password_hash(u["password_hash"], cur):
                        errs["current_password"] = "Current password is incorrect."
                    if len(new) < 8 or not re.search(r"[A-Za-z]", new) or not re.search(r"\d", new):
                        errs["new_password"] = "New password must be at least 8 characters and include a letter and a digit."
                    if new != conf:
                        errs["confirm_password"] = "Passwords do not match."
                    if errs:
                        raise ValidationError(errs)
                    with tx(conn):
                        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(new), u["id"]))
                        audit(conn, "PASSWORD_CHANGE")
                    flash("Password changed.", "success")
                else:
                    if session.get("role") != "admin":
                        abort(403)
                    if section == "store":
                        name = (request.form.get("store_name") or "").strip()
                        if not name:
                            raise ValidationError({"store_name": "Store name is required."})
                        with tx(conn):
                            for k in ("store_name", "store_address", "store_phone", "store_email"):
                                set_setting(conn, k, (request.form.get(k) or "").strip()[:200])
                            audit(conn, "SETTINGS_STORE")
                        flash("Store information saved.", "success")
                    elif section == "alerts":
                        errs = {}
                        dm = parse_number(request.form.get("default_min_threshold"), "Default minimum threshold", errs, "default_min_threshold")
                        basis = request.form.get("alert_basis")
                        if basis not in ("at_or_below", "below"):
                            errs["alert_basis"] = "Choose an alert rule."
                        fvals = {}
                        for k, lab in (("forecast_days", "Usage window"), ("warn_days", "Warning horizon"), ("target_cover_days", "Target cover")):
                            v = (request.form.get(k) or "").strip()
                            if not v.isdigit() or not 1 <= int(v) <= 365:
                                errs[k] = f"{lab} must be a whole number of days from 1 to 365."
                            else:
                                fvals[k] = v
                        if errs:
                            raise ValidationError(errs)
                        with tx(conn):
                            set_setting(conn, "default_min_threshold", f"{dm:g}")
                            set_setting(conn, "alert_basis", basis)
                            for k, v in fvals.items():
                                set_setting(conn, k, v)
                            for r in conn.execute("SELECT id FROM inventory_items").fetchall():
                                sync_alert(conn, r["id"])  # re-evaluate every item under the new rule
                            audit(conn, "SETTINGS_ALERTS", basis)
                        flash("Alert preferences saved and all items re-evaluated.", "success")
                    elif section == "system":
                        errs = {}
                        cur = (request.form.get("currency") or "").strip().upper()
                        if not re.fullmatch(r"[A-Z]{2,5}", cur):
                            errs["currency"] = "Currency code must be 2-5 letters, e.g. NGN."
                        ps = request.form.get("page_size", "")
                        if not ps.isdigit() or not 5 <= int(ps) <= 100:
                            errs["page_size"] = "Rows per page must be between 5 and 100."
                        if errs:
                            raise ValidationError(errs)
                        with tx(conn):
                            set_setting(conn, "currency", cur)
                            set_setting(conn, "page_size", ps)
                            audit(conn, "SETTINGS_SYSTEM")
                        flash("System configuration saved.", "success")
                    elif section == "category_add":
                        name = (request.form.get("category_name") or "").strip()
                        if not name or len(name) > 50:
                            raise ValidationError({"category_name": "Category name is required (max 50 characters)."})
                        if q1("SELECT 1 FROM categories WHERE name=? COLLATE NOCASE", (name,)):
                            raise ValidationError({"category_name": "That category already exists."})
                        with tx(conn):
                            conn.execute("INSERT INTO categories(name) VALUES (?)", (name,))
                            audit(conn, "CATEGORY_ADD", name)
                        flash("Category added.", "success")
                    elif section == "category_delete":
                        cid = request.form.get("category_id", "")
                        if not cid.isdigit():
                            abort(400)
                        if q1("SELECT 1 FROM inventory_items WHERE category_id=?", (int(cid),)):
                            raise ValidationError({"category_id": "Cannot delete a category that still has items."})
                        with tx(conn):
                            conn.execute("DELETE FROM categories WHERE id=?", (int(cid),))
                            audit(conn, "CATEGORY_DELETE", cid)
                        flash("Category deleted.", "success")
                    elif section == "remove_sample":
                        with tx(conn):
                            ids = [r["id"] for r in conn.execute("SELECT id FROM inventory_items WHERE is_sample=1")]
                            for i in ids:
                                conn.execute("DELETE FROM stock_transactions WHERE item_id=?", (i,))
                                conn.execute("DELETE FROM inventory_items WHERE id=?", (i,))
                            conn.execute("DELETE FROM stock_transactions WHERE is_sample=1")
                            conn.execute("DELETE FROM system_notifications WHERE item_id IS NULL OR item_id NOT IN (SELECT id FROM inventory_items)")
                            audit(conn, "SAMPLE_REMOVED", f"{len(ids)} items")
                        flash(f"Removed {len(ids)} sample items and their sample transactions.", "success")
                    else:
                        abort(400)
                return redirect(url_for("settings"))
            except ValidationError as e:
                flash(next(iter(e.errors.values())), "error")
                return render_template("settings.html", **settings_ctx(), errors=e.errors, open_section=section), 422
        return render_template("settings.html", **settings_ctx(), errors={}, open_section=None)

    def settings_ctx():
        s = {r["key"]: r["value"] for r in q("SELECT key,value FROM settings")}
        u = q1("SELECT * FROM users WHERE id=?", (session["user_id"],))
        cats = q("SELECT c.*, (SELECT COUNT(*) FROM inventory_items i WHERE i.category_id=c.id) n FROM categories c ORDER BY name")
        logs = q("""SELECT a.*, u.username FROM audit_logs a LEFT JOIN users u ON u.id=a.user_id ORDER BY a.id DESC LIMIT 15""") \
            if session.get("role") == "admin" else []
        sample = q1("SELECT COUNT(*) c FROM inventory_items WHERE is_sample=1")["c"]
        return dict(s=s, user=u, cats=cats, logs=logs, sample=sample)


app = None
if __name__ == "__main__":
    app = create_app()
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)), debug=False)
