"""Create the SQLite database and (optionally) load clearly-flagged sample data.

Usage:
    python init_db.py            # schema + default categories/settings + demo users + sample data
    python init_db.py --empty    # schema + categories/settings + demo admin only (no sample data)
    python init_db.py --reset    # delete the existing database first
"""
import os
import random
import sqlite3
import sys
from datetime import date, timedelta

from werkzeug.security import generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("FARM_DB", os.path.join(BASE, "farm_inventory.db"))

CATEGORIES = ["Seeds", "Fertilizers", "Pesticides", "Herbicides",
              "Farming tools", "Irrigation equipment", "Other farm supplies"]

DEFAULT_SETTINGS = {
    "store_name": "Green Valley Farm Store",
    "store_address": "Sample address - update in Settings",
    "store_phone": "",
    "store_email": "",
    "currency": "NGN",
    "default_min_threshold": "10",
    "alert_basis": "at_or_below",
    "page_size": "10",
    "forecast_days": "30",
    "warn_days": "14",
    "target_cover_days": "30",
}

SUPPLIERS = [
    ("AgroPlus Seeds Ltd", "0800-000-0001", "Sample supplier (synthetic)"),
    ("GreenGrow Fertilizer Co", "0800-000-0002", "Sample supplier (synthetic)"),
    ("CropShield Chemicals", "0800-000-0003", "Sample supplier (synthetic)"),
    ("FarmTools Depot", "0800-000-0004", "Sample supplier (synthetic)"),
    ("AquaFlow Irrigation", "0800-000-0005", "Sample supplier (synthetic)"),
]

# code, name, category, description, qty(initial), unit, min, price, supplier
ITEMS = [
    ("SD-001", "Hybrid Maize Seed", "Seeds", "High-yield hybrid maize, 25 kg bags", 0, "kg", 100, 1800, "AgroPlus Seeds Ltd"),
    ("SD-002", "Tomato Seed (Roma)", "Seeds", "Roma tomato seed packets", 0, "packet", 30, 1200, "AgroPlus Seeds Ltd"),
    ("SD-003", "Rice Seed (FARO 44)", "Seeds", "Certified lowland rice seed", 0, "kg", 80, 950, "AgroPlus Seeds Ltd"),
    ("SD-004", "Cowpea Seed", "Seeds", "Improved cowpea variety", 0, "kg", 40, 1100, "AgroPlus Seeds Ltd"),
    ("FT-001", "NPK 15-15-15 Fertilizer", "Fertilizers", "50 kg bags", 0, "bag", 20, 24500, "GreenGrow Fertilizer Co"),
    ("FT-002", "Urea 46% N", "Fertilizers", "50 kg bags", 0, "bag", 20, 22000, "GreenGrow Fertilizer Co"),
    ("FT-003", "Organic Compost", "Fertilizers", "25 kg bags", 0, "bag", 15, 6500, "GreenGrow Fertilizer Co"),
    ("PS-001", "Cypermethrin Insecticide", "Pesticides", "1 litre bottles", 0, "litre", 15, 4200, "CropShield Chemicals"),
    ("PS-002", "Mancozeb Fungicide", "Pesticides", "1 kg packs", 0, "kg", 10, 3800, "CropShield Chemicals"),
    ("HB-001", "Glyphosate Herbicide", "Herbicides", "1 litre bottles", 0, "litre", 20, 3500, "CropShield Chemicals"),
    ("TL-001", "Hand Hoe", "Farming tools", "Steel head, wooden handle", 0, "piece", 10, 2500, "FarmTools Depot"),
    ("TL-002", "Cutlass (Machete)", "Farming tools", "18-inch blade", 0, "piece", 10, 1800, "FarmTools Depot"),
    ("TL-003", "Knapsack Sprayer 16L", "Farming tools", "Manual knapsack sprayer", 0, "piece", 5, 18500, "FarmTools Depot"),
    ("TL-004", "Wheelbarrow", "Farming tools", "Heavy duty single wheel", 0, "piece", 3, 32000, "FarmTools Depot"),
    ("IR-001", "Drip Irrigation Tape", "Irrigation equipment", "100 m rolls", 0, "roll", 6, 14000, "AquaFlow Irrigation"),
    ("IR-002", "PVC Pipe 1 inch", "Irrigation equipment", "6 m lengths", 0, "piece", 20, 2800, "AquaFlow Irrigation"),
    ("OT-001", "Polythene Nursery Bags", "Other farm supplies", "Pack of 100", 0, "pack", 15, 1500, "FarmTools Depot"),
    ("OT-002", "Protective Gloves", "Other farm supplies", "Pair, heavy duty", 0, "pair", 15, 1200, "FarmTools Depot"),
]

# Target final quantities so the demo shows normal, low and out-of-stock items.
# (code -> quantity received in total, quantity issued in total)
PLAN = {
    "SD-001": (600, 380), "SD-002": (120, 70), "SD-003": (300, 245), "SD-004": (150, 60),
    "FT-001": (150, 95), "FT-002": (120, 108), "FT-003": (80, 40),
    "PS-001": (60, 42), "PS-002": (40, 40), "HB-001": (90, 55),
    "TL-001": (60, 8), "TL-002": (50, 6), "TL-003": (20, 2), "TL-004": (10, 0),
    "IR-001": (24, 15), "IR-002": (100, 66), "OT-001": (80, 40), "OT-002": (70, 50),
}
RETURNABLE = {"TL-001", "TL-002", "TL-003", "TL-004"}   # tools are lent and returned, not consumed
RECIPIENTS = ["Plot A crew", "Plot B crew", "Nursery unit", "Field team 1", "Field team 2", "Walk-in customer (sample)"]
STAFF = ["Demo Storekeeper"]


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_schema(conn):
    with open(os.path.join(BASE, "schema.sql"), encoding="utf-8") as f:
        conn.executescript(f.read())


def seed_base(conn):
    for c in CATEGORIES:
        conn.execute("INSERT OR IGNORE INTO categories(name) VALUES (?)", (c,))
    for k, v in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?,?)", (k, v))
    conn.execute(
        "INSERT OR IGNORE INTO users(username, full_name, email, password_hash, role) VALUES (?,?,?,?,?)",
        ("admin", "System Administrator", "admin@example.com",
         generate_password_hash("Admin@123"), "admin"))
    conn.execute(
        "INSERT OR IGNORE INTO users(username, full_name, email, password_hash, role) VALUES (?,?,?,?,?)",
        ("storekeeper", "Demo Storekeeper", "store@example.com",
         generate_password_hash("Store@123"), "storekeeper"))


def seed_sample(conn):
    """Insert sample items and a realistic movement history. Every row is flagged is_sample=1.
    Quantities are produced ONLY by recording transactions, never by direct assignment."""
    random.seed(30)
    for name, phone, addr in SUPPLIERS:
        conn.execute("INSERT OR IGNORE INTO suppliers(name, phone, address) VALUES (?,?,?)", (name, phone, addr))
    cat_id = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM categories")}
    sup_id = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM suppliers")}
    today = date.today()
    start = today - timedelta(days=89)
    admin_id = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]

    counter = 0
    events = []  # (date, code, type, qty)
    for code, name, cat, desc, _q, unit, mn, price, sup in ITEMS:
        conn.execute(
            """INSERT INTO inventory_items(item_code,name,category_id,description,quantity,unit,
               min_threshold,unit_price,supplier_id,is_returnable,is_sample,created_at,updated_at)
               VALUES (?,?,?,?,0,?,?,?,?,?,1,?,?)""",
            (code, name, cat_id[cat], desc, unit, mn, price, sup_id[sup], 1 if code in RETURNABLE else 0,
             start.isoformat() + " 08:00:00", start.isoformat() + " 08:00:00"))
        rec, iss = PLAN[code]
        # receipts: 3 deliveries spread over the period
        parts = [0.5, 0.3, 0.2]
        for i, p in enumerate(parts):
            q = round(rec * p) if i < 2 else rec - round(rec * 0.5) - round(rec * 0.3)
            d = start + timedelta(days=[0, 25, 55][i] + random.randint(0, 4))
            events.append((d, code, "IN", q))
        # issues: 6 spread issues, always later than first delivery
        remaining = iss
        for i in range(6):
            q = remaining if i == 5 else max(1, round(iss / 6 * random.uniform(0.6, 1.3)))
            q = min(q, remaining)
            remaining -= q
            if q > 0:
                d = start + timedelta(days=random.randint(6, 89))
                events.append((d, code, "OUT", q))
    events = [e if len(e) == 5 else e + (None,) for e in events]
    borrowers = [r for r in RECIPIENTS if "customer" not in r]
    for code in sorted(RETURNABLE):
        top = {"TL-001": 6, "TL-002": 5, "TL-003": 3, "TL-004": 2}[code]
        for _ in range(4):
            who = random.choice(borrowers)
            n = random.randint(1, top)
            day = random.randint(8, 70)
            events.append((start + timedelta(days=day), code, "LOAN", n, who))
            events.append((start + timedelta(days=day + random.randint(2, 12)), code, "RETURN", n, who))
    # a few loans that are still out
    events.append((start + timedelta(days=86), "TL-001", "LOAN", 5, "Field team 1"))
    events.append((start + timedelta(days=80), "TL-003", "LOAN", 2, "Nursery unit"))
    events.append((start + timedelta(days=78), "TL-004", "LOAN", 1, "Plot B crew"))
    rank = {"IN": 0, "LOAN": 1, "RETURN": 2, "OUT": 3}
    events.sort(key=lambda e: (e[0], rank[e[2]]))
    qty, out_by = {}, {}
    price = {i[0]: i[7] for i in ITEMS}
    sup_of = {i[0]: i[8] for i in ITEMS}
    prefix = {"IN": "STI", "OUT": "STO", "LOAN": "LON", "RETURN": "RET"}
    for d, code, t, q, who in events:
        row = conn.execute("SELECT id FROM inventory_items WHERE item_code=?", (code,)).fetchone()
        cur = qty.get(code, 0)
        if t in ("OUT", "LOAN"):
            q = min(q, cur)
            if q <= 0:
                continue
            new = cur - q
            if t == "LOAN":
                out_by[(code, who)] = out_by.get((code, who), 0) + q
        elif t == "RETURN":
            q = min(q, out_by.get((code, who), 0))
            if q <= 0:
                continue
            out_by[(code, who)] -= q
            new = cur + q
        else:
            new = cur + q
        qty[code] = new
        counter += 1
        ref = f"{prefix[t]}-{d.strftime('%Y%m%d')}-{counter:04d}"
        party = sup_of[code] if t == "IN" else (who if who else random.choice(RECIPIENTS))
        conn.execute(
            """INSERT INTO stock_transactions(reference,item_id,txn_type,quantity,balance_after,unit_price,
               txn_date,responsible,counterparty,notes,created_by,created_at,is_sample)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)""",
            (ref, row["id"], t, q, new, price[code], d.isoformat(), STAFF[0], party,
             "Sample data", admin_id, d.isoformat() + " 10:00:00"))
    for code, q in qty.items():
        conn.execute("UPDATE inventory_items SET quantity=?, updated_at=datetime('now') WHERE item_code=?", (q, code))
    # generate alerts for items that ended low / out
    for r in conn.execute("SELECT id, name, quantity, min_threshold FROM inventory_items"):
        if r["quantity"] <= 0:
            conn.execute("INSERT INTO system_notifications(item_id,kind,message) VALUES (?,?,?)",
                         (r["id"], "OUT", f"{r['name']} is out of stock."))
        elif r["quantity"] <= r["min_threshold"]:
            conn.execute("INSERT INTO system_notifications(item_id,kind,message) VALUES (?,?,?)",
                         (r["id"], "LOW", f"{r['name']} is low on stock ({r['quantity']:g} left, minimum {r['min_threshold']:g})."))


def main():
    args = sys.argv[1:]
    if "--reset" in args and os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    conn = connect()
    create_schema(conn)
    seed_base(conn)
    has_items = conn.execute("SELECT COUNT(*) c FROM inventory_items").fetchone()["c"]
    if "--empty" not in args and not has_items:
        seed_sample(conn)
    conn.commit()
    conn.close()
    print(f"Database ready at {DB_PATH}")
    print("Demo logins -> admin / Admin@123   |   storekeeper / Store@123")


if __name__ == "__main__":
    main()
