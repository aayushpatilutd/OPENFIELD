"""
Load the demo dataset.

Writes straight into the tables rather than going through the parser, because
the point is a portal that is already full of interesting history the moment
someone opens it — not a portal that needs a live Claude key to show anything.

Safe to run repeatedly: it clears the demo rows first.
"""

import os
import json
import hashlib
import logging
from datetime import datetime, timedelta, timezone

import db
import config
from core.util import (norm_invoice, due_date_for, period_of, current_period,
                       age_bucket)
from demo import fixtures as F

log = logging.getLogger(__name__)


def _d(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def wipe():
    """Clear everything the seeder owns, leaving users and settings alone."""
    con = db.get_db()
    try:
        for t in ("statement_lines", "statements", "invoice_lines", "invoices",
                  "qbo_payments", "qbo_bills", "match_rows", "match_runs",
                  "documents", "vendor_contacts", "vendors", "drafts",
                  "agent_runs", "activity", "reports"):
            con.execute(f"DELETE FROM {t}")
        con.commit()
    finally:
        con.close()


def seed(actor="demo-seeder", wipe_first=True) -> dict:
    if wipe_first:
        wipe()

    now = db.utcnow()
    period = current_period()
    con = db.get_db()
    vendor_ids = {}
    try:
        # ── vendors ─────────────────────────────────────────────────────────
        for v in F.VENDORS:
            cur = con.execute("""
                INSERT INTO vendors
                  (name, display_name, active, invoice_email, invoice_subject,
                   statement_email, statement_subject, request_email,
                   terms_days, currency, account_number, payment_method,
                   payment_portal_url, early_pay_discount, qbo_vendor_id,
                   qbo_vendor_name, default_expense_account, statement_cadence,
                   auto_request_statement, auto_push_bills, requires_po,
                   notes, created_at, updated_at)
                VALUES (?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'monthly', 1, ?,?,?,?,?)
            """, (v["name"], v.get("display_name"), v.get("invoice_email"),
                  v.get("invoice_subject"), v.get("statement_email"),
                  v.get("statement_subject"), v.get("request_email"),
                  v.get("terms_days", 30), v.get("currency", "USD"),
                  v.get("account_number"), v.get("payment_method"),
                  v.get("payment_portal_url"), v.get("early_pay_discount"),
                  v.get("qbo_vendor_id"), v.get("qbo_vendor_name"),
                  v.get("default_expense_account"),
                  v.get("auto_push_bills", 0), v.get("requires_po", 0),
                  v.get("notes"), now, now))
            vid = cur.lastrowid
            vendor_ids[v["name"]] = vid
            con.execute("""INSERT INTO vendor_contacts
                           (vendor_id, name, email, role, is_primary)
                           VALUES (?,?,?,'ar_clerk',1)""",
                        (vid, "Accounts Receivable",
                         v.get("request_email") or v.get("invoice_email")))

        # ── invoice documents and invoices ──────────────────────────────────
        for vname, number, ago, amount, po, conf, note in F.INVOICES:
            vid = vendor_ids[vname]
            vmeta = next(x for x in F.VENDORS if x["name"] == vname)
            inv_date = _d(ago)
            due = due_date_for(inv_date, vmeta.get("terms_days", 30))

            sha = hashlib.sha256(f"demo-inv-{vname}-{number}".encode()).hexdigest()
            dcur = con.execute("""
                INSERT INTO documents
                  (vendor_id, kind, source, filename, path, mime, size_bytes,
                   sha256, from_email, subject, received_at, stored_at,
                   parse_status, parsed_at, confidence)
                VALUES (?,?, 'demo', ?,?, 'application/pdf', ?, ?, ?, ?, ?, ?,
                        ?, ?, ?)
            """, (vid, "invoice", f"{number}.pdf",
                  os.path.join(config.STORAGE, "_demo", f"{number}.pdf"),
                  120000 + (ago * 37), sha, vmeta.get("invoice_email"),
                  f"Invoice {number}", _iso(ago), _iso(ago),
                  "needs_review" if conf == "low" else "ok", _iso(ago), conf))
            doc_id = dcur.lastrowid

            job_ref = None
            if po:
                hit = F.demo_job_for_po(po)
                job_ref = hit["job_number"] if hit else None

            icur = con.execute("""
                INSERT OR IGNORE INTO invoices
                  (vendor_id, doc_id, invoice_number, invoice_norm, invoice_date,
                   due_date, amount, currency, po_number, job_ref, description,
                   period, status, confidence, raw_json, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (vid, doc_id, number, norm_invoice(number), inv_date,
                  due.isoformat() if due else None, amount,
                  vmeta.get("currency", "USD"), po, job_ref, note,
                  period_of(inv_date), "open", conf,
                  json.dumps({"demo": True, "note": note}), now, now))
            inv_id = icur.lastrowid
            if inv_id and amount:
                # A couple of lines so the per-invoice view is not empty.
                con.execute("""INSERT INTO invoice_lines
                    (invoice_id, line_no, description, quantity, unit_price, amount)
                    VALUES (?,1,?,?,?,?)""",
                    (inv_id, "Materials", 1, round(amount * 0.8, 2),
                     round(amount * 0.8, 2)))
                con.execute("""INSERT INTO invoice_lines
                    (invoice_id, line_no, description, quantity, unit_price, amount)
                    VALUES (?,2,?,?,?,?)""",
                    (inv_id, "Freight and handling", 1, round(amount * 0.2, 2),
                     round(amount * 0.2, 2)))

        # The duplicate: the same invoice number arriving as a second file.
        # A different sha means it files; the invoice UNIQUE constraint means
        # it does not become a second payable. That is the behaviour to show.
        abc = vendor_ids["Company ABC"]
        con.execute("""
            INSERT INTO documents
              (vendor_id, kind, source, filename, path, mime, size_bytes, sha256,
               from_email, subject, received_at, stored_at, parse_status,
               parsed_at, confidence)
            VALUES (?, 'invoice', 'demo', 'ABC-000011_RESEND.pdf', ?,
                    'application/pdf', 98210, ?, ?, ?, ?, ?, 'ok', ?, 'high')
        """, (abc, os.path.join(config.STORAGE, "_demo", "ABC-000011_RESEND.pdf"),
              hashlib.sha256(b"demo-dup-ABC-000011").hexdigest(),
              "invoices@company-abc.example", "RE: Invoice ABC-000011 (copy)",
              _iso(18), _iso(18), _iso(18)))

        # An unmatched document: a supplier nobody has set up yet.
        con.execute("""
            INSERT INTO documents
              (vendor_id, kind, source, filename, path, mime, size_bytes, sha256,
               from_email, subject, received_at, stored_at, parse_status)
            VALUES (NULL, 'invoice', 'demo', 'invoice-90001.pdf', ?,
                    'application/pdf', 73110, ?, ?, ?, ?, ?, 'needs_review')
        """, (os.path.join(config.STORAGE, "_demo", "invoice-90001.pdf"),
              hashlib.sha256(b"demo-unmatched-90001").hexdigest(),
              "billing@company-new.example", "Invoice 90001 attached",
              _iso(2), _iso(2)))

        # ── statements ──────────────────────────────────────────────────────
        for vname, lines in F.STATEMENT_LINES.items():
            vid = vendor_ids[vname]
            vmeta = next(x for x in F.VENDORS if x["name"] == vname)
            total = round(sum(a for _, _, a, _ in lines), 2)

            sha = hashlib.sha256(f"demo-stmt-{vname}-{period}".encode()).hexdigest()
            dcur = con.execute("""
                INSERT INTO documents
                  (vendor_id, kind, source, filename, path, mime, size_bytes,
                   sha256, from_email, subject, received_at, stored_at,
                   parse_status, parsed_at, confidence)
                VALUES (?, 'statement', 'demo', ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        'ok', ?, 'high')
            """, (vid, f"Statement_{period}.pdf",
                  os.path.join(config.STORAGE, "_demo",
                               f"{vname}_Statement_{period}.pdf"),
                  "application/pdf", 210400, sha,
                  vmeta.get("statement_email"),
                  f"Statement of Account — {period}", _iso(4), _iso(4), _iso(4)))
            doc_id = dcur.lastrowid

            scur = con.execute("""
                INSERT INTO statements
                  (vendor_id, doc_id, statement_date, period, opening_balance,
                   closing_balance, total_due, currency, line_count, confidence,
                   raw_json, created_at)
                VALUES (?,?,?,?,?,?,?,?,?, 'high', ?, ?)
            """, (vid, doc_id, _d(4), period, 0.0, total, total,
                  vmeta.get("currency", "USD"), len(lines),
                  json.dumps({"demo": True}), now))
            stmt_id = scur.lastrowid

            for number, ago, amt, ltype in lines:
                due = due_date_for(_d(ago), vmeta.get("terms_days", 30))
                con.execute("""INSERT INTO statement_lines
                    (statement_id, vendor_id, invoice_number, invoice_norm,
                     invoice_date, due_date, amount, line_type, age_bucket, period)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (stmt_id, vid, number or "", norm_invoice(number or ""),
                     _d(ago), due.isoformat() if due else None, amt, ltype,
                     age_bucket(due), period))

        # ── the QuickBooks mirror ───────────────────────────────────────────
        for i, (vname, doc, ago, total, bal, status) in enumerate(F.QBO_BILLS, 1):
            vid = vendor_ids.get(vname)
            vmeta = next((x for x in F.VENDORS if x["name"] == vname), {})
            due = due_date_for(_d(ago), vmeta.get("terms_days", 30))
            con.execute("""
                INSERT OR REPLACE INTO qbo_bills
                  (qbo_id, vendor_id, vendor_raw, qbo_vendor_id, doc_number,
                   invoice_norm, txn_date, due_date, total_amount, balance,
                   currency, status, period, memo, synced_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (str(9000 + i), vid, vmeta.get("qbo_vendor_name") or vname,
                  vmeta.get("qbo_vendor_id"), doc, norm_invoice(doc), _d(ago),
                  due.isoformat() if due else None, total, bal,
                  vmeta.get("currency", "USD"), status, period_of(_d(ago)),
                  "", now))

        for p in F.demo_qbo_payments():
            vraw = (p.get("VendorRef") or {}).get("name", "")
            vid = next((vendor_ids[v["name"]] for v in F.VENDORS
                        if v.get("qbo_vendor_name") == vraw
                        or v["name"] == vraw), None)
            for ln in p["Line"]:
                for tx in ln["LinkedTxn"]:
                    con.execute("""INSERT OR REPLACE INTO qbo_payments
                        (qbo_id, vendor_id, bill_qbo_id, pay_date, amount,
                         method, reference, synced_at)
                        VALUES (?,?,?,?,?,?,?,?)""",
                        (f"{p['Id']}-{tx['TxnId']}", vid, tx["TxnId"],
                         p["TxnDate"], ln["Amount"], p.get("PayType"),
                         p.get("DocNumber"), now))

        # ── some history so the activity feed is not empty ──────────────────
        feed = [
            ("scan", "Swept the AP mailbox (7 day window)",
             "9 new document(s) filed, 2 already on record, "
             "1 could not be placed against a vendor.", 1, 2),
            ("parse", "Read a statement from Company ABC",
             "9 line(s), 18,700.65 in total.", 1, 4),
            ("parse", "Read invoice YZA-000901 from Company YZA",
             "1150.00 dated " + _d(24) +
             "  — low confidence, worth a glance", 0, 5),
            ("qbo", "Refreshed the QuickBooks mirror",
             "26 bill(s) and 2 payment link(s).", 1, 1),
            ("match", f"Reconciled {period}",
             "Ready for review.", 1, 1),
            ("email", "Sent “Statement of account request” to ar@company-mno.example",
             "Approved and sent by demo@invoicehub.local.", 1, 3),
        ]
        for kind, head, detail, ok, ago in feed:
            con.execute("""INSERT INTO activity
                (at, kind, actor, headline, detail, ok)
                VALUES (?,?,'demo',?,?,?)""",
                (_iso(ago), kind, head, detail, ok))

        # ── a couple of drafts waiting for approval ─────────────────────────
        mno = vendor_ids["Company MNO"]
        con.execute("""INSERT INTO drafts
            (created_at, created_by, vendor_id, kind, period, to_email, subject,
             body, reason, status)
            VALUES (?, 'agent:Month-end statement request', ?, 'request_statement',
                    ?, 'ar@company-mno.example', ?, ?, ?, 'pending')""",
            (_iso(1), mno, period,
             f"Statement of account request — {period}",
             "Hello,\n\nCould you please send us a statement of account for "
             f"{period}?\n\nWe are reconciling our payables for the period and "
             "want to make sure nothing of yours is sitting unpaid on our side. "
             "Our account number with you is ACC-0005.\n\n"
             "Thank you,\nYour Company — Accounts Payable\n",
             f"No {period} statement from Company MNO yet, and "
             "they are set to monthly."))

        de = vendor_ids["Company DEF"]
        con.execute("""INSERT INTO drafts
            (created_at, created_by, vendor_id, kind, period, to_email, subject,
             body, reason, status)
            VALUES (?, 'agent:Chase exceptions', ?, 'query', ?,
                    'ar.team@company-def.example', ?, ?, ?, 'pending')""",
            (_iso(1), de, period,
             f"Query on {period} statement — amounts do not agree",
             "Hello,\n\nReconciling your statement, these lines do not agree "
             "with our records:\n\n  · DEF-000102: your statement shows "
             "8,975.25, our records show 8,750.25\n\nCould you check these and "
             "let us know which is right? We are holding payment on these "
             "lines only — the rest of the statement is being paid as normal.\n\n"
             "Thank you,\nYour Company — Accounts Payable\n",
             "1 amount mismatch with Company DEF on the "
             f"{period} statement."))

        con.commit()
    finally:
        con.close()

    # Run the reconciliation so the portal opens on a finished month.
    from core import matching
    res = matching.run_match(period=period, actor=actor)

    db.say("note", "Loaded the demo dataset",
           f"{len(F.VENDORS)} vendors, {len(F.INVOICES)} invoices, "
           f"{len(F.STATEMENT_LINES)} statements, {len(F.QBO_BILLS)} QuickBooks "
           f"bills. Reconciliation for {period}: {res.get('matched')} matched, "
           f"{res.get('exceptions')} exceptions.", actor=actor)

    return {"ok": True, "period": period,
            "vendors": len(F.VENDORS), "invoices": len(F.INVOICES),
            "statements": len(F.STATEMENT_LINES), "qbo_bills": len(F.QBO_BILLS),
            "match": res}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    db.init_db()
    print(json.dumps(seed(), indent=2, default=str))
