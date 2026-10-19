"""
Microsoft Graph — the AP mailbox.

Two jobs: list messages with attachments so documents file themselves, and send
the emails the agents draft. Client-credentials flow, so it runs unattended
against a shared mailbox rather than borrowing a person's session.

Permissions the app registration needs (application, admin-consented):
    Mail.Read          to sweep the mailbox
    Mail.Send          to send approved drafts
    Mail.ReadWrite     only if you want messages marked as read after filing
"""

import base64
import logging
from datetime import datetime, timedelta, timezone

import requests

import db

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
LOGIN = "https://login.microsoftonline.com"

ATTACHMENT_EXT = {".pdf", ".xlsx", ".xls", ".xlsm", ".csv", ".png", ".jpg", ".jpeg"}


class GraphError(RuntimeError):
    pass


class GraphClient:
    def __init__(self, tenant_id="", client_id="", client_secret="", mailbox=""):
        self.tenant_id     = tenant_id     or db.get_setting("graph_tenant_id")
        self.client_id     = client_id     or db.get_setting("graph_client_id")
        self.client_secret = client_secret or db.get_setting("graph_client_secret")
        self.mailbox       = mailbox       or db.get_setting("graph_mailbox")
        self._token = None
        self._expires = None

    # ── auth ─────────────────────────────────────────────────────────────────

    def configured(self) -> bool:
        return all([self.tenant_id, self.client_id, self.client_secret, self.mailbox])

    def token(self) -> str:
        now = datetime.now(timezone.utc)
        if self._token and self._expires and now < self._expires:
            return self._token
        if not self.configured():
            raise GraphError("Microsoft Graph is not configured — add the tenant, "
                             "client ID, secret and mailbox in Settings.")
        r = requests.post(
            f"{LOGIN}/{self.tenant_id}/oauth2/v2.0/token", timeout=30,
            data={"client_id": self.client_id,
                  "client_secret": self.client_secret,
                  "scope": "https://graph.microsoft.com/.default",
                  "grant_type": "client_credentials"})
        if r.status_code != 200:
            raise GraphError(f"Token request failed ({r.status_code}): {r.text[:300]}")
        body = r.json()
        self._token = body["access_token"]
        # Refresh a minute early so a long sweep does not expire mid-flight.
        self._expires = now + timedelta(seconds=int(body.get("expires_in", 3600)) - 60)
        return self._token

    def _headers(self):
        return {"Authorization": "Bearer " + self.token(),
                "Content-Type": "application/json"}

    def _get(self, url, params=None):
        r = requests.get(url, headers=self._headers(), params=params, timeout=60)
        if r.status_code == 401:
            self._token = None   # one retry on a stale token
            r = requests.get(url, headers=self._headers(), params=params, timeout=60)
        if r.status_code >= 400:
            raise GraphError(f"Graph {r.status_code} on {url.split('?')[0]}: "
                             f"{r.text[:300]}")
        return r.json()

    # ── reading ──────────────────────────────────────────────────────────────

    def test(self) -> dict:
        """Does this configuration actually work? Used by the Settings page."""
        try:
            info = self._get(f"{GRAPH}/users/{self.mailbox}")
            return {"ok": True,
                    "detail": f"Reached {info.get('displayName') or self.mailbox} "
                              f"<{info.get('mail') or self.mailbox}>"}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    def list_messages(self, days_back: int = 7, folder: str = "inbox",
                      only_with_attachments: bool = True, top: int = 100):
        """Messages from the last N days, newest first."""
        since = (datetime.now(timezone.utc) - timedelta(days=days_back)
                 ).strftime("%Y-%m-%dT%H:%M:%SZ")
        flt = f"receivedDateTime ge {since}"
        if only_with_attachments:
            flt += " and hasAttachments eq true"

        url = f"{GRAPH}/users/{self.mailbox}/mailFolders/{folder}/messages"
        params = {
            "$filter": flt,
            "$select": "id,subject,from,receivedDateTime,hasAttachments,"
                       "internetMessageId,bodyPreview",
            "$orderby": "receivedDateTime desc",
            "$top": min(top, 100),
        }
        out = []
        while url and len(out) < top:
            body = self._get(url, params=params)
            out.extend(body.get("value", []))
            url = body.get("@odata.nextLink")
            params = None       # nextLink already carries the query
        return out[:top]

    def list_attachments(self, message_id: str):
        body = self._get(
            f"{GRAPH}/users/{self.mailbox}/messages/{message_id}/attachments")
        return body.get("value", [])

    def attachment_bytes(self, attachment: dict):
        """Raw bytes for a fileAttachment. Returns None for inline/item types.

        Graph gives contentBytes inline for file attachments, which is why
        there is no second fetch here. An itemAttachment (a forwarded email)
        has no contentBytes and is skipped rather than mishandled.
        """
        if attachment.get("@odata.type", "").endswith("fileAttachment"):
            raw = attachment.get("contentBytes")
            if raw:
                return base64.b64decode(raw)
        return None

    def mark_read(self, message_id: str):
        requests.patch(f"{GRAPH}/users/{self.mailbox}/messages/{message_id}",
                       headers=self._headers(), json={"isRead": True}, timeout=30)

    # ── sending ──────────────────────────────────────────────────────────────

    def send_mail(self, to_email: str, subject: str, body: str,
                  cc: str = "", from_mailbox: str = "", html: bool = False):
        """Send one message. Raises on failure so the caller can record why.

        Note this is only ever reached after a human approves a draft — nothing
        in the agent framework calls it directly.
        """
        sender = from_mailbox or db.get_setting("graph_send_as") or self.mailbox

        def recips(s):
            return [{"emailAddress": {"address": a.strip()}}
                    for a in str(s or "").replace(";", ",").split(",") if a.strip()]

        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "HTML" if html else "Text", "content": body},
                "toRecipients": recips(to_email),
                "ccRecipients": recips(cc),
            },
            "saveToSentItems": True,
        }
        r = requests.post(f"{GRAPH}/users/{sender}/sendMail",
                          headers=self._headers(), json=payload, timeout=60)
        if r.status_code not in (200, 202):
            raise GraphError(f"Send failed ({r.status_code}): {r.text[:300]}")
        return True


# ── demo double ──────────────────────────────────────────────────────────────

class DemoGraphClient:
    """Stands in for Graph when the portal is in demo mode.

    It returns a fixed, deliberately awkward mailbox: a normal invoice, a
    statement, a zero-byte attachment, an unknown sender, a duplicate of a
    message already filed, and a vendor whose display name does not match the
    one on file. Those are the cases a demo needs to show.
    """

    def __init__(self, *a, **kw):
        self.mailbox = db.get_setting("graph_mailbox") or "ap@demo-invoicehub.local"

    def configured(self):
        return True

    def test(self):
        return {"ok": True, "detail": f"Demo mailbox {self.mailbox} "
                                      "(no live Graph call was made)"}

    def list_messages(self, days_back=7, folder="inbox",
                      only_with_attachments=True, top=100):
        from demo.fixtures import demo_messages
        return demo_messages(days_back)

    def list_attachments(self, message_id):
        from demo.fixtures import demo_attachments
        return demo_attachments(message_id)

    def attachment_bytes(self, attachment):
        raw = attachment.get("contentBytes")
        return base64.b64decode(raw) if raw else None

    def mark_read(self, message_id):
        return True

    def send_mail(self, to_email, subject, body, cc="", from_mailbox="", html=False):
        db.say("email", f"[demo] Would have emailed {to_email}",
               f"Subject: {subject}\n\n{body[:500]}")
        return True


def get_client():
    """The Graph client the rest of the app should use."""
    if db.demo_mode():
        return DemoGraphClient()
    return GraphClient()
