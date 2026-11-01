# User guide

## Signing in
Open the app, enter your username and password, select **Sign in**. After 5 wrong attempts the account is locked for 5 minutes. Use **Sign out** (the arrow button at the top right) when finished. Sessions expire after 2 hours.

## Dashboard
Shows registered items, units in stock, low-stock and out-of-stock counts, total received and issued, a 12-week movement chart, items per category, recent activity and the items that need attention. Rows marked **Sample** are demonstration data.

## Items
- **Items > New item:** name, category, unit, minimum threshold and price are required. Leave Item ID blank to auto-generate (e.g. `SD-005`). Opening stock is recorded as a stock-in.
- Search by name, ID, description or supplier; filter by category or stock status; click column headings to sort.
- Open an item to see its details, totals and full transaction history. **Edit** changes details but not quantity.
- **Delete** asks for confirmation. If the item has history, you must also tick the box to delete that history (administrator only).

## Recording stock
- **Record stock in:** item, quantity, date, responsible person, supplier, optional price and notes.
- **Record stock out:** item, quantity, date, responsible person and recipient/purpose. The form shows the current stock and warns if you exceed it; the server refuses to issue more than is available.
- Every movement receives a reference (`STI-` for in, `STO-` for out) and appears in **Transaction history**, which can be filtered by type, item, dates and text.

## Low-stock alerts
An item is *Low* when its quantity is at or below its minimum, and *Out* when it reaches zero. The **Alerts & reorder** page lists affected items with filters and a **Restock** shortcut. The number beside it in the top navigation counts open notifications; they clear automatically when the item is restocked. (Administrators can switch the rule to "strictly below" in Settings.)

## Reports
Choose a report, set dates where applicable (and an item for the individual item report), then **Generate**. Use **Print**, **PDF** or **CSV**. Reports: general inventory, individual item, stock-in, stock-out, usage, low-stock, valuation. Low-stock and valuation reports describe current stock, so no date range applies.

## Settings
Store information, your profile, password, alert rule and default threshold, currency code, rows per page, categories, activity log and removal of sample data (administrator functions are disabled for storekeepers).

## Limitations
- Quantities in different units are not added together; dashboard "units in stock" is a simple sum for orientation only.
- Back-dated movements are allowed (not future dates). `balance after` records the balance at the time of entry, not a re-sorted historical balance.
- No user-management screen: the two demonstration accounts are created by `init_db.py`.
- Stock-out valuation uses the item's current unit price (no FIFO/average cost).
- Single-store, single-database, local use; no email/SMS delivery of alerts (alerts are shown in the app only).
- Login lockout is held in memory and resets when the server restarts.
- PDF export uses a plain table layout; for a styled copy use Print > Save as PDF.

## Tool loans (hoes, sprayers, wheelbarrows)
Tick **Returnable tool** on the item form for anything that is lent and brought back. Use **Tool loans > Lend a tool** (item, quantity, borrower, date) and **Record a return** (pick the open loan; the quantity fills in with what is out). Lending lowers the store count, returning raises it. You cannot lend more than is in the store or return more than that borrower has. Lending is not counted as use.

## Reorder planner
**Alerts & reorder** shows each item that is low, out, or forecast to run out within the warning horizon (default 14 days), with days of cover, suggested order quantity, estimated cost and supplier. Suggested order = daily use x target cover days + minimum - quantity in store (rounded up). Daily use is the stock issued in the last 30 days divided by 30. It is a simple average, not a prediction of seasons. Administrators change the three day values under **Settings > Alert preferences**. The **Reorder planner** report gives a purchase list by supplier; the **Tool loans** report lists tools currently out.

