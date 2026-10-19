"""
Claude reads the documents.

A supplier invoice is a picture of a table. Text extraction flattens it and
loses the column structure that tells you which number is the total and which
is a line. Claude reads the PDF natively, so the layout survives — that is the
whole reason this is here rather than a regex over pdftotext output.

Two prompts, because the two documents ask different questions:

  invoice   — one number, one date, one total. Precision matters more than recall.
  statement — every open line. Recall matters more: a line you miss is money
              that silently falls out of the reconciliation.

The model is told to return low confidence rather than guess. A flagged bad
read costs a glance; a confident wrong total costs a payment run.
"""

import os
import json
import base64
import logging

import requests

import db

log = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"
READABLE = {".pdf", ".xlsx", ".xls", ".xlsm", ".csv", ".png", ".jpg", ".jpeg"}

IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


INVOICE_SYSTEM = """You read supplier invoices for an accounts payable team.

Return ONLY a JSON object. No prose, no markdown fence.

{"doc_type":"invoice",
 "invoice_number":"...",
 "invoice_date":"YYYY-MM-DD",
 "due_date":"YYYY-MM-DD or null",
 "subtotal":0.00,
 "tax":0.00,
 "total":0.00,
 "currency":"USD",
 "po_number":"... or null",
 "vendor_name":"... or null",
 "account_number":"... or null",
 "lines":[{"description":"...","quantity":0,"unit_price":0.00,"amount":0.00}],
 "confidence":"high|medium|low",
 "notes":"anything a human should know, or null"}

RULES
- Numbers are plain numbers: 1234.56, never "$1,234.56". A credit is negative.
- A date you cannot read is null. Never invent one.
- The invoice number exactly as printed, including letters and leading zeros.
- total is what the vendor is asking for, after tax and after any discount.
- If this is a credit note, set doc_type to "credit" and make total negative.
- If the document is not an invoice at all, set doc_type to what it is
  ("statement", "remittance", "delivery_note", "other") and fill what you can.
- Set confidence to low if the scan is poor or the layout is ambiguous. Being
  honest about a bad read is more useful than a confident wrong number."""


STATEMENT_SYSTEM = """You read supplier statements of account for an accounts
payable team.

Return ONLY a JSON object. No prose, no markdown fence.

{"doc_type":"statement",
 "statement_date":"YYYY-MM-DD",
 "period":"YYYY-MM",
 "vendor_name":"... or null",
 "account_number":"... or null",
 "opening_balance":0.00,
 "closing_balance":0.00,
 "total_due":0.00,
 "currency":"USD",
 "lines":[{"invoice_number":"...",
           "invoice_date":"YYYY-MM-DD or null",
           "due_date":"YYYY-MM-DD or null",
           "amount":0.00,
           "type":"invoice|credit|payment|finance_charge",
           "age_bucket":"current|1-30|31-60|61-90|90+ or null"}],
 "confidence":"high|medium|low",
 "notes":"anything a human should know, or null"}

RULES
- Numbers are plain numbers. A credit or a payment is NEGATIVE.
- List EVERY line, not a summary. This is the critical instruction: a line you
  omit becomes money that falls out of the reconciliation silently.
- If the statement shows both ageing buckets and individual lines, use the
  lines and put the bucket on each line.
- Do not include the balance-forward row as an invoice line. It belongs in
  opening_balance.
- Invoice numbers exactly as printed, including letters and leading zeros.
- If a line has no invoice number (a finance charge, say), still include it
  with invoice_number null and the right type.
- Set confidence to low if the scan is poor or lines may have been missed."""


class ClaudeClient:
    def __init__(self):
        self.api_key = db.get_setting("claude_api_key")
        self.model   = db.get_setting("claude_model") or "claude-sonnet-5"

    def configured(self) -> bool:
        return bool(self.api_key)

    def test(self) -> dict:
        if not self.configured():
            return {"ok": False, "detail": "No API key set"}
        try:
            r = requests.post(
                API_URL, timeout=60,
                headers={"x-api-key": self.api_key,
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": self.model, "max_tokens": 16,
                      "messages": [{"role": "user", "content": "Reply with OK."}]})
            if r.status_code != 200:
                msg = r.text[:300]
                try:
                    msg = (r.json().get("error") or {}).get("message", msg)
                except Exception:
                    pass
                return {"ok": False, "detail": f"{r.status_code}: {msg}"}
            return {"ok": True, "detail": f"Model {self.model} responded"}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    def call(self, blocks, system: str, max_tokens: int = 8000):
        """One message. Returns (text, error)."""
        if not self.configured():
            return None, ("No Claude API key. Add one in Settings — the portal "
                          "cannot read documents without it.")
        try:
            r = requests.post(
                API_URL, timeout=240,
                headers={"x-api-key": self.api_key,
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": self.model, "max_tokens": max_tokens,
                      "system": system,
                      "messages": [{"role": "user", "content": blocks}]})
            if r.status_code != 200:
                msg = r.text[:300]
                try:
                    msg = (r.json().get("error") or {}).get("message", msg)
                except Exception:
                    pass
                return None, f"Claude {r.status_code}: {msg}"
            body = r.json()
            txt = "".join(b.get("text", "") for b in (body.get("content") or [])
                          if b.get("type") == "text")
            return txt, None
        except requests.Timeout:
            return None, "Claude timed out. A very large statement may need splitting."
        except Exception as e:
            return None, str(e)


# ── turning a file into something Claude can read ───────────────────────────

def file_blocks(path: str, filename: str):
    """(blocks, error). PDFs and images go as-is; spreadsheets become CSV text."""
    ext = os.path.splitext(filename or path)[1].lower()
    if ext not in READABLE:
        return None, f"I do not know how to read a {ext or 'file with no extension'}"
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except Exception as e:
        return None, f"Could not open the file: {e}"
    if not raw:
        return None, "The file is empty"

    if ext == ".pdf":
        return [{"type": "document",
                 "source": {"type": "base64", "media_type": "application/pdf",
                            "data": base64.b64encode(raw).decode()}}], None

    if ext in IMAGE_MIME:
        return [{"type": "image",
                 "source": {"type": "base64", "media_type": IMAGE_MIME[ext],
                            "data": base64.b64encode(raw).decode()}}], None

    # Spreadsheets: hand over the cells as CSV. Several sheets are common on a
    # statement export, so take the first few rather than only the active one.
    try:
        import io
        import pandas as pd
        if ext == ".csv":
            sheets = {"Sheet1": pd.read_csv(io.BytesIO(raw), dtype=str,
                                            keep_default_na=False)}
        else:
            sheets = pd.read_excel(io.BytesIO(raw), dtype=str, sheet_name=None)
        parts = []
        for nm, frame in list(sheets.items())[:5]:
            parts.append(f"--- sheet: {nm} ---\n"
                         + frame.head(600).to_csv(index=False))
        return [{"type": "text", "text": "\n\n".join(parts)[:180000]}], None
    except Exception as e:
        return None, f"Could not read the spreadsheet: {e}"


def extract_json(text):
    """Pull the JSON object out of a reply, tolerating a stray fence."""
    import re
    if not text:
        return None
    t = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j < i:
        return None
    try:
        return json.loads(t[i:j + 1])
    except json.JSONDecodeError:
        # A truncated reply is the usual cause. Try to close it off so a long
        # statement that ran out of tokens still yields the lines it did emit.
        snippet = t[i:j + 1]
        for close in ("]}", "}]}", '"}]}'):
            try:
                return json.loads(snippet + close)
            except json.JSONDecodeError:
                continue
        return None


# ── demo double ──────────────────────────────────────────────────────────────

class DemoClaudeClient:
    def configured(self):
        return True

    def test(self):
        return {"ok": True, "detail": "Demo parser (no live Claude call)"}

    def call(self, blocks, system, max_tokens=8000):
        # The demo seeder writes parsed JSON directly; this path exists only so
        # the Settings page test and any stray call behave.
        return json.dumps({"doc_type": "invoice", "confidence": "high",
                           "invoice_number": "DEMO-0001", "total": 0.0}), None


def get_client():
    if db.demo_mode():
        return DemoClaudeClient()
    return ClaudeClient()
