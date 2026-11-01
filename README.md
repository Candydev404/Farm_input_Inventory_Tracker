# Farm Input Inventory Tracker

NS-CSC 229 Integrated Design Project II - Group 30.
A web application for a small farm store to track seeds, fertilizers, agrochemicals, tools and
irrigation equipment: item records, stock movements, low-stock alerts and usage/valuation reports.

**Stack:** Python 3 + Flask, SQLite, HTML/CSS/JavaScript, Lucide icons and Chart.js (both bundled in `static/vendor`, so the app works offline).

## Prerequisites
- Python 3.10 or newer (developed and tested on 3.13)
- pip

## Install and run
```bash
cd farm-tracker
python -m venv venv                 # optional but recommended
source venv/bin/activate            # Windows: venv\Scripts\activate
pip install -r requirements.txt
python init_db.py --reset           # creates farm_inventory.db with demonstration data
python app.py                       # open http://127.0.0.1:5000
```
Start with an empty store instead of demo data: `python init_db.py --reset --empty`.
(Sample records can also be removed later under Settings > Demonstration data.)

## Demonstration logins
| Role | Username | Password |
|------|----------|----------|
| Administrator | `admin` | `Admin@123` |
| Storekeeper | `storekeeper` | `Store@123` |

These are demonstration credentials only. Change them under Settings > Change password.

## Roles
- **Administrator:** everything, including store information, alert rules, system configuration, categories, deleting items that have history, the activity log.
- **Storekeeper:** items, stock in/out, alerts, reports, own profile and password.

## Project layout
```
app.py            Flask application: routes, validation, atomic stock logic, reports
schema.sql        SQLite schema (constraints, foreign keys, indexes)
init_db.py        Database creation + clearly flagged sample data
templates/        Jinja2 pages          static/   CSS, JS, bundled libraries
tests/            70 automated tests    docs/     screenshots and recorded test output
```

## Run the tests
```bash
python -m pytest tests -v
```
Each test uses its own temporary database; the demo database is never modified.

## Key design rules
- Item quantity changes **only** through recorded stock movements. Each movement and its quantity update
  happen in one SQLite transaction (all-or-nothing).
- A stock-out larger than the available quantity is rejected on the server; the database also has `CHECK (quantity >= 0)`.
- Alerts are recomputed inside the same transaction as every stock change.
- All SQL is parameterised; passwords are hashed; forms carry CSRF tokens; output is HTML-escaped.

## Known limitations
See `docs/USER_GUIDE.md`, section "Limitations".

## Third-party components
Flask, Werkzeug, Jinja2 (BSD-3), ReportLab (BSD), Lucide icons (ISC), Chart.js (MIT), SQLite (public domain).
