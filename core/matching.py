"""
The reconciliation: statement vs invoice vs QuickBooks.

Three sources describe the same money and routinely disagree:

  the statement   — what the vendor says you owe
  the invoice     — the document you actually hold
  QuickBooks      — what your books say is open

Every invoice number seen in ANY of the three becomes one row. That is the
design decision the whole module rests on. A naive implementation iterates the
statement and looks each line up, which makes a bill sitting in QBO that is on
nobody's statement invisible — and that is exactly the bill that gets paid
twice. Here it shows up as `qbo_only`, as loud as anything else.

Verdicts, and what each one means to the person doing the payment run:

  matched           all three agree. Pay it.
  matched_no_doc    statement and QBO agree, but no invoice on file. Payable,
                    though you have no document to defend it.
  amount_mismatch   they disagree on how much. Never pay on a mismatch.
  missing_bill      vendor says you owe it, you hold the invoice, QBO doesn't
                    know. Someone forgot to enter it.
  missing_invoice   vendor says you owe it, QBO has it, but no document.
  missing_statement in your books and you hold the paper, but not on their
                    statement. Often already paid, or billed to another account.
  invoice_only      you hold an invoice nobody else knows about. Enter it.
  statement_only    they claim it and you have nothing at all. Query it.
  qbo_only          open in your books, on no statement, no document.
  credit            a credit note or overpayment. Take it off the payment.
  paid              QBO shows it settled. Here so it is not silently dropped.
"""

import logging
from datetime import datetime, timezone

import db
from core.util import (num, iso, norm_invoice, period_bounds, current_period,
                       amounts_agree, cutoff_for_terms, parse_date, age_bucket)

log = logging.getLogger(__name__)

PAYABLE  = ("matched", "matched_no_doc")
EXCEPTIONS = ("amount_mismatch", "missing_bill", "missing_invoice",
              "missing_statement", "invoice_only", "statement_only", "qbo_only")

VERDICT_LABEL = {
    "matched":           "Matched",
    "matched_no_doc":    "Matched, no invoice on file",
    "amount_mismatch":   "Amounts disagree",
    "missing_bill":      "Not in QuickBooks",
    "missing_invoice":   "No invoice received",
    "missing_statement": "Not on their statement",
    "invoice_only":      "Invoice only",
    "statement_only":    "On their statement only",
    "qbo_only":          "Open in QuickBooks only",
    "credit":            "Credit",
    "paid":              "Already paid",
}

SUGGESTED_ACTION = {
    "matched":           "pay",
    "matched_no_doc":    "pay",
    "credit":            "pay",      # applied as a deduction
    "amount_mismatch":   "query",
    "missing_bill":      "hold",
    "missing_invoice":   "query",
    "missing_statement": "hold",
    "invoice_only":      "hold",
    "statement_only":    "query",
    "qbo_only":          "hold",
    "paid":              "ignore",
}


def run_match(period: str = "", actor: str = "system",
              vendor_id: int = None) -> dict:
    """Build the reconciliation for a period."""
    period = period or current_period()
    try:
        tolerance = float(db.get_setting("match_tolerance", "0.02") or 0.02)
    except (TypeError, ValueError):
        tolerance = 0.02

    start_iso, end_iso = (d.isoformat() for d in period_bounds(period))
    started = db.utcnow()

    con = db.get_db()
    try:
        vfilter = " AND v.id = ?" if vendor_id else ""
        vargs = (vendor_id,) if vendor_id else ()

        vendors = {r["id"]: dict(r) for r in con.execute(
            f"SELECT * FROM vendors v WHERE active=1{vfilter}", vargs).fetchall()}
        if not vendors:
            return {"ok": False, "error": "No active vendors to reconcile."}
        vids = tuple(vendors)
        ph = ",".join("?" * len(vids))

        # Invoices in scope: the period itself, plus anything older still open.
        # An invoice from two months ago that nobody entered is precisely what
        # a reconciliation is for, so the window reaches back rather than
        # filtering to the month and losing it.
        invoices = [dict(r) for r in con.execute(f"""
            SELECT * FROM invoices
            WHERE vendor_id IN ({ph})
              AND (period = ? OR period <= ? OR period IS NULL OR period = '')
              AND status <> 'void'
        """, (*vids, period, period)).fetchall()]

        stmt_lines = [dict(r) for r in con.execute(f"""
            SELECT sl.* FROM statement_lines sl
            JOIN statements s ON s.id = sl.statement_id
            WHERE sl.vendor_id IN ({ph})
              AND (sl.period = ? OR s.period = ?)
        """, (*vids, period, period)).fetchall()]

        qbills = [dict(r) for r in con.execute(f"""
            SELECT * FROM qbo_bills
            WHERE vendor_id IN ({ph})
              AND (status <> 'paid' OR COALESCE(txn_date,'') BETWEEN ? AND ?)
        """, (*vids, start_iso, end_iso)).fetchall()]
    finally:
        con.close()

    # ── bucket everything by (vendor, normalised invoice number) ─────────────
    buckets = {}

    def slot(vid, number):
        key = (vid, norm_invoice(number))
        return buckets.setdefault(key, {
            "vendor_id": vid, "invoice_number": number or "",
            "inv": None, "stmt": None, "qbo": None,
            "stmt_extra": [],    # credits and payments against the same number
        })

    for i in invoices:
        s = slot(i["vendor_id"], i["invoice_number"])
        s["inv"] = i
        s["invoice_number"] = s["invoice_number"] or i["invoice_number"] or ""

    for l in stmt_lines:
        s = slot(l["vendor_id"], l["invoice_number"])
        if l.get("line_type") in ("credit", "payment", "finance_charge"):
            s["stmt_extra"].append(l)
            if not s["stmt"]:
                s["invoice_number"] = s["invoice_number"] or l["invoice_number"] or ""
            continue
        # A vendor can list the same invoice twice on one statement. Sum rather
        # than letting the second row overwrite the first.
        if s["stmt"]:
            s["stmt"] = dict(s["stmt"])
            s["stmt"]["amount"] = round((num(s["stmt"]["amount"]) or 0)
                                        + (num(l["amount"]) or 0), 2)
        else:
            s["stmt"] = l
        s["invoice_number"] = s["invoice_number"] or l["invoice_number"] or ""

    for b in qbills:
        s = slot(b["vendor_id"], b["doc_number"])
        s["qbo"] = b
        s["invoice_number"] = s["invoice_number"] or b["doc_number"] or ""

    _merge_prefix_variants(buckets, vendors)

    # ── decide each one ──────────────────────────────────────────────────────
    rows = []
    for (vid, inorm), v in buckets.items():
        vendor = vendors.get(vid) or {}
        inv, stmt, qb = v["inv"], v["stmt"], v["qbo"]

        a_inv  = num(inv.get("amount")) if inv else None
        a_stmt = num(stmt.get("amount")) if stmt else None
        # The open balance is what is payable, not the original total.
        a_qbo  = None
        if qb:
            a_qbo = num(qb.get("balance"))
            if a_qbo is None:
                a_qbo = num(qb.get("total_amount"))

        extra = round(sum(num(e.get("amount")) or 0 for e in v["stmt_extra"]), 2)

        present = [x for x in (a_inv, a_stmt, a_qbo) if x is not None]
        all_agree = (len(present) > 1 and
                     all(amounts_agree(present[0], p, tolerance) for p in present[1:]))

        is_credit = any((x or 0) < 0 for x in present) and present

        # A statement row that is only a payment, credit or finance charge has
        # no invoice behind it by definition. Without this branch it falls
        # through to `statement_only` with every amount blank, which reads as a
        # mystery exception when it is really just the vendor showing you a
        # payment you already made.
        if not present and v["stmt_extra"]:
            kinds = {e.get("line_type") for e in v["stmt_extra"]}
            label = ("payment" if kinds == {"payment"}
                     else "finance_charge" if kinds == {"finance_charge"}
                     else "credit")
            rows.append({
                "period": period, "vendor_id": vid,
                "vendor_name": vendor.get("name") or "Unmatched vendor",
                "invoice_number": v["invoice_number"] or f"({label.replace('_',' ')})",
                "invoice_norm": inorm,
                "invoice_date": iso(v["stmt_extra"][0].get("invoice_date")),
                "due_date": "",
                "amt_invoice": None, "amt_statement": extra, "amt_qbo": None,
                "verdict": "credit" if extra <= 0 else "statement_only",
                "note": ({
                    "payment": "A payment they have recorded against the "
                               "account. Nothing to pay — it is already gone.",
                    "finance_charge": "A finance charge. Not an invoice; decide "
                                      "whether you accept it before paying.",
                    "credit": "A credit on the account. Deduct it from the "
                              "payment.",
                }[label]),
                "action": "ignore" if label == "payment" else "query",
            })
            continue

        if qb and (qb.get("status") == "paid" or
                   (a_qbo is not None and abs(a_qbo) < 0.005)):
            verdict = "paid"
            note = "QuickBooks shows this settled."
        elif is_credit and len(present) >= 1 and all(x <= 0 for x in present):
            verdict = "credit"
            note = "A credit. Deduct it from the payment rather than paying it."
        elif len(present) == 3 and all_agree:
            verdict, note = "matched", "Statement, invoice and QuickBooks agree."
        elif len(present) > 1 and not all_agree:
            verdict = "amount_mismatch"
            note = (f"Invoice {_s(a_inv)}, statement {_s(a_stmt)}, "
                    f"QuickBooks {_s(a_qbo)}. Do not pay until this is settled.")
        elif stmt and qb and not inv and all_agree:
            verdict = "matched_no_doc"
            note = ("Their statement and QuickBooks agree, but we hold no "
                    "invoice for it.")
        elif stmt and inv and not qb:
            verdict = "missing_bill"
            note = ("On their statement and we hold the invoice, but it is not "
                    "in QuickBooks. It needs entering.")
        elif stmt and qb and not inv:
            verdict = "missing_invoice"
            note = ("On their statement and in QuickBooks, but we never "
                    "received the invoice.")
        elif inv and qb and not stmt:
            verdict = "missing_statement"
            note = ("In QuickBooks and we hold the invoice, but it is not on "
                    "their statement. Often already paid.")
        elif inv and not stmt and not qb:
            verdict = "invoice_only"
            note = "We hold the invoice and nothing else knows about it."
        elif stmt and not inv and not qb:
            verdict = "statement_only"
            note = ("They are claiming this and we have nothing — no invoice, "
                    "nothing in QuickBooks.")
        elif qb and not inv and not stmt:
            verdict = "qbo_only"
            note = ("Open in QuickBooks, on no statement, with no invoice "
                    "on file.")
        else:
            verdict, note = "statement_only", "Unclear — worth a human look."

        if extra:
            note += (f"  Their statement also shows {extra:,.2f} of "
                     f"credits or payments against this number.")

        src = inv or stmt or qb or {}
        i_date = src.get("invoice_date") or (qb.get("txn_date") if qb else "")
        d_date = (inv or {}).get("due_date") or (stmt or {}).get("due_date") \
            or (qb or {}).get("due_date") or ""

        rows.append({
            "period": period, "vendor_id": vid,
            "vendor_name": vendor.get("name") or "Unmatched vendor",
            "invoice_number": v["invoice_number"] or "(no number)",
            "invoice_norm": inorm,
            "invoice_date": iso(i_date), "due_date": iso(d_date),
            "amt_invoice": a_inv, "amt_statement": a_stmt, "amt_qbo": a_qbo,
            "verdict": verdict, "note": note,
            "action": SUGGESTED_ACTION.get(verdict, "hold"),
        })

    # ── persist ──────────────────────────────────────────────────────────────
    matched = sum(1 for r in rows if r["verdict"] in PAYABLE)
    exceptions = sum(1 for r in rows if r["verdict"] in EXCEPTIONS)
    to_pay = round(sum(_payable_amount(r) for r in rows
                       if r["verdict"] in PAYABLE or r["verdict"] == "credit"), 2)

    con = db.get_db()
    try:
        cur = con.execute("""INSERT INTO match_runs
            (period, started_at, finished_at, run_by, row_count, matched,
             exceptions, to_pay) VALUES (?,?,?,?,?,?,?,?)""",
            (period, started, db.utcnow(), actor, len(rows), matched,
             exceptions, to_pay))
        run_id = cur.lastrowid

        # Keep resolutions a human already made. Re-running the match must not
        # silently un-tick something somebody investigated and closed.
        keep = {(r["vendor_id"], r["invoice_norm"]): dict(r) for r in con.execute(
            """SELECT vendor_id, invoice_norm, action, resolved, resolved_by,
                      resolved_at, paid_at, paid_ref
               FROM match_rows WHERE period=? AND (resolved=1 OR paid_at IS NOT NULL)""",
            (period,)).fetchall()}

        con.execute("DELETE FROM match_rows WHERE period=?", (period,))
        for r in rows:
            prev = keep.get((r["vendor_id"], r["invoice_norm"]))
            con.execute("""INSERT INTO match_rows
                (run_id, period, vendor_id, vendor_name, invoice_number,
                 invoice_norm, invoice_date, due_date, amt_invoice,
                 amt_statement, amt_qbo, verdict, note, action, resolved,
                 resolved_by, resolved_at, paid_at, paid_ref)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, period, r["vendor_id"], r["vendor_name"],
                 r["invoice_number"], r["invoice_norm"], r["invoice_date"],
                 r["due_date"], r["amt_invoice"], r["amt_statement"],
                 r["amt_qbo"], r["verdict"], r["note"],
                 (prev or {}).get("action") or r["action"],
                 (prev or {}).get("resolved", 0),
                 (prev or {}).get("resolved_by"), (prev or {}).get("resolved_at"),
                 (prev or {}).get("paid_at"), (prev or {}).get("paid_ref")))
        con.commit()
    finally:
        con.close()

    db.say("match", f"Reconciled {period}",
           f"{len(rows)} invoice number(s) across {len(vendors)} vendor(s): "
           f"{matched} matched, {exceptions} needing attention, "
           f"{to_pay:,.2f} ready to pay.",
           ok=(exceptions == 0), actor=actor)

    return {"ok": True, "period": period, "run_id": run_id, "rows": len(rows),
            "matched": matched, "exceptions": exceptions, "to_pay": to_pay}


def _merge_prefix_variants(buckets, vendors):
    """Fold 'ABC-000001' and '1' into one row.

    Suppliers routinely print a prefixed number on the invoice and a bare one
    on the statement, and whoever keyed it into the ledger used whichever they
    were looking at. Left alone, that single habit turns every one of that
    vendor's invoices into two half-populated rows: one `invoice_only` and one
    `matched_no_doc`, both wrong, and the month looks full of exceptions that
    do not exist.

    The merge is deliberately narrow, because merging two invoices that are
    genuinely different is far worse than leaving a pair unmerged:

      * same vendor only — never across vendors
      * both keys must be letters-then-digits, with identical digits
      * exactly one bare-numeric key, and exactly one distinct prefix

    Two different prefixes sharing a digit run (ABC-100 and DEF-100) is
    ambiguous, so nothing is merged and a note is logged instead.
    """
    from core.util import split_invoice_key

    by_vendor = {}
    for key in list(buckets):
        vid, inorm = key
        prefix, digits = split_invoice_key(inorm)
        if digits is None:
            continue
        by_vendor.setdefault(vid, {}).setdefault(digits, []).append((key, prefix))

    for vid, groups in by_vendor.items():
        for digits, members in groups.items():
            if len(members) < 2:
                continue
            bare = [k for k, p in members if not p]
            prefixes = {p for _, p in members if p}
            if len(bare) != 1 or len(prefixes) != 1:
                vname = (vendors.get(vid) or {}).get("name", "a vendor")
                db.say("match",
                       f"Could not safely merge invoice numbers for {vname}",
                       f"Several numbers share the digits {digits}: "
                       + ", ".join(buckets[k]["invoice_number"] or k[1]
                                   for k, _ in members)
                       + ". Left as separate rows rather than guess.",
                       vendor_id=vid, ok=False)
                continue

            target_key = next(k for k, p in members if p)   # keep the prefixed
            target = buckets[target_key]
            for key, _p in members:
                if key == target_key:
                    continue
                other = buckets.pop(key)
                for slot_name in ("inv", "stmt", "qbo"):
                    if not target.get(slot_name) and other.get(slot_name):
                        target[slot_name] = other[slot_name]
                target["stmt_extra"].extend(other.get("stmt_extra") or [])
                if not target.get("invoice_number"):
                    target["invoice_number"] = other.get("invoice_number") or ""


def _s(v):
    return "—" if v is None else f"{v:,.2f}"


def _payable_amount(r) -> float:
    """What to actually pay on a row: QBO's open balance when there is one,
    because that is the figure the books will clear."""
    for k in ("amt_qbo", "amt_statement", "amt_invoice"):
        if r.get(k) is not None:
            return float(r[k])
    return 0.0


# ── reading the result back ─────────────────────────────────────────────────

def report(period: str = "", vendor_id: int = None) -> dict:
    """The reconciliation grouped by vendor — what the Reports page renders."""
    period = period or current_period()
    con = db.get_db()
    try:
        args = [period]
        where = "WHERE m.period=?"
        if vendor_id:
            where += " AND m.vendor_id=?"
            args.append(vendor_id)
        rows = [dict(r) for r in con.execute(f"""
            SELECT m.*, v.terms_days, v.payment_method, v.payment_portal_url,
                   v.currency, v.account_number
            FROM match_rows m LEFT JOIN vendors v ON v.id = m.vendor_id
            {where} ORDER BY m.vendor_name, m.invoice_number""", args).fetchall()]

        vendors = [dict(r) for r in con.execute(
            "SELECT * FROM vendors WHERE active=1 ORDER BY name").fetchall()]

        got_statement = {r["vendor_id"] for r in con.execute(
            "SELECT DISTINCT vendor_id FROM statements WHERE period=?",
            (period,)).fetchall()}

        run = con.execute("""SELECT * FROM match_runs WHERE period=?
                             ORDER BY id DESC LIMIT 1""", (period,)).fetchone()
    finally:
        con.close()

    groups = {}
    for r in rows:
        g = groups.setdefault(r["vendor_id"] or 0, {
            "vendor_id": r["vendor_id"],
            "vendor_name": r["vendor_name"] or "Unmatched vendor",
            "terms_days": r.get("terms_days"),
            "payment_method": r.get("payment_method"),
            "payment_portal_url": r.get("payment_portal_url"),
            "currency": r.get("currency") or "USD",
            "account_number": r.get("account_number"),
            "statement_received": (r["vendor_id"] in got_statement),
            "rows": [], "to_pay": 0.0, "credits": 0.0,
            "exceptions": 0, "paid": 0,
        })
        g["rows"].append(r)
        if r.get("paid_at"):
            g["paid"] += 1
        elif r["verdict"] in PAYABLE:
            g["to_pay"] += _payable_amount(r)
        elif r["verdict"] == "credit":
            g["credits"] += _payable_amount(r)
        elif r["verdict"] in EXCEPTIONS and not r.get("resolved"):
            g["exceptions"] += 1

    for g in groups.values():
        g["to_pay"] = round(g["to_pay"], 2)
        g["credits"] = round(g["credits"], 2)
        g["net_to_pay"] = round(g["to_pay"] + g["credits"], 2)

    # Vendors needing attention first, then by size. A clerk works top-down.
    ordered = sorted(groups.values(),
                     key=lambda g: (-g["exceptions"], -g["net_to_pay"]))

    silent = [v["name"] for v in vendors if v["id"] not in got_statement]

    totals = {
        "rows": len(rows),
        "matched":    sum(1 for r in rows if r["verdict"] in PAYABLE),
        "exceptions": sum(1 for r in rows if r["verdict"] in EXCEPTIONS
                          and not r.get("resolved")),
        "paid":       sum(1 for r in rows if r.get("paid_at")),
        "to_pay":     round(sum(g["to_pay"] for g in ordered), 2),
        "credits":    round(sum(g["credits"] for g in ordered), 2),
        "net_to_pay": round(sum(g["net_to_pay"] for g in ordered), 2),
        "vendors":    len(ordered),
        "no_statement": silent,
    }
    return {"ok": True, "period": period, "groups": ordered, "totals": totals,
            "run": dict(run) if run else None,
            "verdict_labels": VERDICT_LABEL}


def payment_plan(period: str = "") -> dict:
    """What to actually pay, per vendor, with the exceptions held back."""
    rep = report(period)
    plan = []
    for g in rep["groups"]:
        payable = [r for r in g["rows"]
                   if r["verdict"] in PAYABLE + ("credit",) and not r.get("paid_at")]
        held = [r for r in g["rows"]
                if r["verdict"] in EXCEPTIONS and not r.get("resolved")]
        if not payable and not held:
            continue
        plan.append({
            "vendor_id": g["vendor_id"],
            "vendor": g["vendor_name"],
            "method": g.get("payment_method") or "not set",
            "portal": g.get("payment_portal_url"),
            "account": g.get("account_number"),
            "currency": g.get("currency"),
            "pay_now": g["net_to_pay"],
            "invoice_count": len(payable),
            "invoices": [{"number": r["invoice_number"],
                          "amount": _payable_amount(r),
                          "due": r["due_date"]} for r in payable],
            "held_back": [{"number": r["invoice_number"],
                           "verdict": VERDICT_LABEL.get(r["verdict"], r["verdict"]),
                           "why": r["note"]} for r in held],
        })
    return {"ok": True, "period": rep["period"], "plan": plan,
            "total": rep["totals"]["net_to_pay"],
            "held_count": rep["totals"]["exceptions"]}


def set_row_action(row_id: int, action: str = "", resolved=None,
                   paid_ref: str = "", actor: str = "") -> dict:
    con = db.get_db()
    try:
        row = con.execute("SELECT * FROM match_rows WHERE id=?", (row_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "No such row"}
        sets, args = [], []
        if action:
            sets.append("action=?"); args.append(action)
        if resolved is not None:
            sets.append("resolved=?"); args.append(1 if resolved else 0)
            sets.append("resolved_by=?"); args.append(actor)
            sets.append("resolved_at=?"); args.append(db.utcnow())
        if action == "pay" and paid_ref:
            sets.append("paid_at=?"); args.append(db.utcnow())
            sets.append("paid_ref=?"); args.append(paid_ref)
        if not sets:
            return {"ok": False, "error": "Nothing to change"}
        args.append(row_id)
        con.execute(f"UPDATE match_rows SET {','.join(sets)} WHERE id=?", args)
        con.commit()
    finally:
        con.close()
    return {"ok": True}
