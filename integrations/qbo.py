"""
QuickBooks Online.

QBO is the book of record here. Two directions:

  pull — bills and bill payments, mirrored locally so a reconciliation run is
         reproducible and does not hammer the API

  push — a parsed invoice becomes a QBO Bill

The push is the only write this portal makes to an accounting system, so it is
deliberately conservative: it refuses to create a bill whose DocNumber already
exists for that vendor, because a duplicate bill is far worse than a missing
one. Intuit will happily accept the duplicate; nothing downstream will tell you.

OAuth2: the refresh token is long-lived (100 days, rotating) and stored in
settings. The access token lives an hour and is kept in memory only.
"""

import base64
import logging
from datetime import datetime, timezone

import requests

import db
from core.util import norm_invoice, num, iso, parse_date

log = logging.getLogger(__name__)

AUTH_URL  = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
PROD_API  = "https://quickbooks.api.intuit.com"
SAND_API  = "https://sandbox-quickbooks.api.intuit.com"
MINOR_VER = "70"


class QBOError(RuntimeError):
    pass


class QBOClient:
    def __init__(self):
        self.client_id     = db.get_setting("qbo_client_id")
        self.client_secret = db.get_setting("qbo_client_secret")
        self.realm_id      = db.get_setting("qbo_realm_id")
        self.refresh_token = db.get_setting("qbo_refresh_token")
        self.sandbox       = str(db.get_setting("qbo_sandbox", "0")) in ("1", "true", "True")
        self._access = None

    @property
    def base(self):
        return SAND_API if self.sandbox else PROD_API

    def configured(self) -> bool:
        return all([self.client_id, self.client_secret, self.realm_id])

    def connected(self) -> bool:
        return self.configured() and bool(self.refresh_token)

    # ── OAuth ────────────────────────────────────────────────────────────────

    def authorize_url(self, redirect_uri: str, state: str = "invoicehub") -> str:
        from urllib.parse import urlencode
        return AUTH_URL + "?" + urlencode({
            "client_id": self.client_id,
            "response_type": "code",
            "scope": "com.intuit.quickbooks.accounting",
            "redirect_uri": redirect_uri,
            "state": state,
        })

    def _basic(self):
        raw = f"{self.client_id}:{self.client_secret}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def exchange_code(self, code: str, redirect_uri: str):
        """Turn the one-time OAuth code into tokens and store the refresh one."""
        r = requests.post(TOKEN_URL, timeout=30,
                          headers={"Authorization": self._basic(),
                                   "Accept": "application/json"},
                          data={"grant_type": "authorization_code",
                                "code": code, "redirect_uri": redirect_uri})
        if r.status_code != 200:
            raise QBOError(f"Code exchange failed ({r.status_code}): {r.text[:300]}")
        body = r.json()
        self._access = body.get("access_token")
        self.refresh_token = body.get("refresh_token")
        db.set_setting("qbo_refresh_token", self.refresh_token, "qbo-oauth")
        db.say("qbo", "Connected to QuickBooks Online",
               f"Realm {self.realm_id}{' (sandbox)' if self.sandbox else ''}")
        return True

    def access_token(self) -> str:
        if self._access:
            return self._access
        if not self.connected():
            raise QBOError("QuickBooks is not connected — add the client ID and "
                           "secret in Settings, then press Connect.")
        r = requests.post(TOKEN_URL, timeout=30,
                          headers={"Authorization": self._basic(),
                                   "Accept": "application/json"},
                          data={"grant_type": "refresh_token",
                                "refresh_token": self.refresh_token})
        if r.status_code != 200:
            raise QBOError(f"Token refresh failed ({r.status_code}): {r.text[:300]}. "
                           "The refresh token may have expired — reconnect in Settings.")
        body = r.json()
        self._access = body.get("access_token")
        # Intuit rotates the refresh token; losing the new one locks you out.
        new_refresh = body.get("refresh_token")
        if new_refresh and new_refresh != self.refresh_token:
            self.refresh_token = new_refresh
            db.set_setting("qbo_refresh_token", new_refresh, "qbo-refresh")
        return self._access

    # ── requests ─────────────────────────────────────────────────────────────

    def _call(self, method: str, path: str, body=None, params=None):
        url = f"{self.base}/v3/company/{self.realm_id}/{path.lstrip('/')}"
        params = dict(params or {})
        params.setdefault("minorversion", MINOR_VER)
        headers = {"Authorization": "Bearer " + self.access_token(),
                   "Accept": "application/json",
                   "Content-Type": "application/json"}
        r = requests.request(method, url, headers=headers, json=body,
                             params=params, timeout=60)
        if r.status_code == 401:
            self._access = None
            headers["Authorization"] = "Bearer " + self.access_token()
            r = requests.request(method, url, headers=headers, json=body,
                                 params=params, timeout=60)
        if r.status_code >= 400:
            detail = r.text[:400]
            try:
                f = (r.json().get("Fault") or {}).get("Error") or []
                if f:
                    detail = "; ".join(
                        f"{e.get('Message')} {e.get('Detail','')}".strip() for e in f)
            except Exception:
                pass
            raise QBOError(f"QBO {r.status_code}: {detail}")
        return r.json() if r.content else {}

    def query(self, sql: str):
        body = self._call("GET", "query", params={"query": sql})
        return (body.get("QueryResponse") or {})

    def test(self) -> dict:
        try:
            info = self._call("GET", f"companyinfo/{self.realm_id}")
            name = ((info.get("CompanyInfo") or {}).get("CompanyName")
                    or "unknown company")
            return {"ok": True, "detail": f"Connected to {name}"
                                          + (" (sandbox)" if self.sandbox else "")}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    # ── pulling ──────────────────────────────────────────────────────────────

    def fetch_vendors(self):
        out, start = [], 1
        while True:
            qr = self.query(f"SELECT * FROM Vendor STARTPOSITION {start} MAXRESULTS 100")
            batch = qr.get("Vendor") or []
            out.extend(batch)
            if len(batch) < 100:
                break
            start += 100
        return out

    def fetch_bills(self, since: str = ""):
        """Every bill, or those changed since an ISO timestamp."""
        out, start = [], 1
        where = f" WHERE MetaData.LastUpdatedTime > '{since}'" if since else ""
        while True:
            qr = self.query(
                f"SELECT * FROM Bill{where} STARTPOSITION {start} MAXRESULTS 100")
            batch = qr.get("Bill") or []
            out.extend(batch)
            if len(batch) < 100:
                break
            start += 100
        return out

    def fetch_bill_payments(self, since: str = ""):
        out, start = [], 1
        where = f" WHERE MetaData.LastUpdatedTime > '{since}'" if since else ""
        while True:
            qr = self.query(
                f"SELECT * FROM BillPayment{where} STARTPOSITION {start} MAXRESULTS 100")
            batch = qr.get("BillPayment") or []
            out.extend(batch)
            if len(batch) < 100:
                break
            start += 100
        return out

    def find_bill_by_docnumber(self, qbo_vendor_id: str, doc_number: str):
        """Used to refuse duplicates before creating a bill."""
        if not doc_number:
            return None
        safe = str(doc_number).replace("'", "''")
        qr = self.query(
            f"SELECT * FROM Bill WHERE DocNumber = '{safe}' "
            f"AND VendorRef = '{qbo_vendor_id}' MAXRESULTS 5")
        bills = qr.get("Bill") or []
        return bills[0] if bills else None

    # ── pushing ──────────────────────────────────────────────────────────────

    def create_bill(self, qbo_vendor_id: str, doc_number: str, txn_date: str,
                    due_date: str, lines: list, memo: str = "",
                    currency: str = "USD", expense_account_id: str = ""):
        """Create a Bill. Refuses if one with this DocNumber already exists.

        `lines` is a list of {amount, description, account_id}. When no account
        is given anywhere the call is rejected rather than guessing — posting a
        vendor bill to the wrong expense account is a mess to unwind at
        month-end, and QBO gives no useful error for a missing AccountRef.
        """
        if not qbo_vendor_id:
            raise QBOError("No QuickBooks vendor is linked to this vendor. "
                           "Set it on the vendor record first.")

        existing = self.find_bill_by_docnumber(qbo_vendor_id, doc_number)
        if existing:
            raise QBOError(
                f"QuickBooks already has bill {doc_number} for this vendor "
                f"(id {existing.get('Id')}). Refusing to create a second one.")

        line_payload = []
        for ln in lines:
            acct = ln.get("account_id") or expense_account_id
            if not acct:
                raise QBOError(
                    "No expense account to post to. Set a default expense "
                    "account on the vendor, or supply one per line.")
            line_payload.append({
                "DetailType": "AccountBasedExpenseLineDetail",
                "Amount": round(float(ln.get("amount") or 0), 2),
                "Description": (ln.get("description") or "")[:4000],
                "AccountBasedExpenseLineDetail": {"AccountRef": {"value": str(acct)}},
            })
        if not line_payload:
            raise QBOError("A bill needs at least one line.")

        body = {
            "VendorRef": {"value": str(qbo_vendor_id)},
            "Line": line_payload,
        }
        if doc_number:
            body["DocNumber"] = str(doc_number)[:21]   # QBO caps DocNumber at 21
        if txn_date:
            body["TxnDate"] = iso(txn_date)
        if due_date:
            body["DueDate"] = iso(due_date)
        if memo:
            body["PrivateNote"] = memo[:4000]

        out = self._call("POST", "bill", body=body)
        return out.get("Bill") or {}

    def attach_pdf(self, bill_id: str, file_bytes: bytes, filename: str):
        """Attach the source PDF to the bill, so the document lives with the
        entry rather than only in this portal."""
        url = (f"{self.base}/v3/company/{self.realm_id}/upload"
               f"?minorversion={MINOR_VER}")
        meta = {
            "AttachableRef": [{"EntityRef": {"type": "Bill", "value": str(bill_id)}}],
            "FileName": filename,
            "ContentType": "application/pdf",
        }
        import json as _json
        files = {
            "file_metadata_0": ("meta.json", _json.dumps(meta), "application/json"),
            "file_content_0": (filename, file_bytes, "application/pdf"),
        }
        r = requests.post(url, timeout=120, files=files,
                          headers={"Authorization": "Bearer " + self.access_token(),
                                   "Accept": "application/json"})
        if r.status_code >= 400:
            raise QBOError(f"Attach failed ({r.status_code}): {r.text[:300]}")
        return True


# ── demo double ──────────────────────────────────────────────────────────────

class DemoQBOClient:
    def __init__(self):
        self.realm_id = "demo-realm"
        self.sandbox = True

    def configured(self):  return True
    def connected(self):   return True

    def test(self):
        return {"ok": True, "detail": "Demo company (no live QuickBooks call)"}

    def authorize_url(self, redirect_uri, state="invoicehub"):
        return "/settings?demo_oauth=1"

    def fetch_vendors(self):
        from demo.fixtures import demo_qbo_vendors
        return demo_qbo_vendors()

    def fetch_bills(self, since=""):
        from demo.fixtures import demo_qbo_bills
        return demo_qbo_bills()

    def fetch_bill_payments(self, since=""):
        from demo.fixtures import demo_qbo_payments
        return demo_qbo_payments()

    def find_bill_by_docnumber(self, qbo_vendor_id, doc_number):
        from demo.fixtures import demo_qbo_bills
        for b in demo_qbo_bills():
            if (str(b.get("DocNumber")) == str(doc_number)
                    and str((b.get("VendorRef") or {}).get("value")) == str(qbo_vendor_id)):
                return b
        return None

    def create_bill(self, qbo_vendor_id, doc_number, txn_date, due_date, lines,
                    memo="", currency="USD", expense_account_id=""):
        dup = self.find_bill_by_docnumber(qbo_vendor_id, doc_number)
        if dup:
            raise QBOError(
                f"QuickBooks already has bill {doc_number} for this vendor "
                f"(id {dup.get('Id')}). Refusing to create a second one.")
        new_id = f"DEMO-{abs(hash((qbo_vendor_id, doc_number))) % 90000 + 10000}"
        db.say("qbo", f"[demo] Would have created bill {doc_number}",
               f"Vendor {qbo_vendor_id}, "
               f"{sum(float(l.get('amount') or 0) for l in lines):,.2f}")
        return {"Id": new_id, "DocNumber": doc_number,
                "TotalAmt": sum(float(l.get("amount") or 0) for l in lines)}

    def attach_pdf(self, bill_id, file_bytes, filename):
        return True


def get_client():
    if db.demo_mode():
        return DemoQBOClient()
    return QBOClient()


# ── mirroring into the local tables ──────────────────────────────────────────

def sync_to_db(actor: str = "system") -> dict:
    """Refresh the local copy of QBO bills and payments.

    Balance, not total, decides whether a bill is open: a partially paid bill
    still needs to appear in the match, with only what is left to pay.
    """
    from core.util import match_vendor_name

    client = get_client()
    bills = client.fetch_bills()
    payments = client.fetch_bill_payments()
    now = db.utcnow()

    con = db.get_db()
    try:
        vendors = [dict(r) for r in con.execute(
            "SELECT id, name, qbo_vendor_id, qbo_vendor_name FROM vendors").fetchall()]
        by_qbo = {str(v["qbo_vendor_id"]): v for v in vendors if v.get("qbo_vendor_id")}

        n_bill = 0
        for b in bills:
            vref = (b.get("VendorRef") or {})
            vraw = vref.get("name") or ""
            vid_qbo = str(vref.get("value") or "")
            v = by_qbo.get(vid_qbo) or match_vendor_name(vraw, vendors)

            total = num(b.get("TotalAmt")) or 0.0
            bal   = num(b.get("Balance"))
            bal   = total if bal is None else bal
            if bal <= 0.004:
                status = "paid"
            elif bal < total - 0.004:
                status = "partial"
            else:
                status = "open"

            doc = b.get("DocNumber") or ""
            con.execute("""
                INSERT INTO qbo_bills
                  (qbo_id, vendor_id, vendor_raw, qbo_vendor_id, doc_number,
                   invoice_norm, txn_date, due_date, total_amount, balance,
                   currency, status, period, memo, synced_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(qbo_id) DO UPDATE SET
                  vendor_id=excluded.vendor_id, vendor_raw=excluded.vendor_raw,
                  doc_number=excluded.doc_number, invoice_norm=excluded.invoice_norm,
                  txn_date=excluded.txn_date, due_date=excluded.due_date,
                  total_amount=excluded.total_amount, balance=excluded.balance,
                  status=excluded.status, period=excluded.period,
                  memo=excluded.memo, synced_at=excluded.synced_at
            """, (str(b.get("Id")), v["id"] if v else None, vraw, vid_qbo, doc,
                  norm_invoice(doc), iso(b.get("TxnDate")), iso(b.get("DueDate")),
                  total, bal, (b.get("CurrencyRef") or {}).get("value", "USD"),
                  status, (iso(b.get("TxnDate")) or "")[:7],
                  b.get("PrivateNote") or "", now))
            n_bill += 1

        n_pay = 0
        for p in payments:
            vref = (p.get("VendorRef") or {})
            v = by_qbo.get(str(vref.get("value") or "")) or \
                match_vendor_name(vref.get("name") or "", vendors)
            for ln in (p.get("Line") or []):
                for tx in (ln.get("LinkedTxn") or []):
                    if tx.get("TxnType") != "Bill":
                        continue
                    con.execute("""
                        INSERT OR REPLACE INTO qbo_payments
                          (qbo_id, vendor_id, bill_qbo_id, pay_date, amount,
                           method, reference, synced_at)
                        VALUES (?,?,?,?,?,?,?,?)
                    """, (f"{p.get('Id')}-{tx.get('TxnId')}",
                          v["id"] if v else None, str(tx.get("TxnId")),
                          iso(p.get("TxnDate")), num(ln.get("Amount")),
                          p.get("PayType") or "", p.get("DocNumber") or "", now))
                    n_pay += 1
        con.commit()
    finally:
        con.close()

    db.say("qbo", "Refreshed the QuickBooks mirror",
           f"{n_bill} bill(s) and {n_pay} payment link(s).", actor=actor)
    return {"ok": True, "bills": n_bill, "payments": n_pay}
