"""Automated tests for the Farm Input Inventory Tracker.

Run from the project folder:   python -m pytest tests -v
Every test uses its own temporary database, so the demo database is never touched.
"""
import os
import re
import sqlite3
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as farm  # noqa: E402
import init_db  # noqa: E402

TODAY = date.today().isoformat()


@pytest.fixture()
def env(tmp_path):
    path = str(tmp_path / "test.db")
    init_db.DB_PATH = path
    conn = init_db.connect()
    init_db.create_schema(conn)
    init_db.seed_base(conn)
    conn.commit()
    conn.close()
    application = farm.create_app(path)
    application.config["TESTING"] = True
    return application, path


def token(client, url="/login"):
    html = client.get(url).get_data(as_text=True)
    return re.search(r'name="_csrf" value="([^"]+)"', html).group(1)


def login(client, user="admin", pw="Admin@123"):
    return client.post("/login", data={"username": user, "password": pw, "_csrf": token(client)})


@pytest.fixture()
def client(env):
    c = env[0].test_client()
    login(c)
    return c


def db(env):
    conn = sqlite3.connect(env[1])
    conn.row_factory = sqlite3.Row
    return conn


def post(client, url, data, page=None):
    data = dict(data)
    data["_csrf"] = token(client, page or url)
    return client.post(url, data=data)


def add_item(client, name="Test Maize", cat=1, unit="kg", minimum="10", price="100", opening="", code=""):
    return post(client, "/inventory/new", dict(name=name, category_id=cat, unit=unit, min_threshold=minimum,
                                              unit_price=price, opening_qty=opening, item_code=code, supplier="Test Supplier"))


def stock(client, kind, item_id, qty, **kw):
    d = dict(item_id=item_id, quantity=qty, txn_date=TODAY, responsible="Tester", counterparty="Plot A" if kind == "out" else "Supplier X")
    d.update(kw)
    return post(client, f"/stock/{kind}", d)


def qty_of(env, item_id=1):
    return db(env).execute("SELECT quantity FROM inventory_items WHERE id=?", (item_id,)).fetchone()[0]


def txn_count(env):
    return db(env).execute("SELECT COUNT(*) FROM stock_transactions").fetchone()[0]


# ---------------------------------------------------------------- authentication
def test_login_valid(env):
    c = env[0].test_client()
    r = login(c)
    assert r.status_code == 302 and c.get("/").status_code == 200


def test_login_invalid_password(env):
    c = env[0].test_client()
    r = login(c, pw="wrong")
    assert r.status_code == 200 and b"Incorrect username or password" in r.data
    assert c.get("/").status_code == 302


def test_protected_routes_redirect_when_logged_out(env):
    c = env[0].test_client()
    for url in ["/", "/inventory", "/stock", "/stock/in", "/alerts", "/reports", "/settings", "/inventory/new"]:
        r = c.get(url)
        assert r.status_code == 302 and "/login" in r.headers["Location"], url


def test_logout_ends_session(client):
    r = post(client, "/logout", {}, "/")
    assert r.status_code == 302
    assert client.get("/inventory").status_code == 302


def test_open_redirect_blocked(env):
    c = env[0].test_client()
    r = c.post("/login?next=//evil.example", data={"username": "admin", "password": "Admin@123", "_csrf": token(c)})
    assert r.headers["Location"] == "/"


def test_login_lockout_after_repeated_failures(env):
    c = env[0].test_client()
    for _ in range(5):
        login(c, pw="bad")
    r = login(c)  # correct password but locked
    assert b"Too many failed attempts" in r.data


def test_csrf_required(client):
    r = client.post("/inventory/new", data={"name": "x"})
    assert r.status_code == 400


def test_passwords_are_hashed(env):
    row = db(env).execute("SELECT password_hash FROM users WHERE username='admin'").fetchone()
    assert row[0] != "Admin@123" and row[0].startswith(("scrypt:", "pbkdf2:"))


# ---------------------------------------------------------------- inventory CRUD + validation
def test_add_item_valid_auto_code(client, env):
    r = add_item(client)
    assert r.status_code == 302
    row = db(env).execute("SELECT * FROM inventory_items").fetchone()
    assert row["item_code"] == "SD-001" and row["quantity"] == 0


def test_add_item_with_opening_stock_creates_transaction(client, env):
    add_item(client, opening="50")
    assert qty_of(env) == 50
    t = db(env).execute("SELECT * FROM stock_transactions").fetchone()
    assert t["txn_type"] == "IN" and t["quantity"] == 50 and t["balance_after"] == 50


@pytest.mark.parametrize("field,value", [("name", ""), ("unit", ""), ("min_threshold", "-1"), ("min_threshold", "abc"),
                                         ("unit_price", "-5"), ("category_id", "999"), ("unit_price", "")])
def test_add_item_rejects_invalid_input(client, env, field, value):
    data = dict(name="Valid", category_id=1, unit="kg", min_threshold="5", unit_price="10", opening_qty="", item_code="", supplier="")
    data[field] = value
    r = post(client, "/inventory/new", data)
    assert r.status_code == 422
    assert db(env).execute("SELECT COUNT(*) FROM inventory_items").fetchone()[0] == 0


def test_duplicate_item_code_rejected(client, env):
    add_item(client, code="MAIZE-1")
    r = add_item(client, name="Other", code="maize-1")
    assert r.status_code == 422 and b"already in use" in r.data


def test_edit_item_updates_fields_not_quantity(client, env):
    add_item(client, opening="20")
    r = post(client, "/inventory/1/edit", dict(name="Renamed", category_id=2, unit="bag", min_threshold="3", unit_price="250",
                                              item_code="SD-001", supplier="New Co"), "/inventory/1/edit")
    assert r.status_code == 302
    row = db(env).execute("SELECT * FROM inventory_items WHERE id=1").fetchone()
    assert row["name"] == "Renamed" and row["category_id"] == 2 and row["quantity"] == 20


def test_delete_item_without_history(client, env):
    add_item(client)
    r = post(client, "/inventory/1/delete", {}, "/inventory/1")
    assert r.status_code == 302
    assert db(env).execute("SELECT COUNT(*) FROM inventory_items").fetchone()[0] == 0


def test_delete_item_with_history_needs_explicit_confirmation(client, env):
    add_item(client, opening="10")
    post(client, "/inventory/1/delete", {"with_history": "no"}, "/inventory/1")
    assert db(env).execute("SELECT COUNT(*) FROM inventory_items").fetchone()[0] == 1
    post(client, "/inventory/1/delete", {"with_history": "yes"}, "/inventory/1")
    assert db(env).execute("SELECT COUNT(*) FROM inventory_items").fetchone()[0] == 0
    assert txn_count(env) == 0


def test_storekeeper_cannot_delete_item_with_history(env):
    c = env[0].test_client()
    login(c)
    add_item(c, opening="10")
    c2 = env[0].test_client()
    login(c2, "storekeeper", "Store@123")
    post(c2, "/inventory/1/delete", {"with_history": "yes"}, "/inventory/1")
    assert db(env).execute("SELECT COUNT(*) FROM inventory_items").fetchone()[0] == 1


def test_search_filter_sort(client, env):
    add_item(client, name="Alpha Seed", cat=1, price="50")
    add_item(client, name="Beta Fertilizer", cat=2, price="900")
    assert b"Alpha Seed" in client.get("/inventory?q=alpha").data
    assert b"Beta Fertilizer" not in client.get("/inventory?q=alpha").data
    assert b"Beta Fertilizer" in client.get("/inventory?category=2").data
    assert b"Alpha Seed" not in client.get("/inventory?category=2").data
    html = client.get("/inventory?sort=price&order=desc").get_data(as_text=True)
    assert html.index("Beta Fertilizer") < html.index("Alpha Seed")


def test_sql_injection_in_search_is_inert(client, env):
    add_item(client, name="Safe Item")
    r = client.get("/inventory?q=" + "x' OR '1'='1")
    assert r.status_code == 200 and b"No items match your search" in r.data  # payload treated as literal text
    r = client.get("/inventory?sort=name;DROP TABLE inventory_items")
    assert r.status_code == 200
    assert db(env).execute("SELECT COUNT(*) FROM inventory_items").fetchone()[0] == 1


# ---------------------------------------------------------------- stock movements
def test_stock_in_increases_quantity_and_records_history(client, env):
    add_item(client)
    r = stock(client, "in", 1, "40")
    assert r.status_code == 302 and qty_of(env) == 40
    t = db(env).execute("SELECT * FROM stock_transactions").fetchone()
    assert t["reference"].startswith("STI-") and t["balance_after"] == 40 and t["responsible"] == "Tester"


def test_stock_out_decreases_quantity(client, env):
    add_item(client, opening="40")
    r = stock(client, "out", 1, "15")
    assert r.status_code == 302 and qty_of(env) == 25
    assert db(env).execute("SELECT reference FROM stock_transactions WHERE txn_type='OUT'").fetchone()[0].startswith("STO-")


def test_stock_out_cannot_exceed_available(client, env):
    add_item(client, opening="10")
    before = txn_count(env)
    r = stock(client, "out", 1, "10.5")
    assert r.status_code == 422 and b"Insufficient stock" in r.data
    assert qty_of(env) == 10 and txn_count(env) == before


def test_stock_out_exact_balance_allowed_and_never_negative(client, env):
    add_item(client, opening="10")
    assert stock(client, "out", 1, "10").status_code == 302
    assert qty_of(env) == 0
    assert stock(client, "out", 1, "1").status_code == 422
    assert qty_of(env) == 0


@pytest.mark.parametrize("bad", ["0", "-5", "abc", "", "1e999", "nan"])
def test_invalid_quantities_rejected(client, env, bad):
    add_item(client, opening="10")
    before = txn_count(env)
    assert stock(client, "in", 1, bad).status_code == 422
    assert stock(client, "out", 1, bad).status_code == 422
    assert qty_of(env) == 10 and txn_count(env) == before


def test_future_and_malformed_dates_rejected(client, env):
    add_item(client, opening="10")
    future = (date.today() + timedelta(days=2)).isoformat()
    assert stock(client, "in", 1, "5", txn_date=future).status_code == 422
    assert stock(client, "in", 1, "5", txn_date="31/12/2026").status_code == 422
    assert qty_of(env) == 10


def test_missing_required_fields_rejected(client, env):
    add_item(client, opening="10")
    assert stock(client, "out", 1, "2", responsible="").status_code == 422
    assert stock(client, "out", 1, "2", counterparty="").status_code == 422
    assert stock(client, "out", 999, "2").status_code == 422
    assert qty_of(env) == 10


def test_transaction_is_atomic_when_a_step_fails(client, env, monkeypatch):
    add_item(client, opening="30")
    before_tx = txn_count(env)

    def boom(*a, **k):
        raise RuntimeError("simulated failure after quantity update")
    monkeypatch.setattr(farm, "sync_alert", boom)
    env[0].config["PROPAGATE_EXCEPTIONS"] = False
    r = stock(client, "out", 1, "5")
    assert r.status_code == 500
    assert qty_of(env) == 30, "quantity must roll back"
    assert txn_count(env) == before_tx, "no transaction row may be left behind"


def test_ledger_always_reconciles_with_quantity(client, env):
    add_item(client, opening="100")
    for kind, n in [("out", "30"), ("in", "12.5"), ("out", "7"), ("out", "500"), ("in", "3")]:
        stock(client, kind, 1, n)
    c = db(env)
    ins = c.execute("SELECT COALESCE(SUM(quantity),0) FROM stock_transactions WHERE txn_type='IN'").fetchone()[0]
    outs = c.execute("SELECT COALESCE(SUM(quantity),0) FROM stock_transactions WHERE txn_type='OUT'").fetchone()[0]
    assert qty_of(env) == pytest.approx(ins - outs) == pytest.approx(78.5)
    last = c.execute("SELECT balance_after FROM stock_transactions ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert last == pytest.approx(78.5)


def test_database_rejects_negative_quantity_directly(env):
    conn = db(env)
    conn.execute("INSERT INTO inventory_items(item_code,name,category_id,unit,quantity) VALUES ('X-1','x',1,'kg',5)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE inventory_items SET quantity=-1 WHERE item_code='X-1'")


# ---------------------------------------------------------------- alerts
def test_low_stock_alert_lifecycle(client, env):
    add_item(client, minimum="10", opening="30")
    assert b"Hybrid" not in client.get("/alerts").data
    stock(client, "out", 1, "20")  # 10 left == threshold -> low (at or below)
    page = client.get("/alerts").get_data(as_text=True)
    assert "Test Maize" in page and "Low stock" in page
    assert db(env).execute("SELECT COUNT(*) FROM system_notifications WHERE kind='LOW' AND is_read=0").fetchone()[0] == 1
    stock(client, "out", 1, "10")  # 0 -> out of stock
    assert "Out of stock" in client.get("/alerts").get_data(as_text=True)
    assert db(env).execute("SELECT COUNT(*) FROM system_notifications WHERE kind='OUT' AND is_read=0").fetchone()[0] == 1
    stock(client, "in", 1, "50")  # restocked -> alert clears automatically
    assert "Test Maize" not in client.get("/alerts").get_data(as_text=True).split("Recent notifications")[0].split("<tbody>")[1]
    assert db(env).execute("SELECT COUNT(*) FROM system_notifications WHERE is_read=0").fetchone()[0] == 0


def test_no_duplicate_notifications_for_repeated_low_state(client, env):
    add_item(client, minimum="10", opening="12")
    stock(client, "out", 1, "5")
    stock(client, "out", 1, "1")
    assert db(env).execute("SELECT COUNT(*) FROM system_notifications WHERE kind='LOW'").fetchone()[0] == 1


def test_alert_filters(client, env):
    add_item(client, name="LowOne", minimum="10", opening="5")
    add_item(client, name="EmptyOne", minimum="10", opening="")
    html_out = client.get("/alerts?status=out").get_data(as_text=True)
    html_low = client.get("/alerts?status=low").get_data(as_text=True)
    part = lambda h: h.split("Items at or below minimum</h2>")[1].split("Recent notifications")[0]
    assert "EmptyOne" in part(html_out) and "LowOne" not in part(html_out)
    assert "LowOne" in part(html_low) and "EmptyOne" not in part(html_low)


def test_raising_threshold_triggers_alert_and_rule_change_applies(client, env):
    add_item(client, minimum="5", opening="8")
    assert db(env).execute("SELECT COUNT(*) FROM system_notifications WHERE is_read=0").fetchone()[0] == 0
    post(client, "/inventory/1/edit", dict(name="Test Maize", category_id=1, unit="kg", min_threshold="8", unit_price="100", item_code="SD-001"),
         "/inventory/1/edit")
    assert db(env).execute("SELECT COUNT(*) FROM system_notifications WHERE kind='LOW' AND is_read=0").fetchone()[0] == 1
    post(client, "/settings", dict(section="alerts", alert_basis="below", default_min_threshold="10", forecast_days="30", warn_days="14", target_cover_days="30"), "/settings")
    assert db(env).execute("SELECT COUNT(*) FROM system_notifications WHERE is_read=0").fetchone()[0] == 0  # 8 is not below 8


# ---------------------------------------------------------------- reports
def seed_report_data(client):
    add_item(client, name="Report Item", opening="100", price="10")
    stock(client, "out", 1, "30")
    stock(client, "out", 1, "10")


def test_usage_report_totals(client, env):
    seed_report_data(client)
    csv_text = client.get("/reports?kind=usage&format=csv").get_data(as_text=True)
    import csv as _csv, io as _io
    rows = list(_csv.reader(_io.StringIO(csv_text)))
    data_row = [r for r in rows if r and r[0] == "SD-001"][0]
    assert data_row[3] == "40" and data_row[5] == "2"   # quantity used, number of issues
    assert ["Issue transactions", "2"] in rows


def test_valuation_report_value(client, env):
    seed_report_data(client)
    csv_text = client.get("/reports?kind=valuation&format=csv").get_data(as_text=True)
    assert "NGN 600.00" in csv_text  # 60 kg x 10


def test_date_range_filters_stock_out_report(client, env):
    seed_report_data(client)
    old = (date.today() - timedelta(days=60)).isoformat()
    older = (date.today() - timedelta(days=50)).isoformat()
    html = client.get(f"/reports?kind=stock_out&date_from={old}&date_to={older}").get_data(as_text=True)
    assert "No records for the selected criteria" in html
    html = client.get(f"/reports?kind=stock_out&date_from={old}&date_to={TODAY}").get_data(as_text=True)
    assert "STO-" in html


def test_invalid_date_range_rejected(client, env):
    html = client.get("/reports?kind=usage&date_from=2026-12-31&date_to=2026-01-01").get_data(as_text=True)
    assert "must not be later" in html
    html = client.get("/reports?kind=usage&date_from=garbage").get_data(as_text=True)
    assert "valid dates" in html


def test_individual_item_report_requires_item_and_lists_history(client, env):
    seed_report_data(client)
    assert "Select an item" in client.get("/reports?kind=item").get_data(as_text=True)
    html = client.get("/reports?kind=item&item_id=1").get_data(as_text=True)
    assert html.count("STO-") >= 2 and "STI-" in html and "Generated:" in html


def test_low_stock_report_lists_only_affected(client, env):
    add_item(client, name="Fine Item", minimum="5", opening="50")
    add_item(client, name="Short Item", minimum="50", opening="10")
    csv_text = client.get("/reports?kind=low_stock&format=csv").get_data(as_text=True)
    assert "Short Item" in csv_text and "Fine Item" not in csv_text


def test_pdf_export_is_valid_pdf(client, env):
    seed_report_data(client)
    for kind in ["inventory", "usage", "valuation", "stock_in", "stock_out", "low_stock", "item"]:
        r = client.get(f"/reports?kind={kind}&item_id=1&format=pdf")
        assert r.status_code == 200 and r.data.startswith(b"%PDF") and r.mimetype == "application/pdf", kind


def test_csv_neutralises_formula_injection(client, env):
    add_item(client, name="=cmd|' /C calc'!A0", opening="5")
    csv_text = client.get("/reports?kind=inventory&format=csv").get_data(as_text=True)
    assert "'=cmd" in csv_text and ",=cmd" not in csv_text


def test_html_output_is_escaped(client, env):
    add_item(client, name="<script>alert(1)</script>")
    assert b"<script>alert(1)</script>" not in client.get("/inventory").data


# ---------------------------------------------------------------- settings and roles
def test_password_change_flow(env):
    c = env[0].test_client()
    login(c, "storekeeper", "Store@123")
    r = post(c, "/settings", dict(section="password", current_password="wrong", new_password="NewPass123", confirm_password="NewPass123"), "/settings")
    assert r.status_code == 422
    r = post(c, "/settings", dict(section="password", current_password="Store@123", new_password="short", confirm_password="short"), "/settings")
    assert r.status_code == 422
    r = post(c, "/settings", dict(section="password", current_password="Store@123", new_password="NewPass123", confirm_password="NewPass123"), "/settings")
    assert r.status_code == 302
    c2 = env[0].test_client()
    assert login(c2, "storekeeper", "NewPass123").status_code == 302


def test_storekeeper_cannot_change_store_settings(env):
    c = env[0].test_client()
    login(c, "storekeeper", "Store@123")
    r = post(c, "/settings", dict(section="store", store_name="Hacked"), "/settings")
    assert r.status_code == 403
    assert db(env).execute("SELECT value FROM settings WHERE key='store_name'").fetchone()[0] != "Hacked"


def test_admin_can_update_store_and_system_settings(client, env):
    assert post(client, "/settings", dict(section="store", store_name="My Farm Store", store_address="x", store_phone="", store_email=""), "/settings").status_code == 302
    assert b"My Farm Store" in client.get("/").data
    assert post(client, "/settings", dict(section="system", currency="usd", page_size="3"), "/settings").status_code == 422  # 3 < 5
    assert post(client, "/settings", dict(section="system", currency="usd", page_size="20"), "/settings").status_code == 302
    assert db(env).execute("SELECT value FROM settings WHERE key='currency'").fetchone()[0] == "USD"


def test_pagination(client, env):
    for n in range(14):
        add_item(client, name=f"Bulk {n:02d}")
    page1 = client.get("/inventory").get_data(as_text=True)
    page2 = client.get("/inventory?page=2").get_data(as_text=True)
    assert "Showing 1-10 of 14" in page1 and "Showing 11-14 of 14" in page2
    assert client.get("/inventory?page=999").status_code == 200


# ---------------------------------------------------------------- sample data
def test_sample_data_is_flagged_and_consistent(tmp_path):
    path = str(tmp_path / "demo.db")
    init_db.DB_PATH = path
    conn = init_db.connect()
    init_db.create_schema(conn)
    init_db.seed_base(conn)
    init_db.seed_sample(conn)
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM inventory_items WHERE is_sample=0").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM stock_transactions WHERE is_sample=0").fetchone()[0] == 0
    bad = conn.execute("""SELECT i.item_code FROM inventory_items i WHERE ABS(i.quantity -
        (SELECT COALESCE(SUM(CASE WHEN txn_type IN ('IN','RETURN') THEN quantity ELSE -quantity END),0) FROM stock_transactions t WHERE t.item_id=i.id)) > 0.0001""").fetchall()
    assert bad == []
    assert conn.execute("SELECT COUNT(*) FROM inventory_items WHERE quantity<0").fetchone()[0] == 0


def test_remove_sample_data(env):
    conn = db(env)
    init_db.DB_PATH = env[1]
    init_db.seed_sample(conn)
    conn.commit()
    c = env[0].test_client()
    login(c)
    r = post(c, "/settings", dict(section="remove_sample"), "/settings")
    assert r.status_code == 302
    assert db(env).execute("SELECT COUNT(*) FROM inventory_items").fetchone()[0] == 0
    assert txn_count(env) == 0


# ---------------------------------------------------------------- tool loans
def add_tool(client, name="Hoe", opening="10", minimum="2"):
    return post(client, "/inventory/new", dict(name=name, category_id=4, unit="piece", min_threshold=minimum, unit_price="2500",
                                              opening_qty=opening, supplier="Test Supplier", is_returnable="1"))


def lend(client, item_id, qty, who="Musa", **kw):
    d = dict(item_id=item_id, quantity=qty, txn_date=TODAY, responsible="Tester", counterparty=who)
    d.update(kw)
    return post(client, "/stock/lend", d)


def give_back(client, item_id, qty, who="Musa"):
    return post(client, "/stock/return", dict(loan=f"{item_id}::{who}", quantity=qty, txn_date=TODAY, responsible="Tester"))


def test_lend_reduces_store_and_return_restores_it(client, env):
    add_tool(client)
    r = lend(client, 1, "3")
    assert r.status_code in (200, 302)
    assert qty_of(env) == 7
    assert db(env).execute("SELECT txn_type, balance_after FROM stock_transactions ORDER BY id DESC LIMIT 1").fetchone()[:] == ("LOAN", 7)
    give_back(client, 1, "2")
    assert qty_of(env) == 9
    html = client.get("/loans").get_data(as_text=True)
    assert "Musa" in html and "1 piece" in html


def test_cannot_lend_more_than_in_store(client, env):
    add_tool(client, opening="2")
    r = lend(client, 1, "5")
    assert r.status_code == 422 and qty_of(env) == 2
    assert db(env).execute("SELECT COUNT(*) FROM stock_transactions WHERE txn_type='LOAN'").fetchone()[0] == 0


def test_cannot_return_more_than_borrower_holds(client, env):
    add_tool(client)
    lend(client, 1, "2")
    r = give_back(client, 1, "3")
    assert r.status_code == 422 and qty_of(env) == 8


def test_cannot_return_for_someone_who_borrowed_nothing(client, env):
    add_tool(client)
    lend(client, 1, "2", who="Musa")
    r = give_back(client, 1, "1", who="Ada")
    assert r.status_code == 422 and qty_of(env) == 8


def test_borrower_name_is_case_insensitive(client, env):
    add_tool(client)
    lend(client, 1, "2", who="Musa")
    give_back(client, 1, "2", who="musa")
    assert qty_of(env) == 10
    assert "No tools are on loan" in client.get("/loans").get_data(as_text=True)


def test_loan_rejected_for_non_returnable_item(client, env):
    add_item(client, opening="50")
    r = lend(client, 1, "1")
    assert r.status_code == 422 and qty_of(env) == 50


def test_item_page_shows_who_has_the_tool(client, env):
    add_tool(client)
    lend(client, 1, "4")
    html = client.get("/inventory/1").get_data(as_text=True)
    assert "On loan" in html and "4 piece" in html


def test_returnable_flag_locked_while_tools_are_out(client, env):
    add_tool(client)
    lend(client, 1, "1")
    post(client, "/inventory/1/edit", dict(name="Hoe", category_id=4, unit="piece", min_threshold="2", unit_price="2500", item_code="TL-001"),
         "/inventory/1/edit")
    assert db(env).execute("SELECT is_returnable FROM inventory_items WHERE id=1").fetchone()[0] == 1


def test_loans_report_and_reorder_report_render(client, env):
    add_tool(client)
    lend(client, 1, "2")
    add_item(client, name="Seed", opening="40", minimum="10")
    stock(client, "out", 2, "35")
    assert "Musa" in client.get("/reports?kind=loans&format=csv").get_data(as_text=True)
    assert "Seed" in client.get("/reports?kind=reorder&format=csv").get_data(as_text=True)
    assert client.get("/reports?kind=reorder&format=pdf").data[:4] == b"%PDF"


def test_reorder_suggestion_math(client, env):
    # 90 opening, 85 issued -> 5 left; 85/30 = 2.8333/day
    add_item(client, name="Fert", opening="90", minimum="10")
    stock(client, "out", 1, "85")
    html = client.get("/alerts").get_data(as_text=True)
    # 85 used / 30 days = 2.83/day; need = ceil(2.8333*30 + 10 - 5) = 90
    assert "90 kg" in html


def test_forecast_settings_validation(client, env):
    r = post(client, "/settings", dict(section="alerts", alert_basis="at_or_below", default_min_threshold="10",
                                       forecast_days="0", warn_days="14", target_cover_days="30"), "/settings")
    assert r.status_code == 422


def test_lending_is_not_counted_as_use_in_forecast(client, env):
    add_tool(client, opening="10", minimum="2")
    lend(client, 1, "6")
    html = client.get("/inventory/1").get_data(as_text=True)
    assert "No recent use" in html or "Use per day" not in html  # returnable tools show loans, not a run-out forecast
    row = db(env).execute("SELECT COUNT(*) FROM stock_transactions WHERE txn_type='OUT'").fetchone()[0]
    assert row == 0
