"""
Demo data — entirely fictional.

Every company, invoice number, address and email here is a placeholder:
Company ABC, Company DEF, invoice 1, 2, 3. Nothing in this file corresponds to
a real supplier, a real invoice or a real amount, and nothing from the live
system is reproduced. The `.example` domains are reserved by RFC 2606 and
cannot be registered, so a stray send in a misconfigured demo goes nowhere.

The goal is not "some plausible invoices". It is to put one example of every
situation that breaks an AP tool in front of whoever is being shown the
portal, so the demo answers the hard questions instead of dodging them.

What is deliberately represented:

  clean path         invoice, statement and QBO all agree
  amount mismatch    vendor says one number, the books say another
  penny difference   1 cent apart — inside tolerance, must still match
  missing bill       on their statement, we hold the invoice, not in QBO
  missing invoice    on their statement and in QBO, no document
  statement only     they claim it, we have nothing at all
  qbo only           open in our books, on nobody's statement
  invoice only       we hold a document nobody else knows about
  credit note        negative, must reduce the payment not add to it
  partial payment    balance differs from total
  already paid       settled in QBO, must not be paid twice
  duplicate invoice  same number arriving twice, different file
  number formats     ABC-000001 vs 1 vs INV-000001 across the three systems
  no statement       a vendor who simply did not send one
  low confidence     a bad scan the parser was honest about
  unparseable        a zero-byte attachment
  unknown sender     mail from a vendor not on file
  foreign currency   a CAD vendor
  long name          a vendor name over the Excel 31-char sheet limit
  name collision     two vendors whose names truncate to the same 31 chars
  finance charge     a statement line with no invoice number
  zero-amount        a 0.00 line that must not be paid
  stale invoice      from two periods ago, still unresolved
"""

import base64
from datetime import datetime, timedelta, timezone

from core.util import current_period, period_bounds


def _d(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _dt(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")


PERIOD = current_period()
P_START, P_END = period_bounds(PERIOD)


# ── vendors ──────────────────────────────────────────────────────────────────
#
# The two "Company PQR International Holdings …" entries exist to prove the
# workbook handles Excel's 31-character sheet-name limit: both truncate to
# exactly "Company PQR International Holdi", so the exporter has to
# de-duplicate them rather than silently drop one.

VENDORS = [
    {
        "name": "Company ABC",
        "display_name": "Company ABC Supply Co., Inc.",
        "invoice_email": "invoices@company-abc.example",
        "invoice_subject": "Invoice",
        "statement_email": "statements@company-abc.example",
        "statement_subject": "Statement of Account",
        "request_email": "ar@company-abc.example",
        "terms_days": 30, "currency": "USD",
        "account_number": "ACC-0001",
        "payment_method": "portal",
        "payment_portal_url": "https://pay.company-abc.example",
        "early_pay_discount": "2/10 net 30",
        "qbo_vendor_id": "101", "qbo_vendor_name": "Company ABC Supply Co Inc",
        "default_expense_account": "54",
        "auto_push_bills": 1, "requires_po": 1,
        "notes": "High volume. Invoice numbers print as ABC-000001 on the PDF "
                 "but appear as plain digits on their statement.",
    },
    {
        "name": "Company DEF",
        "invoice_email": "ap@company-def.example",
        "statement_email": "ap@company-def.example",
        "statement_subject": "Monthly Statement",
        "request_email": "ar.team@company-def.example",
        "terms_days": 45, "currency": "USD",
        "account_number": "ACC-0002",
        "payment_method": "ach",
        "qbo_vendor_id": "102", "qbo_vendor_name": "Company DEF",
        "default_expense_account": "54",
        "auto_push_bills": 1,
        "notes": "Sends statements as a spreadsheet rather than a PDF. "
                 "Carries the amount mismatch and the partial payment.",
    },
    {
        "name": "Company GHI",
        "invoice_email": "billing@company-ghi.example",
        "statement_email": "billing@company-ghi.example",
        "request_email": "billing@company-ghi.example",
        "terms_days": 30, "currency": "USD",
        "account_number": "ACC-0003",
        "payment_method": "check",
        "qbo_vendor_id": "103", "qbo_vendor_name": "Company GHI",
        "default_expense_account": "54",
        "notes": "Reliable. The clean example — everything agrees.",
    },
    {
        "name": "Company JKL",
        "invoice_email": "noreply@company-jkl.example",
        "statement_email": "statements@company-jkl.example",
        "request_email": "ar@company-jkl.example",
        "terms_days": 60, "currency": "USD",
        "account_number": "ACC-0004",
        "payment_method": "ach",
        "qbo_vendor_id": "104", "qbo_vendor_name": "Company JKL",
        "default_expense_account": "54",
        "notes": "Net 60, so their run reaches a month further back. "
                 "Carries the stale invoice and the credit note.",
    },
    {
        "name": "Company MNO",
        "invoice_email": "invoices@company-mno.example",
        "statement_email": "statements@company-mno.example",
        "request_email": "ar@company-mno.example",
        "terms_days": 30, "currency": "USD",
        "account_number": "ACC-0005",
        "payment_method": "portal",
        "payment_portal_url": "https://ar.company-mno.example",
        "qbo_vendor_id": "105", "qbo_vendor_name": "Company MNO",
        "default_expense_account": "54",
        "notes": "Did not send a statement this period — the chase case.",
        "_no_statement": True,
    },
    {
        "name": "Company PQR International Holdings Canada",
        "invoice_email": "ar@company-pqr-ca.example",
        "statement_email": "ar@company-pqr-ca.example",
        "request_email": "ar@company-pqr-ca.example",
        "terms_days": 30, "currency": "CAD",
        "account_number": "ACC-0006",
        "payment_method": "ach",
        "qbo_vendor_id": "106",
        "qbo_vendor_name": "Company PQR International Holdings Canada",
        "default_expense_account": "54",
        "notes": "Foreign currency, and a name long enough to collide with "
                 "the entity below once Excel truncates it to 31 characters.",
    },
    {
        "name": "Company PQR International Holdings USA",
        "invoice_email": "us.ar@company-pqr-us.example",
        "statement_email": "us.ar@company-pqr-us.example",
        "request_email": "us.ar@company-pqr-us.example",
        "terms_days": 30, "currency": "USD",
        "account_number": "ACC-0007",
        "payment_method": "ach",
        "qbo_vendor_id": "107",
        "qbo_vendor_name": "Company PQR International Holdings USA",
        "default_expense_account": "54",
        "notes": "Truncates to the same 31 characters as the Canadian entity — "
                 "the sheet-name collision case.",
    },
    {
        "name": "Company STU",
        "invoice_email": "ap@company-stu.example",
        "statement_email": "ap@company-stu.example",
        "request_email": "ap@company-stu.example",
        "terms_days": 30, "currency": "USD",
        "account_number": "ACC-0008",
        "payment_method": "check",
        "qbo_vendor_id": "",       # deliberately unlinked: push must refuse
        "default_expense_account": "",
        "notes": "Not linked to a QuickBooks vendor. Pushing a bill for this "
                 "one must refuse rather than guess.",
    },
    {
        "name": "Company VWX",
        "invoice_email": "billing@company-vwx.example",
        "statement_email": "billing@company-vwx.example",
        "request_email": "billing@company-vwx.example",
        "terms_days": 15, "currency": "USD",
        "account_number": "ACC-0009",
        "payment_method": "card",
        "qbo_vendor_id": "108", "qbo_vendor_name": "Company VWX",
        "default_expense_account": "54",
        "notes": "Short terms, small frequent invoices. Carries the "
                 "zero-amount line and the already-paid bill.",
    },
    {
        "name": "Company YZA",
        "invoice_email": "office@company-yza.example",
        "statement_email": "office@company-yza.example",
        "request_email": "office@company-yza.example",
        "terms_days": 30, "currency": "USD",
        "account_number": "ACC-0010",
        "payment_method": "check",
        "qbo_vendor_id": "109", "qbo_vendor_name": "Company YZA",
        "default_expense_account": "54",
        "notes": "Scans their invoices badly — the low-confidence case.",
    },
]


# ── invoices we hold ─────────────────────────────────────────────────────────
# (vendor, number, date_days_ago, amount, po, confidence, note)

INVOICES = [
    # Company ABC — clean, plus the number-format and duplicate cases
    ("Company ABC", "ABC-000001", 38, 4820.55, "PO-1001", "high", ""),
    ("Company ABC", "ABC-000002", 36, 1290.00, "PO-1002", "high", ""),
    ("Company ABC", "ABC-000003", 33,  612.40, "PO-1003", "high", ""),
    ("Company ABC", "ABC-000006", 28, 7430.19, "PO-1004", "high",
     "Statement shows this a penny different — inside tolerance."),
    ("Company ABC", "ABC-000008", 25, 2150.00, None, "high",
     "We hold it; it never got entered into QuickBooks."),
    ("Company ABC", "ABC-000011", 20,  980.00, "PO-1005", "medium",
     "Duplicate: the same invoice arrived twice as two different files."),

    # Company DEF — mismatch and partial payment
    ("Company DEF", "DEF-000101", 50, 15200.00, "PO-1101", "high", ""),
    ("Company DEF", "DEF-000102", 44,  8750.25, "PO-1102", "high",
     "Their statement says 8,975.25 — a 225.00 difference."),
    ("Company DEF", "DEF-000103", 41, 23100.00, "PO-1103", "high",
     "Partially paid: 10,000 has gone, 13,100 is left."),

    # Company GHI — the clean vendor
    ("Company GHI", "GHI-000201", 35, 3340.00, "PO-1201", "high", ""),
    ("Company GHI", "GHI-000202", 30, 1875.50, "PO-1202", "high", ""),
    ("Company GHI", "GHI-000203", 27,  420.00, None, "high", ""),

    # Company JKL — net 60, a stale one, and a credit
    ("Company JKL", "JKL-000301", 75, 9940.00, "PO-1301", "high",
     "Two periods back and still open."),
    ("Company JKL", "JKL-000302", 52, 5600.00, "PO-1302", "high", ""),
    ("Company JKL", "JKL-CN-000303", 31, -1200.00, None, "high",
     "Credit note. Must reduce the payment."),

    # Company MNO — no statement at all this period
    ("Company MNO", "MNO-000401", 34, 6210.75, "PO-1401", "high", ""),
    ("Company MNO", "MNO-000402", 29, 2480.00, "PO-1402", "high", ""),

    # Company PQR Canada — foreign currency
    ("Company PQR International Holdings Canada",
     "PQR-CA-000501", 32, 4150.00, None, "high", "CAD."),
    ("Company PQR International Holdings Canada",
     "PQR-CA-000502", 26, 1890.00, None, "high", "CAD."),

    # Company PQR USA
    ("Company PQR International Holdings USA",
     "PQR-US-000601", 30, 3300.00, None, "high", ""),

    # Company STU — no QBO vendor link
    ("Company STU", "STU-000701", 33, 1740.00, None, "high",
     "Vendor is not linked to QuickBooks; the push must refuse."),

    # Company VWX — small and frequent, one zero-amount, one already paid
    ("Company VWX", "VWX-000801", 20, 318.90, None, "high", ""),
    ("Company VWX", "VWX-000802", 17, 1042.60, None, "high", ""),
    ("Company VWX", "VWX-000803", 14,    0.00, None, "high",
     "Zero amount. Must not be paid or pushed."),
    ("Company VWX", "VWX-000804", 11,  255.00, None, "high",
     "Already paid in QuickBooks."),

    # Company YZA — the bad scans
    ("Company YZA", "YZA-000901", 24, 1150.00, None, "low",
     "Poor scan. Parser flagged it honestly."),
    ("Company YZA", "YZA-000902", 19,  640.00, None, "low",
     "Poor scan, and nobody else knows about this one."),
]


# ── statement lines ──────────────────────────────────────────────────────────
# (vendor, number, date_days_ago, amount, type)

STATEMENT_LINES = {
    "Company ABC": [
        ("1",       38, 4820.55, "invoice"),   # same invoice, no prefix
        ("2",       36, 1290.00, "invoice"),
        ("0000003", 33,  612.40, "invoice"),   # leading zeros
        ("6",       28, 7430.20, "invoice"),   # a penny out
        ("8",       25, 2150.00, "invoice"),   # we hold it, QBO does not
        ("11",      20,  980.00, "invoice"),
        ("14",      15, 1875.00, "invoice"),   # we never received this one
        (None,       5,   42.50, "finance_charge"),
        ("5",       45, -500.00, "payment"),
    ],
    "Company DEF": [
        ("DEF-000101", 50, 15200.00, "invoice"),
        ("DEF-000102", 44,  8975.25, "invoice"),  # mismatch vs our 8750.25
        ("DEF-000103", 41, 13100.00, "invoice"),  # the remaining balance
        ("DEF-000104", 30,  4400.00, "invoice"),  # statement only
    ],
    "Company GHI": [
        ("GHI-000201", 35, 3340.00, "invoice"),
        ("GHI-000202", 30, 1875.50, "invoice"),
        ("GHI-000203", 27,  420.00, "invoice"),
    ],
    "Company JKL": [
        ("JKL-000301", 75, 9940.00, "invoice"),
        ("JKL-000302", 52, 5600.00, "invoice"),
        ("JKL-CN-000303", 31, -1200.00, "credit"),
    ],
    "Company PQR International Holdings Canada": [
        ("PQR-CA-000501", 32, 4150.00, "invoice"),
        ("PQR-CA-000502", 26, 1890.00, "invoice"),
    ],
    "Company PQR International Holdings USA": [
        ("PQR-US-000601", 30, 3300.00, "invoice"),
    ],
    "Company STU": [
        ("STU-000701", 33, 1740.00, "invoice"),
    ],
    "Company VWX": [
        ("VWX-000801", 20,  318.90, "invoice"),
        ("VWX-000802", 17, 1042.60, "invoice"),
        ("VWX-000803", 14,    0.00, "invoice"),
        ("VWX-000804", 11,  255.00, "invoice"),
    ],
    "Company YZA": [
        ("YZA-000901", 24, 1150.00, "invoice"),
    ],
    # Company MNO: deliberately absent.
}


# ── what QuickBooks believes ─────────────────────────────────────────────────
# (vendor, doc_number, date_days_ago, total, balance, status)

QBO_BILLS = [
    ("Company ABC", "INV-000001", 38, 4820.55, 4820.55, "open"),
    ("Company ABC", "INV-000002", 36, 1290.00, 1290.00, "open"),
    ("Company ABC", "0000003",    33,  612.40,  612.40, "open"),
    ("Company ABC", "6",          28, 7430.19, 7430.19, "open"),
    # ABC-000008 deliberately absent — the missing_bill case
    ("Company ABC", "11",         20,  980.00,  980.00, "open"),
    ("Company ABC", "14",         15, 1875.00, 1875.00, "open"),
    ("Company ABC", "9",          60, 3300.00, 3300.00, "open"),   # qbo_only

    ("Company DEF", "DEF-000101", 50, 15200.00, 15200.00, "open"),
    ("Company DEF", "DEF-000102", 44,  8750.25,  8750.25, "open"),
    ("Company DEF", "DEF-000103", 41, 23100.00, 13100.00, "partial"),

    ("Company GHI", "GHI-000201", 35, 3340.00, 3340.00, "open"),
    ("Company GHI", "GHI-000202", 30, 1875.50, 1875.50, "open"),
    ("Company GHI", "GHI-000203", 27,  420.00,  420.00, "open"),

    ("Company JKL", "JKL-000301", 75, 9940.00, 9940.00, "open"),
    ("Company JKL", "JKL-000302", 52, 5600.00, 5600.00, "open"),
    ("Company JKL", "JKL-CN-000303", 31, -1200.00, -1200.00, "open"),

    ("Company MNO", "MNO-000401", 34, 6210.75, 6210.75, "open"),
    ("Company MNO", "MNO-000402", 29, 2480.00, 2480.00, "open"),

    ("Company PQR International Holdings Canada",
     "PQR-CA-000501", 32, 4150.00, 4150.00, "open"),
    ("Company PQR International Holdings Canada",
     "PQR-CA-000502", 26, 1890.00, 1890.00, "open"),
    ("Company PQR International Holdings USA",
     "PQR-US-000601", 30, 3300.00, 3300.00, "open"),

    ("Company VWX", "VWX-000801", 20,  318.90,  318.90, "open"),
    ("Company VWX", "VWX-000802", 17, 1042.60, 1042.60, "open"),
    ("Company VWX", "VWX-000803", 14,    0.00,    0.00, "paid"),
    ("Company VWX", "VWX-000804", 11,  255.00,    0.00, "paid"),

    ("Company YZA", "YZA-000901", 24, 1150.00, 1150.00, "open"),
    # YZA-000902 absent — invoice_only
    # STU-000701 absent from QBO — Company STU is unlinked anyway
]


# ── the shapes the demo clients return ───────────────────────────────────────

def demo_qbo_vendors():
    return [{"Id": v["qbo_vendor_id"],
             "DisplayName": v.get("qbo_vendor_name") or v["name"],
             "Active": True}
            for v in VENDORS if v.get("qbo_vendor_id")]


def demo_qbo_bills():
    by_name = {v["name"]: v for v in VENDORS}
    out = []
    for i, (vname, doc, ago, total, bal, status) in enumerate(QBO_BILLS, 1):
        v = by_name.get(vname, {})
        out.append({
            "Id": str(9000 + i),
            "DocNumber": doc,
            "TxnDate": _d(ago),
            "DueDate": _d(ago - int(v.get("terms_days", 30))),
            "TotalAmt": total,
            "Balance": bal,
            "CurrencyRef": {"value": v.get("currency", "USD")},
            "VendorRef": {"value": v.get("qbo_vendor_id") or "0",
                          "name": v.get("qbo_vendor_name") or vname},
            "PrivateNote": "",
        })
    return out


def demo_qbo_payments():
    # The 10,000 against Company DEF's DEF-000103, and Company VWX's 000804.
    return [
        {"Id": "7001", "TxnDate": _d(20), "PayType": "Check",
         "DocNumber": "CHK-100001",
         "VendorRef": {"value": "102", "name": "Company DEF"},
         "Line": [{"Amount": 10000.00,
                   "LinkedTxn": [{"TxnId": "9010", "TxnType": "Bill"}]}]},
        {"Id": "7002", "TxnDate": _d(6), "PayType": "CreditCard",
         "DocNumber": "CC-100002",
         "VendorRef": {"value": "108", "name": "Company VWX"},
         "Line": [{"Amount": 255.00,
                   "LinkedTxn": [{"TxnId": "9026", "TxnType": "Bill"}]}]},
    ]


# ── a deliberately awkward mailbox ───────────────────────────────────────────

_TINY_PDF = base64.b64encode(
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n").decode()


def demo_messages(days_back=7):
    """Six messages: a normal invoice, a statement, an unknown sender, an
    empty attachment, a duplicate resend, and one whose subject tells the
    classifier nothing."""
    return [
        {"id": "m1", "internetMessageId": "<m1@company-abc.example>",
         "subject": "Invoice ABC-000020",
         "from": {"emailAddress": {"address": "invoices@company-abc.example"}},
         "receivedDateTime": _dt(1), "hasAttachments": True},
        {"id": "m2", "internetMessageId": "<m2@company-def.example>",
         "subject": "Monthly Statement — your account ACC-0002",
         "from": {"emailAddress": {"address": "ap@company-def.example"}},
         "receivedDateTime": _dt(1), "hasAttachments": True},
        {"id": "m3", "internetMessageId": "<m3@unknown.example>",
         "subject": "Invoice 90001 attached",
         "from": {"emailAddress": {"address": "billing@company-new.example"}},
         "receivedDateTime": _dt(2), "hasAttachments": True},
        {"id": "m4", "internetMessageId": "<m4@company-vwx.example>",
         "subject": "Invoice VWX-000810",
         "from": {"emailAddress": {"address": "billing@company-vwx.example"}},
         "receivedDateTime": _dt(2), "hasAttachments": True},
        {"id": "m5", "internetMessageId": "<m5@company-abc.example>",
         "subject": "RE: Invoice ABC-000020 (resend)",
         "from": {"emailAddress": {"address": "invoices@company-abc.example"}},
         "receivedDateTime": _dt(3), "hasAttachments": True},
        {"id": "m6", "internetMessageId": "<m6@company-ghi.example>",
         "subject": "Your document",
         "from": {"emailAddress": {"address": "billing@company-ghi.example"}},
         "receivedDateTime": _dt(3), "hasAttachments": True},
    ]


def _att(name, content=_TINY_PDF, ctype="application/pdf"):
    return {"@odata.type": "#microsoft.graph.fileAttachment",
            "name": name, "contentType": ctype, "contentBytes": content}


def demo_attachments(message_id):
    unique = base64.b64encode(
        base64.b64decode(_TINY_PDF) + message_id.encode()).decode()
    return {
        "m1": [_att("ABC-000020.pdf", unique)],
        "m2": [_att("Statement_ACC-0002.pdf", unique)],
        "m3": [_att("invoice-90001.pdf", unique)],
        # Zero-byte: must be reported, not silently dropped.
        "m4": [_att("VWX-000810.pdf", "")],
        # Same bytes as m1: the hash must recognise it as already filed.
        "m5": [_att("ABC-000020.pdf",
                    base64.b64encode(
                        base64.b64decode(_TINY_PDF) + b"m1").decode())],
        "m6": [_att("GHI-000210.pdf", unique)],
    }.get(message_id, [])


# ── ServiceTitan demo ────────────────────────────────────────────────────────

_PO_TO_JOB = {
    "PO-1001": ("J-1001", "Site works — Project Alpha"),
    "PO-1002": ("J-1001", "Site works — Project Alpha"),
    "PO-1003": ("J-1002", "Fit-out — Project Bravo"),
    "PO-1004": ("J-1003", "Remedial works — Project Charlie"),
    "PO-1101": ("J-1004", "Supply and install — Project Delta"),
    "PO-1102": ("J-1004", "Supply and install — Project Delta"),
    "PO-1103": ("J-1005", "Panels — Project Echo"),
    "PO-1201": ("J-1006", "Internal works — Project Foxtrot"),
    "PO-1202": ("J-1007", "Glazing — Project Golf"),
    "PO-1301": ("J-1008", "Roof works — Project Hotel"),
    "PO-1302": ("J-1008", "Roof works — Project Hotel"),
    "PO-1401": ("J-1009", "Entrance works — Project India"),
    "PO-1402": ("J-1009", "Entrance works — Project India"),
}


def demo_purchase_order(po_number):
    hit = _PO_TO_JOB.get(str(po_number or "").strip())
    if not hit:
        return None
    return {"id": 1, "number": po_number, "jobId": hit[0]}


def demo_job(job_id):
    for num, summary in _PO_TO_JOB.values():
        if num == job_id:
            return {"id": job_id, "jobNumber": num, "summary": summary}
    return None


def demo_job_for_po(po_number):
    hit = _PO_TO_JOB.get(str(po_number or "").strip())
    if not hit:
        return None
    return {"job_id": hit[0], "job_number": hit[0], "summary": hit[1],
            "business_unit": "Operations"}
