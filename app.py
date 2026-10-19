#!/usr/bin/env python3
"""
Openfield — intelligent accounts-payable operations.

    python app.py            then open http://localhost:5100

What it does, end to end:

    vendors are set up once, with the rules for how their post arrives
      ↓
    the AP mailbox is swept and every attachment files itself (Microsoft Graph)
      ↓
    Claude reads the PDFs and turns them into invoices and statement lines
      ↓
    parsed invoices are pushed into QuickBooks Online as bills
      ↓
    statements are reconciled against QBO's open bills
      ↓
    a workbook comes out with one tab per vendor, and a payment plan

Agents run the recurring parts on a schedule. None of them ever send an email
on their own — they write drafts and a human presses send.
"""

import os
import io
import json
import logging
from datetime import datetime, timedelta, timezone

from flask import (Flask, request, jsonify, render_template, session,
                   redirect, send_file, abort)

import config
import db
import auth
from core import intake, parsing, matching, reporting, billing, agents
from core.util import current_period, num, norm_invoice
from integrations import graph, qbo, servicetitan, claude_api

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S")
log = logging.getLogger("invoicehub")

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = config.MAX_UPLOAD_MB * 1024 * 1024
app.permanent_session_lifetime = timedelta(days=7)

db.init_db()


@app.context_processor
def inject_globals():
    return {
        "user": auth.current_user(),
        "demo_mode": db.demo_mode(),
        "company_name": db.get_setting("company_name", "Your Company"),
        "default_password_warning": auth.using_default_password(),
        "period": current_period(),
        "encryption_ok": config.ENCRYPTION_AVAILABLE,
    }


def body():
    return request.get_json(silent=True) or {}


def me():
    u = auth.current_user()
    return (u or {}).get("email", "system")


def ok(**kw):
    return jsonify({"ok": True, **kw})


def err(message, code=400, **kw):
    return jsonify({"ok": False, "error": message, **kw}), code


# ════════════════════════════════════════════════════════════════════════════
#  pages
# ════════════════════════════════════════════════════════════════════════════

@app.route("/login", methods=["GET", "POST"])
def page_login():
    if request.method == "POST":
        d = request.form if request.form else body()
        user, error = auth.login(d.get("email", ""), d.get("password", ""))
        if error:
            return render_template("login.html", error=error,
                                   email=d.get("email", ""))
        return redirect("/")
    return render_template("login.html", error=None, email="")


@app.route("/logout")
def page_logout():
    auth.logout()
    return redirect("/login")


@app.route("/")
@auth.require_login
def page_dashboard():
    return render_template("dashboard.html", active="dashboard")


@app.route("/vendors")
@auth.require_login
def page_vendors():
    return render_template("vendors.html", active="vendors")


@app.route("/inbox")
@auth.require_login
def page_inbox():
    return render_template("inbox.html", active="inbox")


@app.route("/invoices")
@auth.require_login
def page_invoices():
    return render_template("invoices.html", active="invoices")


@app.route("/statements")
@auth.require_login
def page_statements():
    return render_template("statements.html", active="statements")


@app.route("/reconciliation")
@auth.require_login
def page_reconciliation():
    return render_template("reconciliation.html", active="reconciliation")


@app.route("/reports")
@auth.require_login
def page_reports():
    return render_template("reports.html", active="reports")


@app.route("/agents")
@auth.require_login
def page_agents():
    return render_template("agents.html", active="agents")


@app.route("/drafts")
@auth.require_login
def page_drafts():
    return render_template("drafts.html", active="drafts")


@app.route("/settings")
@auth.require_login
def page_settings():
    return render_template("settings.html", active="settings")


@app.route("/activity")
@auth.require_login
def page_activity():
    return render_template("activity.html", active="activity")


# ════════════════════════════════════════════════════════════════════════════
#  dashboard
# ════════════════════════════════════════════════════════════════════════════

@app.route("/api/dashboard")
@auth.require_login
def api_dashboard():
    period = request.args.get("period") or current_period()
    con = db.get_db()
    try:
        def one(sql, args=()):
            r = con.execute(sql, args).fetchone()
            return (r[0] if r else 0) or 0

        stats = {
            "vendors":        one("SELECT COUNT(*) FROM vendors WHERE active=1"),
            "docs_pending":   one("SELECT COUNT(*) FROM documents "
                                  "WHERE parse_status='pending'"),
            "docs_review":    one("SELECT COUNT(*) FROM documents "
                                  "WHERE parse_status IN ('needs_review','failed')"),
            "docs_unmatched": one("SELECT COUNT(*) FROM documents "
                                  "WHERE vendor_id IS NULL"),
            "invoices_open":  one("SELECT COUNT(*) FROM invoices "
                                  "WHERE status='open'"),
            "not_pushed":     one("SELECT COUNT(*) FROM invoices "
                                  "WHERE qbo_bill_id IS NULL AND status='open'"),
            "qbo_open":       one("SELECT COUNT(*) FROM qbo_bills "
                                  "WHERE status IN ('open','partial')"),
            "qbo_balance":    one("SELECT COALESCE(SUM(balance),0) FROM qbo_bills "
                                  "WHERE status IN ('open','partial')"),
            "drafts_pending": one("SELECT COUNT(*) FROM drafts "
                                  "WHERE status='pending'"),
            "agents_on":      one("SELECT COUNT(*) FROM agents WHERE enabled=1"),
        }

        statements_in = one(
            "SELECT COUNT(DISTINCT vendor_id) FROM statements WHERE period=?",
            (period,))
        stats["statements_received"] = statements_in
        stats["statements_missing"] = max(stats["vendors"] - statements_in, 0)

        recent = [dict(r) for r in con.execute(
            "SELECT * FROM activity ORDER BY at DESC LIMIT 15").fetchall()]

        ageing = [dict(r) for r in con.execute("""
            SELECT CASE
                     WHEN due_date IS NULL OR due_date='' THEN 'unknown'
                     WHEN date(due_date) >= date('now') THEN 'current'
                     WHEN date(due_date) >= date('now','-30 day') THEN '1-30'
                     WHEN date(due_date) >= date('now','-60 day') THEN '31-60'
                     WHEN date(due_date) >= date('now','-90 day') THEN '61-90'
                     ELSE '90+' END bucket,
                   COUNT(*) n, COALESCE(SUM(balance),0) amount
            FROM qbo_bills WHERE status IN ('open','partial')
            GROUP BY bucket""").fetchall()]
    finally:
        con.close()

    rep = matching.report(period)
    return ok(period=period, stats=stats, recent=recent, ageing=ageing,
              totals=rep["totals"],
              top_vendors=[{"vendor": g["vendor_name"],
                            "net": g["net_to_pay"],
                            "exceptions": g["exceptions"],
                            "statement": g["statement_received"]}
                           for g in rep["groups"][:10]])


# ════════════════════════════════════════════════════════════════════════════
#  vendors
# ════════════════════════════════════════════════════════════════════════════

VENDOR_FIELDS = (
    "name", "display_name", "active", "invoice_email", "invoice_subject",
    "statement_email", "statement_subject", "request_email", "cc_emails",
    "terms_days", "currency", "account_number", "payment_method",
    "payment_portal_url", "early_pay_discount", "qbo_vendor_id",
    "qbo_vendor_name", "st_vendor_id", "default_expense_account",
    "statement_cadence", "auto_request_statement", "auto_push_bills",
    "requires_po", "notes",
)


@app.route("/api/vendors")
@auth.require_login
def api_vendors():
    con = db.get_db()
    try:
        rows = [dict(r) for r in con.execute("""
            SELECT v.*,
              (SELECT COUNT(*) FROM invoices i WHERE i.vendor_id=v.id) invoice_count,
              (SELECT COUNT(*) FROM documents d WHERE d.vendor_id=v.id) doc_count,
              (SELECT COUNT(*) FROM statements s WHERE s.vendor_id=v.id
                 AND s.period=?) statement_this_period,
              (SELECT COALESCE(SUM(balance),0) FROM qbo_bills q
                 WHERE q.vendor_id=v.id AND q.status IN ('open','partial')) open_balance
            FROM vendors v ORDER BY v.active DESC, v.name
        """, (current_period(),)).fetchall()]
    finally:
        con.close()
    return ok(vendors=rows)


@app.route("/api/vendors", methods=["POST"])
@auth.require("edit")
def api_vendor_save():
    d = body()
    vid = d.get("id")
    name = (d.get("name") or "").strip()
    if not name:
        return err("A vendor needs a name.")

    fields = {k: d.get(k) for k in VENDOR_FIELDS if k in d}
    fields["name"] = name
    for k in ("terms_days",):
        if k in fields:
            try:
                fields[k] = int(fields[k] or 30)
            except (TypeError, ValueError):
                fields[k] = 30
    for k in ("active", "auto_request_statement", "auto_push_bills", "requires_po"):
        if k in fields:
            fields[k] = 1 if str(fields[k]).lower() in ("1", "true", "yes", "on") else 0

    con = db.get_db()
    try:
        if vid:
            sets = ", ".join(f"{k}=?" for k in fields)
            con.execute(f"UPDATE vendors SET {sets}, updated_at=? WHERE id=?",
                        (*fields.values(), db.utcnow(), vid))
            action = "Updated"
        else:
            dupe = con.execute("SELECT id FROM vendors WHERE lower(name)=lower(?)",
                               (name,)).fetchone()
            if dupe:
                return err(f"There is already a vendor called {name}.")
            cols = ", ".join(fields)
            qs = ", ".join("?" * len(fields))
            cur = con.execute(
                f"INSERT INTO vendors ({cols}, created_at, updated_at) "
                f"VALUES ({qs}, ?, ?)",
                (*fields.values(), db.utcnow(), db.utcnow()))
            vid = cur.lastrowid
            action = "Added"
        con.commit()
    finally:
        con.close()
    db.say("note", f"{action} the vendor {name}", vendor_id=vid, actor=me())
    return ok(id=vid)


@app.route("/api/vendors/<int:vid>", methods=["DELETE"])
@auth.require("admin")
def api_vendor_delete(vid):
    con = db.get_db()
    try:
        r = con.execute("SELECT name FROM vendors WHERE id=?", (vid,)).fetchone()
        if not r:
            return err("No such vendor", 404)
        n_docs = con.execute("SELECT COUNT(*) c FROM documents WHERE vendor_id=?",
                             (vid,)).fetchone()["c"]
        if n_docs and not request.args.get("force"):
            # Deleting a vendor with history orphans its documents. Make the
            # caller say so explicitly rather than discovering it afterwards.
            return err(f"{r['name']} has {n_docs} document(s) on file. "
                       f"Deactivate it instead, or pass force=1 to delete "
                       f"anyway — the documents will be unfiled, not removed.",
                       409, needs_force=True, documents=n_docs)
        con.execute("DELETE FROM vendors WHERE id=?", (vid,))
        con.commit()
    finally:
        con.close()
    db.say("note", f"Deleted the vendor {r['name']}", actor=me(), ok=False)
    return ok()


@app.route("/api/vendors/<int:vid>")
@auth.require_login
def api_vendor_detail(vid):
    period = request.args.get("period") or current_period()
    con = db.get_db()
    try:
        v = con.execute("SELECT * FROM vendors WHERE id=?", (vid,)).fetchone()
        if not v:
            return err("No such vendor", 404)
        out = dict(v)
        out["contacts"] = [dict(r) for r in con.execute(
            "SELECT * FROM vendor_contacts WHERE vendor_id=?", (vid,)).fetchall()]
        out["invoices"] = [dict(r) for r in con.execute(
            "SELECT * FROM invoices WHERE vendor_id=? ORDER BY invoice_date DESC "
            "LIMIT 200", (vid,)).fetchall()]
        out["documents"] = [dict(r) for r in con.execute(
            "SELECT id,kind,filename,received_at,parse_status,confidence "
            "FROM documents WHERE vendor_id=? ORDER BY received_at DESC LIMIT 100",
            (vid,)).fetchall()]
        out["statements"] = [dict(r) for r in con.execute(
            "SELECT * FROM statements WHERE vendor_id=? ORDER BY period DESC "
            "LIMIT 24", (vid,)).fetchall()]
        out["qbo_bills"] = [dict(r) for r in con.execute(
            "SELECT * FROM qbo_bills WHERE vendor_id=? ORDER BY txn_date DESC "
            "LIMIT 200", (vid,)).fetchall()]
    finally:
        con.close()
    out["reconciliation"] = matching.report(period, vendor_id=vid)
    return ok(vendor=out)


# ════════════════════════════════════════════════════════════════════════════
#  documents
# ════════════════════════════════════════════════════════════════════════════

@app.route("/api/documents")
@auth.require_login
def api_documents():
    status = request.args.get("status", "")
    kind = request.args.get("kind", "")
    vendor = request.args.get("vendor_id", "")
    unmatched = request.args.get("unmatched") == "1"

    sql = """SELECT d.*, v.name vendor_name FROM documents d
             LEFT JOIN vendors v ON v.id=d.vendor_id WHERE 1=1"""
    args = []
    if status:
        sql += " AND d.parse_status=?"; args.append(status)
    if kind:
        sql += " AND d.kind=?"; args.append(kind)
    if vendor:
        sql += " AND d.vendor_id=?"; args.append(vendor)
    if unmatched:
        sql += " AND d.vendor_id IS NULL"
    sql += " ORDER BY d.received_at DESC LIMIT 500"

    con = db.get_db()
    try:
        rows = [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()
    return ok(documents=rows)


@app.route("/api/documents/upload", methods=["POST"])
@auth.require("edit")
def api_document_upload():
    fs = request.files.get("file")
    if not fs:
        return err("No file came through.")
    raw = fs.read()
    if not raw:
        return err("That file is empty.")

    vendor = None
    vid = request.form.get("vendor_id")
    if vid:
        con = db.get_db()
        try:
            r = con.execute("SELECT * FROM vendors WHERE id=?", (vid,)).fetchone()
            vendor = dict(r) if r else None
        finally:
            con.close()

    kind = request.form.get("kind") or "unknown"
    doc_id, created = intake.record_document(
        raw, fs.filename, vendor=vendor, kind=kind, source="upload",
        subject=f"Uploaded by {me()}", mime=fs.mimetype or "")
    if not created:
        return err("That exact file is already on record.", 409, doc_id=doc_id)

    db.say("file", f"{me()} uploaded “{fs.filename}”",
           f"as a {kind}" + (f" for {vendor['name']}" if vendor else
                             " — not yet assigned to a vendor"),
           vendor_id=(vendor or {}).get("id"), actor=me())

    if request.form.get("parse") == "1" and vendor:
        result = parsing.parse_document(doc_id, actor=me())
        return ok(doc_id=doc_id, parsed=result)
    return ok(doc_id=doc_id)


@app.route("/api/documents/<int:doc_id>/assign", methods=["POST"])
@auth.require("edit")
def api_document_assign(doc_id):
    d = body()
    res = intake.assign_document(doc_id, d.get("vendor_id"),
                                 d.get("kind", ""), actor=me())
    return (ok(**res) if res.get("ok") else err(res.get("error")))


@app.route("/api/documents/<int:doc_id>/parse", methods=["POST"])
@auth.require("edit")
def api_document_parse(doc_id):
    res = parsing.parse_document(doc_id, actor=me())
    return (ok(**res) if res.get("ok") else err(res.get("error")))


@app.route("/api/documents/<int:doc_id>/file")
@auth.require_login
def api_document_file(doc_id):
    con = db.get_db()
    try:
        r = con.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    finally:
        con.close()
    if not r:
        abort(404)
    path = r["path"]
    if not path or not os.path.exists(path):
        return err("The file is not on disk. In demo mode the documents are "
                   "records only — there is no PDF behind them.", 404)
    return send_file(path, download_name=r["filename"])


@app.route("/api/scan", methods=["POST"])
@auth.require("edit")
def api_scan():
    d = body()
    res = intake.scan_mailbox(days_back=d.get("days_back"), actor=me())
    return (ok(**res) if res.get("ok") else err(res.get("error")))


@app.route("/api/scan/status")
@auth.require_login
def api_scan_status():
    return ok(**intake.SCAN_STATE)


@app.route("/api/parse/pending", methods=["POST"])
@auth.require("edit")
def api_parse_pending():
    d = body()
    return ok(**parsing.parse_pending(limit=int(d.get("limit", 25)), actor=me()))


# ════════════════════════════════════════════════════════════════════════════
#  invoices and the QBO push
# ════════════════════════════════════════════════════════════════════════════

@app.route("/api/invoices")
@auth.require_login
def api_invoices():
    status = request.args.get("status", "")
    vendor = request.args.get("vendor_id", "")
    pushed = request.args.get("pushed", "")
    q = (request.args.get("q") or "").strip()

    sql = """SELECT i.*, v.name vendor_name, v.qbo_vendor_id,
                    d.filename doc_filename
             FROM invoices i
             LEFT JOIN vendors v   ON v.id=i.vendor_id
             LEFT JOIN documents d ON d.id=i.doc_id
             WHERE 1=1"""
    args = []
    if status:
        sql += " AND i.status=?"; args.append(status)
    if vendor:
        sql += " AND i.vendor_id=?"; args.append(vendor)
    if pushed == "0":
        sql += " AND i.qbo_bill_id IS NULL"
    elif pushed == "1":
        sql += " AND i.qbo_bill_id IS NOT NULL"
    if q:
        sql += " AND (i.invoice_number LIKE ? OR i.po_number LIKE ? "
        sql += " OR v.name LIKE ?)"
        args += [f"%{q}%"] * 3
    sql += " ORDER BY i.invoice_date DESC, i.id DESC LIMIT 500"

    con = db.get_db()
    try:
        rows = [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()
    return ok(invoices=rows)


@app.route("/api/invoices/<int:iid>", methods=["POST"])
@auth.require("edit")
def api_invoice_update(iid):
    d = body()
    allowed = ("invoice_number", "invoice_date", "due_date", "amount",
               "po_number", "description", "status", "currency", "confidence")
    fields = {k: d[k] for k in allowed if k in d}
    if not fields:
        return err("Nothing to change.")
    if "amount" in fields:
        fields["amount"] = num(fields["amount"])
    if "invoice_number" in fields:
        fields["invoice_norm"] = norm_invoice(fields["invoice_number"])

    con = db.get_db()
    try:
        sets = ", ".join(f"{k}=?" for k in fields)
        con.execute(f"UPDATE invoices SET {sets}, updated_at=? WHERE id=?",
                    (*fields.values(), db.utcnow(), iid))
        con.commit()
    finally:
        con.close()
    db.say("note", f"{me()} edited invoice #{iid}",
           ", ".join(f"{k} → {v}" for k, v in fields.items()), actor=me())
    return ok()


@app.route("/api/invoices/<int:iid>/push", methods=["POST"])
@auth.require("push")
def api_invoice_push(iid):
    d = body()
    res = billing.push_invoice(iid, actor=me(),
                               allow_low_confidence=bool(d.get("override")))
    if res.get("ok"):
        return ok(**res)
    return err(res.get("error"), 400, needs_override=res.get("needs_override"))


@app.route("/api/invoices/push-ready", methods=["POST"])
@auth.require("push")
def api_push_ready():
    return ok(**billing.push_ready(actor=me()))


# ════════════════════════════════════════════════════════════════════════════
#  statements
# ════════════════════════════════════════════════════════════════════════════

@app.route("/api/statements")
@auth.require_login
def api_statements():
    period = request.args.get("period") or ""
    sql = """SELECT s.*, v.name vendor_name, d.filename
             FROM statements s
             LEFT JOIN vendors v   ON v.id=s.vendor_id
             LEFT JOIN documents d ON d.id=s.doc_id WHERE 1=1"""
    args = []
    if period:
        sql += " AND s.period=?"; args.append(period)
    sql += " ORDER BY s.period DESC, v.name"

    con = db.get_db()
    try:
        rows = [dict(r) for r in con.execute(sql, args).fetchall()]
        for r in rows:
            r["lines"] = [dict(x) for x in con.execute(
                "SELECT * FROM statement_lines WHERE statement_id=? "
                "ORDER BY invoice_date", (r["id"],)).fetchall()]
        missing = [dict(x) for x in con.execute("""
            SELECT id, name, request_email, statement_email FROM vendors
            WHERE active=1 AND id NOT IN
              (SELECT vendor_id FROM statements WHERE period=?)
            ORDER BY name""", (period or current_period(),)).fetchall()]
    finally:
        con.close()
    return ok(statements=rows, missing=missing,
              period=period or current_period())


# ════════════════════════════════════════════════════════════════════════════
#  reconciliation
# ════════════════════════════════════════════════════════════════════════════

@app.route("/api/match/run", methods=["POST"])
@auth.require("edit")
def api_match_run():
    d = body()
    res = matching.run_match(period=d.get("period") or current_period(),
                             actor=me(), vendor_id=d.get("vendor_id"))
    return (ok(**res) if res.get("ok") else err(res.get("error")))


@app.route("/api/match/report")
@auth.require_login
def api_match_report():
    return jsonify(matching.report(request.args.get("period") or current_period(),
                                   vendor_id=request.args.get("vendor_id")))


@app.route("/api/match/plan")
@auth.require_login
def api_match_plan():
    return jsonify(matching.payment_plan(
        request.args.get("period") or current_period()))


@app.route("/api/match/row/<int:row_id>", methods=["POST"])
@auth.require("edit")
def api_match_row(row_id):
    d = body()
    res = matching.set_row_action(row_id, action=d.get("action", ""),
                                  resolved=d.get("resolved"),
                                  paid_ref=d.get("paid_ref", ""), actor=me())
    return (ok() if res.get("ok") else err(res.get("error")))


@app.route("/api/qbo/sync", methods=["POST"])
@auth.require("edit")
def api_qbo_sync():
    try:
        return ok(**qbo.sync_to_db(actor=me()))
    except Exception as e:
        return err(str(e))


# ════════════════════════════════════════════════════════════════════════════
#  reports
# ════════════════════════════════════════════════════════════════════════════

@app.route("/api/reports")
@auth.require_login
def api_reports():
    con = db.get_db()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM reports ORDER BY created_at DESC LIMIT 100").fetchall()]
    finally:
        con.close()
    return ok(reports=rows)


@app.route("/api/reports/build", methods=["POST"])
@auth.require_login
def api_report_build():
    d = body()
    try:
        res = reporting.build_workbook(period=d.get("period") or current_period(),
                                       created_by=me())
        return ok(**res)
    except Exception as e:
        log.exception("Report build failed")
        return err(str(e))


@app.route("/api/reports/<int:rid>/download")
@auth.require_login
def api_report_download(rid):
    con = db.get_db()
    try:
        r = con.execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
    finally:
        con.close()
    if not r or not os.path.exists(r["path"]):
        return err("That report file is no longer on disk.", 404)
    return send_file(r["path"], as_attachment=True, download_name=r["filename"])


@app.route("/api/reports/download-latest")
@auth.require_login
def api_report_latest():
    period = request.args.get("period") or current_period()
    res = reporting.build_workbook(period=period, created_by=me())
    return send_file(res["path"], as_attachment=True,
                     download_name=res["filename"])


# ════════════════════════════════════════════════════════════════════════════
#  agents and drafts
# ════════════════════════════════════════════════════════════════════════════

@app.route("/api/agents")
@auth.require_login
def api_agents():
    con = db.get_db()
    try:
        rows = [dict(r) for r in con.execute("""
            SELECT a.*, v.name vendor_name FROM agents a
            LEFT JOIN vendors v ON v.id=a.vendor_id ORDER BY a.name""").fetchall()]
        runs = [dict(r) for r in con.execute(
            "SELECT * FROM agent_runs ORDER BY started_at DESC LIMIT 50").fetchall()]
    finally:
        con.close()
    return ok(agents=rows, runs=runs, kinds=agents.AGENT_KINDS)


@app.route("/api/agents", methods=["POST"])
@auth.require("edit")
def api_agent_save():
    d = body()
    aid = d.get("id")
    name = (d.get("name") or "").strip()
    kind = d.get("kind")
    if not name:
        return err("An agent needs a name.")
    if kind not in agents.AGENT_KINDS:
        return err(f"I do not know how to run a '{kind}' agent. "
                   f"Pick one of: {', '.join(agents.AGENT_KINDS)}.")

    cron = (d.get("cron") or "").strip()
    skind = d.get("schedule_kind") or "cron"
    if skind == "cron" and cron and len(cron.split()) != 5:
        return err("A cron schedule needs five fields: minute hour day month "
                   "weekday. For example '0 8 2 * *' is 08:00 on the 2nd.")

    fields = {
        "name": name, "kind": kind,
        "description": d.get("description") or agents.AGENT_KINDS[kind],
        "enabled": 1 if str(d.get("enabled")).lower() in ("1", "true", "yes", "on") else 0,
        "schedule_kind": skind, "cron": cron or None,
        "interval_mins": d.get("interval_mins") or None,
        "vendor_id": d.get("vendor_id") or None,
        "params_json": json.dumps(d.get("params") or {}),
        "requires_approval": 1 if d.get("requires_approval", True) else 0,
    }

    con = db.get_db()
    try:
        if aid:
            sets = ", ".join(f"{k}=?" for k in fields)
            con.execute(f"UPDATE agents SET {sets} WHERE id=?",
                        (*fields.values(), aid))
        else:
            cols, qs = ", ".join(fields), ", ".join("?" * len(fields))
            cur = con.execute(
                f"INSERT INTO agents ({cols}, created_at, created_by) "
                f"VALUES ({qs}, ?, ?)", (*fields.values(), db.utcnow(), me()))
            aid = cur.lastrowid
        con.commit()
    finally:
        con.close()
    agents.reload_schedule(app)
    db.say("agent", f"{me()} saved the agent “{name}”",
           f"{kind}, {'enabled' if fields['enabled'] else 'disabled'}"
           + (f", cron {cron}" if cron else ""), actor=me())
    return ok(id=aid)


@app.route("/api/agents/<int:aid>", methods=["DELETE"])
@auth.require("admin")
def api_agent_delete(aid):
    con = db.get_db()
    try:
        con.execute("DELETE FROM agents WHERE id=?", (aid,))
        con.commit()
    finally:
        con.close()
    agents.reload_schedule(app)
    return ok()


@app.route("/api/agents/<int:aid>/run", methods=["POST"])
@auth.require("edit")
def api_agent_run(aid):
    res = agents.run_agent(aid, trigger="manual", actor=me())
    return (ok(**res) if res.get("ok")
            else err(res.get("error") or "The agent failed.", 400, **res))


@app.route("/api/agents/<int:aid>/toggle", methods=["POST"])
@auth.require("edit")
def api_agent_toggle(aid):
    con = db.get_db()
    try:
        r = con.execute("SELECT enabled, name FROM agents WHERE id=?",
                        (aid,)).fetchone()
        if not r:
            return err("No such agent", 404)
        new = 0 if r["enabled"] else 1
        con.execute("UPDATE agents SET enabled=? WHERE id=?", (new, aid))
        con.commit()
    finally:
        con.close()
    agents.reload_schedule(app)
    db.say("agent", f"{'Enabled' if new else 'Disabled'} “{r['name']}”",
           actor=me())
    return ok(enabled=new)


@app.route("/api/drafts")
@auth.require_login
def api_drafts():
    status = request.args.get("status", "pending")
    con = db.get_db()
    try:
        sql = """SELECT d.*, v.name vendor_name FROM drafts d
                 LEFT JOIN vendors v ON v.id=d.vendor_id"""
        args = []
        if status and status != "all":
            sql += " WHERE d.status=?"; args.append(status)
        sql += " ORDER BY d.created_at DESC LIMIT 200"
        rows = [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()
    return ok(drafts=rows)


@app.route("/api/drafts/<int:did>", methods=["POST"])
@auth.require_login
def api_draft_act(did):
    d = body()
    action = d.get("action")

    if action == "update":
        fields = {k: d[k] for k in ("to_email", "cc_email", "subject", "body")
                  if k in d}
        if not fields:
            return err("Nothing to change.")
        con = db.get_db()
        try:
            sets = ", ".join(f"{k}=?" for k in fields)
            con.execute(f"UPDATE drafts SET {sets} WHERE id=? AND status='pending'",
                        (*fields.values(), did))
            con.commit()
        finally:
            con.close()
        return ok()

    if action == "discard":
        con = db.get_db()
        try:
            con.execute("UPDATE drafts SET status='discarded' WHERE id=?", (did,))
            con.commit()
        finally:
            con.close()
        db.say("email", f"{me()} discarded draft #{did}", actor=me())
        return ok()

    if action == "send":
        if not auth.can("approve"):
            return err("Your role cannot approve and send email.", 403)
        res = agents.send_draft(did, actor=me())
        return (ok() if res.get("ok") else err(res.get("error")))

    return err("Tell me what to do with it: update, discard or send.")


# ════════════════════════════════════════════════════════════════════════════
#  activity
# ════════════════════════════════════════════════════════════════════════════

@app.route("/api/activity")
@auth.require_login
def api_activity():
    kind = request.args.get("kind", "")
    limit = min(int(request.args.get("limit", 200)), 1000)
    con = db.get_db()
    try:
        sql = """SELECT a.*, v.name vendor_name FROM activity a
                 LEFT JOIN vendors v ON v.id=a.vendor_id"""
        args = []
        if kind:
            sql += " WHERE a.kind=?"; args.append(kind)
        sql += " ORDER BY a.at DESC LIMIT ?"; args.append(limit)
        rows = [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()
    return ok(activity=rows)


# ════════════════════════════════════════════════════════════════════════════
#  settings and integrations
# ════════════════════════════════════════════════════════════════════════════

@app.route("/api/settings")
@auth.require_login
def api_settings():
    """Integration config. Secrets come back masked, never in the clear."""
    stored = db.all_settings()
    out = {}
    for name, spec in config.INTEGRATIONS.items():
        fields = []
        for key, label, secret, hint, required in spec["fields"]:
            raw = stored.get(key) or config.env_fallback(key)
            fields.append({
                "key": key, "label": label, "secret": bool(secret),
                "hint": hint, "required": bool(required),
                "set": bool(raw),
                "value": (config.mask(raw) if secret else (raw or "")),
                "from_env": bool(not stored.get(key) and config.env_fallback(key)),
            })
        out[name] = {"label": spec["label"], "blurb": spec["blurb"],
                     "docs": spec["docs"], "fields": fields}

    general = {k: stored.get(k, v) for k, v in config.DEFAULTS.items()
               if k not in ("qbo_sandbox",)}
    return ok(integrations=out, general=general,
              encryption=config.ENCRYPTION_AVAILABLE,
              demo_mode=db.demo_mode())


@app.route("/api/settings", methods=["POST"])
@auth.require("admin")
def api_settings_save():
    d = body()
    changed = []
    for key, value in (d.get("settings") or {}).items():
        # An unchanged secret arrives as its mask. Writing that back would
        # replace the real credential with bullets.
        if config.is_secret(key) and str(value).startswith("•"):
            continue
        db.set_setting(key, value, who=me())
        changed.append(key)
    if changed:
        db.say("settings", f"{me()} updated settings",
               ", ".join(sorted(changed)), actor=me())
    return ok(changed=changed)


@app.route("/api/settings/test/<name>", methods=["POST"])
@auth.require("admin")
def api_settings_test(name):
    clients = {
        "graph": graph.get_client,
        "qbo": qbo.get_client,
        "servicetitan": servicetitan.get_client,
        "claude": claude_api.get_client,
    }
    if name not in clients:
        return err(f"I do not know an integration called '{name}'.")
    try:
        res = clients[name]().test()
    except Exception as e:
        res = {"ok": False, "detail": str(e)}
    db.say("settings", f"Tested {config.INTEGRATIONS[name]['label']}",
           res.get("detail", ""), ok=res.get("ok"), actor=me())
    return jsonify(res)


@app.route("/api/qbo/connect")
@auth.require("admin")
def api_qbo_connect():
    redirect_uri = (db.get_setting("qbo_redirect_uri")
                    or request.url_root.rstrip("/") + "/api/qbo/callback")
    client = qbo.QBOClient()
    if not client.configured():
        return err("Add the QuickBooks client ID, secret and realm first.")
    return redirect(client.authorize_url(redirect_uri))


@app.route("/api/qbo/callback")
@auth.require("admin")
def api_qbo_callback():
    code = request.args.get("code")
    realm = request.args.get("realmId")
    if not code:
        return err("QuickBooks did not send a code back.")
    redirect_uri = (db.get_setting("qbo_redirect_uri")
                    or request.url_root.rstrip("/") + "/api/qbo/callback")
    if realm:
        db.set_setting("qbo_realm_id", realm, who=me())
    try:
        qbo.QBOClient().exchange_code(code, redirect_uri)
    except Exception as e:
        return err(str(e))
    return redirect("/settings?connected=qbo")


@app.route("/api/demo/seed", methods=["POST"])
@auth.require("admin")
def api_demo_seed():
    from demo import seed as demo_seed
    try:
        return ok(**demo_seed.seed(actor=me()))
    except Exception as e:
        log.exception("Demo seed failed")
        return err(str(e))


@app.route("/api/demo/wipe", methods=["POST"])
@auth.require("admin")
def api_demo_wipe():
    from demo import seed as demo_seed
    demo_seed.wipe()
    db.say("note", f"{me()} cleared all operational data", actor=me(), ok=False)
    return ok()


@app.route("/api/me")
@auth.require_login
def api_me():
    u = auth.current_user()
    return ok(user={k: u[k] for k in ("id", "email", "name", "role")},
              permissions=sorted(auth.ROLES.get(u["role"], set())))


@app.route("/api/me/password", methods=["POST"])
@auth.require_login
def api_change_password():
    d = body()
    new = d.get("new_password") or ""
    if len(new) < 8:
        return err("Use at least 8 characters.")
    u = auth.current_user()
    from werkzeug.security import check_password_hash
    if not check_password_hash(u["password_hash"], d.get("current_password") or ""):
        return err("That is not your current password.")
    auth.set_password(u["id"], new, actor=me())
    return ok()


@app.route("/api/users")
@auth.require("admin")
def api_users():
    con = db.get_db()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT id,email,name,role,active,created_at,last_login "
            "FROM users ORDER BY email").fetchall()]
    finally:
        con.close()
    return ok(users=rows)


@app.route("/api/users", methods=["POST"])
@auth.require("admin")
def api_user_create():
    d = body()
    if len(d.get("password") or "") < 8:
        return err("Give them a password of at least 8 characters.")
    uid, error = auth.create_user(d.get("email", ""), d.get("name", ""),
                                  d["password"], d.get("role", "clerk"),
                                  actor=me())
    return (ok(id=uid) if uid else err(error))


@app.route("/healthz")
def healthz():
    """Health check for Render and anything else watching the service.

    Deliberately touches the database. A process that is up but cannot reach
    its own storage is not healthy, and on a platform with an ephemeral
    filesystem that is exactly the failure worth catching — it means the disk
    did not mount and every write is about to vanish on the next deploy.

    No auth: a health check that needs a session is not a health check.
    """
    try:
        con = db.get_db()
        try:
            con.execute("SELECT 1 FROM settings LIMIT 1").fetchone()
        finally:
            con.close()
    except Exception as e:
        log.error("Health check failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 503

    storage_writable = os.access(config.STORAGE, os.W_OK)
    return jsonify({
        "ok": True,
        "demo_mode": db.demo_mode(),
        "storage_writable": storage_writable,
        "encryption": config.ENCRYPTION_AVAILABLE,
        "db": config.DB_PATH,
    }), (200 if storage_writable else 503)


@app.errorhandler(413)
def too_large(e):
    return err(f"That file is larger than the {config.MAX_UPLOAD_MB} MB limit.",
               413)


@app.errorhandler(500)
def server_error(e):
    log.exception("Unhandled error")
    return err("Something broke on the server. The detail is in the log.", 500)


if __name__ == "__main__":
    agents.start_scheduler(app)
    port = int(os.getenv("PORT", "5100"))
    log.info("InvoiceHub on http://localhost:%s  (demo mode: %s)",
             port, "ON" if db.demo_mode() else "off")
    app.run(host="0.0.0.0", port=port, debug=False)
