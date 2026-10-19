"""
Automated agents.

An agent is a named job with a schedule and a plain-English description of what
it does. They live in the database rather than in code so an AP clerk can add
"ask Company ABC for a statement on the 2nd" without a deploy.

The rule that matters: **an agent never sends email.** Agents that would
communicate with a vendor write a draft and stop. A person opens the Drafts
queue, reads it, and presses send. This is not timidity — an AP agent that can
email suppliers unsupervised will eventually chase a vendor for an invoice you
already paid, and you will hear about it from your supplier rather than from
your software.

Everything an agent does is written to the activity log in the first person,
so the portal reads as a colleague's work you can audit rather than a cron job
you have to trust.
"""

import json
import logging
import traceback
from datetime import datetime, timezone, timedelta

import db
from core.util import current_period, parse_date

log = logging.getLogger(__name__)


AGENT_KINDS = {
    "scan_mailbox":     "Sweep the AP mailbox and file attachments",
    "parse_pending":    "Read whatever is sitting unparsed",
    "sync_qbo":         "Refresh the QuickBooks mirror",
    "push_bills":       "Create QuickBooks bills for approved invoices",
    "run_match":        "Reconcile statements, invoices and QuickBooks",
    "request_statement":"Draft statement requests to vendors",
    "request_invoice":  "Draft requests for invoices we never received",
    "chase_exceptions": "Draft queries about mismatches and disputes",
    "report":           "Build the reconciliation workbook",
}


# ── drafting ─────────────────────────────────────────────────────────────────

def make_draft(vendor_id, kind, subject, body, reason="", period="",
               to_email="", cc_email="", created_by="agent") -> int:
    """Queue an email for a human to approve. Returns the draft id."""
    con = db.get_db()
    try:
        v = con.execute("SELECT * FROM vendors WHERE id=?", (vendor_id,)).fetchone()
        vend = dict(v) if v else {}
        to = (to_email or vend.get("request_email")
              or vend.get("statement_email") or vend.get("invoice_email") or "")
        cur = con.execute("""INSERT INTO drafts
            (created_at, created_by, vendor_id, kind, period, to_email, cc_email,
             subject, body, reason, status)
            VALUES (?,?,?,?,?,?,?,?,?,?, 'pending')""",
            (db.utcnow(), created_by, vendor_id, kind, period, to,
             cc_email or vend.get("cc_emails") or "", subject, body, reason))
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def send_draft(draft_id: int, actor: str) -> dict:
    """Send an approved draft. The only path to outbound email in the portal."""
    from integrations import graph

    con = db.get_db()
    try:
        row = con.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
    finally:
        con.close()
    if not row:
        return {"ok": False, "error": "No such draft"}
    d = dict(row)
    if d["status"] == "sent":
        return {"ok": False, "error": "That draft has already been sent"}
    if not d.get("to_email"):
        return {"ok": False, "error":
                "No address to send to. Set a request email on the vendor, or "
                "type one on the draft."}

    try:
        graph.get_client().send_mail(
            to_email=d["to_email"], subject=d["subject"], body=d["body"],
            cc=d.get("cc_email") or "")
    except Exception as e:
        con = db.get_db()
        try:
            con.execute("UPDATE drafts SET error=? WHERE id=?",
                        (str(e)[:1000], draft_id))
            con.commit()
        finally:
            con.close()
        db.say("email", f"Could not send to {d['to_email']}", str(e),
               vendor_id=d.get("vendor_id"), ok=False, actor=actor)
        return {"ok": False, "error": str(e)}

    con = db.get_db()
    try:
        con.execute("""UPDATE drafts SET status='sent', sent_at=?, sent_by=?,
                       approved_by=?, error=NULL WHERE id=?""",
                    (db.utcnow(), actor, actor, draft_id))
        con.commit()
    finally:
        con.close()
    db.say("email", f"Sent “{d['subject']}” to {d['to_email']}",
           f"Approved and sent by {actor}.", vendor_id=d.get("vendor_id"),
           actor=actor)
    return {"ok": True}


# ── the agent bodies ─────────────────────────────────────────────────────────

def _agent_scan_mailbox(params, actor):
    from core import intake
    res = intake.scan_mailbox(days_back=params.get("days_back"), actor=actor)
    if not res.get("ok"):
        raise RuntimeError(res.get("error", "scan failed"))
    return res.get("filed", 0), res.get("detail", "")


def _agent_parse_pending(params, actor):
    from core import parsing
    res = parsing.parse_pending(limit=int(params.get("limit", 25)), actor=actor)
    return res["parsed"], (f"{res['parsed']} parsed, {res['failed']} failed "
                           f"of {res['considered']} considered")


def _agent_sync_qbo(params, actor):
    from integrations import qbo
    res = qbo.sync_to_db(actor=actor)
    return res["bills"], f"{res['bills']} bill(s), {res['payments']} payment link(s)"


def _agent_push_bills(params, actor):
    from core import billing
    res = billing.push_ready(actor=actor, limit=int(params.get("limit", 50)))
    detail = f"{res['pushed']} pushed, {res['failed']} refused"
    if res.get("errors"):
        detail += ". " + "; ".join(e[:120] for e in res["errors"][:3])
    return res["pushed"], detail


def _agent_run_match(params, actor):
    from core import matching
    res = matching.run_match(period=params.get("period") or current_period(),
                             actor=actor)
    if not res.get("ok"):
        raise RuntimeError(res.get("error", "match failed"))
    return res["rows"], (f"{res['matched']} matched, {res['exceptions']} "
                         f"exceptions, {res['to_pay']:,.2f} to pay")


def _agent_report(params, actor):
    from core import reporting
    res = reporting.build_workbook(period=params.get("period") or current_period(),
                                   created_by=actor)
    return res["vendor_tabs"], f"{res['filename']} ({res['vendor_tabs']} vendor tabs)"


def _agent_request_statement(params, actor):
    """Draft a statement request to every vendor due one."""
    period = params.get("period") or current_period()
    only = params.get("vendor_id")
    company = db.get_setting("company_name", "our team")

    con = db.get_db()
    try:
        q = """SELECT v.* FROM vendors v
               WHERE v.active=1 AND v.auto_request_statement=1
                 AND v.statement_cadence <> 'none'"""
        args = []
        if only:
            q += " AND v.id=?"
            args.append(only)
        vendors = [dict(r) for r in con.execute(q, args).fetchall()]
        have = {r["vendor_id"] for r in con.execute(
            "SELECT DISTINCT vendor_id FROM statements WHERE period=?",
            (period,)).fetchall()}
        pending = {r["vendor_id"] for r in con.execute(
            """SELECT DISTINCT vendor_id FROM drafts
               WHERE kind='request_statement' AND period=?
                 AND status IN ('pending','approved','sent')""",
            (period,)).fetchall()}
    finally:
        con.close()

    made = []
    for v in vendors:
        if v["id"] in have:
            continue            # they already sent one
        if v["id"] in pending:
            continue            # we already asked; do not nag twice
        to = v.get("request_email") or v.get("statement_email")
        if not to:
            db.say("agent", f"No address to ask {v['name']} for a statement",
                   "Set a request email on the vendor record.",
                   vendor_id=v["id"], ok=False, actor=actor)
            continue

        subject = f"Statement of account request — {period}"
        body = (
            f"Hello,\n\n"
            f"Could you please send us a statement of account for {period}?\n\n"
            f"We are reconciling our payables for the period and want to make "
            f"sure nothing of yours is sitting unpaid on our side."
            + (f" Our account number with you is {v['account_number']}.\n\n"
               if v.get("account_number") else "\n\n")
            + f"Thank you,\n{company} — Accounts Payable\n")
        make_draft(v["id"], "request_statement", subject, body,
                   reason=(f"No {period} statement from {v['name']} yet, and "
                           f"they are set to monthly."),
                   period=period, to_email=to, created_by=actor)
        made.append(v["name"])

    detail = (f"Drafted {len(made)} statement request(s): "
              + ", ".join(made[:8]) + ("…" if len(made) > 8 else "")
              ) if made else "Every vendor has already sent a statement, or has been asked."
    return len(made), detail


def _agent_request_invoice(params, actor):
    """Where a statement shows an invoice we never received, ask for a copy."""
    period = params.get("period") or current_period()
    company = db.get_setting("company_name", "our team")

    con = db.get_db()
    try:
        rows = [dict(r) for r in con.execute("""
            SELECT m.*, v.name vendor_name, v.request_email, v.statement_email,
                   v.account_number
            FROM match_rows m JOIN vendors v ON v.id = m.vendor_id
            WHERE m.period=? AND m.verdict IN ('missing_invoice','statement_only')
              AND m.resolved=0
            ORDER BY v.name, m.invoice_number""", (period,)).fetchall()]
        already = {(r["vendor_id"], r["period"]) for r in con.execute(
            """SELECT vendor_id, period FROM drafts
               WHERE kind='request_invoice' AND period=?
                 AND status IN ('pending','approved','sent')""",
            (period,)).fetchall()}
    finally:
        con.close()

    by_vendor = {}
    for r in rows:
        by_vendor.setdefault(r["vendor_id"], []).append(r)

    made = 0
    for vid, items in by_vendor.items():
        if (vid, period) in already:
            continue
        v0 = items[0]
        to = v0.get("request_email") or v0.get("statement_email")
        if not to:
            continue
        lines = "\n".join(
            f"  · {i['invoice_number']}"
            + (f"   dated {i['invoice_date']}" if i.get("invoice_date") else "")
            + (f"   {i['amt_statement']:,.2f}"
               if i.get("amt_statement") is not None else "")
            for i in items[:40])
        subject = f"Copy invoices requested — {period}"
        body = (
            f"Hello,\n\n"
            f"Your {period} statement lists the following invoices, which we "
            f"do not appear to have received:\n\n{lines}\n\n"
            f"Could you send copies so we can get them entered and paid? "
            f"We are holding payment on these until we have the documents."
            + (f"\n\nOur account number is {v0['account_number']}."
               if v0.get("account_number") else "")
            + f"\n\nThank you,\n{company} — Accounts Payable\n")
        make_draft(vid, "request_invoice", subject, body,
                   reason=(f"{len(items)} invoice(s) on the {period} statement "
                           f"from {v0['vendor_name']} that we never received."),
                   period=period, to_email=to, created_by=actor)
        made += 1

    return made, (f"Drafted {made} request(s) for missing invoices"
                  if made else "No missing invoices to chase.")


def _agent_chase_exceptions(params, actor):
    """Draft a query for every amount that does not agree."""
    period = params.get("period") or current_period()
    company = db.get_setting("company_name", "our team")

    con = db.get_db()
    try:
        rows = [dict(r) for r in con.execute("""
            SELECT m.*, v.name vendor_name, v.request_email, v.statement_email
            FROM match_rows m JOIN vendors v ON v.id = m.vendor_id
            WHERE m.period=? AND m.verdict='amount_mismatch' AND m.resolved=0
            ORDER BY v.name""", (period,)).fetchall()]
        already = {r["vendor_id"] for r in con.execute(
            """SELECT DISTINCT vendor_id FROM drafts
               WHERE kind='query' AND period=? AND status IN
                     ('pending','approved','sent')""", (period,)).fetchall()}
    finally:
        con.close()

    by_vendor = {}
    for r in rows:
        by_vendor.setdefault(r["vendor_id"], []).append(r)

    made = 0
    for vid, items in by_vendor.items():
        if vid in already:
            continue
        v0 = items[0]
        to = v0.get("request_email") or v0.get("statement_email")
        if not to:
            continue
        lines = "\n".join(
            f"  · {i['invoice_number']}: your statement shows "
            f"{_fmt(i.get('amt_statement'))}, our records show "
            f"{_fmt(i.get('amt_qbo') if i.get('amt_qbo') is not None else i.get('amt_invoice'))}"
            for i in items[:30])
        subject = f"Query on {period} statement — amounts do not agree"
        body = (f"Hello,\n\n"
                f"Reconciling your {period} statement, these lines do not "
                f"agree with our records:\n\n{lines}\n\n"
                f"Could you check these and let us know which is right? We are "
                f"holding payment on these lines only — the rest of the "
                f"statement is being paid as normal.\n\n"
                f"Thank you,\n{company} — Accounts Payable\n")
        make_draft(vid, "query", subject, body,
                   reason=(f"{len(items)} amount mismatch(es) with "
                           f"{v0['vendor_name']} on the {period} statement."),
                   period=period, to_email=to, created_by=actor)
        made += 1

    return made, (f"Drafted {made} query email(s)"
                  if made else "No mismatches to query.")


def _fmt(v):
    return "—" if v is None else f"{v:,.2f}"


RUNNERS = {
    "scan_mailbox":      _agent_scan_mailbox,
    "parse_pending":     _agent_parse_pending,
    "sync_qbo":          _agent_sync_qbo,
    "push_bills":        _agent_push_bills,
    "run_match":         _agent_run_match,
    "report":            _agent_report,
    "request_statement": _agent_request_statement,
    "request_invoice":   _agent_request_invoice,
    "chase_exceptions":  _agent_chase_exceptions,
}


# ── running one ──────────────────────────────────────────────────────────────

def run_agent(agent_id: int, trigger: str = "manual", actor: str = "") -> dict:
    con = db.get_db()
    try:
        row = con.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
    finally:
        con.close()
    if not row:
        return {"ok": False, "error": "No such agent"}
    a = dict(row)

    runner = RUNNERS.get(a["kind"])
    if not runner:
        return {"ok": False, "error": f"I do not know how to run a "
                                      f"'{a['kind']}' agent"}

    who = actor or f"agent:{a['name']}"
    started = db.utcnow()
    con = db.get_db()
    try:
        cur = con.execute("""INSERT INTO agent_runs
            (agent_id, agent_name, started_at, status, trigger)
            VALUES (?,?,?, 'running', ?)""", (agent_id, a["name"], started, trigger))
        run_id = cur.lastrowid
        con.commit()
    finally:
        con.close()

    params = db.jload(a.get("params_json"), {}) or {}
    if a.get("vendor_id"):
        params.setdefault("vendor_id", a["vendor_id"])

    try:
        items, detail = runner(params, who)
        status, error = "ok", None
    except Exception as e:
        log.exception("Agent %s failed", a["name"])
        items, detail, status = 0, "", "failed"
        error = f"{e}\n{traceback.format_exc()[-1200:]}"
        db.say("agent", f"{a['name']} failed", str(e), ok=False, actor=who)

    finished = db.utcnow()
    con = db.get_db()
    try:
        con.execute("""UPDATE agent_runs SET finished_at=?, status=?, summary=?,
                       detail=?, items=?, error=? WHERE id=?""",
                    (finished, status, (detail or "")[:400], detail, items,
                     error, run_id))
        con.execute("""UPDATE agents SET last_run_at=?, last_status=?,
                       last_summary=?, run_count=run_count+1 WHERE id=?""",
                    (finished, status, (detail or "")[:400], agent_id))
        con.commit()
    finally:
        con.close()

    if status == "ok":
        db.say("agent", f"{a['name']} ran", detail or "nothing to do",
               actor=who)
    return {"ok": status == "ok", "agent": a["name"], "items": items,
            "detail": detail, "error": error, "run_id": run_id}


# ── the scheduler ────────────────────────────────────────────────────────────

_scheduler = None


def start_scheduler(app=None):
    """Wire enabled agents into APScheduler.

    Jobs are rebuilt from the database rather than declared in code, so
    toggling an agent in the UI takes effect on the next reload without a
    restart. `reload_schedule` is called whenever an agent is edited.
    """
    global _scheduler
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger
    except ImportError:
        log.warning("APScheduler is not installed — agents will only run when "
                    "you press Run. Fix: pip install APScheduler")
        return None

    if _scheduler:
        return _scheduler
    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.start()
    reload_schedule(app)
    log.info("Agent scheduler started")
    return _scheduler


def reload_schedule(app=None):
    """Rebuild every scheduled job from the agents table."""
    if not _scheduler:
        return
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    for job in _scheduler.get_jobs():
        job.remove()

    con = db.get_db()
    try:
        agents = [dict(r) for r in con.execute(
            "SELECT * FROM agents WHERE enabled=1").fetchall()]
    finally:
        con.close()

    for a in agents:
        trigger = None
        try:
            if a["schedule_kind"] == "cron" and a.get("cron"):
                parts = a["cron"].split()
                if len(parts) == 5:
                    trigger = CronTrigger(minute=parts[0], hour=parts[1],
                                          day=parts[2], month=parts[3],
                                          day_of_week=parts[4], timezone="UTC")
            elif a["schedule_kind"] == "interval" and a.get("interval_mins"):
                trigger = IntervalTrigger(minutes=int(a["interval_mins"]))
        except Exception as e:
            log.warning("Agent %s has an unusable schedule (%s): %s",
                        a["name"], a.get("cron"), e)
            continue
        if not trigger:
            continue

        def make_job(agent_id, flask_app):
            def job():
                # A scheduled run needs the app context for anything that
                # touches config or url building.
                if flask_app:
                    with flask_app.app_context():
                        run_agent(agent_id, trigger="schedule")
                else:
                    run_agent(agent_id, trigger="schedule")
            return job

        _scheduler.add_job(make_job(a["id"], app), trigger,
                           id=f"agent-{a['id']}", replace_existing=True,
                           max_instances=1, coalesce=True)

    # Record when each will next fire, so the UI can show it.
    con = db.get_db()
    try:
        for job in _scheduler.get_jobs():
            try:
                aid = int(job.id.split("-")[1])
            except (IndexError, ValueError):
                continue
            nxt = job.next_run_time.isoformat() if job.next_run_time else None
            con.execute("UPDATE agents SET next_run_at=? WHERE id=?", (nxt, aid))
        con.commit()
    finally:
        con.close()
    log.info("Scheduled %d agent(s)", len(_scheduler.get_jobs()))
