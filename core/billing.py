"""
Pushing a parsed invoice into QuickBooks Online as a Bill.

This is the only place the portal writes to an accounting system, so the rules
are strict and all of them are about not creating a duplicate:

  * an invoice already carrying a qbo_bill_id is never pushed again
  * QBO is asked whether that DocNumber already exists for that vendor
  * a low-confidence parse is refused unless someone explicitly overrides
  * an invoice with no number is refused outright — DocNumber is the only
    handle anyone will ever have on it afterwards

A duplicate bill is quiet. Nothing downstream flags it, the vendor happily
cashes both, and it surfaces months later as an unexplained credit. Refusing
to push is always the cheaper mistake.
"""

import logging

import db
from core.util import num, iso
from integrations import qbo

log = logging.getLogger(__name__)


def push_invoice(invoice_id: int, actor="system", allow_low_confidence=False,
                 attach_pdf=True) -> dict:
    con = db.get_db()
    try:
        row = con.execute("""
            SELECT i.*, v.name vendor_name, v.qbo_vendor_id,
                   v.default_expense_account, v.auto_push_bills,
                   d.path doc_path, d.filename doc_filename
            FROM invoices i
            LEFT JOIN vendors v   ON v.id = i.vendor_id
            LEFT JOIN documents d ON d.id = i.doc_id
            WHERE i.id=?""", (invoice_id,)).fetchone()
    finally:
        con.close()
    if not row:
        return {"ok": False, "error": "No such invoice"}
    inv = dict(row)

    if inv.get("qbo_bill_id"):
        return {"ok": False, "error": f"Already in QuickBooks as bill "
                                      f"{inv['qbo_bill_id']}",
                "qbo_bill_id": inv["qbo_bill_id"]}

    if not inv.get("invoice_number"):
        return {"ok": False, "error":
                "This invoice has no number. QuickBooks needs a DocNumber — "
                "without one nobody can match it later. Add the number first."}

    if inv.get("confidence") == "low" and not allow_low_confidence:
        return {"ok": False, "needs_override": True, "error":
                "The parse was low confidence. Check the figures, then push "
                "with the override if they are right."}

    if not inv.get("qbo_vendor_id"):
        return {"ok": False, "error":
                f"{inv.get('vendor_name')} is not linked to a QuickBooks "
                "vendor. Set that on the vendor record first."}

    amount = num(inv.get("amount"))
    if amount is None or abs(amount) < 0.005:
        return {"ok": False, "error": "The invoice amount is zero or unreadable."}

    con = db.get_db()
    try:
        lines = [dict(r) for r in con.execute(
            "SELECT * FROM invoice_lines WHERE invoice_id=? ORDER BY line_no",
            (invoice_id,)).fetchall()]
    finally:
        con.close()

    acct = inv.get("default_expense_account") or ""
    payload_lines = []
    for ln in lines:
        amt = num(ln.get("amount"))
        if amt is None or abs(amt) < 0.005:
            continue
        payload_lines.append({"amount": amt,
                              "description": ln.get("description") or "",
                              "account_id": ln.get("account") or acct})

    # Lines that do not add up to the invoice total mean the parse dropped
    # something. Post one line for the real total rather than silently
    # under-billing the expense account.
    line_sum = round(sum(l["amount"] for l in payload_lines), 2)
    if not payload_lines or abs(line_sum - amount) > 0.02:
        if payload_lines:
            log.info("Invoice %s: lines sum to %s but total is %s — posting a "
                     "single line for the total.", invoice_id, line_sum, amount)
        payload_lines = [{
            "amount": amount,
            "description": (f"Invoice {inv['invoice_number']}"
                            + (f" — PO {inv['po_number']}" if inv.get("po_number") else "")),
            "account_id": acct,
        }]

    memo_bits = [f"Imported by InvoiceHub from {inv.get('doc_filename') or 'upload'}"]
    if inv.get("po_number"):
        memo_bits.append(f"PO {inv['po_number']}")
    if inv.get("job_ref"):
        memo_bits.append(f"Job {inv['job_ref']}")

    client = qbo.get_client()
    try:
        bill = client.create_bill(
            qbo_vendor_id=inv["qbo_vendor_id"],
            doc_number=inv["invoice_number"],
            txn_date=inv.get("invoice_date"),
            due_date=inv.get("due_date"),
            lines=payload_lines,
            memo=" · ".join(memo_bits),
            currency=inv.get("currency") or "USD",
            expense_account_id=acct)
    except Exception as e:
        con = db.get_db()
        try:
            con.execute("UPDATE invoices SET qbo_push_error=?, updated_at=? "
                        "WHERE id=?", (str(e)[:1000], db.utcnow(), invoice_id))
            con.commit()
        finally:
            con.close()
        db.say("qbo", f"Could not push invoice {inv['invoice_number']} "
                      f"for {inv.get('vendor_name')}", str(e),
               vendor_id=inv.get("vendor_id"), ok=False, actor=actor)
        return {"ok": False, "error": str(e)}

    bill_id = str(bill.get("Id") or "")
    con = db.get_db()
    try:
        con.execute("""UPDATE invoices SET qbo_bill_id=?, qbo_pushed_at=?,
                       qbo_push_error=NULL, status='pushed', updated_at=?
                       WHERE id=?""",
                    (bill_id, db.utcnow(), db.utcnow(), invoice_id))
        con.commit()
    finally:
        con.close()

    if attach_pdf and inv.get("doc_path"):
        try:
            import os
            if os.path.exists(inv["doc_path"]) and \
                    inv["doc_path"].lower().endswith(".pdf"):
                with open(inv["doc_path"], "rb") as fh:
                    client.attach_pdf(bill_id, fh.read(),
                                      inv.get("doc_filename") or "invoice.pdf")
        except Exception as e:
            # The bill exists; a missing attachment is not worth failing over.
            log.info("Could not attach the PDF to bill %s: %s", bill_id, e)

    db.say("qbo", f"Pushed invoice {inv['invoice_number']} to QuickBooks",
           f"{inv.get('vendor_name')} · {amount:,.2f} · bill {bill_id}",
           vendor_id=inv.get("vendor_id"), actor=actor)
    return {"ok": True, "qbo_bill_id": bill_id, "amount": amount}


def push_ready(actor="system", limit: int = 50) -> dict:
    """Push every invoice that is eligible and whose vendor allows it.

    Vendor-level opt-in, not a global switch: most teams want automatic for
    the high-volume suppliers whose invoices are clean, and a human eye on
    the rest.
    """
    con = db.get_db()
    try:
        ids = [r["id"] for r in con.execute("""
            SELECT i.id FROM invoices i
            JOIN vendors v ON v.id = i.vendor_id
            WHERE i.qbo_bill_id IS NULL
              AND i.status = 'open'
              AND i.invoice_number IS NOT NULL AND i.invoice_number <> ''
              AND COALESCE(i.confidence,'medium') <> 'low'
              AND v.qbo_vendor_id IS NOT NULL AND v.qbo_vendor_id <> ''
              AND v.auto_push_bills = 1
            ORDER BY i.created_at LIMIT ?""", (limit,)).fetchall()]
    finally:
        con.close()

    pushed, failed, errors = 0, 0, []
    for inv_id in ids:
        res = push_invoice(inv_id, actor=actor)
        if res.get("ok"):
            pushed += 1
        else:
            failed += 1
            errors.append(res.get("error", ""))

    return {"ok": True, "pushed": pushed, "failed": failed,
            "considered": len(ids), "errors": errors[:10]}
