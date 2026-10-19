"""
Getting documents in: the mailbox sweep, and manual upload.

The sweep is idempotent by construction. Every attachment is hashed and the
hash is unique in the documents table, so a vendor resending an invoice, a
reply-all on a thread, or the sweep overlapping its own window all produce the
same single document. That one constraint removes the entire class of
duplicate-invoice bugs that plague mailbox-driven AP.

Anything the sweep cannot place against a vendor is still filed — as unmatched,
for a human to assign. Dropping it would be worse: an invoice nobody can see is
an invoice that gets paid twice later, on a chaser.
"""

import os
import hashlib
import logging
import threading
from datetime import datetime, timezone

import db
import config
from core.util import (safe_filename, classify_document, match_vendor_by_email,
                       match_vendor_name)
from integrations import graph

log = logging.getLogger(__name__)

SCAN_STATE = {"running": False, "detail": "", "started": None, "finished": None,
              "filed": 0, "skipped": 0, "unmatched": 0, "error": None}

_SCAN_LOCK = threading.Lock()


def vendor_dir(vendor_name: str, kind: str) -> str:
    safe = safe_filename(vendor_name or "_unmatched") or "_unmatched"
    sub = {"statement": "statements", "credit": "credits",
           "remittance": "remittances"}.get(kind, "invoices")
    path = os.path.join(config.STORAGE, safe, sub)
    os.makedirs(path, exist_ok=True)
    return path


def store_file(raw: bytes, filename: str, vendor_name: str, kind: str):
    """Write bytes to the vendor's folder, avoiding a name collision."""
    folder = vendor_dir(vendor_name, kind)
    base = safe_filename(filename)
    path = os.path.join(folder, base)
    stem, ext = os.path.splitext(base)
    n = 1
    while os.path.exists(path):
        path = os.path.join(folder, f"{stem}_{n}{ext}")
        n += 1
    with open(path, "wb") as fh:
        fh.write(raw)
    return path


def record_document(raw: bytes, filename: str, *, vendor=None, kind="unknown",
                    source="upload", message_id="", from_email="", subject="",
                    received_at="", mime=""):
    """Insert one document. Returns (doc_id, created).

    created is False when this exact file is already on record — the caller
    uses that to report 'skipped' rather than silently treating it as new.
    """
    sha = hashlib.sha256(raw).hexdigest()
    con = db.get_db()
    try:
        existing = con.execute("SELECT id FROM documents WHERE sha256=?",
                               (sha,)).fetchone()
        if existing:
            return existing["id"], False
    finally:
        con.close()

    vendor_name = (vendor or {}).get("name") if vendor else None
    path = store_file(raw, filename, vendor_name, kind)

    con = db.get_db()
    try:
        cur = con.execute("""
            INSERT INTO documents
              (vendor_id, kind, source, filename, path, mime, size_bytes, sha256,
               message_id, from_email, subject, received_at, stored_at,
               parse_status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending')
        """, ((vendor or {}).get("id"), kind, source, filename, path, mime,
              len(raw), sha, message_id, from_email, subject,
              received_at or db.utcnow(), db.utcnow()))
        con.commit()
        return cur.lastrowid, True
    finally:
        con.close()


def scan_mailbox(days_back=None, actor="system") -> dict:
    """Sweep the AP mailbox and file every attachment."""
    with _SCAN_LOCK:
        if SCAN_STATE["running"]:
            return {"ok": False, "error": "A scan is already running"}
        SCAN_STATE.update(running=True, error=None, filed=0, skipped=0,
                          unmatched=0, started=db.utcnow(), finished=None,
                          detail="opening the mailbox")

    filed = skipped = unmatched = 0
    try:
        client = graph.get_client()
        if not client.configured():
            raise RuntimeError("Microsoft Graph is not configured — add the "
                               "mailbox details in Settings.")

        try:
            back = int(days_back if days_back is not None
                       else (db.get_setting("scan_days") or 7))
        except (TypeError, ValueError):
            back = 7

        con = db.get_db()
        try:
            vendors = [dict(r) for r in con.execute(
                "SELECT * FROM vendors WHERE active=1").fetchall()]
        finally:
            con.close()

        SCAN_STATE["detail"] = f"listing the last {back} day(s)"
        messages = client.list_messages(days_back=back)
        SCAN_STATE["detail"] = f"{len(messages)} message(s) to look at"

        for i, msg in enumerate(messages, 1):
            SCAN_STATE["detail"] = f"message {i} of {len(messages)}"
            frm = (((msg.get("from") or {}).get("emailAddress") or {})
                   .get("address") or "")
            subject = msg.get("subject") or ""
            received = msg.get("receivedDateTime") or ""
            vendor = match_vendor_by_email(frm, subject, vendors)

            try:
                attachments = client.list_attachments(msg["id"])
            except Exception as e:
                db.say("scan", f"Could not open the attachments on “{subject[:60]}”",
                       str(e), ok=False, actor=actor)
                continue

            for att in attachments:
                name = att.get("name") or "attachment"
                ext = os.path.splitext(name)[1].lower()
                if ext not in graph.ATTACHMENT_EXT:
                    continue
                raw = client.attachment_bytes(att)
                if not raw:
                    # A zero-byte or inline attachment. Worth saying so: a
                    # vendor whose PDF never arrives looks identical to a
                    # vendor who never sent one unless this is logged.
                    db.say("scan", f"Empty attachment “{name}” from {frm or 'unknown'}",
                           f"Subject: {subject[:120]}", ok=False, actor=actor)
                    skipped += 1
                    continue

                kind = classify_document(vendor, subject, name)
                doc_id, created = record_document(
                    raw, name, vendor=vendor, kind=kind, source="mailbox",
                    message_id=msg.get("internetMessageId") or msg.get("id"),
                    from_email=frm, subject=subject, received_at=received,
                    mime=att.get("contentType") or "")
                if created:
                    filed += 1
                    if not vendor:
                        unmatched += 1
                else:
                    skipped += 1
                SCAN_STATE.update(filed=filed, skipped=skipped, unmatched=unmatched)

        detail = (f"{filed} new document(s) filed, {skipped} already on record"
                  + (f", {unmatched} could not be placed against a vendor"
                     if unmatched else ""))
        db.say("scan", f"Swept the AP mailbox ({back} day window)", detail,
               ok=(unmatched == 0), actor=actor)
        SCAN_STATE.update(running=False, finished=db.utcnow(), detail=detail)
        return {"ok": True, "filed": filed, "skipped": skipped,
                "unmatched": unmatched, "detail": detail}

    except Exception as e:
        SCAN_STATE.update(running=False, finished=db.utcnow(), error=str(e),
                          detail="failed")
        db.say("scan", "The mailbox sweep failed", str(e), ok=False, actor=actor)
        return {"ok": False, "error": str(e)}


def assign_document(doc_id: int, vendor_id: int, kind: str = "", actor="") -> dict:
    """Place an unmatched document against a vendor, and move the file."""
    con = db.get_db()
    try:
        doc = con.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        if not doc:
            return {"ok": False, "error": "No such document"}
        doc = dict(doc)
        vend = con.execute("SELECT * FROM vendors WHERE id=?",
                           (vendor_id,)).fetchone()
        if not vend:
            return {"ok": False, "error": "No such vendor"}
        vend = dict(vend)
    finally:
        con.close()

    new_kind = kind or doc.get("kind") or "unknown"
    new_path = doc["path"]
    if doc.get("path") and os.path.exists(doc["path"]):
        try:
            with open(doc["path"], "rb") as fh:
                raw = fh.read()
            new_path = store_file(raw, doc["filename"], vend["name"], new_kind)
            os.remove(doc["path"])
        except Exception as e:
            log.warning("Could not move %s: %s", doc["path"], e)

    con = db.get_db()
    try:
        con.execute("""UPDATE documents SET vendor_id=?, kind=?, path=?,
                       parse_status=CASE WHEN parse_status='ok'
                                         THEN 'pending' ELSE parse_status END
                       WHERE id=?""",
                    (vendor_id, new_kind, new_path, doc_id))
        con.commit()
    finally:
        con.close()

    db.say("file", f"Filed “{doc['filename']}” under {vend['name']}",
           f"as a {new_kind}", vendor_id=vendor_id, actor=actor)
    return {"ok": True, "doc_id": doc_id, "vendor_id": vendor_id, "kind": new_kind}
