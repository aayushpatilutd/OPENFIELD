"""
ServiceTitan — optional, and used for one thing: tying a vendor bill to a job.

A purchase order number on an invoice is the only reliable bridge between what
a supplier billed and the job it was bought for. When ST is connected, a parsed
PO number is looked up and the job reference is written onto the invoice, which
is what turns a pile of bills into job costing.

Everything here degrades quietly. If ST is not configured the portal works
exactly as before, minus the job column.
"""

import logging
from datetime import datetime, timedelta, timezone

import requests

import db

log = logging.getLogger(__name__)

AUTH_URL = "https://auth.servicetitan.io/connect/token"
API_BASE = "https://api.servicetitan.io"


class STError(RuntimeError):
    pass


class ServiceTitanClient:
    def __init__(self):
        self.tenant_id     = db.get_setting("st_tenant_id")
        self.client_id     = db.get_setting("st_client_id")
        self.client_secret = db.get_setting("st_client_secret")
        self.app_key       = db.get_setting("st_app_key")
        self._token = None
        self._expires = None

    def configured(self) -> bool:
        return all([self.tenant_id, self.client_id, self.client_secret, self.app_key])

    def token(self) -> str:
        now = datetime.now(timezone.utc)
        if self._token and self._expires and now < self._expires:
            return self._token
        if not self.configured():
            raise STError("ServiceTitan is not configured.")
        r = requests.post(AUTH_URL, timeout=30,
                          data={"grant_type": "client_credentials",
                                "client_id": self.client_id,
                                "client_secret": self.client_secret})
        if r.status_code != 200:
            raise STError(f"ServiceTitan auth failed ({r.status_code}): {r.text[:300]}")
        body = r.json()
        self._token = body["access_token"]
        self._expires = now + timedelta(seconds=int(body.get("expires_in", 900)) - 60)
        return self._token

    def _headers(self):
        return {"Authorization": self.token(),
                "ST-App-Key": self.app_key,
                "Accept": "application/json"}

    def _get(self, path, params=None):
        url = f"{API_BASE}{path}"
        r = requests.get(url, headers=self._headers(), params=params, timeout=60)
        if r.status_code == 401:
            self._token = None
            r = requests.get(url, headers=self._headers(), params=params, timeout=60)
        if r.status_code >= 400:
            raise STError(f"ServiceTitan {r.status_code} on {path}: {r.text[:300]}")
        return r.json()

    def test(self) -> dict:
        try:
            body = self._get(f"/settings/v2/tenant/{self.tenant_id}/business-units",
                             {"page": 1, "pageSize": 1})
            n = body.get("totalCount", 0)
            return {"ok": True, "detail": f"Reached tenant {self.tenant_id} "
                                          f"({n} business unit(s))"}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    def find_purchase_order(self, po_number: str):
        """The PO behind an invoice, if ST knows it."""
        if not po_number:
            return None
        try:
            body = self._get(
                f"/inventory/v2/tenant/{self.tenant_id}/purchase-orders",
                {"number": str(po_number), "page": 1, "pageSize": 5})
            data = body.get("data") or []
            return data[0] if data else None
        except STError as e:
            log.info("PO lookup for %s failed: %s", po_number, e)
            return None

    def get_job(self, job_id):
        if not job_id:
            return None
        try:
            return self._get(f"/jpm/v2/tenant/{self.tenant_id}/jobs/{job_id}")
        except STError:
            return None

    def job_for_po(self, po_number: str):
        """(job_id, job_number, summary) for a PO, or None all the way down."""
        po = self.find_purchase_order(po_number)
        if not po:
            return None
        job_id = po.get("jobId")
        if not job_id:
            return None
        job = self.get_job(job_id) or {}
        return {"job_id": job_id,
                "job_number": job.get("jobNumber") or str(job_id),
                "summary": (job.get("summary") or "")[:500],
                "business_unit": job.get("businessUnitId")}


class DemoSTClient:
    def configured(self):
        return True

    def test(self):
        return {"ok": True, "detail": "Demo ServiceTitan tenant (no live call)"}

    def find_purchase_order(self, po_number):
        from demo.fixtures import demo_purchase_order
        return demo_purchase_order(po_number)

    def get_job(self, job_id):
        from demo.fixtures import demo_job
        return demo_job(job_id)

    def job_for_po(self, po_number):
        from demo.fixtures import demo_job_for_po
        return demo_job_for_po(po_number)


def get_client():
    if db.demo_mode():
        return DemoSTClient()
    return ServiceTitanClient()
