"""
Schema and data access for InvoiceHub.

SQLite, deliberately. The whole AP month for a mid-size contractor is a few
thousand rows; the operational cost of running Postgres for that is not repaid.
Every table below is written so moving to Postgres later is a dialect change,
not a redesign — no SQLite-only types, no implicit rowid dependence.

One rule runs through the schema: a document, an invoice, a statement line and
a QBO bill are four different things, even when they describe the same money.
Collapsing them is what makes AP tools lie. Each keeps its own row and its own
provenance, and the match table is where they are brought together — so when
the three disagree, you can see all three.
"""

import os
import json
import sqlite3
import logging
from datetime import datetime, timezone

import config

log = logging.getLogger(__name__)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_db() -> sqlite3.Connection:
    con = sqlite3.connect(config.DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    return con


SCHEMA = """
-- ── people ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE,
    name          TEXT,
    password_hash TEXT NOT NULL,
    role          TEXT DEFAULT 'clerk',    -- admin | approver | clerk | viewer
    active        INTEGER DEFAULT 1,
    created_at    TEXT,
    last_login    TEXT
);

-- ── configuration, including encrypted API credentials ──────────────────────
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT,
    updated_by TEXT
);

-- ── vendors ─────────────────────────────────────────────────────────────────
-- The routing rules live on the vendor because only the vendor knows how its
-- supplier writes a subject line. Guessing centrally is how documents get
-- misfiled.
CREATE TABLE IF NOT EXISTS vendors (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL UNIQUE,
    display_name        TEXT,
    active              INTEGER DEFAULT 1,

    -- how their post arrives
    invoice_email       TEXT,
    invoice_subject     TEXT,
    statement_email     TEXT,
    statement_subject   TEXT,
    request_email       TEXT,      -- where to ask for a missing document
    cc_emails           TEXT,

    -- commercial terms
    terms_days          INTEGER DEFAULT 30,
    currency            TEXT DEFAULT 'USD',
    account_number      TEXT,
    payment_method      TEXT,      -- ach | check | card | portal
    payment_portal_url  TEXT,
    early_pay_discount  TEXT,      -- free text, e.g. '2/10 net 30'

    -- links into the other systems
    qbo_vendor_id       TEXT,
    qbo_vendor_name     TEXT,
    st_vendor_id        TEXT,
    default_expense_account TEXT,

    -- agent behaviour
    statement_cadence   TEXT DEFAULT 'monthly',  -- monthly | weekly | none
    auto_request_statement INTEGER DEFAULT 1,
    auto_push_bills     INTEGER DEFAULT 0,
    requires_po         INTEGER DEFAULT 0,

    notes               TEXT,
    created_at          TEXT,
    updated_at          TEXT
);
CREATE INDEX IF NOT EXISTS idx_vendor_active ON vendors(active, name);

CREATE TABLE IF NOT EXISTS vendor_contacts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_id  INTEGER NOT NULL REFERENCES vendors(id) ON DELETE CASCADE,
    name       TEXT,
    email      TEXT,
    phone      TEXT,
    role       TEXT,          -- ar_clerk | rep | manager
    is_primary INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_vcontact ON vendor_contacts(vendor_id);

-- ── inbound documents ───────────────────────────────────────────────────────
-- sha256 is unique: the same attachment arriving twice (a resend, a reply-all)
-- is one document, not two. This single constraint removes most duplicate pain.
CREATE TABLE IF NOT EXISTS documents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_id     INTEGER REFERENCES vendors(id) ON DELETE SET NULL,
    kind          TEXT DEFAULT 'unknown',   -- invoice | statement | credit | remittance | unknown
    source        TEXT DEFAULT 'mailbox',   -- mailbox | upload | api | demo
    filename      TEXT,
    path          TEXT,
    mime          TEXT,
    size_bytes    INTEGER,
    sha256        TEXT UNIQUE,
    message_id    TEXT,
    from_email    TEXT,
    subject       TEXT,
    received_at   TEXT,
    stored_at     TEXT,
    parse_status  TEXT DEFAULT 'pending',   -- pending | ok | failed | skipped | needs_review
    parse_error   TEXT,
    parsed_at     TEXT,
    parsed_json   TEXT,
    confidence    TEXT,                     -- high | medium | low
    reviewed_by   TEXT,
    reviewed_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_doc_vendor ON documents(vendor_id, kind, received_at);
CREATE INDEX IF NOT EXISTS idx_doc_status ON documents(parse_status);

-- ── invoices we hold ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS invoices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_id       INTEGER REFERENCES vendors(id) ON DELETE SET NULL,
    doc_id          INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    invoice_number  TEXT,
    invoice_norm    TEXT,          -- normalised key used for matching
    invoice_date    TEXT,
    due_date        TEXT,
    amount          REAL,
    tax             REAL,
    currency        TEXT DEFAULT 'USD',
    po_number       TEXT,
    job_ref         TEXT,          -- ServiceTitan job this belongs to
    description     TEXT,
    period          TEXT,          -- YYYY-MM this invoice belongs to
    status          TEXT DEFAULT 'open',   -- open | pushed | paid | disputed | void
    qbo_bill_id     TEXT,
    qbo_pushed_at   TEXT,
    qbo_push_error  TEXT,
    confidence      TEXT,
    raw_json        TEXT,
    created_at      TEXT,
    updated_at      TEXT,
    UNIQUE(vendor_id, invoice_norm, amount)
);
CREATE INDEX IF NOT EXISTS idx_inv_vendor ON invoices(vendor_id, period);
CREATE INDEX IF NOT EXISTS idx_inv_norm   ON invoices(invoice_norm);
CREATE INDEX IF NOT EXISTS idx_inv_status ON invoices(status);

CREATE TABLE IF NOT EXISTS invoice_lines (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id  INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    line_no     INTEGER,
    description TEXT,
    quantity    REAL,
    unit_price  REAL,
    amount      REAL,
    account     TEXT
);
CREATE INDEX IF NOT EXISTS idx_invline ON invoice_lines(invoice_id);

-- ── statements ──────────────────────────────────────────────────────────────
-- A statement is a document with a header and lines, not a bag of invoices.
-- Keeping the header lets us answer "did they send one this month" without
-- inferring it from whether any lines happened to parse.
CREATE TABLE IF NOT EXISTS statements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_id       INTEGER REFERENCES vendors(id) ON DELETE SET NULL,
    doc_id          INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    statement_date  TEXT,
    period          TEXT,
    opening_balance REAL,
    closing_balance REAL,
    total_due       REAL,
    currency        TEXT DEFAULT 'USD',
    line_count      INTEGER DEFAULT 0,
    confidence      TEXT,
    raw_json        TEXT,
    created_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_stmt ON statements(vendor_id, period);

CREATE TABLE IF NOT EXISTS statement_lines (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    statement_id   INTEGER NOT NULL REFERENCES statements(id) ON DELETE CASCADE,
    vendor_id      INTEGER,
    invoice_number TEXT,
    invoice_norm   TEXT,
    invoice_date   TEXT,
    due_date       TEXT,
    amount         REAL,
    line_type      TEXT DEFAULT 'invoice',  -- invoice | credit | payment | finance_charge
    age_bucket     TEXT,                    -- current | 30 | 60 | 90 | 120+
    period         TEXT
);
CREATE INDEX IF NOT EXISTS idx_stmtline ON statement_lines(statement_id);
CREATE INDEX IF NOT EXISTS idx_stmtline_norm ON statement_lines(vendor_id, invoice_norm);

-- ── the QBO mirror ──────────────────────────────────────────────────────────
-- A local copy of what QBO believes, stamped with when we last looked. Matching
-- against a cached mirror rather than live calls keeps a reconciliation run
-- reproducible: two people running the same period get the same answer.
CREATE TABLE IF NOT EXISTS qbo_bills (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    qbo_id         TEXT UNIQUE,
    vendor_id      INTEGER REFERENCES vendors(id) ON DELETE SET NULL,
    vendor_raw     TEXT,
    qbo_vendor_id  TEXT,
    doc_number     TEXT,
    invoice_norm   TEXT,
    txn_date       TEXT,
    due_date       TEXT,
    total_amount   REAL,
    balance        REAL,
    currency       TEXT DEFAULT 'USD',
    status         TEXT,        -- open | paid | partial | void
    period         TEXT,
    memo           TEXT,
    synced_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_qbobill ON qbo_bills(vendor_id, invoice_norm);
CREATE INDEX IF NOT EXISTS idx_qbobill_status ON qbo_bills(status, balance);

CREATE TABLE IF NOT EXISTS qbo_payments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    qbo_id      TEXT UNIQUE,
    vendor_id   INTEGER REFERENCES vendors(id) ON DELETE SET NULL,
    bill_qbo_id TEXT,
    pay_date    TEXT,
    amount      REAL,
    method      TEXT,
    reference   TEXT,
    synced_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_qbopay ON qbo_payments(vendor_id, pay_date);

-- ── reconciliation ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS match_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    period      TEXT,
    started_at  TEXT,
    finished_at TEXT,
    run_by      TEXT,
    row_count   INTEGER DEFAULT 0,
    matched     INTEGER DEFAULT 0,
    exceptions  INTEGER DEFAULT 0,
    to_pay      REAL DEFAULT 0,
    note        TEXT
);

-- One row per (vendor, invoice number) per period. Every number that any of the
-- three sources knows about appears, including the ones only one source knows —
-- a bill with no invoice behind it is exactly as interesting as a missing bill.
CREATE TABLE IF NOT EXISTS match_rows (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         INTEGER REFERENCES match_runs(id) ON DELETE CASCADE,
    period         TEXT,
    vendor_id      INTEGER,
    vendor_name    TEXT,
    invoice_number TEXT,
    invoice_norm   TEXT,
    invoice_date   TEXT,
    due_date       TEXT,
    amt_invoice    REAL,      -- what the invoice PDF says
    amt_statement  REAL,      -- what their statement says
    amt_qbo        REAL,      -- what QBO has as the open balance
    verdict        TEXT,
    note           TEXT,
    action         TEXT,      -- pay | hold | query | ignore
    resolved       INTEGER DEFAULT 0,
    resolved_by    TEXT,
    resolved_at    TEXT,
    paid_at        TEXT,
    paid_ref       TEXT,
    UNIQUE(period, vendor_id, invoice_norm)
);
CREATE INDEX IF NOT EXISTS idx_matchrow ON match_rows(period, vendor_id, verdict);

-- ── agents ──────────────────────────────────────────────────────────────────
-- An agent is a named, scheduled job with a plain-English description of what
-- it does. Storing the schedule in the database rather than in code means the
-- AP clerk can add "chase Company ABC for a statement on the 2nd" themselves.
CREATE TABLE IF NOT EXISTS agents (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL UNIQUE,
    kind           TEXT NOT NULL,   -- scan_mailbox | request_statement |
                                    -- request_invoice | sync_qbo | push_bills |
                                    -- run_match | chase_exceptions | report
    description    TEXT,
    enabled        INTEGER DEFAULT 1,
    schedule_kind  TEXT DEFAULT 'cron',   -- cron | interval | manual
    cron           TEXT,                  -- '0 7 2 * *'  (min hour dom mon dow)
    interval_mins  INTEGER,
    vendor_id      INTEGER REFERENCES vendors(id) ON DELETE CASCADE,
    params_json    TEXT,
    requires_approval INTEGER DEFAULT 1,  -- outbound email is never automatic
    last_run_at    TEXT,
    last_status    TEXT,
    last_summary   TEXT,
    next_run_at    TEXT,
    run_count      INTEGER DEFAULT 0,
    created_at     TEXT,
    created_by     TEXT
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id    INTEGER REFERENCES agents(id) ON DELETE CASCADE,
    agent_name  TEXT,
    started_at  TEXT,
    finished_at TEXT,
    status      TEXT,      -- running | ok | failed | skipped
    trigger     TEXT,      -- schedule | manual | chained
    summary     TEXT,
    detail      TEXT,
    items       INTEGER DEFAULT 0,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_agentrun ON agent_runs(agent_id, started_at);

-- ── outbound email, always approval-gated ───────────────────────────────────
CREATE TABLE IF NOT EXISTS drafts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT,
    created_by TEXT,        -- an agent name, or a user email
    vendor_id  INTEGER REFERENCES vendors(id) ON DELETE SET NULL,
    kind       TEXT,        -- request_statement | request_invoice | query |
                            -- remittance | dispute
    period     TEXT,
    to_email   TEXT,
    cc_email   TEXT,
    subject    TEXT,
    body       TEXT,
    reason     TEXT,        -- why this was written, in plain words
    status     TEXT DEFAULT 'pending',   -- pending | approved | sent | discarded
    approved_by TEXT,
    sent_at    TEXT,
    sent_by    TEXT,
    error      TEXT
);
CREATE INDEX IF NOT EXISTS idx_draft ON drafts(status, created_at);

-- ── audit ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS activity (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT,
    kind      TEXT,     -- scan | file | parse | match | email | qbo | agent |
                        -- auth | settings | error | note
    actor     TEXT,
    vendor_id INTEGER,
    headline  TEXT,
    detail    TEXT,
    ok        INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_activity ON activity(at);
CREATE INDEX IF NOT EXISTS idx_activity_kind ON activity(kind, at);

-- ── generated reports ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS reports (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    period     TEXT,
    filename   TEXT,
    path       TEXT,
    size_bytes INTEGER,
    vendor_tabs INTEGER,
    created_at TEXT,
    created_by TEXT
);
"""


def init_db():
    con = get_db()
    try:
        con.executescript(SCHEMA)
        con.commit()
        _seed_defaults(con)
        _seed_builtin_agents(con)
        con.commit()
    finally:
        con.close()
    log.info("Database ready at %s", config.DB_PATH)


def _seed_defaults(con):
    now = utcnow()
    for k, v in config.DEFAULTS.items():
        con.execute(
            "INSERT OR IGNORE INTO settings (key,value,updated_at,updated_by) "
            "VALUES (?,?,?,'system')", (k, v, now))

    # An admin so a fresh install is reachable. The password is deliberately
    # obvious and the UI nags until it is changed.
    row = con.execute("SELECT COUNT(*) c FROM users").fetchone()
    if not row["c"]:
        from werkzeug.security import generate_password_hash
        con.execute(
            "INSERT INTO users (email,name,password_hash,role,active,created_at) "
            "VALUES (?,?,?,?,1,?)",
            ("admin@invoicehub.local", "Administrator",
             generate_password_hash("changeme"), "admin", now))
        log.warning("Created the default admin admin@invoicehub.local / changeme "
                    "— change this before exposing the portal.")


BUILTIN_AGENTS = [
    ("Mailbox sweep", "scan_mailbox",
     "Opens the AP mailbox, files every attachment against a vendor, and hands "
     "anything it cannot place to a human.",
     "cron", "*/30 * * * *", None, 0),
    ("Parse the backlog", "parse_pending",
     "Reads whatever is sitting unparsed and extracts the numbers.",
     "cron", "5 * * * *", None, 0),
    ("Sync QuickBooks", "sync_qbo",
     "Refreshes the local mirror of QBO bills and payments so a reconciliation "
     "run is comparing against current balances.",
     "cron", "0 6 * * *", None, 0),
    ("Push approved bills", "push_bills",
     "Creates a QBO bill for every parsed invoice that has been approved and "
     "does not have one yet.",
     "cron", "0 7 * * *", None, 1),
    ("Month-end statement request", "request_statement",
     "On the second of the month, writes to every vendor set to monthly asking "
     "for a statement. Drafts only — a person presses send.",
     "cron", "0 8 2 * *", None, 1),
    ("Chase missing invoices", "request_invoice",
     "Where a statement shows an invoice we never received, drafts a note asking "
     "for a copy.",
     "cron", "0 9 * * 1", None, 1),
    ("Run the match", "run_match",
     "Compares invoices, statements and QBO for the open period.",
     "cron", "30 6 * * *", None, 0),
    ("Month-end report", "report",
     "Builds the reconciliation workbook, one tab per vendor.",
     "cron", "0 10 3 * *", None, 0),
]


def _seed_builtin_agents(con):
    now = utcnow()
    for name, kind, desc, skind, cron, interval, approval in BUILTIN_AGENTS:
        con.execute(
            "INSERT OR IGNORE INTO agents "
            "(name,kind,description,enabled,schedule_kind,cron,interval_mins,"
            " requires_approval,created_at,created_by) "
            "VALUES (?,?,?,0,?,?,?,?,?,'system')",
            (name, kind, desc, skind, cron, interval, approval, now))


# ── small helpers used everywhere ────────────────────────────────────────────

def get_setting(key: str, default: str = "") -> str:
    """Database first, then environment, then the declared default."""
    con = get_db()
    try:
        r = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    finally:
        con.close()
    if r and r["value"] not in (None, ""):
        v = r["value"]
        return config.decrypt(v) if config.is_secret(key) else v
    env = config.env_fallback(key)
    if env:
        return env
    return config.DEFAULTS.get(key, default)


def set_setting(key: str, value: str, who: str = ""):
    stored = config.encrypt(value) if config.is_secret(key) else value
    con = get_db()
    try:
        con.execute(
            "INSERT INTO settings (key,value,updated_at,updated_by) VALUES (?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (key, stored, utcnow(), who))
        con.commit()
    finally:
        con.close()


def all_settings() -> dict:
    con = get_db()
    try:
        rows = con.execute("SELECT key, value FROM settings").fetchall()
    finally:
        con.close()
    out = {}
    for r in rows:
        out[r["key"]] = (config.decrypt(r["value"])
                         if config.is_secret(r["key"]) else r["value"])
    for k, v in config.DEFAULTS.items():
        out.setdefault(k, v)
    return out


def demo_mode() -> bool:
    return str(get_setting("demo_mode", "1")).lower() in ("1", "true", "yes", "on")


def say(kind: str, headline: str, detail: str = "", vendor_id=None,
        ok: bool = True, actor: str = "system"):
    """Write to the activity log. This is the audit trail and the thing that
    makes the portal readable as a colleague's work rather than a black box."""
    con = get_db()
    try:
        con.execute(
            "INSERT INTO activity (at,kind,actor,vendor_id,headline,detail,ok) "
            "VALUES (?,?,?,?,?,?,?)",
            (utcnow(), kind, actor, vendor_id, headline,
             (detail or "")[:6000], 1 if ok else 0))
        con.commit()
    finally:
        con.close()
    (log.info if ok else log.warning)("[%s] %s — %s", kind, headline,
                                      (detail or "")[:200])


def jload(s, default=None):
    try:
        return json.loads(s) if s else (default if default is not None else {})
    except Exception:
        return default if default is not None else {}
