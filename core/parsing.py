"""
Reading what a filed document says.

An invoice becomes one `invoices` row. A statement becomes a `statements`
header plus many `statement_lines`. They are stored separately on purpose: the
statement line is the vendor's claim, the invoice row is the document we hold,
and reconciliation is precisely the business of noticing when those differ.

Nothing here writes to QuickBooks. Parsing and pushing are separate steps so a
bad read never becomes an accounting entry without someone seeing it first.
"""

import json
import logging

import db
from core.util import (num, iso, norm_invoice, period_of, due_date_for,
                       parse_date, match_vendor_name, age_bucket)
from integrations import claude_api, servicetitan

log = logging.getLogger(__name__)


def _mark(doc_id, status, error=None, parsed=None, confidence=None):
    con = db.get_db()
    try:
        con.execute("""UPDATE documents SET parse_status=?, parse_error=?,
                       parsed_at=?, parsed_json=?, confidence=? WHERE id=?""",
                    (status, (error or "")[:2000], db.utcnow(),
                     json.dumps(parsed) if parsed is not None else None,
                     confidence, doc_id))
        con.commit()
    finally:
        con.close()


def parse_document(doc_id: int, actor="system") -> dict:
    """Read one document and store what it says."""
    con = db.get_db()
    try:
        row = con.execute("""SELECT d.*, v.name vendor_name, v.terms_days,
                                    v.currency vendor_currency
                             FROM documents d
                             LEFT JOIN vendors v ON v.id = d.vendor_id
                             WHERE d.id=?""", (doc_id,)).fetchone()
    finally:
        con.close()
    if not row:
        return {"ok": False, "error": "No such document"}
    doc = dict(row)

    if not doc.get("vendor_id"):
        _mark(doc_id, "needs_review",
              "Not filed against a vendor yet — assign it first.")
        return {"ok": False, "error": "Document is not assigned to a vendor"}

    blocks, err = claude_api.file_blocks(doc["path"], doc["filename"])
    if err:
        _mark(doc_id, "failed", err)
        db.say("parse", f"Could not read “{doc['filename']}”", err,
               vendor_id=doc["vendor_id"], ok=False, actor=actor)
        return {"ok": False, "error": err}

    kind = doc.get("kind") or "unknown"
    system = (claude_api.STATEMENT_SYSTEM if kind == "statement"
              else claude_api.INVOICE_SYSTEM)

    client = claude_api.get_client()
    text, err = client.call(blocks, system,
                            max_tokens=16000 if kind == "statement" else 6000)
    if err:
        _mark(doc_id, "failed", err)
        db.say("parse", f"Could not read “{doc['filename']}”", err,
               vendor_id=doc["vendor_id"], ok=False, actor=actor)
        return {"ok": False, "error": err}

    data = claude_api.extract_json(text)
    if not data:
        _mark(doc_id, "failed", "The reply was not usable JSON")
        return {"ok": False, "error": "Could not understand the parser's reply"}

    # The classifier guessed from a subject line; the parser has actually read
    # the page. When they disagree, the parser wins and the document is refiled.
    actual = (data.get("doc_type") or kind).lower()
    if actual in ("invoice", "statement", "credit", "remittance") and actual != kind:
        con = db.get_db()
        try:
            con.execute("UPDATE documents SET kind=? WHERE id=?", (actual, doc_id))
            con.commit()
        finally:
            con.close()
        db.say("parse", f"“{doc['filename']}” is a {actual}, not a {kind}",
               "Refiled on what the document actually says.",
               vendor_id=doc["vendor_id"], actor=actor)
        kind = actual
        # A statement read with the invoice prompt has no usable lines.
        if actual == "statement" and "lines" not in data:
            text, err = client.call(blocks, claude_api.STATEMENT_SYSTEM,
                                    max_tokens=16000)
            data = claude_api.extract_json(text) or data

    conf = (data.get("confidence") or "medium").lower()
    try:
        if kind == "statement":
            result = _store_statement(doc, data, conf, actor)
        else:
            result = _store_invoice(doc, data, conf, actor)
    except Exception as e:
        log.exception("Storing the parse of document %s failed", doc_id)
        _mark(doc_id, "failed", str(e), parsed=data, confidence=conf)
        return {"ok": False, "error": str(e)}

    status = "needs_review" if conf == "low" else "ok"
    _mark(doc_id, status, None, parsed=data, confidence=conf)
    result.update(ok=True, confidence=conf, status=status)
    return result


def _store_invoice(doc, data, conf, actor):
    vendor_id = doc["vendor_id"]
    number = str(data.get("invoice_number") or "").strip()
    total  = num(data.get("total"))
    if total is None:
        total = num(data.get("subtotal"))

    # A credit note is a negative invoice. Storing it as a positive and
    # remembering to subtract it later is how credits get paid twice.
    if (data.get("doc_type") or "").lower() == "credit" and total and total > 0:
        total = -total

    inv_date = iso(data.get("invoice_date"))
    due = iso(data.get("due_date")) or iso(
        due_date_for(inv_date, doc.get("terms_days") or 30))
    po = str(data.get("po_number") or "").strip() or None

    # Tie it to a job if ServiceTitan knows the PO.
    job_ref = None
    if po:
        try:
            st = servicetitan.get_client()
            if st.configured():
                hit = st.job_for_po(po)
                if hit:
                    job_ref = f"{hit.get('job_number')}"
        except Exception as e:
            log.info("ST lookup failed for PO %s: %s", po, e)

    now = db.utcnow()
    con = db.get_db()
    try:
        cur = con.execute("""
            INSERT INTO invoices
              (vendor_id, doc_id, invoice_number, invoice_norm, invoice_date,
               due_date, amount, tax, currency, po_number, job_ref, description,
               period, status, confidence, raw_json, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'open',?,?,?,?)
            ON CONFLICT(vendor_id, invoice_norm, amount) DO UPDATE SET
              doc_id=excluded.doc_id, invoice_date=excluded.invoice_date,
              due_date=excluded.due_date, po_number=excluded.po_number,
              job_ref=COALESCE(excluded.job_ref, invoices.job_ref),
              confidence=excluded.confidence, raw_json=excluded.raw_json,
              updated_at=excluded.updated_at
        """, (vendor_id, doc["id"], number, norm_invoice(number), inv_date, due,
              total, num(data.get("tax")),
              data.get("currency") or doc.get("vendor_currency") or "USD",
              po, job_ref, (data.get("notes") or "")[:500],
              period_of(inv_date), conf, json.dumps(data), now, now))
        inv_id = cur.lastrowid
        if not inv_id:
            got = con.execute("""SELECT id FROM invoices
                                 WHERE vendor_id=? AND invoice_norm=? AND amount=?""",
                              (vendor_id, norm_invoice(number), total)).fetchone()
            inv_id = got["id"] if got else None

        if inv_id:
            con.execute("DELETE FROM invoice_lines WHERE invoice_id=?", (inv_id,))
            for i, ln in enumerate(data.get("lines") or [], 1):
                con.execute("""INSERT INTO invoice_lines
                               (invoice_id, line_no, description, quantity,
                                unit_price, amount)
                               VALUES (?,?,?,?,?,?)""",
                            (inv_id, i, (ln.get("description") or "")[:500],
                             num(ln.get("quantity")), num(ln.get("unit_price")),
                             num(ln.get("amount"))))
        con.commit()
    finally:
        con.close()

    db.say("parse", f"Read invoice {number or '(no number)'} "
                    f"from {doc.get('vendor_name')}",
           f"{total if total is not None else '—'} dated {inv_date or 'unknown'}"
           + (f", PO {po}" if po else "")
           + (f", job {job_ref}" if job_ref else "")
           + ("  — low confidence, worth a glance" if conf == "low" else ""),
           vendor_id=vendor_id, ok=(conf != "low"), actor=actor)
    return {"kind": "invoice", "invoice_id": inv_id,
            "invoice_number": number, "amount": total}


def _store_statement(doc, data, conf, actor):
    vendor_id = doc["vendor_id"]
    s_date = iso(data.get("statement_date"))
    period = (data.get("period") or period_of(s_date)
              or period_of(doc.get("received_at")) or "")
    lines = data.get("lines") or []

    con = db.get_db()
    try:
        # Re-parsing a statement replaces it rather than stacking a second copy.
        old = con.execute("SELECT id FROM statements WHERE doc_id=?",
                          (doc["id"],)).fetchone()
        if old:
            con.execute("DELETE FROM statements WHERE id=?", (old["id"],))

        cur = con.execute("""
            INSERT INTO statements
              (vendor_id, doc_id, statement_date, period, opening_balance,
               closing_balance, total_due, currency, line_count, confidence,
               raw_json, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (vendor_id, doc["id"], s_date, period,
              num(data.get("opening_balance")), num(data.get("closing_balance")),
              num(data.get("total_due")), data.get("currency") or "USD",
              len(lines), conf, json.dumps(data), db.utcnow()))
        stmt_id = cur.lastrowid

        kept = 0
        for ln in lines:
            amt = num(ln.get("amount"))
            if amt is None:
                continue
            ltype = (ln.get("type") or "invoice").lower()
            # A payment or credit must be negative however the vendor printed
            # it, or the statement total will never tie out.
            if ltype in ("credit", "payment") and amt > 0:
                amt = -amt
            number = str(ln.get("invoice_number") or "").strip()
            l_due = iso(ln.get("due_date"))
            con.execute("""INSERT INTO statement_lines
                           (statement_id, vendor_id, invoice_number, invoice_norm,
                            invoice_date, due_date, amount, line_type, age_bucket,
                            period)
                           VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (stmt_id, vendor_id, number, norm_invoice(number),
                         iso(ln.get("invoice_date")), l_due, amt, ltype,
                         ln.get("age_bucket") or age_bucket(l_due), period))
            kept += 1
        con.execute("UPDATE statements SET line_count=? WHERE id=?", (kept, stmt_id))
        con.commit()
    finally:
        con.close()

    # Does the sum of the lines equal the total the statement claims? When it
    # does not, lines were probably missed, and saying so here is far cheaper
    # than discovering it during a payment run.
    claimed = num(data.get("total_due"))
    summed = round(sum(num(l.get("amount")) or 0 for l in lines), 2)
    warn = ""
    if claimed is not None and abs(claimed - summed) > 0.02:
        warn = (f"  Their total says {claimed:,.2f} but the lines add to "
                f"{summed:,.2f} — a difference of {claimed - summed:,.2f}. "
                "Some lines may not have been read.")

    db.say("parse", f"Read a statement from {doc.get('vendor_name')} "
                    f"({period or 'period unknown'})",
           f"{kept} line(s), {summed:,.2f} in total.{warn}"
           + ("  Low confidence — worth a glance." if conf == "low" else ""),
           vendor_id=vendor_id, ok=(not warn and conf != "low"), actor=actor)
    return {"kind": "statement", "statement_id": stmt_id, "lines": kept,
            "total": summed, "balance_warning": warn.strip() or None}


def parse_pending(limit: int = 25, actor="system") -> dict:
    """Work through the unparsed backlog."""
    con = db.get_db()
    try:
        ids = [r["id"] for r in con.execute(
            """SELECT id FROM documents
               WHERE parse_status='pending' AND vendor_id IS NOT NULL
               ORDER BY received_at LIMIT ?""", (limit,)).fetchall()]
    finally:
        con.close()

    ok = failed = 0
    for doc_id in ids:
        res = parse_document(doc_id, actor=actor)
        if res.get("ok"):
            ok += 1
        else:
            failed += 1
    return {"ok": True, "parsed": ok, "failed": failed, "considered": len(ids)}
